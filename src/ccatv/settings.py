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


def _env_non_negative_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0:
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
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
    dvbstreamer_manage_process: bool = True
    dvbstreamer_debug_output: bool = False
    dvbstreamer_stop_timeout_seconds: float = 5.0
    ota_epg_channel_name: str = "BBC TWO HD"
    dvb_adapter_count: int = 1
    dvb_adapter_index: int = 0
    dvbctrl_timeout_seconds: float = 10.0
    recording_pre_start_seconds: int = 120
    recording_post_finish_seconds: int = 900
    recording_early_growth_checks: int = 3
    recording_early_growth_interval_seconds: float = 2.0
    recording_periodic_growth_checks: int = 1
    recording_periodic_growth_interval_seconds: float = 30.0
    recording_growth_min_bytes: int = 1
    recording_final_stability_checks: int = 2
    recording_final_stability_interval_seconds: float = 2.0
    comskip_ini_path: str = "/home/chris/.config/comskip/comskip.ini"
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
            dvbstreamer_manage_process=_env_bool(
                "CCATV_DVBSTREAMER_MANAGE_PROCESS",
                True,
            ),
            dvbstreamer_debug_output=_env_bool(
                "CCATV_DVBSTREAMER_DEBUG_OUTPUT",
                False,
            ),
            dvbstreamer_stop_timeout_seconds=stop_timeout_seconds,
            ota_epg_channel_name=_env_non_empty_str(
                "CCATV_OTA_EPG_CHANNEL_NAME",
                runtime_config.ota_epg_channel_name,
            ),
            dvb_adapter_count=_env_positive_int(
                "CCATV_DVB_ADAPTER_COUNT",
                runtime_config.dvb_adapter_count,
            ),
            dvb_adapter_index=_env_non_negative_int("CCATV_DVB_ADAPTER_INDEX", 0),
            dvbctrl_timeout_seconds=timeout_seconds,
            recording_pre_start_seconds=_env_non_negative_int(
                "CCATV_RECORDING_PRE_START_SECONDS",
                120,
            ),
            recording_post_finish_seconds=_env_non_negative_int(
                "CCATV_RECORDING_POST_FINISH_SECONDS",
                900,
            ),
            recording_early_growth_checks=_env_positive_int(
                "CCATV_RECORDING_EARLY_GROWTH_CHECKS",
                3,
            ),
            recording_early_growth_interval_seconds=_env_non_negative_float(
                "CCATV_RECORDING_EARLY_GROWTH_INTERVAL_SECONDS",
                2.0,
            ),
            recording_periodic_growth_checks=_env_positive_int(
                "CCATV_RECORDING_PERIODIC_GROWTH_CHECKS",
                1,
            ),
            recording_periodic_growth_interval_seconds=_env_non_negative_float(
                "CCATV_RECORDING_PERIODIC_GROWTH_INTERVAL_SECONDS",
                30.0,
            ),
            recording_growth_min_bytes=_env_positive_int(
                "CCATV_RECORDING_GROWTH_MIN_BYTES",
                1,
            ),
            recording_final_stability_checks=_env_positive_int(
                "CCATV_RECORDING_FINAL_STABILITY_CHECKS",
                2,
            ),
            recording_final_stability_interval_seconds=_env_non_negative_float(
                "CCATV_RECORDING_FINAL_STABILITY_INTERVAL_SECONDS",
                2.0,
            ),
            comskip_ini_path=_env_non_empty_str(
                "CCATV_COMSKIP_INI_PATH",
                "/home/chris/.config/comskip/comskip.ini",
            ),
            database_path=os.getenv(
                "CCATV_DATABASE_PATH",
                str(Path(user_data_dir("ccatv", appauthor=False)) / "ccatv.sqlite3"),
            ),
        )
