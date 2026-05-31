from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ccatv.storage import PersistenceStore, initialize_database
from ccatv.tvrecorder.commands import DvbCtrlCommand
from ccatv.tvrecorder.dvbctrl import DvbCtrlResult
from ccatv.tvrecorder.postprocess import PostProcessingRequest, PostProcessingResult
from ccatv.tvrecorder.service import (
    RecordingHealthCheckPolicy,
    RecordingPaddingPolicy,
    TvRecorderService,
)

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(slots=True)
class StubDvbCtrlClient:
    responses: dict[str, DvbCtrlResult] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)

    def run_command(self, command: str) -> DvbCtrlResult:
        self.commands.append(command)
        return self.responses[command]


@dataclass(slots=True)
class StubPostProcessor:
    success: bool = True
    raise_error: bool = False
    requests: list[PostProcessingRequest] = field(default_factory=list)

    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        self.requests.append(request)
        if self.raise_error:
            raise RuntimeError("post-processing failed hard")
        if self.success:
            return PostProcessingResult(success=True, message="ok")
        return PostProcessingResult(success=False, message="failed")


def _result(command: str, stdout: str) -> DvbCtrlResult:
    return DvbCtrlResult(
        command=("dvbctrl", *command.split()),
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_typed_command_render_quotes_arguments() -> None:
    cmd = DvbCtrlCommand(name="select", args=("BBC ONE HD",))
    assert cmd.render() == "select 'BBC ONE HD'"


def test_current_status_prefers_kv_service_field() -> None:
    client = StubDvbCtrlClient(
        responses={
            "current": _result("current", _fixture("current_output.txt")),
        }
    )
    service = TvRecorderService(client)

    status = service.current_status()

    assert status.service_name == "BBC TWO HD"
    assert status.fields["service"] == "BBC TWO HD"


def test_stats_snapshot_coerces_numeric_values() -> None:
    client = StubDvbCtrlClient(
        responses={
            "stats": _result("stats", _fixture("stats_output.txt")),
        }
    )
    service = TvRecorderService(client)

    snapshot = service.stats_snapshot()

    assert snapshot.metrics["packets"] == 12345
    assert snapshot.metrics["dropped packets"] == 12
    assert snapshot.metrics["rate"] == 5.5
    assert snapshot.metrics["state"] == "good"


def test_frontend_status_extracts_lock_and_signal_fields() -> None:
    client = StubDvbCtrlClient(
        responses={
            "festatus": _result(
                "festatus",
                _fixture("festatus_output.txt"),
            )
        }
    )
    service = TvRecorderService(client)

    status = service.frontend_status()

    assert status.locked is True
    assert status.signal == 78
    assert status.snr == 34
    assert status.ber == 0


def test_frontend_status_extracts_lock_from_tuner_status_line() -> None:
    client = StubDvbCtrlClient(
        responses={
            "festatus": _result(
                "festatus",
                (
                    "Tuner status: [ Signal, Lock, Carrier, VITERBI, Sync ]\n"
                    "Signal Strength: 100%\n"
                    "SNR: 100%\n"
                    "BER: -1\n"
                ),
            )
        }
    )
    service = TvRecorderService(client)

    status = service.frontend_status()

    assert status.locked is True
    assert status.signal == 100
    assert status.snr == 100
    assert status.ber == -1


def test_select_service_uses_typed_command_path() -> None:
    client = StubDvbCtrlClient(
        responses={
            "select 'BBC ONE HD'": _result("select", "ok\n"),
        }
    )
    service = TvRecorderService(client)

    service.select_service("BBC ONE HD")

    assert client.commands == ["select 'BBC ONE HD'"]


def test_recording_lifecycle_includes_post_processing(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    service = TvRecorderService(StubDvbCtrlClient(), persistence=persistence)
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        completed = service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )
        post_processing = service.start_recording_post_processing(recording.id)
        ready = service.mark_recording_ready(recording.id)

        assert recording.state == "recording"
        assert completed.state == "capture_completed"
        assert completed.ended_at_utc == "2026-05-23T11:00:00Z"
        assert post_processing.state == "post_processing"
        assert post_processing.ended_at_utc == "2026-05-23T11:00:00Z"
        assert ready.state == "ready"
        assert ready.ended_at_utc == "2026-05-23T11:00:00Z"
    finally:
        connection.close()


def test_scheduler_job_lifecycle_uses_persistence(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    service = TvRecorderService(StubDvbCtrlClient(), persistence=persistence)
    try:
        job = service.schedule_recording(
            channel_name="BBC ONE HD",
            start_at_utc="2026-05-23T12:00:00Z",
            duration_seconds=3600,
        )
        running = service.mark_scheduler_job_running(job.id)
        completed = service.mark_scheduler_job_completed(job.id)

        assert job.state == "scheduled"
        assert job.start_at_utc == "2026-05-23T11:58:00Z"
        assert job.duration_seconds == 4620
        assert running.state == "running"
        assert completed.state == "completed"
    finally:
        connection.close()


def test_schedule_recording_uses_custom_padding_policy(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        padding_policy=RecordingPaddingPolicy(
            post_finish_seconds=300, pre_start_seconds=60
        ),
    )
    try:
        job = service.schedule_recording(
            channel_name="BBC ONE HD",
            start_at_utc="2026-05-23T12:00:00Z",
            duration_seconds=3600,
        )

        assert job.start_at_utc == "2026-05-23T11:59:00Z"
        assert job.duration_seconds == 3960
    finally:
        connection.close()


def test_scheduler_job_failure_transition_uses_persistence(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    service = TvRecorderService(StubDvbCtrlClient(), persistence=persistence)
    try:
        job = service.schedule_recording(
            channel_name="BBC ONE HD",
            start_at_utc="2026-05-23T12:00:00Z",
            duration_seconds=3600,
        )
        failed = service.mark_scheduler_job_failed(job.id)

        assert failed.state == "failed"
    finally:
        connection.close()


def test_recording_failure_preserves_capture_end_timestamp(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    service = TvRecorderService(StubDvbCtrlClient(), persistence=persistence)
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        completed = service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )
        failed = service.mark_recording_failed(recording.id)

        assert completed.ended_at_utc == "2026-05-23T11:00:00Z"
        assert failed.state == "failed"
        assert failed.ended_at_utc == "2026-05-23T11:00:00Z"
    finally:
        connection.close()


def test_recording_failure_before_capture_sets_end_timestamp(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    service = TvRecorderService(StubDvbCtrlClient(), persistence=persistence)
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        failed = service.mark_recording_failed(recording.id)

        assert failed.state == "failed"
        assert failed.ended_at_utc is not None
        assert failed.ended_at_utc.endswith("Z")
    finally:
        connection.close()


def test_persistence_methods_require_configured_store() -> None:
    service = TvRecorderService(StubDvbCtrlClient())

    with pytest.raises(RuntimeError, match="persistence store is not configured"):
        service.schedule_recording(
            channel_name="BBC ONE HD",
            start_at_utc="2026-05-23T12:00:00Z",
            duration_seconds=3600,
        )


def test_run_post_processing_success_marks_ready(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    post_processor = StubPostProcessor(success=True)
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        post_processor=post_processor,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )

        ready = service.run_recording_post_processing(recording.id)

        assert ready.state == "ready"
        assert post_processor.requests
        assert post_processor.requests[0].recording_id == recording.id
        assert post_processor.requests[0].output_path == "/tmp/bbc2.ts"
    finally:
        connection.close()


def test_run_post_processing_failure_marks_failed(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    post_processor = StubPostProcessor(success=False)
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        post_processor=post_processor,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )

        failed = service.run_recording_post_processing(recording.id)

        assert failed.state == "failed"
        assert failed.ended_at_utc == "2026-05-23T11:00:00Z"
    finally:
        connection.close()


def test_run_post_processing_exception_marks_failed_and_reraises(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    post_processor = StubPostProcessor(raise_error=True)
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        post_processor=post_processor,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )

        with pytest.raises(RuntimeError, match="post-processing failed hard"):
            service.run_recording_post_processing(recording.id)

        row = persistence.get_recording(recording.id, required=True)
        assert row.state == "failed"
        assert row.ended_at_utc == "2026-05-23T11:00:00Z"
    finally:
        connection.close()


def test_verify_recording_output_growth_marks_failed_when_no_growth(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    sizes = iter([100, 100, 100])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        file_size_reader=lambda _path: next(sizes, 100),
        sleep_fn=lambda _seconds: None,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )

        failed = service.verify_recording_output_growth(
            recording.id,
            checks=2,
            interval_seconds=0,
        )

        assert failed.state == "failed"
    finally:
        connection.close()


def test_verify_recording_output_growth_accepts_growth(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    sizes = iter([100, 130, 130])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        file_size_reader=lambda _path: next(sizes, 130),
        sleep_fn=lambda _seconds: None,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )

        result = service.verify_recording_output_growth(
            recording.id,
            checks=2,
            interval_seconds=0,
            min_growth_bytes=10,
        )

        assert result.state == "recording"
    finally:
        connection.close()


def test_verify_recording_output_stable_after_stop_marks_failed_on_change(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    sizes = iter([200, 200, 220])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        file_size_reader=lambda _path: next(sizes, 220),
        sleep_fn=lambda _seconds: None,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )

        failed = service.verify_recording_output_stable_after_stop(
            recording.id,
            checks=2,
            interval_seconds=0,
        )

        assert failed.state == "failed"
    finally:
        connection.close()


def test_verify_recording_output_stable_after_stop_accepts_stable(
    tmp_path: Path,
) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    sizes = iter([200, 200, 200])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        file_size_reader=lambda _path: next(sizes, 200),
        sleep_fn=lambda _seconds: None,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        completed = service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )

        stable = service.verify_recording_output_stable_after_stop(
            recording.id,
            checks=2,
            interval_seconds=0,
        )

        assert stable.state == "capture_completed"
        assert stable.ended_at_utc == completed.ended_at_utc
    finally:
        connection.close()


def test_default_growth_and_stability_policy_methods(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    persistence = PersistenceStore(connection=connection)
    sizes = iter([100, 120, 120, 121, 121, 121])
    service = TvRecorderService(
        StubDvbCtrlClient(),
        persistence=persistence,
        file_size_reader=lambda _path: next(sizes, 121),
        health_policy=RecordingHealthCheckPolicy(
            early_growth_checks=1,
            early_growth_interval_seconds=0,
            final_stability_checks=2,
            final_stability_interval_seconds=0,
            growth_min_bytes=1,
            periodic_growth_checks=1,
            periodic_growth_interval_seconds=0,
        ),
        sleep_fn=lambda _seconds: None,
    )
    try:
        recording = service.begin_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            started_at_utc="2026-05-23T10:00:00Z",
        )
        early = service.verify_recording_output_growth_early(recording.id)
        periodic = service.verify_recording_output_growth_periodic(recording.id)
        service.mark_recording_capture_completed(
            recording.id,
            ended_at_utc="2026-05-23T11:00:00Z",
        )
        stable = service.verify_recording_output_stable_after_stop_default(recording.id)

        assert early.state == "recording"
        assert periodic.state == "recording"
        assert stable.state == "capture_completed"
    finally:
        connection.close()


def test_resolve_service_name_uses_database_mapping_when_present(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    service_list = "BBC ONE East\nQUEST\n5 HD\n"

    class _StubDvbCtrl:
        def run_command(self, command: str) -> DvbCtrlResult:
            return DvbCtrlResult(
                command=tuple(command.split()),
                returncode=0,
                stdout=service_list,
                stderr="",
            )

    connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number,
            dvbstreamer_service_name
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        ("schedules_direct", "100", "Quest", "QUEST HD", "12", "QUEST HD"),
    )
    connection.commit()

    service = TvRecorderService(
        _StubDvbCtrl(),
        persistence=PersistenceStore(connection=connection),
    )

    try:
        assert service.resolve_service_name("Quest") == "QUEST HD"
    finally:
        connection.close()


def test_resolve_service_name_matches_case_insensitively() -> None:
    service_list = "BBC ONE East\nQUEST\n5 HD\n"

    class _StubDvbCtrl:
        def run_command(self, command: str) -> DvbCtrlResult:
            return DvbCtrlResult(
                command=tuple(command.split()),
                returncode=0,
                stdout=service_list,
                stderr="",
            )

    service = TvRecorderService(_StubDvbCtrl())

    assert service.resolve_service_name("Quest") == "QUEST"
    assert service.resolve_service_name("bbc one east") == "BBC ONE East"
    assert service.resolve_service_name("5 hd") == "5 HD"


def test_resolve_service_name_returns_original_when_not_found() -> None:
    class _StubDvbCtrl:
        def run_command(self, command: str) -> DvbCtrlResult:
            return DvbCtrlResult(
                command=tuple(command.split()),
                returncode=0,
                stdout="BBC ONE East\nQUEST\n",
                stderr="",
            )

    service = TvRecorderService(_StubDvbCtrl())

    assert service.resolve_service_name("Unknown Channel") == "Unknown Channel"


def test_list_service_channel_name_map_uses_serviceinfo_ids() -> None:
    class _StubDvbCtrl:
        def run_command(self, command: str) -> DvbCtrlResult:
            responses = {
                "lsservices": DvbCtrlResult(
                    command=("dvbctrl", "lsservices"),
                    returncode=0,
                    stdout="BBC ONE East\nBBC News\n",
                    stderr="",
                ),
                "serviceinfo 'BBC ONE East'": DvbCtrlResult(
                    command=("dvbctrl", "serviceinfo", "BBC ONE East"),
                    returncode=0,
                    stdout=(
                        'Name                : "BBC ONE East"\n'
                        "ID                  : 233a.1047.1047\n"
                    ),
                    stderr="",
                ),
                "serviceinfo 'BBC News'": DvbCtrlResult(
                    command=("dvbctrl", "serviceinfo", "BBC News"),
                    returncode=0,
                    stdout=(
                        'Name                : "BBC News"\n'
                        "ID                  : 233a.1047.1100\n"
                    ),
                    stderr="",
                ),
            }
            return responses[command]

    service = TvRecorderService(_StubDvbCtrl())

    assert service.list_service_channel_name_map() == {
        "0x233a:0x1047:0x1047": "BBC ONE East",
        "0x233a:0x1047:0x1100": "BBC News",
    }
