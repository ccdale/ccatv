from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_state_dir

from ccatv.metadata.schedules_direct_contract import SDCredentials


class SchedulesDirectConfigError(Exception):
    """Raised when runtime Schedules Direct configuration is invalid."""


@dataclass(frozen=True, slots=True)
class SDTokenCache:
    token: str
    token_expires_utc: str


@dataclass(frozen=True, slots=True)
class SDResponseCacheEntry:
    expires_at_utc: str
    payload: object


class SchedulesDirectCredentialStore:
    """Loads Schedules Direct username/password from local runtime config."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_credentials_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> SDCredentials:
        credential_path = self._resolve_credentials_path()
        if not credential_path.exists():
            raise SchedulesDirectConfigError(
                f"Schedules Direct credentials file not found. Expected: {self._path}"
            )

        try:
            raw_data = json.loads(credential_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchedulesDirectConfigError(
                "Schedules Direct credentials file is not valid JSON"
            ) from exc

        if not isinstance(raw_data, dict):
            raise SchedulesDirectConfigError(
                "Schedules Direct credentials file must be a JSON object"
            )

        scope = raw_data.get("schedulesdirect", raw_data)
        if not isinstance(scope, dict):
            raise SchedulesDirectConfigError(
                "Schedules Direct credentials object is malformed"
            )

        username = str(scope.get("username", "")).strip()
        password = str(scope.get("password", "")).strip()
        if not username or not password:
            raise SchedulesDirectConfigError(
                "Schedules Direct username/password are required"
            )

        return SDCredentials(username=username, password=password)

    def _resolve_credentials_path(self) -> Path:
        if self._path.exists():
            return self._path

        # Backward-compatible fallback for earlier SD-only config draft.
        if self._path.name == "tvrecorder.json":
            legacy_path = self._path.with_name("schedules_direct.json")
            if legacy_path.exists():
                return legacy_path

        return self._path


class SchedulesDirectTokenCacheStore:
    """Stores the provider API token in local runtime state (never in repo files)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_token_cache_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> SDTokenCache | None:
        if not self._path.exists():
            return None

        try:
            raw_data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        if not isinstance(raw_data, dict):
            return None

        token = str(raw_data.get("token", "")).strip()
        token_expires_utc = str(raw_data.get("token_expires_utc", "")).strip()
        if not token or not token_expires_utc:
            return None

        try:
            _parse_utc(token_expires_utc)
        except ValueError:
            return None

        return SDTokenCache(token=token, token_expires_utc=token_expires_utc)

    def save(self, cache: SDTokenCache) -> None:
        _parse_utc(cache.token_expires_utc)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": cache.token,
            "token_expires_utc": cache.token_expires_utc,
        }
        self._path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


class SchedulesDirectResponseCacheStore:
    """Stores non-secret Schedules Direct response payloads in local cache state."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_response_cache_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self, key: str) -> object | None:
        now = datetime.now(timezone.utc)
        cache = self._read_cache_map()
        if cache is None:
            return None

        entry = cache.get(key)
        if not isinstance(entry, dict):
            return None

        expires_at_utc = str(entry.get("expires_at_utc", "")).strip()
        if not expires_at_utc:
            return None

        try:
            expires_at = _parse_utc(expires_at_utc)
        except ValueError:
            return None

        if expires_at <= now:
            return None

        return entry.get("payload")

    def save(self, *, key: str, payload: object, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        cache = self._read_cache_map() or {}
        self._prune_expired_entries(cache)
        cache[key] = {
            "expires_at_utc": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "payload": payload,
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()

    def _read_cache_map(self) -> dict[str, object] | None:
        if not self._path.exists():
            return None

        try:
            raw_data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        if not isinstance(raw_data, dict):
            return None

        return raw_data

    def _prune_expired_entries(self, cache: dict[str, object]) -> None:
        now = datetime.now(timezone.utc)
        expired_keys: list[str] = []
        for key, value in cache.items():
            if not isinstance(value, dict):
                expired_keys.append(key)
                continue
            expires_at_utc = str(value.get("expires_at_utc", "")).strip()
            if not expires_at_utc:
                expired_keys.append(key)
                continue
            try:
                expires_at = _parse_utc(expires_at_utc)
            except ValueError:
                expired_keys.append(key)
                continue
            if expires_at <= now:
                expired_keys.append(key)

        for key in expired_keys:
            cache.pop(key, None)


def _default_credentials_path() -> Path:
    return Path(user_config_dir("ccatv", appauthor=False)) / "tvrecorder.json"


def _default_token_cache_path() -> Path:
    return (
        Path(user_state_dir("ccatv", appauthor=False))
        / "schedules_direct_token_cache.json"
    )


def _default_response_cache_path() -> Path:
    return (
        Path(user_cache_dir("ccatv", appauthor=False))
        / "schedules_direct_response_cache.json"
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return parsed.replace(tzinfo=timezone.utc)
