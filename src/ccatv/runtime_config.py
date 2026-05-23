from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir


class RuntimeConfigError(Exception):
    """Raised when ccatv runtime configuration cannot be loaded."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Persisted ccatv runtime configuration loaded from disk."""

    dvb_adapter_count: int = 1
    dvbstreamer_host: str = "localhost"


@dataclass(frozen=True, slots=True)
class RuntimeConfigStore:
    """Load and persist ccatv runtime config under XDG config."""

    config_dir: Path = field(
        default_factory=lambda: Path(user_config_dir("ccatv", appauthor=False))
    )
    file_name: str = "runtime.json"

    @property
    def path(self) -> Path:
        """Return the full config file path."""
        return self.config_dir / self.file_name

    def load(self) -> RuntimeConfig:
        """Load runtime config from disk, returning defaults when missing."""
        if not self.path.exists():
            return RuntimeConfig()

        try:
            raw_data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeConfigError(
                f"unable to read runtime config: {self.path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeConfigError(
                f"invalid runtime config JSON: {self.path}"
            ) from exc

        if not isinstance(raw_data, dict):
            raise RuntimeConfigError(f"invalid runtime config shape: {self.path}")

        host = raw_data.get("dvbstreamer_host", "localhost")
        if not isinstance(host, str) or not host.strip():
            raise RuntimeConfigError(f"invalid dvbstreamer_host value: {self.path}")

        adapter_count = raw_data.get("dvb_adapter_count", 1)
        if not isinstance(adapter_count, int) or adapter_count < 1:
            raise RuntimeConfigError(f"invalid dvb_adapter_count value: {self.path}")

        return RuntimeConfig(
            dvb_adapter_count=adapter_count,
            dvbstreamer_host=host.strip(),
        )

    def save(self, config: RuntimeConfig) -> Path:
        """Persist runtime config to disk with user-only permissions."""
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700)

        payload = {
            "dvb_adapter_count": config.dvb_adapter_count,
            "dvbstreamer_host": config.dvbstreamer_host,
        }
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(self.path, 0o600)
        return self.path


__all__ = ["RuntimeConfig", "RuntimeConfigError", "RuntimeConfigStore"]
