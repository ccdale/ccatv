from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from ccatv.metadata.schedules_direct_api import (
    SchedulesDirectHttpClient,
    _parse_retry_after_header,
    _TransportHttpError,
)
from ccatv.metadata.schedules_direct_contract import (
    GuideSyncWindow,
    SchedulesDirectAuthenticationError,
    SchedulesDirectRateLimitError,
    SDCredentials,
)
from ccatv.metadata.schedules_direct_runtime import SDTokenCache


@dataclass(slots=True)
class StubTransport:
    responses: list[object | Exception]
    calls: list[dict[str, object]] = field(default_factory=list)

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
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "payload": payload,
            "query": query,
            "timeout_seconds": timeout_seconds,
        })
        if not self.responses:
            raise AssertionError("unexpected extra request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@dataclass(slots=True)
class StubTokenCacheStore:
    load_result: SDTokenCache | None = None
    saved: list[SDTokenCache] = field(default_factory=list)

    def load(self) -> SDTokenCache | None:
        return self.load_result

    def save(self, cache: SDTokenCache) -> None:
        self.saved.append(cache)


@dataclass(slots=True)
class StubResponseCacheStore:
    cached: dict[str, object] = field(default_factory=dict)

    def load(self, key: str) -> object | None:
        return self.cached.get(key)

    def save(self, *, key: str, payload: object, ttl_seconds: int) -> None:
        del ttl_seconds
        self.cached[key] = payload


def test_authenticate_requests_token_and_caches() -> None:
    transport = StubTransport(
        responses=[
            {
                "code": 0,
                "token": "token-abc",
                "datetime": "2026-05-23T10:00:00Z",
            }
        ]
    )
    cache = StubTokenCacheStore()
    client = SchedulesDirectHttpClient(
        transport=transport,
        response_cache_store=StubResponseCacheStore(),
        token_cache_store=cache,
    )

    asyncio.run(client.authenticate(SDCredentials(username="alice", password="secret")))

    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"].endswith("/token")
    assert transport.calls[0]["payload"] == {
        "username": "alice",
        "password": hashlib.sha1("secret".encode("utf-8")).hexdigest(),
    }
    assert cache.saved
    assert cache.saved[0].token == "token-abc"


def test_get_account_status_uses_cached_token() -> None:
    transport = StubTransport(
        responses=[
            {
                "systemStatus": [
                    {
                        "date": "2026-05-23T11:00:00Z",
                        "message": "Online",
                    }
                ],
                "maxLineups": 4,
            }
        ]
    )
    cache = StubTokenCacheStore(
        load_result=SDTokenCache(
            token="cached-token",
            token_expires_utc="2099-01-01T00:00:00Z",
        )
    )
    client = SchedulesDirectHttpClient(
        transport=transport,
        response_cache_store=StubResponseCacheStore(),
        token_cache_store=cache,
    )

    status = asyncio.run(client.get_account_status())

    assert status.provider_message == "Online"
    assert status.max_lineups == 4
    assert len(transport.calls) == 1
    assert transport.calls[0]["headers"]["token"] == "cached-token"


def test_authenticate_failure_does_not_leak_credentials() -> None:
    transport = StubTransport(
        responses=[
            {
                "code": 3000,
                "response": "Bad username/password",
            }
        ]
    )
    client = SchedulesDirectHttpClient(
        response_cache_store=StubResponseCacheStore(),
        transport=transport,
    )

    with pytest.raises(SchedulesDirectAuthenticationError) as excinfo:
        asyncio.run(
            client.authenticate(
                SDCredentials(username="alice", password="very-secret"),
            )
        )

    message = str(excinfo.value).lower()
    assert "very-secret" not in message
    assert "alice" not in message


def test_list_lineups_parses_payload() -> None:
    transport = StubTransport(
        responses=[
            {
                "lineups": [
                    {
                        "lineup": "USA-TEST-X",
                        "name": "Test Lineup",
                        "transport": "Antenna",
                        "country": "USA",
                        "location": "02134",
                    }
                ]
            }
        ]
    )
    cache = StubTokenCacheStore(
        load_result=SDTokenCache(
            token="cached-token",
            token_expires_utc="2099-01-01T00:00:00Z",
        )
    )
    client = SchedulesDirectHttpClient(
        response_cache_store=StubResponseCacheStore(),
        token_cache_store=cache,
        transport=transport,
    )

    lineups = asyncio.run(client.list_lineups(country="USA", postal_code="02134"))

    assert len(lineups) == 1
    assert lineups[0].lineup_id == "USA-TEST-X"
    assert lineups[0].name == "Test Lineup"


def test_get_schedules_parses_program_slots() -> None:
    transport = StubTransport(
        responses=[
            {
                "map": [{"stationID": "101", "channel": "1"}],
                "stations": [
                    {
                        "stationID": "101",
                        "callsign": "BBC1",
                        "name": "BBC One",
                    }
                ],
            },
            [
                {
                    "stationID": "101",
                    "programs": [
                        {
                            "programID": "EP0001",
                            "airDateTime": "2026-05-24T10:00:00Z",
                            "duration": 1800,
                            "new": True,
                            "audioProperties": ["stereo"],
                            "videoProperties": ["hd"],
                        }
                    ],
                }
            ],
        ]
    )
    cache = StubTokenCacheStore(
        load_result=SDTokenCache(
            token="cached-token",
            token_expires_utc="2099-01-01T00:00:00Z",
        )
    )
    client = SchedulesDirectHttpClient(
        response_cache_store=StubResponseCacheStore(),
        token_cache_store=cache,
        transport=transport,
    )

    schedules = asyncio.run(
        client.get_schedules(
            lineup_id="UK-TEST",
            window=GuideSyncWindow(
                start_utc=datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc),
                end_utc=datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
            ),
        )
    )

    assert len(schedules) == 1
    assert schedules[0].station_id == "101"
    assert schedules[0].program_id == "EP0001"
    assert schedules[0].duration_seconds == 1800
    assert schedules[0].is_new is True
    assert schedules[0].audio_properties == ("stereo",)
    assert schedules[0].video_properties == ("hd",)


