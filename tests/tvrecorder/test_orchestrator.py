from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ccatv.storage import PersistenceStore, initialize_database
from ccatv.tvrecorder.orchestrator import (
    PeriodicCheckPolicy,
    RecorderOrchestrator,
)
from ccatv.tvrecorder.service import (
    RecordingHealthCheckPolicy,
    RecordingPaddingPolicy,
    TvRecorderService,
)


@dataclass(slots=True)
class StubDvbCtrlClient:
    def run_command(self, command: str):
        raise AssertionError(f"unexpected dvbctrl command: {command}")


@dataclass(slots=True)
class StubCaptureController:
    fail_start: bool = False
    fail_stop: bool = False
    start_calls: list[tuple[str, str]] = field(default_factory=list)
    stop_calls: list[tuple[str, str]] = field(default_factory=list)

    def start_capture(self, *, channel_name: str, output_path: str) -> None:
        self.start_calls.append((channel_name, output_path))
        if self.fail_start:
            raise RuntimeError("start failed")

    def stop_capture(self, *, channel_name: str, output_path: str) -> None:
        self.stop_calls.append((channel_name, output_path))
        if self.fail_stop:
            raise RuntimeError("stop failed")


@dataclass(slots=True)
class FakeClock:
    now_seconds: float

    def now(self) -> float:
        return self.now_seconds

    def sleep(self, seconds: float) -> None:
        self.now_seconds += seconds


def _iso_at(timestamp_seconds: float) -> str:
    return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_orchestrator_run_job_success_path(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    sizes = iter([100, 120, 120, 150, 150, 150])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: next(sizes, 150),
        sleep_fn=lambda _seconds: None,
    )
    capture = StubCaptureController()
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        capture_controller=capture,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=10,
        )

        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/bbc2.ts")
        scheduler = persistence.get_scheduler_job(job.id, required=True)
        recording = persistence.get_recording(result.recording_id or -1, required=True)

        assert result.scheduler_state == "completed"
        assert result.recording_state == "ready"
        assert result.error is None
        assert scheduler.state == "completed"
        assert recording.state == "ready"
        assert capture.start_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
        assert capture.stop_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
    finally:
        connection.close()


def test_orchestrator_marks_scheduler_failed_on_periodic_growth_failure(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    sizes = iter([100, 120, 120, 120])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: next(sizes, 120),
        sleep_fn=lambda _seconds: None,
    )
    capture = StubCaptureController()
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        capture_controller=capture,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=10,
        )

        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/bbc2.ts")
        scheduler = persistence.get_scheduler_job(job.id, required=True)
        recording = persistence.get_recording(result.recording_id or -1, required=True)

        assert result.scheduler_state == "failed"
        assert result.recording_state == "failed"
        assert result.error is not None
        assert scheduler.state == "failed"
        assert recording.state == "failed"
        assert capture.start_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
        assert capture.stop_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
    finally:
        connection.close()


def test_orchestrator_periodic_growth_uses_elapsed_interval_window(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)

    def _file_size_reader(_path: str) -> int:
        elapsed = clock.now_seconds - 1_748_000_000.0
        if elapsed < 2:
            return 100
        if elapsed < 10:
            return 120
        return 150

    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=2,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=_file_size_reader,
        sleep_fn=clock.sleep,
    )
    capture = StubCaptureController()
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        capture_controller=capture,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        job = service.schedule_recording(
            channel_name="5 HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=10,
        )

        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/5hd.ts")
        scheduler = persistence.get_scheduler_job(job.id, required=True)
        recording = persistence.get_recording(result.recording_id or -1, required=True)

        assert result.scheduler_state == "completed"
        assert result.recording_state == "ready"
        assert result.error is None
        assert scheduler.state == "completed"
        assert recording.state == "ready"
    finally:
        connection.close()


