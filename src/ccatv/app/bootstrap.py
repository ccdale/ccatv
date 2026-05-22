from __future__ import annotations

import logging
from dataclasses import dataclass

from ccatv.logging_config import configure_logging
from ccatv.settings import AppSettings
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient
from ccatv.tvrecorder.manager import DvbStreamerConfig, DvbStreamerManager
from ccatv.tvrecorder.preflight import WritePreflightChecker


@dataclass(frozen=True, slots=True)
class AppContext:
    """Bootstrapped runtime context shared by top-level app components."""

    settings: AppSettings
    logger: logging.Logger
    dvbctrl: DvbCtrlClient
    dvbstreamer: DvbStreamerManager
    write_preflight: WritePreflightChecker


def bootstrap_app() -> AppContext:
    """Create settings, logging, and key adapter clients for startup."""
    settings = AppSettings.from_env()
    configure_logging(settings.log_level)
    logger = logging.getLogger("ccatv")
    dvbctrl = DvbCtrlClient(
        executable_path=settings.dvbctrl_path,
        host=settings.dvbstreamer_host,
        adapter_index=settings.dvb_adapter_index,
        timeout_seconds=settings.dvbctrl_timeout_seconds,
    )
    dvbstreamer = DvbStreamerManager(
        config=DvbStreamerConfig(
            adapter_index=settings.dvb_adapter_index,
            bind_address=settings.dvbstreamer_bind_address,
            executable_path=settings.dvbstreamer_path,
            output_mrl=settings.dvbstreamer_output_mrl,
        ),
        stop_timeout_seconds=settings.dvbstreamer_stop_timeout_seconds,
    )
    write_preflight = WritePreflightChecker(
        host=settings.dvbstreamer_host,
        adapter_count=settings.dvb_adapter_count,
        preferred_adapter_index=settings.dvb_adapter_index,
        executable_path=settings.dvbctrl_path,
        timeout_seconds=settings.dvbctrl_timeout_seconds,
        transient_retry_count=dvbctrl.transient_retry_count,
        transient_retry_delay_seconds=dvbctrl.transient_retry_delay_seconds,
    )
    return AppContext(
        settings=settings,
        logger=logger,
        dvbctrl=dvbctrl,
        dvbstreamer=dvbstreamer,
        write_preflight=write_preflight,
    )
