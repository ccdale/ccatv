from __future__ import annotations

from dataclasses import dataclass
import os


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Runtime settings loaded from environment variables."""

    log_level: str = "INFO"
    dvbctrl_path: str = "dvbctrl"
    dvbstreamer_host: str = "localhost"
    dvb_adapter_index: int = 0
    dvbctrl_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> AppSettings:
        """Build settings from environment with sane defaults."""
        timeout_raw = os.getenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "10.0")
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError:
            timeout_seconds = 10.0

        return cls(
            log_level=os.getenv("CCATV_LOG_LEVEL", "INFO").upper(),
            dvbctrl_path=os.getenv("CCATV_DVBCTRL_PATH", "dvbctrl"),
            dvbstreamer_host=os.getenv("CCATV_DVBSTREAMER_HOST", "localhost"),
            dvb_adapter_index=_env_int("CCATV_DVB_ADAPTER_INDEX", 0),
            dvbctrl_timeout_seconds=timeout_seconds,
        )