def test_get_programs_parses_program_metadata() -> None:
    transport = StubTransport(
        responses=[
            [
                {
                    "programID": "EP0001",
                    "titles": [{"title120": "Morning News"}],
                    "episodeTitle150": "Top Headlines",
                    "descriptions": {
                        "description1000": [
                            {
                                "description": "Latest national and global updates.",
                            }
                        ]
                    },
                    "genres": ["News"],
                    "originalAirDate": "2026-05-24",
                    "episodeImage": {"uri": "https://img.example/episode.jpg"},
                }
            ]
        ]
    )
    cache = StubTokenCacheStore(
        load_result=SDTokenCache(
            token="cached-token",
            token_expires_utc="2099-01-01T00:00:00Z",
        )
    )
    client = SchedulesDirectHttpClient(
        response_cache_store=StubResponseCacheStore(),
        token_cache_store=cache,
        transport=transport,
    )

    programs = asyncio.run(client.get_programs(["EP0001"]))

    assert len(programs) == 1
    assert programs[0].program_id == "EP0001"
    assert programs[0].title == "Morning News"
    assert programs[0].episode_title == "Top Headlines"
    assert programs[0].description == "Latest national and global updates."
    assert programs[0].genres == ("News",)
    assert programs[0].artwork_urls == ("https://img.example/episode.jpg",)


def test_get_programs_uses_response_cache() -> None:
    transport = StubTransport(
        responses=[
            [
                {
                    "programID": "EP0001",
                    "titles": [{"title120": "Morning News"}],
                }
            ]
        ]
    )
    cache = StubTokenCacheStore(
        load_result=SDTokenCache(
            token="cached-token",
            token_expires_utc="2099-01-01T00:00:00Z",
        )
    )
    response_cache = StubResponseCacheStore()
    client = SchedulesDirectHttpClient(
        response_cache_store=response_cache,
        token_cache_store=cache,
        transport=transport,
    )

    first = asyncio.run(client.get_programs(["EP0001"]))
    second = asyncio.run(client.get_programs(["EP0001"]))

    assert len(first) == 1
    assert len(second) == 1
    assert len(transport.calls) == 1


def test_rate_limit_retries_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = StubTransport(
        responses=[
            _TransportHttpError(
                status_code=429,
                payload={"code": 4001},
                message="HTTP status 429",
                retry_after_seconds=1,
            ),
            {
                "systemStatus": [
                    {
                        "date": "2026-05-23T11:00:00Z",
                        "message": "Online",
                    }
                ],
                "maxLineups": 4,
            },
        ]
    )
    cache = StubTokenCacheStore(
        load_result=SDTokenCache(
            token="cached-token",
            token_expires_utc="2099-01-01T00:00:00Z",
        )
    )
    client = SchedulesDirectHttpClient(
        response_cache_store=StubResponseCacheStore(),
        token_cache_store=cache,
        transport=transport,
    )

    async def _noop_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(
        "ccatv.metadata.schedules_direct_api.asyncio.sleep", _noop_sleep
    )

    status = asyncio.run(client.get_account_status())

    assert status.max_lineups == 4
    assert len(transport.calls) == 2


def test_rate_limit_exhaustion_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = StubTransport(
        responses=[
            _TransportHttpError(
                status_code=429,
                payload={"code": 4001},
                message="HTTP status 429",
                retry_after_seconds=2,
            ),
            _TransportHttpError(
                status_code=429,
                payload={"code": 4001},
                message="HTTP status 429",
                retry_after_seconds=2,
            ),
        ]
    )
    cache = StubTokenCacheStore(
        load_result=SDTokenCache(
            token="cached-token",
            token_expires_utc="2099-01-01T00:00:00Z",
        )
    )
    client = SchedulesDirectHttpClient(
        max_rate_limit_retries=1,
        response_cache_store=StubResponseCacheStore(),
        token_cache_store=cache,
        transport=transport,
    )

    async def _noop_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(
        "ccatv.metadata.schedules_direct_api.asyncio.sleep", _noop_sleep
    )

    with pytest.raises(SchedulesDirectRateLimitError) as excinfo:
        asyncio.run(client.get_account_status())

    assert excinfo.value.retry_after_seconds == 2


def test_parse_retry_after_header_with_delta_seconds() -> None:
    assert _parse_retry_after_header("12") == 12


def test_parse_retry_after_header_rejects_invalid_value() -> None:
    assert _parse_retry_after_header("not-a-delay") is None
