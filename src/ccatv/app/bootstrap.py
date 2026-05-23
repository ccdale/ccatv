from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ccatv.logging_config import configure_logging
from ccatv.settings import AppSettings
from ccatv.storage import PersistenceStore, initialize_database
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient
from ccatv.tvrecorder.manager import DvbStreamerConfig, DvbStreamerManager
from ccatv.tvrecorder.orchestrator import (
    DvbCtrlCaptureController,
    PeriodicCheckPolicy,
    RecorderOrchestrator,
)
from ccatv.tvrecorder.postprocess import NoOpPostProcessingRunner
from ccatv.tvrecorder.preflight import WritePreflightChecker
from ccatv.tvrecorder.service import (
    RecordingHealthCheckPolicy,
    RecordingPaddingPolicy,
    TvRecorderService,
)


@dataclass(frozen=True, slots=True)
class AppContext:
    """Bootstrapped runtime context shared by top-level app components."""

    settings: AppSettings
    logger: logging.Logger
    dvbctrl: DvbCtrlClient
    dvbstreamer: DvbStreamerManager
    write_preflight: WritePreflightChecker
    persistence: PersistenceStore
    tvrecorder: TvRecorderService
    recorder_orchestrator: RecorderOrchestrator


def close_app_context(context: AppContext) -> None:
    """Best-effort shutdown for bootstrap resources."""
    logger = context.logger

    try:
        context.dvbstreamer.stop(force_kill=True)
    except Exception:
        logger.warning("failed to stop dvbstreamer during shutdown", exc_info=True)

    try:
        context.persistence.connection.close()
    except Exception:
        logger.warning("failed to close persistence connection", exc_info=True)


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
    persistence = PersistenceStore(
        connection=initialize_database(Path(settings.database_path))
    )
    tvrecorder = TvRecorderService(
        dvbctrl,
        persistence=persistence,
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=settings.recording_early_growth_checks,
            early_growth_interval_seconds=settings.recording_early_growth_interval_seconds,
            final_stability_checks=settings.recording_final_stability_checks,
            final_stability_interval_seconds=settings.recording_final_stability_interval_seconds,
            growth_min_bytes=settings.recording_growth_min_bytes,
            periodic_growth_checks=settings.recording_periodic_growth_checks,
            periodic_growth_interval_seconds=settings.recording_periodic_growth_interval_seconds,
        ),
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=settings.recording_post_finish_seconds,
            pre_start_seconds=settings.recording_pre_start_seconds,
        ),
        post_processor=NoOpPostProcessingRunner(),
    )
    recorder_orchestrator = RecorderOrchestrator(
        service=tvrecorder,
        persistence=persistence,
        capture_controller=DvbCtrlCaptureController(service=tvrecorder),
        periodic_policy=PeriodicCheckPolicy(
            growth_min_bytes=settings.recording_growth_min_bytes,
            interval_seconds=settings.recording_periodic_growth_interval_seconds,
        ),
    )
    return AppContext(
        settings=settings,
        logger=logger,
        dvbctrl=dvbctrl,
        dvbstreamer=dvbstreamer,
        write_preflight=write_preflight,
        persistence=persistence,
        tvrecorder=tvrecorder,
        recorder_orchestrator=recorder_orchestrator,
    )
