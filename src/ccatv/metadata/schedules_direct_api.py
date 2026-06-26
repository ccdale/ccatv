from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib import error, parse, request

from ccatv import __app_name__, __version__
from ccatv.metadata.schedules_direct_contract import (
    GuideSyncWindow,
    SchedulesDirectApiError,
    SchedulesDirectAuthenticationError,
    SchedulesDirectClient,
    SchedulesDirectRateLimitError,
    SchedulesDirectTransportError,
    SDAccountStatus,
    SDCredentials,
    SDLineup,
    SDProgram,
    SDScheduleEntry,
    SDStation,
)
from ccatv.metadata.schedules_direct_runtime import (
    SchedulesDirectResponseCacheStore,
    SchedulesDirectTokenCacheStore,
    SDTokenCache,
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
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _TransportHttpError(Exception):
    status_code: int
    payload: object | None
    message: str
    retry_after_seconds: int | None = None


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
            retry_after_seconds = _parse_retry_after_header(
                exc.headers.get("Retry-After")
            )
            raise _TransportHttpError(
                status_code=exc.code,
                payload=parsed_payload,
                message=f"HTTP status {exc.code}",
                retry_after_seconds=retry_after_seconds,
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
        max_rate_limit_retries: int = 3,
        rate_limit_backoff_seconds: float = 2.0,
        response_cache_store: SchedulesDirectResponseCacheStore | None = None,
        token_cache_store: SchedulesDirectTokenCacheStore | None = None,
        transport: JsonHttpTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_rate_limit_retries = max(0, max_rate_limit_retries)
        self._rate_limit_backoff_seconds = max(0.1, rate_limit_backoff_seconds)
        self._response_cache_store = (
            response_cache_store or SchedulesDirectResponseCacheStore()
        )
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
            cache_ttl_seconds=300,
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
            cache_ttl_seconds=86_400,
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
            cache_ttl_seconds=21_600,
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
        station_ids = [
            station.station_id for station in await self.get_lineup_stations(lineup_id)
        ]
        if not station_ids:
            return []

        dates = _window_dates(window)
        if not dates:
            return []

        payload = await self._request_json(
            method="POST",
            route="schedules",
            payload=[
                {
                    "stationID": station_id,
                    "date": dates,
                }
                for station_id in station_ids
            ],
            token_required=True,
            cache_ttl_seconds=1_800,
        )
        if not isinstance(payload, list):
            return []

        result: list[SDScheduleEntry] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            station_id = _pick_str(row, "stationID", "stationId")
            if not station_id:
                continue

            programs = row.get("programs")
            if not isinstance(programs, list):
                continue

            for item in programs:
                if not isinstance(item, dict):
                    continue
                entry = _parse_schedule_entry(item=item, station_id=station_id)
                if entry is None:
                    continue
                if (
                    entry.end_utc <= window.start_utc
                    or entry.start_utc >= window.end_utc
                ):
                    continue
                result.append(entry)

        return sorted(
            result,
            key=lambda entry: (entry.start_utc, entry.station_id, entry.program_id),
        )

    async def get_programs(self, program_ids: list[str]) -> list[SDProgram]:
        unique_program_ids = list(
            dict.fromkeys(pid.strip() for pid in program_ids if pid.strip())
        )
        if not unique_program_ids:
            return []

        result: list[SDProgram] = []
        for batch in _chunked(unique_program_ids, 500):
            payload = await self._request_json(
                method="POST",
                route="programs",
                payload=batch,
                token_required=True,
                cache_ttl_seconds=86_400,
            )
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict):
                    continue
                program = _parse_program(item)
                if program is None:
                    continue
                result.append(program)

        return result

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
        cache_ttl_seconds: int | None = None,
    ) -> object:
        headers = {"User-Agent": self._user_agent}
        if token_required:
            await self._ensure_token()
            if not self._token:
                raise SchedulesDirectAuthenticationError(
                    "Schedules Direct token unavailable"
                )
            headers["token"] = self._token

        cache_key: str | None = None
        if cache_ttl_seconds and cache_ttl_seconds > 0 and route != "token":
            cache_key = _build_cache_key(
                method=method,
                route=route,
                payload=payload,
                query=query,
            )
            cached = self._response_cache_store.load(cache_key)
            if cached is not None:
                return cached

        url = f"{self._base_url}/{route.lstrip('/')}"
        attempts = self._max_rate_limit_retries + 1
        for attempt in range(attempts):
            try:
                response_payload = await asyncio.to_thread(
                    self._transport.request_json,
                    method=method,
                    url=url,
                    headers=headers,
                    payload=payload,
                    query=query,
                    timeout_seconds=self._timeout_seconds,
                )
                if cache_key is not None and cache_ttl_seconds is not None:
                    self._response_cache_store.save(
                        key=cache_key,
                        payload=response_payload,
                        ttl_seconds=cache_ttl_seconds,
                    )
                return response_payload
            except _TransportHttpError as exc:
                if exc.status_code == 401:
                    raise SchedulesDirectAuthenticationError(
                        "Schedules Direct rejected authentication"
                    ) from exc
                if exc.status_code == 429:
                    retry_after_seconds = _extract_retry_after_seconds(exc)
                    if attempt < attempts - 1:
                        delay = retry_after_seconds
                        if delay is None:
                            delay = int(self._rate_limit_backoff_seconds * (2**attempt))
                        await asyncio.sleep(max(1, delay))
                        continue
                    raise SchedulesDirectRateLimitError(
                        "Schedules Direct rate limited this request",
                        retry_after_seconds=retry_after_seconds,
                    ) from exc
                raise SchedulesDirectApiError(
                    code=None,
                    message=f"Schedules Direct HTTP error ({exc.status_code})",
                ) from exc

        raise SchedulesDirectApiError(
            code=None, message="Schedules Direct request failed"
        )

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
                "password": _sha1_hex(self._credentials.password),
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


