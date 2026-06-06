from __future__ import annotations

import logging
import re
import shlex
import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ccatv.storage import PersistenceStore, RecordingStateRecord, SchedulerJobRecord
from ccatv.tvrecorder.service import TvRecorderService


@dataclass(frozen=True, slots=True)
class PeriodicCheckPolicy:
    growth_min_bytes: int = 1
    interval_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    job_id: int
    scheduler_state: str
    recording_id: int | None
    recording_state: str | None
    error: str | None = None


class CaptureController(Protocol):
    def start_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None: ...

    def stop_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class NoOpCaptureController:
    def start_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None:
        del channel_name, output_path

    def stop_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None:
        del channel_name, output_path


@dataclass(frozen=True, slots=True)
class DvbCtrlCaptureController:
    service: TvRecorderService

    def start_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None:
        resolved = self.service.resolve_service_name(channel_name)
        self.service.select_service(resolved)
        output_mrl = f"file://{output_path}"
        self.service.run_raw(f"setmrl {shlex.quote(output_mrl)}")

    def stop_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None:
        del channel_name, output_path
        self.service.run_raw("setmrl null://")


@dataclass(frozen=True, slots=True)
class ServiceFilterCaptureController:
    service: TvRecorderService
    avs_only_status: str = "on"

    def start_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None:
        resolved_service_name = self.service.resolve_service_name(channel_name)
        filter_name = _build_service_filter_name(
            channel_name=channel_name,
            output_path=output_path,
        )

        self.service.add_service_filter(filter_name)
        self.service.set_service_filter_service(filter_name, resolved_service_name)
        self.service.set_service_filter_avs_only(
            filter_name,
            self.avs_only_status,
        )
        output_mrl = f"file://{output_path}"
        self.service.set_service_filter_output(filter_name, output_mrl)

    def stop_capture(
        self,
        *,
        channel_name: str,
        output_path: str,
    ) -> None:
        filter_name = _build_service_filter_name(
            channel_name=channel_name,
            output_path=output_path,
        )

        set_output_error: Exception | None = None
        remove_error: Exception | None = None
        try:
            self.service.set_service_filter_output(filter_name, "null://")
        except Exception as exc:
            set_output_error = exc

        try:
            self.service.remove_service_filter(filter_name)
        except Exception as exc:
            if not _is_missing_service_filter_error(exc):
                remove_error = exc

        if set_output_error is not None:
            if remove_error is not None:
                raise RuntimeError(
                    "failed to stop service filter capture: "
                    f"set output failed: {set_output_error}; "
                    f"remove failed: {remove_error}"
                ) from set_output_error
            raise RuntimeError(
                f"failed to stop service filter capture: set output failed: {set_output_error}"
            ) from set_output_error

        if remove_error is not None:
            raise RuntimeError(
                f"failed to stop service filter capture: remove failed: {remove_error}"
            ) from remove_error


