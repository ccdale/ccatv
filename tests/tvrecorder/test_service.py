from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ccatv.storage import PersistenceStore, initialize_database
from ccatv.tvrecorder.commands import DvbCtrlCommand
from ccatv.tvrecorder.dvbctrl import DvbCtrlResult
from ccatv.tvrecorder.service import TvRecorderService

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(slots=True)
class StubDvbCtrlClient:
    responses: dict[str, DvbCtrlResult] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)

    def run_command(self, command: str) -> DvbCtrlResult:
        self.commands.append(command)
        return self.responses[command]


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
        assert running.state == "running"
        assert completed.state == "completed"
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
