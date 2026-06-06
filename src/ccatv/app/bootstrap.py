from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from ccatv.logging_config import configure_logging
from ccatv.settings import AppSettings
from ccatv.storage import PersistenceStore, initialize_database
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient
from ccatv.tvrecorder.manager import DvbStreamerConfig, DvbStreamerManager
from ccatv.tvrecorder.orchestrator import (
    PeriodicCheckPolicy,
    RecorderOrchestrator,
    ServiceFilterCaptureController,
)
from ccatv.tvrecorder.postprocess import NfoSidecarPostProcessingRunner
from ccatv.tvrecorder.preflight import WritePreflightChecker
from ccatv.tvrecorder.service import (
    RecordingHealthCheckPolicy,
    RecordingPaddingPolicy,
    TvRecorderService,
)


@dataclass(slots=True)
class AdapterSlot:
    """One dvb adapter slot with its own dvbstreamer process and capture controller."""

    adapter_index: int
    dvbstreamer: DvbStreamerManager
    capture_controller: ServiceFilterCaptureController


@dataclass(slots=True)
class AdapterPool:
    """Thread-safe pool of dvb adapter slots for concurrent recordings."""

    slots: list[AdapterSlot]
    _available: Any = field(default_factory=list, init=False, repr=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._available = list(self.slots)

    @property
    def capacity(self) -> int:
        return len(self.slots)

    @property
    def available_count(self) -> int:
        with self._lock:
            return len(self._available)

    @property
    def in_use_count(self) -> int:
        return self.capacity - self.available_count

    def acquire(self) -> AdapterSlot | None:
        """Return an idle slot, or None if all slots are in use."""
        with self._lock:
            if self._available:
                return self._available.pop(0)
            return None

    def release(self, slot: AdapterSlot) -> None:
        """Return a slot to the pool after a recording finishes."""
        with self._lock:
            self._available.append(slot)

    def idle_slots_snapshot(self) -> tuple[AdapterSlot, ...]:
        """Return a point-in-time snapshot of idle adapter slots."""
        with self._lock:
            return tuple(self._available)

    def disable_idle_slot(self, adapter_index: int) -> AdapterSlot | None:
        """Remove an idle slot from scheduling and return it for external cleanup.

        Returns None when the slot is unknown or currently in use.
        """
        with self._lock:
            slot = next(
                (candidate for candidate in self.slots if candidate.adapter_index == adapter_index),
                None,
            )
            if slot is None:
                return None
            if slot not in self._available:
                return None
            self._available.remove(slot)
            self.slots.remove(slot)
            return slot

    def stop_all(self, *, force_kill: bool = True) -> None:
        """Gracefully stop all dvbstreamer processes in the pool."""
        for slot in self.slots:
            try:
                slot.dvbstreamer.stop(force_kill=force_kill)
            except Exception:
                pass


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
    adapter_pool: AdapterPool
    worker_cycle_lock: object = field(default_factory=Lock)


def close_app_context(context: AppContext) -> None:
    """Best-effort shutdown for bootstrap resources."""
    logger = context.logger

    try:
        context.adapter_pool.stop_all(force_kill=True)
    except Exception:
        logger.warning("failed to stop adapter pool during shutdown", exc_info=True)

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
            debug_output=getattr(settings, "dvbstreamer_debug_output", False),
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
        post_processor=NfoSidecarPostProcessingRunner(
            run_comskip=True,
            comskip_command=(
                "/usr/bin/comskip",
                f"--ini={Path.home()}/.config/comskip/comskip.ini",
            ),
        ),
    )
    recorder_orchestrator = RecorderOrchestrator(
        service=tvrecorder,
        persistence=persistence,
        capture_controller=ServiceFilterCaptureController(service=tvrecorder),
        logger=logger,
        periodic_policy=PeriodicCheckPolicy(
            growth_min_bytes=settings.recording_growth_min_bytes,
            interval_seconds=settings.recording_periodic_growth_interval_seconds,
        ),
    )

    # Build one adapter slot per configured DVB adapter.
    # Adapter 0 reuses the already-started tvrecorder/dvbctrl for zero cost.
    # Adapters 1..N get their own DvbStreamerManager and TvRecorderService.
    health_policy = RecordingHealthCheckPolicy(
        early_growth_checks=settings.recording_early_growth_checks,
        early_growth_interval_seconds=settings.recording_early_growth_interval_seconds,
        final_stability_checks=settings.recording_final_stability_checks,
        final_stability_interval_seconds=settings.recording_final_stability_interval_seconds,
        growth_min_bytes=settings.recording_growth_min_bytes,
        periodic_growth_checks=settings.recording_periodic_growth_checks,
        periodic_growth_interval_seconds=settings.recording_periodic_growth_interval_seconds,
    )
    padding_policy = RecordingPaddingPolicy(
        post_finish_seconds=settings.recording_post_finish_seconds,
        pre_start_seconds=settings.recording_pre_start_seconds,
    )
    adapter_slots: list[AdapterSlot] = []
    for adapter_idx in range(settings.dvb_adapter_count):
        if adapter_idx == settings.dvb_adapter_index:
            slot_ctrl = ServiceFilterCaptureController(service=tvrecorder)
            slot_mgr = dvbstreamer
        else:
            slot_dvbctrl = DvbCtrlClient(
                executable_path=settings.dvbctrl_path,
                host=settings.dvbstreamer_host,
                adapter_index=adapter_idx,
                timeout_seconds=settings.dvbctrl_timeout_seconds,
            )
            slot_mgr = DvbStreamerManager(
                config=DvbStreamerConfig(
                    adapter_index=adapter_idx,
                    bind_address=settings.dvbstreamer_bind_address,
                    debug_output=getattr(settings, "dvbstreamer_debug_output", False),
                    executable_path=settings.dvbstreamer_path,
                    output_mrl=settings.dvbstreamer_output_mrl,
                ),
                stop_timeout_seconds=settings.dvbstreamer_stop_timeout_seconds,
            )
            slot_svc = TvRecorderService(
                slot_dvbctrl,
                persistence=persistence,
                health_policy=health_policy,
                padding_policy=padding_policy,
            )
            slot_ctrl = ServiceFilterCaptureController(service=slot_svc)
        adapter_slots.append(
            AdapterSlot(
                adapter_index=adapter_idx,
                dvbstreamer=slot_mgr,
                capture_controller=slot_ctrl,
            )
        )
    adapter_pool = AdapterPool(slots=adapter_slots)
    recorder_orchestrator.adapter_pool = adapter_pool

    return AppContext(
        settings=settings,
        logger=logger,
        dvbctrl=dvbctrl,
        dvbstreamer=dvbstreamer,
        write_preflight=write_preflight,
        persistence=persistence,
        tvrecorder=tvrecorder,
        recorder_orchestrator=recorder_orchestrator,
        adapter_pool=adapter_pool,
    )
