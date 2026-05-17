from __future__ import annotations

import logging
from dataclasses import dataclass

from ccatv.logging_config import configure_logging
from ccatv.settings import AppSettings
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient


@dataclass(frozen=True, slots=True)
class AppContext:
    """Bootstrapped runtime context shared by top-level app components."""

    settings: AppSettings
    logger: logging.Logger
    dvbctrl: DvbCtrlClient


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
    return AppContext(settings=settings, logger=logger, dvbctrl=dvbctrl)