@dataclass(slots=True)
class SchedulerWorker:
    orchestrator: RecorderOrchestrator
    output_path_builder: Callable[[SchedulerJobRecord], str]
    max_jobs_per_cycle: int | None = None
    poll_interval_seconds: float = 5.0
    sleep_fn: Callable[[float], None] = time.sleep

    def run_cycle(self, *, now_utc: str | None = None) -> list[OrchestratorResult]:
        return self.orchestrator.run_due_jobs(
            output_path_builder=self.output_path_builder,
            now_utc=now_utc,
            max_jobs=self.max_jobs_per_cycle,
        )

    def run_forever(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        while True:
            self.run_cycle()
            self.sleep_fn(self.poll_interval_seconds)


def build_recording_output_path(
    *,
    directory: str,
    job: SchedulerJobRecord,
    extension: str = ".ts",
) -> str:
    safe_channel = _sanitize_channel_name(job.channel_name)
    utc_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = extension if extension.startswith(".") else f".{extension}"
    return f"{directory.rstrip('/')}/{safe_channel}-{job.id}-{utc_stamp}{suffix}"


@dataclass(slots=True)
class RecorderOrchestrator:
    service: TvRecorderService
    persistence: PersistenceStore
    capture_controller: CaptureController = NoOpCaptureController()
    periodic_policy: PeriodicCheckPolicy = PeriodicCheckPolicy()
    now_fn: Callable[[], float] = time.time
    sleep_fn: Callable[[float], None] = time.sleep
    should_stop: Callable[[], bool] = lambda: False
    logger: logging.Logger = logging.getLogger("ccatv")
    thread_factory: Callable[..., Any] = threading.Thread
    _active_jobs: dict[int, tuple[Any, list[OrchestratorResult]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _results_lock: Any = field(default_factory=threading.Lock, init=False, repr=False)
    adapter_pool: Any = None  # AdapterPool | None; injected after construction

    def list_due_scheduler_jobs(
        self,
        *,
        now_utc: str | None = None,
    ) -> list[SchedulerJobRecord]:
        now_timestamp = (
            _parse_utc_iso(now_utc) if now_utc is not None else self.now_fn()
        )
        due_jobs = [
            job
            for job in self.persistence.list_scheduler_jobs()
            if job.state == "scheduled"
            and _parse_utc_iso(job.start_at_utc) <= now_timestamp
        ]
        due_jobs.sort(key=lambda job: (_parse_utc_iso(job.start_at_utc), job.id))
        return due_jobs

    def run_due_jobs(
        self,
        *,
        output_path_builder: Callable[[SchedulerJobRecord], str],
        now_utc: str | None = None,
        max_jobs: int | None = None,
    ) -> list[OrchestratorResult]:
        # Collect results from recording threads that completed since the last cycle
        completed_results = self._collect_completed_threads()

        # Find new due jobs, skipping any already running on a recording thread
        due_jobs = self.list_due_scheduler_jobs(now_utc=now_utc)
        with self._results_lock:
            active_job_ids = set(self._active_jobs.keys())
        due_jobs = [j for j in due_jobs if j.id not in active_job_ids]

        total_due_jobs = len(due_jobs)
        if total_due_jobs > 0:
            deferred_jobs = 0
            if max_jobs is not None:
                deferred_jobs = max(0, total_due_jobs - max_jobs)
                due_jobs = due_jobs[:max_jobs]

            executing_ids = [job.id for job in due_jobs]
            self.logger.info(
                "scheduler cycle: due_jobs=%s executing=%s deferred=%s max_jobs_per_cycle=%s job_ids=%s",
                total_due_jobs,
                len(due_jobs),
                deferred_jobs,
                max_jobs,
                executing_ids,
            )

            for job in due_jobs:
                output_path = output_path_builder(job)
                result_holder: list[OrchestratorResult] = []
                thread = self.thread_factory(
                    target=self._run_job_in_thread,
                    args=(job.id, output_path, result_holder),
                    daemon=True,
                    name=f"ccatv-recording-{job.id}",
                )
                with self._results_lock:
                    self._active_jobs[job.id] = (thread, result_holder)
                thread.start()

        # Also collect threads that finished synchronously (e.g. test runners)
        just_finished = self._collect_completed_threads()
        return completed_results + just_finished

    def _collect_completed_threads(self) -> list[OrchestratorResult]:
        """Pop and return results from recording threads that have finished."""
        collected: list[OrchestratorResult] = []
        with self._results_lock:
            done_ids = [
                jid for jid, (t, _) in self._active_jobs.items() if not t.is_alive()
            ]
            for job_id in done_ids:
                _, result_holder = self._active_jobs.pop(job_id)
                collected.extend(result_holder)
        return collected

    def _run_job_in_thread(
        self,
        job_id: int,
        output_path: str,
        result_holder: list[OrchestratorResult],
    ) -> None:
        """Run a scheduler job and store the result for collection on the next cycle."""
        pool = self.adapter_pool
        slot = None
        if pool is not None:
            slot = pool.acquire()
            if slot is None:
                self.logger.warning(
                    "no free adapter slot: failing job_id=%s (pool_capacity=%s in_use=%s available=%s)",
                    job_id,
                    getattr(pool, "capacity", "?"),
                    getattr(pool, "in_use_count", "?"),
                    getattr(pool, "available_count", "?"),
                )
                # No free slot means we fail this job immediately by design.
                try:
                    self.service.mark_scheduler_job_failed(job_id)
                except Exception:
                    pass
                result_holder.append(
                    OrchestratorResult(
                        job_id=job_id,
                        scheduler_state="failed",
                        recording_id=None,
                        recording_state=None,
                        error="no adapter slot available",
                    )
                )
                return
            # Ensure the slot's dvbstreamer is running before we try to record
            slot_state = getattr(slot.dvbstreamer.status(), "state", None)
            from ccatv.tvrecorder.manager import DvbStreamerState
            if slot_state != DvbStreamerState.RUNNING:
                try:
                    slot.dvbstreamer.start()
                    self.logger.info(
                        "adapter %s dvbstreamer started for job_id=%s",
                        slot.adapter_index,
                        job_id,
                    )
                except Exception as exc:
                    self.logger.error(
                        "adapter %s dvbstreamer failed to start for job_id=%s: %s",
                        slot.adapter_index,
                        job_id,
                        exc,
                    )
                    pool.release(slot)
                    result_holder.append(
                        OrchestratorResult(
                            job_id=job_id,
                            scheduler_state="failed",
                            recording_id=None,
                            recording_state=None,
                            error=f"adapter {slot.adapter_index} dvbstreamer failed to start: {exc}",
                        )
                    )
                    return

        capture_override = slot.capture_controller if slot is not None else None
        try:
            result = self.run_job(
                job_id=job_id,
                output_path=output_path,
                capture_controller=capture_override,
            )
        finally:
            if slot is not None and pool is not None:
                pool.release(slot)
        result_holder.append(result)

    def join_running_jobs(self) -> None:
        """Block until all active recording threads have completed. Called on shutdown."""
        with self._results_lock:
            snapshot = list(self._active_jobs.items())
        for job_id, (thread, _) in snapshot:
            self.logger.info(
                "shutdown: waiting for active recording to finish: job_id=%s", job_id
            )
            thread.join()

    def run_job(
        self,
        *,
        job_id: int,
        output_path: str,
        capture_controller: Any = None,
    ) -> OrchestratorResult:
        effective_capture = (
            capture_controller if capture_controller is not None else self.capture_controller
        )
        job = self.persistence.get_scheduler_job(job_id, required=True)
        now_timestamp = self.now_fn()
        if job.state != "scheduled":
            raise ValueError(
                f"scheduler job {job.id} is not runnable in state '{job.state}'"
            )
        if _parse_utc_iso(job.start_at_utc) > now_timestamp:
            raise ValueError(f"scheduler job {job.id} is not due yet")

        self.service.mark_scheduler_job_running(job.id)
        recording: RecordingStateRecord | None = None
        capture_started = False
        cleanup_stop_error: str | None = None
        effective_duration_seconds = _effective_recording_duration_seconds(
            job=job,
            started_at_timestamp=now_timestamp,
        )

        if effective_duration_seconds <= 0:
            self.logger.warning(
                "skipping job: programme window expired before recording could start: "
                "job_id=%s channel=%s program=%s program_stop_at_utc=%s",
                job.id,
                job.channel_name,
                job.program_title,
                job.program_stop_at_utc,
            )
            try:
                scheduler = self.service.mark_scheduler_job_failed(job.id)
            except Exception:
                scheduler = None
            return OrchestratorResult(
                job_id=job.id,
                scheduler_state="failed" if scheduler is None else scheduler.state,
                recording_id=None,
                recording_state=None,
                error="programme window expired before recording could start",
            )

        try:
            recording_started_at = self.now_fn()
            recording = self.service.begin_recording(
                channel_name=job.channel_name,
                output_path=output_path,
                started_at_utc=_format_utc_iso(recording_started_at),
                program_title=job.program_title,
                program_description=job.program_description,
                program_start_at_utc=job.program_start_at_utc,
                program_stop_at_utc=job.program_stop_at_utc,
            )
            self.logger.info(
                "recording started: job_id=%s channel=%s program=%s duration=%s seconds output=%s",
                job.id,
                job.channel_name,
                job.program_title,
                effective_duration_seconds,
                output_path,
            )
            effective_capture.start_capture(
                channel_name=job.channel_name,
                output_path=output_path,
            )
            capture_started = True

            early = self.service.verify_recording_output_growth_early(recording.id)
            if early.state == "failed":
                raise RuntimeError("early growth check failed")

            self.logger.debug(
                "recording growth checks started: job_id=%s recording_id=%s",
                job.id,
                recording.id,
            )
            self._run_periodic_growth_checks(
                recording_id=recording.id,
                recording_duration_seconds=effective_duration_seconds,
            )
            self.logger.debug(
                "recording growth checks completed: job_id=%s recording_id=%s",
                job.id,
                recording.id,
            )

            capture_started = False
            try:
                effective_capture.stop_capture(
                    channel_name=job.channel_name,
                    output_path=output_path,
                )
            except Exception as exc:
                raise RuntimeError(f"failed stopping capture: {exc}") from exc

            self.service.mark_recording_capture_completed(
                recording.id,
                ended_at_utc=_format_utc_iso(self.now_fn()),
            )
            self.logger.debug(
                "recording capture completed: job_id=%s recording_id=%s",
                job.id,
                recording.id,
            )
            stable = self.service.verify_recording_output_stable_after_stop_default(
                recording.id
            )
            if stable.state == "failed":
                raise RuntimeError("final stability check failed")

            final = self.service.run_recording_post_processing(recording.id)
            if final.state != "ready":
                raise RuntimeError("post-processing did not produce ready state")

            scheduler = self.service.mark_scheduler_job_completed(job.id)
            self.logger.info(
                "recording completed successfully: job_id=%s channel=%s program=%s recording_id=%s",
                job.id,
                job.channel_name,
                job.program_title,
                final.id,
            )
            return OrchestratorResult(
                job_id=job.id,
                scheduler_state=scheduler.state,
                recording_id=final.id,
                recording_state=final.state,
            )
        except Exception as exc:
            if capture_started:
                try:
                    effective_capture.stop_capture(
                        channel_name=job.channel_name,
                        output_path=output_path,
                    )
                except Exception as cleanup_exc:
                    cleanup_stop_error = _append_error(
                        cleanup_stop_error,
                        "cleanup stop_capture failed",
                        str(cleanup_exc),
                    )

            if recording is not None:
                current = self.persistence.get_recording(recording.id, required=False)
                if current is None:
                    recording_state = None
                    recording_id = recording.id
                else:
                    if current.state not in {"failed", "ready"}:
                        try:
                            current = self.service.mark_recording_failed(recording.id)
                        except Exception as state_exc:
                            cleanup_stop_error = _append_error(
                                cleanup_stop_error,
                                "failed to mark recording failed",
                                str(state_exc),
                            )
                    recording_state = current.state
                    recording_id = current.id
            else:
                recording_state = None
                recording_id = None

            scheduler_state = "failed"
            try:
                scheduler = self.service.mark_scheduler_job_failed(job.id)
                scheduler_state = scheduler.state
            except Exception as state_exc:
                cleanup_stop_error = _append_error(
                    cleanup_stop_error,
                    "failed to mark scheduler job failed",
                    str(state_exc),
                )

            error_message = str(exc)
            if cleanup_stop_error:
                error_message = f"{error_message}; {cleanup_stop_error}"
            return OrchestratorResult(
                job_id=job.id,
                scheduler_state=scheduler_state,
                recording_id=recording_id,
                recording_state=recording_state,
                error=error_message,
            )

    def _run_periodic_growth_checks(
        self,
        *,
        recording_id: int,
        recording_duration_seconds: int,
    ) -> None:
        if self.periodic_policy.interval_seconds <= 0:
            raise ValueError("periodic interval_seconds must be > 0")
        if self.periodic_policy.growth_min_bytes < 1:
            raise ValueError("periodic growth_min_bytes must be at least 1")
        if recording_duration_seconds <= 0:
            return

        remaining_seconds = float(recording_duration_seconds)
        check_count = 0
        while remaining_seconds > 0:
            # Exit early if shutdown is requested
            if self.should_stop():
                self.logger.info(
                    "recording interrupted by shutdown: recording_id=%s remaining_seconds=%.1f checks_completed=%d",
                    recording_id,
                    remaining_seconds,
                    check_count,
                )
                break

            sleep_seconds = min(
                self.periodic_policy.interval_seconds,
                remaining_seconds,
            )
            remaining_seconds -= sleep_seconds
            check_count += 1
            result = self.service.verify_recording_output_growth(
                recording_id,
                checks=1,
                interval_seconds=sleep_seconds,
                min_growth_bytes=self.periodic_policy.growth_min_bytes,
            )
            if result.state == "failed":
                raise RuntimeError("periodic growth check failed")
            
            # Log progress for longer recordings (every 5 checks or at key intervals)
            if check_count % 5 == 0 or remaining_seconds <= 30:
                self.logger.debug(
                    "recording in progress: recording_id=%s remaining_seconds=%.1f checks_completed=%d",
                    recording_id,
                    remaining_seconds,
                    check_count,
                )


def _append_error(existing: str | None, label: str, detail: str) -> str:
    message = f"{label}: {detail}"
    if existing:
        return f"{existing}; {message}"
    return message


def _build_service_filter_name(*, channel_name: str, output_path: str) -> str:
    channel_token = _sanitize_channel_name(channel_name)
    suffix = hashlib.sha1(output_path.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{channel_token}-{suffix}"


def _is_missing_service_filter_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = (
        "no such service filter",
        "service filter not found",
        "unknown service filter",
        "does not exist",
    )
    return any(marker in message for marker in markers)


_MIN_RECORDING_SECONDS = 30


def _effective_recording_duration_seconds(
    *,
    job: SchedulerJobRecord,
    started_at_timestamp: float,
) -> int:
    if job.program_stop_at_utc is None:
        return job.duration_seconds

    stop_timestamp = _parse_utc_iso(job.program_stop_at_utc)
    remaining_seconds = int(max(0.0, stop_timestamp - started_at_timestamp))
    effective = min(job.duration_seconds, remaining_seconds)
    if effective < _MIN_RECORDING_SECONDS:
        return 0
    return effective


def _parse_utc_iso(value: str) -> float:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _format_utc_iso(timestamp_seconds: float) -> str:
    return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _sanitize_channel_name(value: str) -> str:
    collapsed = re.sub(r"\s+", "-", value.strip().lower())
    cleaned = re.sub(r"[^a-z0-9._-]", "", collapsed)
    return cleaned or "channel"