def _sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _build_cache_key(
    *,
    method: str,
    route: str,
    payload: object | None,
    query: dict[str, str] | None,
) -> str:
    identity = {
        "method": method,
        "route": route,
        "payload": payload,
        "query": query,
    }
    rendered = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _extract_retry_after_seconds(exc: _TransportHttpError) -> int | None:
    if exc.retry_after_seconds is not None:
        return exc.retry_after_seconds
    if isinstance(exc.payload, dict):
        retry_value = _pick_int(exc.payload, "retryAfter", "retry_after")
        if retry_value is not None and retry_value > 0:
            return retry_value
    return None


def _parse_retry_after_header(raw_value: object) -> int | None:
    if not isinstance(raw_value, str):
        return None

    value = raw_value.strip()
    if not value:
        return None

    if value.isdigit():
        seconds = int(value)
        return seconds if seconds > 0 else None

    try:
        retry_after_dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if retry_after_dt.tzinfo is None:
        retry_after_dt = retry_after_dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delay_seconds = int((retry_after_dt - now).total_seconds())
    return delay_seconds if delay_seconds > 0 else None


def _chunked(values: list[str], chunk_size: int) -> list[list[str]]:
    return [
        values[index : index + chunk_size]
        for index in range(0, len(values), chunk_size)
    ]


def _window_dates(window: GuideSyncWindow) -> list[str]:
    start_date = window.start_utc.date()
    end_date = window.end_utc.date()
    if end_date < start_date:
        return []

    dates: list[str] = []
    current = start_date
    while current <= end_date:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def _parse_schedule_entry(
    *, item: dict[str, object], station_id: str
) -> SDScheduleEntry | None:
    program_id = _pick_str(item, "programID", "programId")
    air_datetime = _pick_str(item, "airDateTime")
    duration_seconds = _pick_int(item, "duration")
    if (
        not program_id
        or not air_datetime
        or duration_seconds is None
        or duration_seconds <= 0
    ):
        return None

    start_utc = _parse_optional_utc(air_datetime)
    if start_utc is None:
        return None

    end_utc = start_utc + timedelta(seconds=duration_seconds)
    return SDScheduleEntry(
        station_id=station_id,
        program_id=program_id,
        start_utc=start_utc,
        end_utc=end_utc,
        duration_seconds=duration_seconds,
        is_live=_pick_bool(item, "liveTapeDelay", "isLive") or False,
        is_new=_pick_bool(item, "new", "isNew") or False,
        audio_properties=_pick_str_sequence(item, "audioProperties"),
        video_properties=_pick_str_sequence(item, "videoProperties"),
    )


