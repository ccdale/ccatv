from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

from ccatv.runtime_config import RuntimeConfig, RuntimeConfigError, RuntimeConfigStore


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_positive_int(name: str, default: int) -> int:
    value = _env_int(name, default)
    if value < 1:
        return default
    return value


def _env_non_negative_int(name: str, default: int) -> int:
    value = _env_int(name, default)
    if value < 0:
        return default
    return value


def _env_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _env_non_empty_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    trimmed = raw.strip()
    if not trimmed:
        return default
    return trimmed


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
    dvb_adapter_count: int = 1
    dvb_adapter_index: int = 0
    dvbctrl_timeout_seconds: float = 10.0
    database_path: str = str(
        Path(user_data_dir("ccatv", appauthor=False)) / "ccatv.sqlite3"
    )

    @classmethod
    def from_env(cls) -> AppSettings:
        """Build settings from environment with sane defaults."""
        try:
            runtime_config = RuntimeConfigStore().load()
        except RuntimeConfigError:
            runtime_config = RuntimeConfig()
        timeout_seconds = _env_positive_float("CCATV_DVBCTRL_TIMEOUT_SECONDS", 10.0)
        stop_timeout_seconds = _env_positive_float(
            "CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", 5.0
        )

        return cls(
            log_level=os.getenv("CCATV_LOG_LEVEL", "INFO").upper(),
            dvbctrl_path=os.getenv("CCATV_DVBCTRL_PATH", "dvbctrl"),
            dvbstreamer_bind_address=os.getenv(
                "CCATV_DVBSTREAMER_BIND_ADDRESS",
                "127.0.0.1",
            ),
            dvbstreamer_host=_env_non_empty_str(
                "CCATV_DVBSTREAMER_HOST",
                runtime_config.dvbstreamer_host,
            ),
            dvbstreamer_output_mrl=os.getenv("CCATV_DVBSTREAMER_OUTPUT_MRL", "null://"),
            dvbstreamer_path=os.getenv("CCATV_DVBSTREAMER_PATH", "dvbstreamer"),
            dvbstreamer_stop_timeout_seconds=stop_timeout_seconds,
            dvb_adapter_count=_env_positive_int(
                "CCATV_DVB_ADAPTER_COUNT",
                runtime_config.dvb_adapter_count,
            ),
            dvb_adapter_index=_env_non_negative_int("CCATV_DVB_ADAPTER_INDEX", 0),
            dvbctrl_timeout_seconds=timeout_seconds,
            database_path=os.getenv(
                "CCATV_DATABASE_PATH",
                str(Path(user_data_dir("ccatv", appauthor=False)) / "ccatv.sqlite3"),
            ),
        )
