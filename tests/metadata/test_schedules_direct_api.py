from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from ccatv.metadata.schedules_direct_api import SchedulesDirectHttpClient
from ccatv.metadata.schedules_direct_contract import (
    SDCredentials,
    SchedulesDirectAuthenticationError,
)
from ccatv.metadata.schedules_direct_runtime import SDTokenCache


@dataclass(slots=True)
class StubTransport:
    responses: list[object]
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
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "query": query,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


@dataclass(slots=True)
class StubTokenCacheStore:
    load_result: SDTokenCache | None = None
    saved: list[SDTokenCache] = field(default_factory=list)

    def load(self) -> SDTokenCache | None:
        return self.load_result

    def save(self, cache: SDTokenCache) -> None:
        self.saved.append(cache)


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
        token_cache_store=cache,
    )

    asyncio.run(client.authenticate(SDCredentials(username="alice", password="secret")))

    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"].endswith("/token")
    assert transport.calls[0]["payload"] == {
        "username": "alice",
        "password": "secret",
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
    client = SchedulesDirectHttpClient(transport=transport)

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
    client = SchedulesDirectHttpClient(transport=transport, token_cache_store=cache)

    lineups = asyncio.run(client.list_lineups(country="USA", postal_code="02134"))

    assert len(lineups) == 1
    assert lineups[0].lineup_id == "USA-TEST-X"
    assert lineups[0].name == "Test Lineup"
