from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccatv.metadata.schedules_direct_contract import SDCredentials
from ccatv.metadata.schedules_direct_runtime import (
    SchedulesDirectConfigError,
    SchedulesDirectCredentialStore,
    SchedulesDirectResponseCacheStore,
    SchedulesDirectTokenCacheStore,
    SDTokenCache,
)


def test_credential_store_loads_top_level_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules_direct.json"
    config_path.write_text(
        json.dumps({"username": "alice", "password": "secret"}),
        encoding="utf-8",
    )

    store = SchedulesDirectCredentialStore(path=config_path)

    assert store.load() == SDCredentials(username="alice", password="secret")


def test_credential_store_loads_nested_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules_direct.json"
    config_path.write_text(
        json.dumps({
            "schedulesdirect": {
                "username": "alice",
                "password": "secret",
            }
        }),
        encoding="utf-8",
    )

    store = SchedulesDirectCredentialStore(path=config_path)

    assert store.load() == SDCredentials(username="alice", password="secret")


def test_credential_store_rejects_missing_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "schedules_direct.json"
    config_path.write_text(json.dumps({"username": "alice"}), encoding="utf-8")

    store = SchedulesDirectCredentialStore(path=config_path)

    with pytest.raises(
        SchedulesDirectConfigError,
        match="username/password are required",
    ):
        store.load()


def test_credential_store_uses_legacy_fallback_when_primary_missing(
    tmp_path: Path,
) -> None:
    primary_path = tmp_path / "tvrecorder.json"
    legacy_path = tmp_path / "schedules_direct.json"
    legacy_path.write_text(
        json.dumps({"username": "alice", "password": "secret"}),
        encoding="utf-8",
    )

    store = SchedulesDirectCredentialStore(path=primary_path)

    assert store.load() == SDCredentials(username="alice", password="secret")


def test_token_cache_roundtrip(tmp_path: Path) -> None:
    cache_path = tmp_path / "sd_token.json"
    store = SchedulesDirectTokenCacheStore(path=cache_path)

    store.save(
        SDTokenCache(
            token="token-123",
            token_expires_utc="2026-05-23T23:59:59Z",
        )
    )

    assert store.load() == SDTokenCache(
        token="token-123",
        token_expires_utc="2026-05-23T23:59:59Z",
    )


def test_token_cache_clear(tmp_path: Path) -> None:
    cache_path = tmp_path / "sd_token.json"
    store = SchedulesDirectTokenCacheStore(path=cache_path)
    store.save(
        SDTokenCache(
            token="token-123",
            token_expires_utc="2026-05-23T23:59:59Z",
        )
    )

    store.clear()

    assert store.load() is None


def test_response_cache_roundtrip(tmp_path: Path) -> None:
    cache_path = tmp_path / "sd_response_cache.json"
    store = SchedulesDirectResponseCacheStore(path=cache_path)

    store.save(key="stations:v1", payload={"stations": ["101"]}, ttl_seconds=60)

    assert store.load("stations:v1") == {"stations": ["101"]}


def test_response_cache_ignores_expired_entries(tmp_path: Path) -> None:
    cache_path = tmp_path / "sd_response_cache.json"
    cache_path.write_text(
        json.dumps({
            "stations:v1": {
                "expires_at_utc": "2000-01-01T00:00:00Z",
                "payload": {"stations": ["101"]},
            }
        }),
        encoding="utf-8",
    )
    store = SchedulesDirectResponseCacheStore(path=cache_path)

    assert store.load("stations:v1") is None
