from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class TvRecorderConfigError(Exception):
    """Raised when tvrecorder configuration cannot be loaded."""


@dataclass(frozen=True, slots=True)
class DvbCtrlCredentials:
    """Local dvbctrl authentication credentials."""

    password: str
    username: str


@dataclass(frozen=True, slots=True)
class TvRecorderConfig:
    """Persisted tvrecorder configuration loaded from disk."""

    dvbctrl_credentials: DvbCtrlCredentials | None = None


@dataclass(frozen=True, slots=True)
class TvRecorderConfigStore:
    """Load and persist tvrecorder config under the user config directory."""

    config_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "ccatv")
    file_name: str = "tvrecorder.json"

    @property
    def path(self) -> Path:
        """Return the full config file path."""
        return self.config_dir / self.file_name

    def load(self) -> TvRecorderConfig:
        """Load config from disk, returning defaults when no file exists."""
        if not self.path.exists():
            return TvRecorderConfig()

        try:
            raw_data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise TvRecorderConfigError(
                f"unable to read tvrecorder config: {self.path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise TvRecorderConfigError(
                f"invalid tvrecorder config JSON: {self.path}"
            ) from exc

        if not isinstance(raw_data, dict):
            raise TvRecorderConfigError(f"invalid tvrecorder config shape: {self.path}")

        dvbctrl = raw_data.get("dvbctrl")
        if dvbctrl is None:
            return TvRecorderConfig()
        if not isinstance(dvbctrl, dict):
            raise TvRecorderConfigError(f"invalid dvbctrl config shape: {self.path}")

        username = dvbctrl.get("username")
        password = dvbctrl.get("password")
        if not username or not password:
            return TvRecorderConfig()

        return TvRecorderConfig(
            dvbctrl_credentials=DvbCtrlCredentials(
                password=str(password),
                username=str(username),
            )
        )

    def save(self, config: TvRecorderConfig) -> Path:
        """Persist config to disk and tighten filesystem permissions."""
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700)

        payload: dict[str, dict[str, str]] = {}
        if config.dvbctrl_credentials is not None:
            payload["dvbctrl"] = {
                "password": config.dvbctrl_credentials.password,
                "username": config.dvbctrl_credentials.username,
            }

        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(self.path, 0o600)
        return self.path


__all__ = [
    "DvbCtrlCredentials",
    "TvRecorderConfig",
    "TvRecorderConfigError",
    "TvRecorderConfigStore",
]
