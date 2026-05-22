from __future__ import annotations

import os
from dataclasses import dataclass


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
    dvbstreamer_bind_address: str = "127.0.0.1"
    dvbstreamer_host: str = "localhost"
    dvbstreamer_output_mrl: str = "null://"
    dvbstreamer_path: str = "dvbstreamer"
    dvbstreamer_stop_timeout_seconds: float = 5.0
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

        stop_timeout_raw = os.getenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", "5.0")
        try:
            stop_timeout_seconds = float(stop_timeout_raw)
        except ValueError:
            stop_timeout_seconds = 5.0

        return cls(
            log_level=os.getenv("CCATV_LOG_LEVEL", "INFO").upper(),
            dvbctrl_path=os.getenv("CCATV_DVBCTRL_PATH", "dvbctrl"),
            dvbstreamer_bind_address=os.getenv(
                "CCATV_DVBSTREAMER_BIND_ADDRESS",
                "127.0.0.1",
            ),
            dvbstreamer_host=os.getenv("CCATV_DVBSTREAMER_HOST", "localhost"),
            dvbstreamer_output_mrl=os.getenv("CCATV_DVBSTREAMER_OUTPUT_MRL", "null://"),
            dvbstreamer_path=os.getenv("CCATV_DVBSTREAMER_PATH", "dvbstreamer"),
            dvbstreamer_stop_timeout_seconds=stop_timeout_seconds,
            dvb_adapter_index=_env_int("CCATV_DVB_ADAPTER_INDEX", 0),
            dvbctrl_timeout_seconds=timeout_seconds,
        )
