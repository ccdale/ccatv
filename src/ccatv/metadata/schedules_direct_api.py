from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib import error, parse, request

from ccatv import __app_name__, __version__
from ccatv.metadata.schedules_direct_contract import (
    GuideSyncWindow,
    SDAccountStatus,
    SDCredentials,
    SDLineup,
    SDProgram,
    SDScheduleEntry,
    SDStation,
    SchedulesDirectApiError,
    SchedulesDirectAuthenticationError,
    SchedulesDirectClient,
    SchedulesDirectRateLimitError,
    SchedulesDirectTransportError,
)
from ccatv.metadata.schedules_direct_runtime import (
    SDTokenCache,
    SchedulesDirectTokenCacheStore,
)


class JsonHttpTransport(Protocol):
    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: object | None,
        query: dict[str, str] | None,
        timeout_seconds: float,
    ) -> object:
        ...


@dataclass(frozen=True, slots=True)
class _TransportHttpError(Exception):
    status_code: int
    payload: object | None
    message: str


class UrlLibJsonTransport:
    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: object | None,
        query: dict[str, str] | None,
        timeout_seconds: float,
    ) -> object:
        full_url = url
        if query:
            full_url = f"{url}?{parse.urlencode(query)}"

        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers = {
                **headers,
                "Content-Type": "application/json",
            }

        req = request.Request(full_url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                raw_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            parsed_payload = _parse_json_or_none(raw_body)
            raise _TransportHttpError(
                status_code=exc.code,
                payload=parsed_payload,
                message=f"HTTP status {exc.code}",
            ) from exc
        except Exception as exc:
            raise SchedulesDirectTransportError(
                "Failed to communicate with Schedules Direct"
            ) from exc

        parsed = _parse_json_or_none(raw_body)
        if parsed is None:
            raise SchedulesDirectTransportError(
                "Schedules Direct returned non-JSON response"
            )
        return parsed


class SchedulesDirectHttpClient(SchedulesDirectClient):
    """Schedules Direct client with runtime-only credential and token handling."""

    def __init__(
        self,
        *,
        base_url: str = "https://json.schedulesdirect.org/20141201",
        timeout_seconds: float = 15.0,
        token_cache_store: SchedulesDirectTokenCacheStore | None = None,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._token_cache_store = token_cache_store
        self._transport = transport or UrlLibJsonTransport()
        self._credentials: SDCredentials | None = None
        self._token: str | None = None
        self._token_expires_at_epoch: float = 0
        self._user_agent = f"{__app_name__}/{__version__}"

    async def authenticate(self, credentials: SDCredentials) -> None:
        self._credentials = credentials
        await self._refresh_token(force=True)

    async def get_account_status(self) -> SDAccountStatus:
        payload = await self._request_json(
            method="GET",
            route="status",
            token_required=True,
        )
        status_payload = payload if isinstance(payload, dict) else {}

        message = _extract_latest_system_message(status_payload)
        expires = _parse_optional_utc(
            _pick_str(status_payload, "accountExpires", "account_expires")
        )
        max_lineups = _pick_int(status_payload, "maxLineups", "max_lineups")
        return SDAccountStatus(
            account_expires_utc=expires,
            max_lineups=max_lineups,
            provider_message=message,
        )

    async def list_lineups(self, country: str, postal_code: str) -> list[SDLineup]:
        payload = await self._request_json(
            method="GET",
            route="lineups",
            query={"country": country, "postalcode": postal_code},
            token_required=True,
        )
        items: list[object]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("lineups"), list):
            items = list(payload["lineups"])
        else:
            items = []

        result: list[SDLineup] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            lineup_id = _pick_str(item, "lineup", "lineupID", "lineupId")
            name = _pick_str(item, "name")
            if not lineup_id:
                continue
            result.append(
                SDLineup(
                    lineup_id=lineup_id,
                    name=name or lineup_id,
                    transport=_pick_str(item, "transport"),
                    country=_pick_str(item, "country"),
                    postal_code=_pick_str(item, "location", "postalCode"),
                )
            )
        return result

    async def get_lineup_stations(self, lineup_id: str) -> list[SDStation]:
        payload = await self._request_json(
            method="GET",
            route=f"lineups/{lineup_id}",
            token_required=True,
        )
        if not isinstance(payload, dict):
            return []

        channel_by_station: dict[str, str] = {}
        mapping = payload.get("map")
        if isinstance(mapping, list):
            for row in mapping:
                if not isinstance(row, dict):
                    continue
                station_id = _pick_str(row, "stationID", "stationId")
                channel = _pick_str(row, "channel")
                if station_id and channel:
                    channel_by_station[station_id] = channel

        stations = payload.get("stations")
        if not isinstance(stations, list):
            return []

        result: list[SDStation] = []
        for station in stations:
            if not isinstance(station, dict):
                continue
            station_id = _pick_str(station, "stationID", "stationId")
            callsign = _pick_str(station, "callsign")
            name = _pick_str(station, "name")
            if not station_id or not callsign or not name:
                continue
            result.append(
                SDStation(
                    station_id=station_id,
                    callsign=callsign,
                    name=name,
                    channel=channel_by_station.get(station_id),
                )
            )
        return result

    async def get_schedules(
        self,
        lineup_id: str,
        window: GuideSyncWindow,
    ) -> list[SDScheduleEntry]:
        del lineup_id, window
        return []

    async def get_programs(self, program_ids: list[str]) -> list[SDProgram]:
        del program_ids
        return []

    async def close(self) -> None:
        return None

    async def _request_json(
        self,
        *,
        method: str,
        route: str,
        payload: object | None = None,
        query: dict[str, str] | None = None,
        token_required: bool,
    ) -> object:
        headers = {"User-Agent": self._user_agent}
        if token_required:
            await self._ensure_token()
            if not self._token:
                raise SchedulesDirectAuthenticationError(
                    "Schedules Direct token unavailable"
                )
            headers["token"] = self._token

        url = f"{self._base_url}/{route.lstrip('/')}"
        try:
            return await asyncio.to_thread(
                self._transport.request_json,
                method=method,
                url=url,
                headers=headers,
                payload=payload,
                query=query,
                timeout_seconds=self._timeout_seconds,
            )
        except _TransportHttpError as exc:
            if exc.status_code == 401:
                raise SchedulesDirectAuthenticationError(
                    "Schedules Direct rejected authentication"
                ) from exc
            if exc.status_code == 429:
                raise SchedulesDirectRateLimitError(
                    "Schedules Direct rate limited this request"
                ) from exc
            raise SchedulesDirectApiError(
                code=None,
                message=f"Schedules Direct HTTP error ({exc.status_code})",
            ) from exc

    async def _ensure_token(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        if self._token and self._token_expires_at_epoch > now:
            return

        cached = self._token_cache_store.load() if self._token_cache_store else None
        if cached is not None:
            cached_expires = _parse_utc(cached.token_expires_utc).timestamp()
            if cached_expires > now:
                self._token = cached.token
                self._token_expires_at_epoch = cached_expires
                return

        await self._refresh_token(force=False)

    async def _refresh_token(self, *, force: bool) -> None:
        del force
        if self._credentials is None:
            raise SchedulesDirectAuthenticationError(
                "Schedules Direct credentials are not configured"
            )

        payload = await self._request_json(
            method="POST",
            route="token",
            payload={
                "username": self._credentials.username,
                "password": self._credentials.password,
            },
            token_required=False,
        )
        if not isinstance(payload, dict):
            raise SchedulesDirectAuthenticationError(
                "Schedules Direct token response was invalid"
            )

        code = _pick_int(payload, "code")
        if code != 0:
            raise SchedulesDirectAuthenticationError(
                "Schedules Direct rejected credential authentication"
            )

        token = _pick_str(payload, "token")
        token_datetime = _pick_str(payload, "datetime")
        if not token or not token_datetime:
            raise SchedulesDirectAuthenticationError(
                "Schedules Direct token response was incomplete"
            )

        token_issued = _parse_utc(token_datetime)
        token_expires = token_issued + timedelta(hours=23)
        self._token = token
        self._token_expires_at_epoch = token_expires.timestamp()

        if self._token_cache_store is not None:
            self._token_cache_store.save(
                SDTokenCache(
                    token=token,
                    token_expires_utc=token_expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )


def _parse_json_or_none(raw_value: str) -> object | None:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return None


def _pick_str(payload: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            trimmed = value.strip()
            if trimmed:
                return trimmed
    return None


def _pick_int(payload: dict[str, object], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.isdigit():
                return int(stripped)
    return None


def _parse_utc(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return parsed.replace(tzinfo=timezone.utc)


def _parse_optional_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return _parse_utc(value)
    except ValueError:
        return None


def _extract_latest_system_message(payload: dict[str, object]) -> str | None:
    system_status = payload.get("systemStatus")
    if not isinstance(system_status, list):
        return None

    latest_timestamp = -1.0
    latest_message: str | None = None
    for entry in system_status:
        if not isinstance(entry, dict):
            continue
        date_value = _pick_str(entry, "date")
        message_value = _pick_str(entry, "message")
        if date_value is None or message_value is None:
            continue
        parsed = _parse_optional_utc(date_value)
        if parsed is None:
            continue
        parsed_ts = parsed.timestamp()
        if parsed_ts > latest_timestamp:
            latest_timestamp = parsed_ts
            latest_message = message_value
    return latest_message


__all__ = [
    "JsonHttpTransport",
    "SchedulesDirectHttpClient",
    "UrlLibJsonTransport",
]