def test_orchestrator_run_due_jobs_filters_scheduled_due_items(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    sizes = iter([100, 120, 120, 140, 140, 140])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: next(sizes, 140),
        sleep_fn=clock.sleep,
    )
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        due_job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=10,
        )
        future_job = service.schedule_recording(
            channel_name="BBC ONE HD",
            start_at_utc=_iso_at(clock.now_seconds + 120),
            duration_seconds=20,
        )
        running_job = service.schedule_recording(
            channel_name="BBC NEWS HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=10,
        )
        service.mark_scheduler_job_running(running_job.id)

        results = orchestrator.run_due_jobs(
            output_path_builder=lambda job: f"/tmp/{job.channel_name}.ts",
        )

        assert [result.job_id for result in results] == [due_job.id]
        assert results[0].scheduler_state == "completed"
        assert (
            persistence.get_scheduler_job(due_job.id, required=True).state
            == "completed"
        )
        assert (
            persistence.get_scheduler_job(future_job.id, required=True).state
            == "scheduled"
        )
        assert (
            persistence.get_scheduler_job(running_job.id, required=True).state
            == "running"
        )
    finally:
        connection.close()


def test_orchestrator_start_capture_failure_marks_job_and_recording_failed(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: 100,
        sleep_fn=lambda _seconds: None,
    )
    capture = StubCaptureController(fail_start=True)
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        capture_controller=capture,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=10,
        )

        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/bbc2.ts")

        assert result.scheduler_state == "failed"
        assert result.recording_state == "failed"
        assert result.error == "start failed"
        assert capture.start_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
        assert capture.stop_calls == []
    finally:
        connection.close()


def test_orchestrator_reports_cleanup_stop_failure_context(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    sizes = iter([100, 120, 120, 120])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: next(sizes, 120),
        sleep_fn=lambda _seconds: None,
    )
    capture = StubCaptureController(fail_stop=True)
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        capture_controller=capture,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=10,
        )

        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/bbc2.ts")

        assert result.scheduler_state == "failed"
        assert result.recording_state == "failed"
        assert result.error is not None
        assert "periodic growth check failed" in result.error
        assert "cleanup stop_capture failed: stop failed" in result.error
        assert capture.start_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
        assert capture.stop_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
    finally:
        connection.close()


def test_orchestrator_short_recording_does_not_oversleep_periodic_interval(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    sizes = iter([100, 120, 120, 140, 140, 140])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: next(sizes, 140),
        sleep_fn=clock.sleep,
    )
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(clock.now_seconds - 2),
            duration_seconds=5,
        )

        before = clock.now_seconds
        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/bbc2.ts")
        after = clock.now_seconds

        assert result.scheduler_state == "completed"
        assert after - before == 5
    finally:
        connection.close()


def test_orchestrator_late_start_uses_remaining_programme_time(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    sizes = iter([100, 120, 120, 140, 140, 140])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: next(sizes, 140),
        sleep_fn=clock.sleep,
    )
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=10.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        start_at = clock.now_seconds - 50
        stop_at = clock.now_seconds + 10
        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(start_at),
            duration_seconds=300,
            program_start_at_utc=_iso_at(start_at),
            program_stop_at_utc=_iso_at(stop_at),
        )

        before = clock.now_seconds
        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/bbc2.ts")
        after = clock.now_seconds

        assert result.scheduler_state == "completed"
        assert after - before == 10
    finally:
        connection.close()


def test_orchestrator_main_path_stop_capture_failure_marks_failed_once(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    clock = FakeClock(now_seconds=1_748_000_000.0)
    sizes = iter([100, 120, 120, 140, 140])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=0, pre_start_seconds=0
        ),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=1,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        file_size_reader=lambda _path: next(sizes, 140),
        sleep_fn=lambda _seconds: None,
    )
    capture = StubCaptureController(fail_stop=True)
    orchestrator = RecorderOrchestrator(
        service=service,
        persistence=persistence,
        capture_controller=capture,
        periodic_policy=PeriodicCheckPolicy(growth_min_bytes=1, interval_seconds=5.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )

    try:
        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_iso_at(clock.now_seconds - 5),
            duration_seconds=5,
        )

        result = orchestrator.run_job(job_id=job.id, output_path="/tmp/bbc2.ts")
        scheduler = persistence.get_scheduler_job(job.id, required=True)

        assert result.scheduler_state == "failed"
        assert result.recording_state == "failed"
        assert result.error is not None
        assert "failed stopping capture: stop failed" in result.error
        assert scheduler.state == "failed"
        assert capture.start_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
        assert capture.stop_calls == [("BBC TWO HD", "/tmp/bbc2.ts")]
    finally:
        connection.close()