def _parse_program(item: dict[str, object]) -> SDProgram | None:
    program_id = _pick_str(item, "programID", "programId")
    if not program_id:
        return None

    title = _extract_program_title(item)
    if title is None:
        return None

    return SDProgram(
        program_id=program_id,
        title=title,
        episode_title=_extract_program_episode_title(item),
        description=_extract_program_description(item),
        original_air_date=_parse_optional_date(_pick_str(item, "originalAirDate")),
        season_number=_extract_program_season_number(item),
        episode_number=_extract_program_episode_number(item),
        episode_id_onscreen=_extract_program_episode_id_onscreen(item),
        genres=_pick_str_sequence(item, "genres"),
        artwork_urls=_extract_artwork_urls(item),
    )


def _extract_program_title(item: dict[str, object]) -> str | None:
    titles = item.get("titles")
    if isinstance(titles, list):
        for title in titles:
            if not isinstance(title, dict):
                continue
            value = _pick_str(title, "title120", "title")
            if value:
                return value
    return _pick_str(item, "title")


def _extract_program_episode_title(item: dict[str, object]) -> str | None:
    value = _pick_str(item, "episodeTitle150")
    if value:
        return value
    titles = item.get("episodeTitle")
    if isinstance(titles, list):
        for title in titles:
            if not isinstance(title, dict):
                continue
            value = _pick_str(title, "title150", "title")
            if value:
                return value
    return None


def _extract_program_description(item: dict[str, object]) -> str | None:
    descriptions = item.get("descriptions")
    if isinstance(descriptions, dict):
        for key in ("description1000", "description100"):
            values = descriptions.get(key)
            if not isinstance(values, list):
                continue
            for entry in values:
                if not isinstance(entry, dict):
                    continue
                value = _pick_str(entry, "description")
                if value:
                    return value
    return _pick_str(item, "description")


def _extract_artwork_urls(item: dict[str, object]) -> tuple[str, ...]:
    urls: list[str] = []
    episode_image = item.get("episodeImage")
    if isinstance(episode_image, dict):
        uri = _pick_str(episode_image, "uri")
        if uri:
            urls.append(uri)

    for key in ("keyArt", "showImages"):
        images = item.get(key)
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict):
                continue
            uri = _pick_str(image, "uri")
            if uri:
                urls.append(uri)

    # Keep insertion order but drop duplicates.
    return tuple(dict.fromkeys(urls))


def _extract_program_season_number(item: dict[str, object]) -> int | None:
    direct = _pick_int(item, "season", "seasonNumber", "seasonNum")
    if direct is not None:
        return direct
    return _pick_int_from_metadata(item, "season", "seasonNumber", "seasonNum")


def _extract_program_episode_number(item: dict[str, object]) -> int | None:
    direct = _pick_int(item, "episode", "episodeNumber", "episodeNum")
    if direct is not None:
        return direct
    return _pick_int_from_metadata(item, "episode", "episodeNumber", "episodeNum")


def _extract_program_episode_id_onscreen(item: dict[str, object]) -> str | None:
    explicit = _pick_str(item, "syndicatedEpisodeNumber", "episodeID", "episodeId")
    if explicit:
        return explicit

    season_number = _extract_program_season_number(item)
    episode_number = _extract_program_episode_number(item)
    if season_number is not None and episode_number is not None:
        return f"S{season_number:02d}E{episode_number:02d}"
    return None


def _pick_int_from_metadata(payload: dict[str, object], *keys: str) -> int | None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None

    for value in metadata.values():
        if isinstance(value, dict):
            picked = _pick_int(value, *keys)
            if picked is not None:
                return picked
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            picked = _pick_int(item, *keys)
            if picked is not None:
                return picked
    return None


def _pick_bool(payload: dict[str, object], *keys: str) -> bool | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "false"}:
                return lowered == "true"
    return None


def _pick_str_sequence(payload: dict[str, object], key: str) -> tuple[str, ...]:
    raw_values = payload.get(key)
    if not isinstance(raw_values, list):
        return ()

    values: list[str] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            continue
        trimmed = raw_value.strip()
        if not trimmed:
            continue
        values.append(trimmed)
    return tuple(dict.fromkeys(values))


def _parse_optional_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
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
