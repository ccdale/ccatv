from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

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
        self.service.select_service(channel_name)
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
        due_jobs = self.list_due_scheduler_jobs(now_utc=now_utc)
        if max_jobs is not None:
            due_jobs = due_jobs[:max_jobs]

        return [
            self.run_job(job_id=job.id, output_path=output_path_builder(job))
            for job in due_jobs
        ]

    def run_job(self, *, job_id: int, output_path: str) -> OrchestratorResult:
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

        try:
            recording_started_at = self.now_fn()
            recording = self.service.begin_recording(
                channel_name=job.channel_name,
                output_path=output_path,
                started_at_utc=_format_utc_iso(recording_started_at),
            )
            self.capture_controller.start_capture(
                channel_name=job.channel_name,
                output_path=output_path,
            )
            capture_started = True

            early = self.service.verify_recording_output_growth_early(recording.id)
            if early.state == "failed":
                raise RuntimeError("early growth check failed")

            self._run_periodic_growth_checks(
                recording_id=recording.id,
                recording_duration_seconds=job.duration_seconds,
            )

            try:
                self.capture_controller.stop_capture(
                    channel_name=job.channel_name,
                    output_path=output_path,
                )
            except Exception as exc:
                raise RuntimeError(f"failed stopping capture: {exc}") from exc
            capture_started = False

            self.service.mark_recording_capture_completed(
                recording.id,
                ended_at_utc=_format_utc_iso(self.now_fn()),
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
            return OrchestratorResult(
                job_id=job.id,
                scheduler_state=scheduler.state,
                recording_id=final.id,
                recording_state=final.state,
            )
        except Exception as exc:
            if capture_started:
                try:
                    self.capture_controller.stop_capture(
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
        while remaining_seconds > 0:
            sleep_seconds = min(
                self.periodic_policy.interval_seconds,
                remaining_seconds,
            )
            self.sleep_fn(sleep_seconds)
            remaining_seconds -= sleep_seconds
            result = self.service.verify_recording_output_growth(
                recording_id,
                checks=1,
                interval_seconds=0,
                min_growth_bytes=self.periodic_policy.growth_min_bytes,
            )
            if result.state == "failed":
                raise RuntimeError("periodic growth check failed")


def _append_error(existing: str | None, label: str, detail: str) -> str:
    message = f"{label}: {detail}"
    if existing:
        return f"{existing}; {message}"
    return message


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
