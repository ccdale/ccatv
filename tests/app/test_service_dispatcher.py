from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccatv.app.service_dispatcher import (
    API_VERSION,
    SERVICE_CAPABILITIES,
    SERVICE_COMMANDS,
    ServiceCommandDispatcher,
    ServiceCommandError,
)
from ccatv.metadata.schedules_direct_contract import (
    SchedulesDirectApiError,
    SchedulesDirectAuthenticationError,
    SchedulesDirectRateLimitError,
    SchedulesDirectTransportError,
)
from ccatv.runtime_config import RuntimeConfigStore
from ccatv.storage import PersistenceStore, apply_migrations
from ccatv.tvrecorder.config import TvRecorderConfigStore
from ccatv.tvrecorder.manager import DvbStreamerState
from ccatv.tvrecorder.orchestrator import OrchestratorResult
from ccatv.tvrecorder.service import TvRecorderService


@dataclass(slots=True)
class StubWorker:
    results: list[OrchestratorResult]

    def run_cycle(self):
        return self.results


@dataclass(slots=True)
class StubLock:
    entered: int = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


class _MockPopen:
    """Minimal Popen stand-in for epgdata streaming tests."""

    def __init__(
        self,
        stdout_data: str = "<epg></epg>",
        *,
        returncode: int = 0,
        stderr_data: str = "",
    ) -> None:
        self.args = ["dvbctrl", "-h", "localhost", "-a", "0", "epgdata"]
        self.returncode = returncode
        self._stdout_data = stdout_data
        self._stderr_data = stderr_data
        self.signals_sent: list[int] = []
        self.communicate_timeouts: list[float | None] = []
        self.killed = False

    def send_signal(self, sig) -> None:  # noqa: ANN001
        self.signals_sent.append(sig)

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_timeouts.append(timeout)
        return self._stdout_data, self._stderr_data

    def kill(self) -> None:
        self.killed = True


def _build_context() -> SimpleNamespace:
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    persistence = PersistenceStore(connection=connection)
    tvrecorder = TvRecorderService(
        dvbctrl=SimpleNamespace(),
        persistence=persistence,
    )
    return SimpleNamespace(
        logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        ),
        persistence=persistence,
        settings=SimpleNamespace(
            database_path=":memory:",
            ota_epg_channel_name="BBC TWO HD",
        ),
        tvrecorder=tvrecorder,
        dvbctrl=SimpleNamespace(
            executable_path="dvbctrl",
            host="localhost",
            adapter_index=0,
            timeout_seconds=10.0,
            transient_retry_count=2,
            transient_retry_delay_seconds=0.2,
            run_command=lambda _command: SimpleNamespace(
                stdout="",
                stderr="",
                returncode=0,
            ),
            start_command=lambda _command: _MockPopen(),
        ),
        dvbstreamer=SimpleNamespace(
            health_check=lambda: SimpleNamespace(state=DvbStreamerState.RUNNING),
            start=lambda: None,
        ),
    )


def test_dispatch_service_health_get() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["status"] == "ok"
    assert payload["database"]["reachable"] is True
    assert payload["database"]["readable"] is True
    assert payload["database"]["writable"] is True
    assert payload["database"]["error"] is None
    assert payload["database"]["failedAt"] is None
    assert payload["recorder"]["workerEnabled"] is True


def test_dispatch_recording_schedule_create() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.create",
        "payload": {
            "channelName": "BBC TWO HD",
            "startAtUtc": "2026-05-25T21:00:00Z",
            "durationSeconds": 3600,
        },
    })

    assert response["ok"] is True
    job = response["payload"]["job"]
    assert job["id"] == 1
    assert job["state"] == "scheduled"


def test_dispatch_recording_schedule_create_rejects_invalid_timestamp() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.create",
        "payload": {
            "channelName": "BBC TWO HD",
            "startAtUtc": "2026/05/25 21:00:00",
            "durationSeconds": 3600,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_recording_schedule_create_rejects_invalid_program_stop_at_utc() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.schedule.create",
            "payload": {
                "channelName": "BBC TWO HD",
                "startAtUtc": "2026-05-25T21:00:00Z",
                "durationSeconds": 3600,
                "programStopAtUtc": "2026/05/25 22:00:00",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_recording_schedule_create_rejects_program_with_too_little_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return cls(2026, 5, 25, 21, 59, 40, tzinfo=timezone.utc)

    monkeypatch.setattr("ccatv.app.service_dispatcher.datetime", _FakeDatetime)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.schedule.create",
            "payload": {
                "channelName": "BBC TWO HD",
                "startAtUtc": "2026-05-25T21:00:00Z",
                "durationSeconds": 3600,
                "programStopAtUtc": "2026-05-25T22:00:00Z",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"
    assert "less than 30 seconds remaining" in response["error"]["message"]


def test_dispatch_recording_schedule_list_filters_state() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    context.tvrecorder.schedule_recording(
        channel_name="BBC TWO HD",
        start_at_utc="2026-05-25T21:00:00Z",
        duration_seconds=3600,
    )
    context.persistence.create_scheduler_job(
        channel_name="BBC ONE HD",
        start_at_utc="2026-05-25T22:00:00Z",
        duration_seconds=1800,
        state="completed",
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.list",
        "payload": {"state": "scheduled"},
    })

    assert response["ok"] is True
    jobs = response["payload"]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["channelName"] == "BBC TWO HD"
    assert jobs[0]["state"] == "scheduled"


def test_dispatch_recording_schedule_cancel_marks_job_cancelled() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    job = context.tvrecorder.schedule_recording(
        channel_name="BBC TWO HD",
        start_at_utc="2026-05-25T21:00:00Z",
        duration_seconds=3600,
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.cancel",
        "payload": {"id": job.id},
    })

    assert response["ok"] is True
    assert response["payload"]["job"]["id"] == job.id
    assert response["payload"]["job"]["state"] == "cancelled"


def test_dispatch_recording_schedule_cancel_rejects_non_scheduled_job() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    job = context.persistence.create_scheduler_job(
        channel_name="BBC ONE HD",
        start_at_utc="2026-05-25T22:00:00Z",
        duration_seconds=1800,
        state="running",
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.cancel",
        "payload": {"id": job.id},
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"

def test_dispatch_recording_list_returns_recordings() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
        """
        INSERT INTO recordings(
            channel_name,
            output_path,
            state,
            started_at_utc,
            ended_at_utc
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (
            "BBC TWO HD",
            "/tmp/bbc2.ts",
            "capture_completed",
            "2026-05-25T20:00:00Z",
            "2026-05-25T21:00:00Z",
        ),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.list",
            "payload": {},
        }
    )

    assert response["ok"] is True
    recordings = response["payload"]["recordings"]
    assert len(recordings) == 1
    assert recordings[0]["channelName"] == "BBC TWO HD"
    assert recordings[0]["outputPath"] == "/tmp/bbc2.ts"
    assert recordings[0]["state"] == "capture_completed"
    assert recordings[0]["programTitle"] == "bbc2"
    assert recordings[0]["description"] is None
    assert recordings[0]["fileSizeBytes"] is None


def test_dispatch_recording_list_reads_nfo_title_and_description(
    tmp_path: Path,
) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    output_path = tmp_path / "doctor_who.ts"
    output_path.write_bytes(b"0" * 2048)
    output_path.with_suffix(".nfo").write_text(
        """
        <movie>
          <title>Doctor Who</title>
          <plot>The Doctor investigates a temporal anomaly.</plot>
        </movie>
        """.strip(),
        encoding="utf-8",
    )

    context.persistence.connection.execute(
        """
        INSERT INTO recordings(
            channel_name,
            output_path,
            state,
            started_at_utc,
            ended_at_utc
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (
            "BBC ONE HD",
            str(output_path),
            "capture_completed",
            "2026-05-25T20:00:00Z",
            "2026-05-25T21:00:00Z",
        ),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.list",
            "payload": {},
        }
    )

    assert response["ok"] is True
    recording = response["payload"]["recordings"][0]
    assert recording["programTitle"] == "Doctor Who"
    assert recording["description"] == "The Doctor investigates a temporal anomaly."
    assert recording["fileSizeBytes"] == 2048

def test_dispatch_recording_schedule_create_round_trips_in_list() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    create_response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.create",
        "payload": {
            "channelName": "C4 HD",
            "startAtUtc": "2099-05-25T21:00:00Z",
            "durationSeconds": 1800,
            "programTitle": "Film4 Premiere",
            "programDescription": "A premiere event.",
            "programStartAtUtc": "2099-05-25T21:00:00Z",
            "programStopAtUtc": "2099-05-25T21:30:00Z",
        },
    })

    assert create_response["ok"] is True
    created_job_id = create_response["payload"]["job"]["id"]

    list_response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.list",
        "payload": {},
    })

    assert list_response["ok"] is True
    jobs = list_response["payload"]["jobs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == created_job_id
    assert job["channelName"] == "C4 HD"
    assert isinstance(job["startAtUtc"], str)
    assert job["startAtUtc"]
    assert isinstance(job["durationSeconds"], int)
    assert job["durationSeconds"] > 0
    assert job["state"] == "scheduled"
    assert job["programTitle"] == "Film4 Premiere"
    assert job["programDescription"] == "A premiere event."
    assert job["programStartAtUtc"] == "2099-05-25T21:00:00Z"
    assert job["programStopAtUtc"] == "2099-05-25T21:30:00Z"


def test_dispatch_recording_list_prefers_persisted_programme_snapshot() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
        """
        INSERT INTO recordings(
            channel_name,
            output_path,
            state,
            started_at_utc,
            ended_at_utc,
            program_title,
            program_description,
            program_start_at_utc,
            program_stop_at_utc
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Talking Pictures TV",
            "/tmp/talking-pictures-tv.ts",
            "capture_completed",
            "2026-05-29T20:00:00Z",
            "2026-05-29T22:00:00Z",
            "The Saint",
            "Simon Templar tackles an unusual mystery.",
            "2026-05-29T20:30:00Z",
            "2026-05-29T21:30:00Z",
        ),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.list",
            "payload": {},
        }
    )

    assert response["ok"] is True
    recording = response["payload"]["recordings"][0]
    assert recording["programTitle"] == "The Saint"
    assert recording["description"] == "Simon Templar tackles an unusual mystery."
    assert recording["programStartAtUtc"] == "2026-05-29T20:30:00Z"
    assert recording["programStopAtUtc"] == "2026-05-29T21:30:00Z"


def test_dispatch_recording_delete_removes_row_and_files(tmp_path: Path) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    output_path = tmp_path / "recordings" / "bbc2.ts"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"recording")
    output_path.with_suffix(".nfo").write_text("meta", encoding="utf-8")

    created = context.persistence.create_recording(
        channel_name="BBC TWO HD",
        output_path=str(output_path),
        state="ready",
    )

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.delete",
            "payload": {"id": created.id, "deleteFiles": True},
        }
    )

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["id"] == created.id
    assert str(output_path) in payload["fileDelete"]["deleted"]
    assert str(output_path.with_suffix(".nfo")) in payload["fileDelete"]["deleted"]
    assert context.persistence.get_recording(created.id) is None


def test_dispatch_recording_delete_not_found_returns_not_found() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.delete",
            "payload": {"id": 999},
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "NOT_FOUND"


def test_dispatch_recording_metadata_backfill_uses_epg_match() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        ("schedules_direct", "250", "Talking Pictures TV", "TPTV", "82"),
    )
    context.persistence.connection.execute(
        """
        INSERT INTO epg_programs(
            source,
            source_program_id,
            title,
            description_long
        ) VALUES(?, ?, ?, ?)
        """,
        (
            "schedules_direct",
            "prog-250-1",
            "The Saint",
            "Simon Templar solves a case.",
        ),
    )

    channel_id = context.persistence.connection.execute(
        "SELECT id FROM epg_channels WHERE source_channel_id = ?",
        ("250",),
    ).fetchone()
    program_id = context.persistence.connection.execute(
        "SELECT id FROM epg_programs WHERE source_program_id = ?",
        ("prog-250-1",),
    ).fetchone()
    assert channel_id is not None
    assert program_id is not None

    context.persistence.connection.execute(
        """
        INSERT INTO epg_broadcasts(channel_id, program_id, start_utc, stop_utc, duration_seconds)
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            int(channel_id[0]),
            int(program_id[0]),
            "2026-05-29T20:30:00Z",
            "2026-05-29T21:30:00Z",
            3600,
        ),
    )

    context.persistence.connection.execute(
        """
        INSERT INTO recordings(
            channel_name,
            output_path,
            state,
            started_at_utc,
            ended_at_utc
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (
            "Talking Pictures TV",
            "/tmp/tptv.ts",
            "capture_completed",
            "2026-05-29T20:31:00Z",
            "2026-05-29T21:31:00Z",
        ),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.metadata.backfill",
            "payload": {},
        }
    )

    assert response["ok"] is True
    assert response["payload"]["updatedFromEpg"] == 1
    assert response["payload"]["updatedFromNfo"] == 0

    recording = context.persistence.list_recordings()[0]
    assert recording.program_title == "The Saint"
    assert recording.program_description == "Simon Templar solves a case."
    assert recording.program_start_at_utc == "2026-05-29T20:30:00Z"
    assert recording.program_stop_at_utc == "2026-05-29T21:30:00Z"


def test_dispatch_recording_metadata_backfill_uses_nfo_fallback(tmp_path: Path) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    output_path = tmp_path / "talking-pictures.ts"
    output_path.write_bytes(b"0")
    output_path.with_suffix(".nfo").write_text(
        """
        <movie>
          <title>Cellar Club</title>
          <plot>Classic horror showcase.</plot>
        </movie>
        """.strip(),
        encoding="utf-8",
    )

    context.persistence.connection.execute(
        """
        INSERT INTO recordings(
            channel_name,
            output_path,
            state,
            started_at_utc,
            ended_at_utc
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (
            "Talking Pictures TV",
            str(output_path),
            "capture_completed",
            "2026-05-29T22:00:00Z",
            "2026-05-29T23:00:00Z",
        ),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "recording.metadata.backfill",
            "payload": {},
        }
    )

    assert response["ok"] is True
    assert response["payload"]["updatedFromEpg"] == 0
    assert response["payload"]["updatedFromNfo"] == 1

    recording = context.persistence.list_recordings()[0]
    assert recording.program_title == "Cellar Club"
    assert recording.program_description == "Classic horror showcase."


def test_dispatch_recording_schedule_list_returns_empty_when_no_jobs() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.schedule.list",
        "payload": {},
    })

    assert response["ok"] is True
    assert response["payload"]["jobs"] == []


def test_dispatch_recording_status_get_no_active_recordings() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.status.get",
        "payload": {},
    })

    assert response["ok"] is True
    assert response["payload"]["isRecording"] is False
    assert response["payload"]["activeCount"] == 0
    assert response["payload"]["activeRecordings"] == []
    assert response["payload"]["nextScheduled"] is None
    assert response["payload"]["adapters"] == []


def test_dispatch_recording_status_get_includes_adapter_statuses() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    slot = SimpleNamespace(
        adapter_index=1,
        dvbstreamer=SimpleNamespace(
            health_check=lambda: SimpleNamespace(
                state=DvbStreamerState.RUNNING,
                pid=4321,
            )
        ),
        capture_controller=SimpleNamespace(
            service=SimpleNamespace(
                current_status=lambda: SimpleNamespace(service_name="BBC TWO HD"),
                frontend_status=lambda: SimpleNamespace(
                    locked=True,
                    signal=82,
                    snr=35,
                    ber=0,
                ),
                stats_snapshot=lambda: SimpleNamespace(
                    metrics={"packets": 1000}
                ),
            )
        ),
    )
    context.adapter_pool = SimpleNamespace(
        slots=[slot],
        idle_slots_snapshot=lambda: (slot,),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.status.get",
        "payload": {},
    })

    assert response["ok"] is True
    adapters = response["payload"]["adapters"]
    assert len(adapters) == 1
    assert adapters[0]["adapterIndex"] == 1
    assert adapters[0]["allocation"] == "free"
    assert adapters[0]["dvbStreamerState"] == DvbStreamerState.RUNNING.value
    assert adapters[0]["tunedService"] == "BBC TWO HD"
    assert adapters[0]["frontend"]["locked"] is True
    assert adapters[0]["stats"]["packets"] == 1000


def test_dispatch_recording_status_get_includes_next_scheduled_job() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    job = context.persistence.create_scheduler_job(
        channel_name="BBC TWO HD",
        start_at_utc="2099-06-14T14:30:00Z",
        duration_seconds=1800,
        state="scheduled",
        program_title="Gardeners' World",
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.status.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["isRecording"] is False
    assert payload["nextScheduled"] == {
        "jobId": job.id,
        "channel": "BBC TWO HD",
        "program": "Gardeners' World",
        "startAtUtc": "2099-06-14T14:30:00Z",
    }


def test_dispatch_recording_status_get_with_active_recording() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.create_recording(
        channel_name="BBC One East HD",
        output_path="/tmp/bbc1.ts",
        state="recording",
        started_at_utc="2026-06-14T12:43:00Z",
        program_title="Points of View",
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.status.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["isRecording"] is True
    assert payload["activeCount"] == 1
    rec = payload["activeRecordings"][0]
    assert rec["channel"] == "BBC One East HD"
    assert rec["program"] == "Points of View"
    assert isinstance(rec["elapsedSeconds"], int)
    assert rec["elapsedSeconds"] >= 0


def test_dispatch_recording_status_get_excludes_completed_recordings() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.create_recording(
        channel_name="BBC One East HD",
        output_path="/tmp/bbc1.ts",
        state="ready",
        started_at_utc="2026-06-14T12:43:00Z",
        program_title="Points of View",
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.status.get",
        "payload": {},
    })

    assert response["ok"] is True
    assert response["payload"]["isRecording"] is False
    assert response["payload"]["activeCount"] == 0


def test_dispatch_recording_status_get_links_scheduler_job_id() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    job = context.persistence.create_scheduler_job(
        channel_name="Channel 4 HD",
        start_at_utc="2026-06-14T12:58:00Z",
        duration_seconds=1800,
        state="scheduled",
    )
    context.persistence.update_scheduler_job_state(job.id, state="running")
    context.persistence.create_recording(
        channel_name="Channel 4 HD",
        output_path="/tmp/ch4.ts",
        state="recording",
        started_at_utc="2026-06-14T12:58:00Z",
        program_title="The Simpsons",
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.status.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["isRecording"] is True
    rec = payload["activeRecordings"][0]
    assert rec["jobId"] == job.id
    assert rec["channel"] == "Channel 4 HD"
    assert rec["program"] == "The Simpsons"


def test_dispatch_recording_status_get_is_in_service_commands() -> None:
    assert "recording.status.get" in SERVICE_COMMANDS


def test_dispatch_metadata_guide_list_returns_programs_for_channel() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        ("schedules_direct", "100", "BBC TWO HD", "BBCTWO", "2"),
    )
    context.persistence.connection.execute(
        """
        INSERT INTO epg_programs(
            source,
            source_program_id,
            title,
            description_long,
            genre_primary
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        (
            "schedules_direct",
            "p1",
            "Newsnight",
            "Late-night news and analysis",
            "News",
        ),
    )
    context.persistence.connection.execute(
        """
        INSERT INTO epg_broadcasts(
            channel_id,
            program_id,
            start_utc,
            stop_utc,
            duration_seconds
        ) VALUES(1, 1, ?, ?, ?)
        """,
        ("2026-05-25T21:00:00Z", "2026-05-25T22:00:00Z", 3600),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.guide.list",
            "payload": {
                "channel": "BBC TWO HD",
                "startAtUtc": "2026-05-25T20:00:00Z",
                "windowHours": 4,
            },
        }
    )

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["channel"] == "BBC TWO HD"
    programs = payload["programs"]
    assert len(programs) == 1
    assert programs[0]["title"] == "Newsnight"
    assert programs[0]["channelName"] == "BBC TWO HD"
    assert programs[0]["startAtUtc"] == "2026-05-25T21:00:00Z"
    assert programs[0]["genre"] == "News"


def test_dispatch_metadata_guide_list_matches_spacing_variant_channel_name() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.executemany(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            ("schedules_direct", "sd-1", "Talking Pictures TV", "TALKPIC", "82"),
            ("dvbstreamer_ota", "ota-1", "TalkingPictures TV", None, None),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_programs(
            source,
            source_program_id,
            title,
            description_long,
            metadata_json
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            (
                "schedules_direct",
                "sd-p1",
                "The Mind of Mr. J. G. Reeder",
                "Classic crime drama.",
                None,
            ),
            (
                "dvbstreamer_ota",
                "ota-p1",
                "The Mind of Mr. J. G. Reeder",
                "Classic crime drama.",
                '{"contentRef":"talkingpicturestv.co.uk/6E4000014280","seriesRef":"talkingpicturestv.co.uk/6E400000382113"}',
            ),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_broadcasts(
            channel_id,
            program_id,
            start_utc,
            stop_utc,
            duration_seconds
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "2026-05-25T21:00:00Z", "2026-05-25T22:00:00Z", 3600),
            (2, 2, "2026-05-25T21:00:00Z", "2026-05-25T22:00:00Z", 3600),
        ],
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.guide.list",
            "payload": {
                "channel": "Talking Pictures TV",
                "startAtUtc": "2026-05-25T20:00:00Z",
                "windowHours": 4,
            },
        }
    )

    assert response["ok"] is True
    programs = response["payload"]["programs"]
    assert len(programs) == 2
    assert any(
        program["seriesRef"] == "talkingpicturestv.co.uk/6E400000382113"
        for program in programs
    )


def test_dispatch_metadata_films_list_returns_duration_filtered_programs() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.executemany(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            ("dvbstreamer_ota", "200", "Film4", "FILM4", "14"),
            ("schedules_direct", "100", "Film4", "FILM4", "14"),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_programs(
            source,
            source_program_id,
            title,
            description_long,
            genre_primary,
            metadata_json
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "dvbstreamer_ota",
                "p-ota",
                "The Film",
                "Feature film",
                "Movie",
                '{"contentRef":"example.org/content-1","seriesRef":"example.org/series-1"}',
            ),
            (
                "schedules_direct",
                "p-sd",
                "The Film",
                "Feature film",
                "Movie",
                '{"contentRef":"example.org/content-1"}',
            ),
            ("dvbstreamer_ota", "p-short", "Too Short", "Not a film", "Movie", None),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_broadcasts(
            channel_id,
            program_id,
            start_utc,
            stop_utc,
            duration_seconds
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "2026-05-25T21:00:00Z", "2026-05-25T23:00:00Z", 7200),
            (2, 2, "2026-05-25T21:00:00Z", "2026-05-25T23:00:00Z", 7200),
            (1, 3, "2026-05-25T23:30:00Z", "2026-05-26T00:00:00Z", 1800),
        ],
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.films.list",
            "payload": {
                "startAtUtc": "2026-05-25T20:00:00Z",
                "windowHours": 8,
                "channelScope": "all",
                "minDurationHours": 1.5,
                "maxDurationHours": 3.5,
            },
        }
    )

    assert response["ok"] is True
    films = response["payload"]["films"]
    assert len(films) == 1
    assert films[0]["title"] == "The Film"
    assert films[0]["channelName"] == "Film4"
    assert films[0]["durationSeconds"] == 7200
    assert films[0]["source"] == "dvbstreamer_ota"
    assert films[0]["contentRef"] == "example.org/content-1"
    assert films[0]["seriesRef"] == "example.org/series-1"


def test_dispatch_metadata_films_list_favourites_scope_filters_channels() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.executemany(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number,
            favorite_channel
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        [
            ("dvbstreamer_ota", "200", "Film4", "FILM4", "14", 1),
            ("dvbstreamer_ota", "201", "BBC TWO HD", "BBC2", "2", 0),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_programs(
            source,
            source_program_id,
            title,
            description_long,
            genre_primary
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            ("dvbstreamer_ota", "p1", "Film A", "Feature", "Movie"),
            ("dvbstreamer_ota", "p2", "Film B", "Feature", "Movie"),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_broadcasts(
            channel_id,
            program_id,
            start_utc,
            stop_utc,
            duration_seconds
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "2026-05-25T21:00:00Z", "2026-05-25T23:00:00Z", 7200),
            (2, 2, "2026-05-25T22:00:00Z", "2026-05-26T00:00:00Z", 7200),
        ],
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.films.list",
            "payload": {
                "startAtUtc": "2026-05-25T20:00:00Z",
                "windowHours": 8,
            },
        }
    )

    assert response["ok"] is True
    films = response["payload"]["films"]
    assert len(films) == 1
    assert films[0]["channelName"] == "Film4"


def test_dispatch_metadata_films_list_excludes_radio_and_no_pid_channels() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.executemany(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            ("dvbstreamer_ota", "200", "Film4", "FILM4", "14"),
            ("dvbstreamer_ota", "201", "BBC Radio 4", "RADIO4", "704"),
            ("dvbstreamer_ota", "202", "Web Movie Channel", "WEBMOV", "999"),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_programs(
            source,
            source_program_id,
            title,
            description_long,
            genre_primary
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            ("dvbstreamer_ota", "p1", "Film A", "Feature", "Movie"),
            ("dvbstreamer_ota", "p2", "Film B", "Feature", "Movie"),
            ("dvbstreamer_ota", "p3", "Film C", "Feature", "Movie"),
        ],
    )
    context.persistence.connection.executemany(
        """
        INSERT INTO epg_broadcasts(
            channel_id,
            program_id,
            start_utc,
            stop_utc,
            duration_seconds
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "2026-05-25T21:00:00Z", "2026-05-25T23:00:00Z", 7200),
            (2, 2, "2026-05-25T21:30:00Z", "2026-05-25T23:30:00Z", 7200),
            (3, 3, "2026-05-25T22:00:00Z", "2026-05-26T00:00:00Z", 7200),
        ],
    )
    context.persistence.connection.commit()

    def _run_serviceinfo(command) -> SimpleNamespace:
        service_name = command.args[0]
        if service_name == "Film4":
            return SimpleNamespace(
                stdout="Type: TV\nVideo PID: 0x0078\nAudio PID: 0x0082\n"
            )
        if service_name == "BBC Radio 4":
            return SimpleNamespace(
                stdout="Type: Radio\nAudio PID: 0x0140\n"
            )
        return SimpleNamespace(
            stdout="Type: TV\nVideo PID: none\nAudio PID: none\n"
        )

    context.tvrecorder = SimpleNamespace(
        resolve_service_name=lambda name: name,
        run=_run_serviceinfo,
    )

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.films.list",
            "payload": {
                "startAtUtc": "2026-05-25T20:00:00Z",
                "windowHours": 8,
                "channelScope": "all",
            },
        }
    )

    assert response["ok"] is True
    films = response["payload"]["films"]
    assert len(films) == 1
    assert films[0]["channelName"] == "Film4"


def test_auto_schedule_series_recordings_skips_recorded_content_ref() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.set_series_recording_subscription("example.org/series-1", True)
    context.persistence.mark_recorded_content_ref(
        content_ref="example.org/content-1",
        series_ref="example.org/series-1",
        title="Episode 1",
        recording_id=1,
    )

    context.persistence.connection.execute(
        """
        INSERT INTO epg_channels(source, source_channel_id, display_name)
        VALUES(?, ?, ?)
        """,
        ("dvbstreamer_ota", "200", "Film4"),
    )
    context.persistence.connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title, metadata_json)
        VALUES(?, ?, ?, ?)
        """,
        (
            "dvbstreamer_ota",
            "p1",
            "Episode 1",
            '{"contentRef":"example.org/content-1","seriesRef":"example.org/series-1"}',
        ),
    )
    context.persistence.connection.execute(
        """
        INSERT INTO epg_broadcasts(channel_id, program_id, start_utc, stop_utc, duration_seconds)
        VALUES(1, 1, ?, ?, ?)
        """,
        ("2099-05-25T21:00:00Z", "2099-05-25T22:00:00Z", 3600),
    )
    context.persistence.connection.commit()

    context.tvrecorder = SimpleNamespace(
        resolve_service_name=lambda name: name,
        run=lambda _command: SimpleNamespace(stdout="Type: TV\nVideo PID: 0x100\n"),
        schedule_recording=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("must not schedule")),
    )

    stats = dispatcher._auto_schedule_series_recordings()

    assert stats["scheduled"] == 0
    assert stats["skipped"] == 1


def test_dispatch_metadata_films_list_rejects_invalid_duration_range() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.films.list",
            "payload": {
                "minDurationHours": 3.5,
                "maxDurationHours": 1.5,
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_metadata_films_list_rejects_invalid_channel_scope() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.films.list",
            "payload": {
                "channelScope": "invalid",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_metadata_channels_list_returns_deduplicated_channels() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.executemany(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        [
            ("schedules_direct", "100", "BBC TWO HD", "BBCTWO", "2"),
            ("dvbstreamer_ota", "200", "BBC TWO HD", "BBC2", "2"),
            ("schedules_direct", "300", "BBC FOUR", "BBC4", "9"),
        ],
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.list",
            "payload": {},
        }
    )

    assert response["ok"] is True
    channels = response["payload"]["channels"]
    assert len(channels) == 2

    bbc_two = channels[0]
    assert bbc_two["name"] == "BBC TWO HD"
    assert bbc_two["epgName"] == "BBC TWO HD"
    assert bbc_two["callsign"] == "BBC2"
    assert bbc_two["logicalChannelNumber"] == "2"
    assert bbc_two["source"] == "dvbstreamer_ota"
    assert bbc_two["sourceChannelId"] == "200"
    assert bbc_two["dvbstreamerServiceName"] is None
    assert bbc_two["favoriteChannel"] is False
    assert bbc_two["guideName"] == "BBC TWO HD"
    assert bbc_two["guideLogicalChannelNumber"] == "2"
    assert bbc_two["broadcasterName"] is None
    assert bbc_two["schedulesDirectName"] == "BBC TWO HD"
    assert len(bbc_two["sourceVariants"]) == 2

    bbc_four = channels[1]
    assert bbc_four["name"] == "BBC FOUR"
    assert bbc_four["epgName"] == "BBC FOUR"
    assert bbc_four["callsign"] == "BBC4"
    assert bbc_four["logicalChannelNumber"] == "9"
    assert bbc_four["source"] == "schedules_direct"
    assert bbc_four["sourceChannelId"] == "300"
    assert bbc_four["dvbstreamerServiceName"] is None
    assert bbc_four["favoriteChannel"] is False
    assert bbc_four["guideName"] == "BBC FOUR"
    assert bbc_four["guideLogicalChannelNumber"] == "9"
    assert bbc_four["broadcasterName"] is None
    assert bbc_four["schedulesDirectName"] == "BBC FOUR"
    assert len(bbc_four["sourceVariants"]) == 1


def test_dispatch_metadata_channels_list_keeps_favorite_from_fallback_source() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.executemany(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number,
            favorite_channel
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        [
            ("schedules_direct", "100", "BBC FOUR HD", "BBC4HD", "106", 1),
            ("dvbstreamer_ota", "200", "BBC FOUR HD", None, None, 0),
        ],
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.list",
            "payload": {},
        }
    )

    assert response["ok"] is True
    channels = response["payload"]["channels"]
    assert len(channels) == 1
    channel = channels[0]
    assert channel["name"] == "BBC FOUR HD"
    assert channel["epgName"] == "BBC FOUR HD"
    assert channel["callsign"] == "BBC4HD"
    assert channel["logicalChannelNumber"] == "106"
    assert channel["source"] == "dvbstreamer_ota"
    assert channel["sourceChannelId"] == "200"
    assert channel["dvbstreamerServiceName"] is None
    assert channel["favoriteChannel"] is True
    assert channel["guideName"] == "BBC FOUR HD"
    assert channel["guideLogicalChannelNumber"] == "106"
    assert channel["broadcasterName"] is None
    assert channel["schedulesDirectName"] == "BBC FOUR HD"
    assert len(channel["sourceVariants"]) == 2


def test_dispatch_metadata_channels_service_name_set_updates_mapping() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        ("schedules_direct", "100", "Quest", "QUEST", "12"),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.service-name.set",
            "payload": {
                "channelName": "Quest",
                "serviceName": "QUEST",
            },
        }
    )

    assert response["ok"] is True
    assert response["payload"] == {"channelName": "Quest", "updatedRows": 1}
    assert context.persistence.get_dvbstreamer_service_name("Quest") == "QUEST"


def test_dispatch_metadata_channels_service_name_set_returns_not_found() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.service-name.set",
            "payload": {
                "channelName": "Unknown",
                "serviceName": "UNKNOWN",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "NOT_FOUND"


def test_dispatch_metadata_channels_service_name_set_clears_mapping() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
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
        ("schedules_direct", "101", "BBC One East", "BBC1E", "1", "BBC ONE East"),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.service-name.set",
            "payload": {
                "channelName": "BBC One East",
                "serviceName": None,
            },
        }
    )

    assert response["ok"] is True
    assert response["payload"] == {"channelName": "BBC One East", "updatedRows": 1}
    assert context.persistence.get_dvbstreamer_service_name("BBC One East") is None


def test_dispatch_metadata_channels_favorite_set_updates_flag() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        ("schedules_direct", "301", "BBC News", "BBCNEWS", "231"),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.favorite.set",
            "payload": {
                "channelName": "BBC News",
                "favorite": True,
            },
        }
    )

    assert response["ok"] is True
    assert response["payload"] == {
        "channelName": "BBC News",
        "favorite": True,
        "updatedRows": 1,
    }
    assert context.persistence.get_favorite_channel("BBC News") is True


def test_dispatch_metadata_channels_favorite_set_rejects_non_boolean() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.favorite.set",
            "payload": {
                "channelName": "BBC News",
                "favorite": "yes",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_metadata_channels_lineup_set_saves_override() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    context.persistence.connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        ) VALUES(?, ?, ?, ?, ?)
        """,
        ("dvbstreamer_ota", "200", "BBC FOUR HD", "BBC4HD", "106"),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.lineup.set",
            "payload": {
                "epgChannelName": "BBC FOUR HD",
                "broadcasterName": "BBC FOUR HD",
                "schedulesDirectName": "BBC Four HD",
                "guideName": "BBC4",
                "guideLogicalChannelNumber": "9",
            },
        }
    )

    assert response["ok"] is True
    assert response["payload"]["epgChannelName"] == "BBC FOUR HD"
    assert response["payload"]["guideName"] == "BBC4"
    assert response["payload"]["guideLogicalChannelNumber"] == "9"


def test_dispatch_metadata_channels_favorite_set_rejects_empty_channel_name() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.favorite.set",
            "payload": {
                "channelName": "   ",
                "favorite": True,
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_metadata_channels_favorite_set_returns_not_found() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.favorite.set",
            "payload": {
                "channelName": "Unknown",
                "favorite": True,
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "NOT_FOUND"


def test_dispatch_metadata_series_recording_set_and_list() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    set_response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.series.recording.set",
            "payload": {
                "seriesRef": "example.org/series-1",
                "enabled": True,
            },
        }
    )

    assert set_response["ok"] is True
    assert set_response["payload"]["seriesRef"] == "example.org/series-1"
    assert set_response["payload"]["enabled"] is True

    list_response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.series.recording.list",
            "payload": {},
        }
    )

    assert list_response["ok"] is True
    assert list_response["payload"]["subscriptions"] == [
        {
            "seriesRef": "example.org/series-1",
            "enabled": True,
        }
    ]


def test_dispatch_metadata_series_recording_set_rejects_invalid_payload() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.series.recording.set",
            "payload": {
                "seriesRef": "   ",
                "enabled": "yes",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_parse_ts_id_from_serviceinfo() -> None:
    raw = (
        'Name                : "BBC TWO HD"\n'
        "Type                : Digital TV\n"
        "ID                  : 233a.4087.4440\n"
        "Source              : 0x4440\n"
    )
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    assert dispatcher._parse_ts_id_from_serviceinfo(raw) == "0x4087"


def test_parse_ts_id_from_serviceinfo_returns_none_when_absent() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    assert dispatcher._parse_ts_id_from_serviceinfo("Type: Digital TV\n") is None


def test_dispatch_ota_multimux_sync_rejects_invalid_capture_seconds() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.ota.multimux.sync.run",
            "payload": {"captureSeconds": -1},
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_ota_multimux_sync_returns_stats_per_mux() -> None:
    """Happy path: two services on different muxes are each captured once."""
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    BBC_INFO = (
        'Name                : "BBC TWO HD"\n'
        "Type                : Digital TV\n"
        "ID                  : 233a.1047.10bf\n"
        "Video PID           : 0x0203\n"
        "Audio PID           : 0x0204\n"
    )
    TPTV_INFO = (
        'Name                : "TalkingPictures TV"\n'
        "Type                : Digital TV\n"
        "ID                  : 233a.6040.6e40\n"
        "Video PID           : 0x03fc\n"
        "Audio PID           : 0x03fd\n"
    )
    service_infos = {"BBC TWO HD": BBC_INFO, "TalkingPictures TV": TPTV_INFO}

    captures: list[str] = []

    def _run_serviceinfo(command):
        name = command.args[0]
        return SimpleNamespace(stdout=service_infos.get(name, "Type: Unknown\n"))

    def _fake_capture(*, grab_command, capture_seconds):
        captures.append(grab_command)
        return SimpleNamespace(stdout="")

    context.tvrecorder = SimpleNamespace(
        list_services=lambda: ["BBC TWO HD", "TalkingPictures TV"],
        resolve_service_name=lambda name: name,
        select_service=lambda _: None,
        frontend_status=lambda: SimpleNamespace(locked=True),
        list_service_channel_name_map=lambda: {},
        run=_run_serviceinfo,
        run_raw=lambda _: None,
    )
    context.dvbctrl = SimpleNamespace(
        executable_path="dvbctrl",
        host="localhost",
        adapter_index=0,
        timeout_seconds=10.0,
        transient_retry_count=2,
        transient_retry_delay_seconds=0.2,
        run_command=lambda _cmd: SimpleNamespace(stdout="", stderr="", returncode=0),
        start_command=lambda _cmd: _MockPopen(),
    )
    dispatcher._capture_ota_epg_stream_with_clients = (
        lambda **_kw: _fake_capture(
            grab_command=str(_kw.get("grab_command", "epgdata")),
            capture_seconds=float(_kw.get("capture_seconds", 0.0)),
        )
    )
    dispatcher._wait_for_frontend_lock_with_service = lambda **_kw: None

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.ota.multimux.sync.run",
            "payload": {
                "captureSeconds": 1.0,
                "maxRetries": 0,
            },
        }
    )

    assert response["ok"] is True
    stats = response["payload"]["stats"]
    assert stats["muxesAttempted"] == 2
    assert stats["muxesOk"] == 2
    assert stats["muxesFailed"] == 0
    assert len(captures) == 2


def test_dispatch_ota_multimux_sync_skips_radio_channels() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    TV_INFO = (
        'Name                : "Channel 4 HD"\n'
        "Type                : Digital TV\n"
        "ID                  : 233a.4087.4500\n"
        "Video PID           : 0x0201\n"
    )
    RADIO_INFO = (
        'Name                : "BBC Radio 4"\n'
        "Type                : Radio\n"
        "ID                  : 233a.1047.1b00\n"
        "Audio PID           : 0x0104\n"
    )

    captures: list[str] = []

    context.tvrecorder = SimpleNamespace(
        list_services=lambda: ["BBC Radio 4", "Channel 4 HD"],
        resolve_service_name=lambda name: name,
        select_service=lambda _: None,
        frontend_status=lambda: SimpleNamespace(locked=True),
        list_service_channel_name_map=lambda: {},
        run=lambda cmd: SimpleNamespace(
            stdout=RADIO_INFO if cmd.args[0] == "BBC Radio 4" else TV_INFO
        ),
        run_raw=lambda _: None,
    )
    context.dvbctrl = SimpleNamespace(
        executable_path="dvbctrl",
        host="localhost",
        adapter_index=0,
        timeout_seconds=10.0,
        transient_retry_count=2,
        transient_retry_delay_seconds=0.2,
        run_command=lambda _cmd: SimpleNamespace(stdout="", stderr="", returncode=0),
        start_command=lambda _cmd: _MockPopen(),
    )
    dispatcher._capture_ota_epg_stream_with_clients = (
        lambda **_kw: captures.append("") or SimpleNamespace(stdout="")
    )
    dispatcher._wait_for_frontend_lock_with_service = lambda **_kw: None

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.ota.multimux.sync.run",
            "payload": {"captureSeconds": 1.0, "maxRetries": 0},
        }
    )

    assert response["ok"] is True
    stats = response["payload"]["stats"]
    # Radio channel excluded: only 1 mux (Channel 4 HD) should have been attempted
    assert stats["muxesAttempted"] == 1
    assert stats["muxesOk"] == 1



    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    context.tvrecorder = SimpleNamespace(
        list_services=lambda: ["QUEST", "BBC TWO HD", "quest", "5 HD"]
    )

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.dvbservices.list",
            "payload": {},
        }
    )

    assert response["ok"] is True
    assert response["payload"]["available"] is True
    assert response["payload"]["error"] is None
    assert response["payload"]["services"] == ["5 HD", "BBC TWO HD", "QUEST"]


def test_dispatch_metadata_channels_dvbservices_list_handles_runtime_failure() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    def _broken_list_services() -> list[str]:
        raise RuntimeError("dvbctrl unavailable")

    context.tvrecorder = SimpleNamespace(list_services=_broken_list_services)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.channels.dvbservices.list",
            "payload": {},
        }
    )

    assert response["ok"] is True
    assert response["payload"]["available"] is False
    assert response["payload"]["services"] == []
    assert "dvbctrl unavailable" in str(response["payload"]["error"])


def test_dispatch_metadata_guide_list_validates_channel() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.guide.list",
            "payload": {"channel": "   "},
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_metadata_guide_list_validates_start_at_utc() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch(
        {
            "apiVersion": API_VERSION,
            "command": "metadata.guide.list",
            "payload": {
                "channel": "BBC TWO HD",
                "startAtUtc": "2026/05/25 20:00:00",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_metadata_sd_sync_status_get_empty() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.status.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["lastRun"]["id"] is None
    assert payload["lastRun"]["status"] is None
    assert payload["lastRun"]["finishedAtUtc"] is None
    assert payload["checkpoint"]["lastSuccessfulIngestUtc"] is None


def test_dispatch_metadata_sd_sync_status_get_with_data() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    context.persistence.connection.execute(
        """
        INSERT INTO epg_ingest_runs(source, started_at_utc, finished_at_utc, status)
        VALUES(?, ?, ?, ?)
        """,
        (
            "schedules_direct",
            "2026-05-25T20:00:00Z",
            "2026-05-25T20:02:00Z",
            "ok",
        ),
    )
    context.persistence.connection.execute(
        """
        INSERT INTO epg_source_checkpoints(source, last_successful_ingest_utc)
        VALUES(?, ?)
        """,
        ("schedules_direct", "2026-05-25T20:02:00Z"),
    )
    context.persistence.connection.commit()

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.status.get",
        "payload": {"source": "schedules_direct"},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["lastRun"]["id"] == 1
    assert payload["lastRun"]["status"] == "ok"
    assert payload["lastRun"]["finishedAtUtc"] == "2026-05-25T20:02:00Z"
    assert payload["checkpoint"]["lastSuccessfulIngestUtc"] == "2026-05-25T20:02:00Z"


def test_dispatch_metadata_sd_sync_status_get_rejects_invalid_source() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.status.get",
        "payload": {"source": "other"},
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_service_info_get() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.info.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["apiVersion"] == API_VERSION
    assert payload["appName"] == "ccatv"
    assert isinstance(payload["appVersion"], str)
    assert payload["appVersion"]
    assert isinstance(payload["capabilities"], list)
    assert all(isinstance(capability, str) for capability in payload["capabilities"])
    assert isinstance(payload["commands"], list)
    assert all(isinstance(command, str) for command in payload["commands"])
    assert payload["capabilities"] == SERVICE_CAPABILITIES
    assert payload["commands"] == SERVICE_COMMANDS


def test_dispatch_runtime_setup_save_persists_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    runtime_store = RuntimeConfigStore(config_dir=tmp_path / "ccatv")
    recorder_store = TvRecorderConfigStore(config_dir=tmp_path / "dvbstreamer")
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.RuntimeConfigStore",
        lambda: runtime_store,
    )
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.TvRecorderConfigStore",
        lambda: recorder_store,
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "runtime.setup.save",
        "payload": {
            "adapterCount": 4,
            "host": "druidmedia",
            "otaEpgChannelName": "BBC ONE East",
            "password": "secret",
            "sdLineupId": "UK-TEST",
            "username": "alice",
        },
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["credentialsPath"].endswith("userconfig.json")
    assert payload["runtimeConfigPath"].endswith("runtime.json")

    runtime = runtime_store.load()
    assert runtime.dvb_adapter_count == 4
    assert runtime.dvbstreamer_host == "druidmedia"
    assert runtime.ota_epg_channel_name == "BBC ONE East"
    assert runtime.sd_lineup_id == "UK-TEST"

    recorder = recorder_store.load()
    assert recorder.dvbctrl_credentials is not None
    assert recorder.dvbctrl_credentials.username == "alice"
    assert recorder.dvbctrl_credentials.password == "secret"


def test_dispatch_runtime_setup_save_rejects_invalid_payload() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "runtime.setup.save",
        "payload": {
            "adapterCount": 0,
            "host": " ",
            "otaEpgChannelName": " ",
            "password": "",
            "sdLineupId": " ",
            "username": " ",
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_service_info_capabilities_map_to_command_prefixes() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.info.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    capabilities = payload["capabilities"]
    commands = payload["commands"]
    assert capabilities
    assert commands
    for capability in capabilities:
        assert any(command.startswith(f"{capability}.") for command in commands)
    for command in commands:
        assert any(command.startswith(f"{capability}.") for capability in capabilities)


def test_dispatch_service_health_get_degraded_when_connection_closed() -> None:
    context = _build_context()
    context.persistence.connection.close()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["status"] == "degraded"
    assert payload["database"]["reachable"] is False
    assert payload["database"]["readable"] is False
    assert payload["database"]["writable"] is False
    assert payload["database"]["error"]
    assert payload["database"]["failedAt"] == "read.select"


def test_dispatch_service_health_get_degraded_when_write_probe_fails() -> None:
    class _ReadOnlyLikeConnection:
        def execute(self, sql: str):
            if sql == "SELECT 1":
                return None
            raise sqlite3.OperationalError("attempt to write a readonly database")

    context = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        persistence=SimpleNamespace(connection=_ReadOnlyLikeConnection()),
        settings=SimpleNamespace(database_path=":memory:"),
    )
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["status"] == "degraded"
    assert payload["database"]["reachable"] is False
    assert payload["database"]["readable"] is True
    assert payload["database"]["writable"] is False
    assert "readonly" in payload["database"]["error"].lower()
    assert payload["database"]["failedAt"]


def test_dispatch_service_health_get_reports_transaction_begin_failure() -> None:
    class _BeginFailConnection:
        in_transaction = False

        def execute(self, sql: str):
            if sql == "SELECT 1":
                return None
            if sql == "BEGIN":
                raise sqlite3.OperationalError("database is locked")
            return None

    context = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        persistence=SimpleNamespace(connection=_BeginFailConnection()),
        settings=SimpleNamespace(database_path=":memory:"),
    )
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["status"] == "degraded"
    assert payload["database"]["readable"] is True
    assert payload["database"]["writable"] is False
    assert payload["database"]["failedAt"] == "write.transaction.begin"


def test_dispatch_service_health_get_reports_transaction_insert_failure() -> None:
    class _InsertFailConnection:
        in_transaction = False

        def execute(self, sql: str):
            if sql in {"SELECT 1", "BEGIN", "ROLLBACK"}:
                return None
            if sql == "CREATE TEMP TABLE IF NOT EXISTS ccatv_health_probe (v INTEGER)":
                return None
            if sql == "INSERT INTO ccatv_health_probe (v) VALUES (1)":
                raise sqlite3.OperationalError("disk I/O error")
            return None

    context = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        persistence=SimpleNamespace(connection=_InsertFailConnection()),
        settings=SimpleNamespace(database_path=":memory:"),
    )
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["status"] == "degraded"
    assert payload["database"]["readable"] is True
    assert payload["database"]["writable"] is False
    assert payload["database"]["failedAt"] == "write.tempTable.insert"


def test_dispatch_service_health_get_reports_transaction_cleanup_failure() -> None:
    class _InsertAndRollbackFailConnection:
        in_transaction = False

        def execute(self, sql: str):
            if sql in {"SELECT 1", "BEGIN"}:
                return None
            if sql == "CREATE TEMP TABLE IF NOT EXISTS ccatv_health_probe (v INTEGER)":
                return None
            if sql == "INSERT INTO ccatv_health_probe (v) VALUES (1)":
                raise sqlite3.OperationalError("disk full")
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("rollback failed")
            return None

    context = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        persistence=SimpleNamespace(connection=_InsertAndRollbackFailConnection()),
        settings=SimpleNamespace(database_path=":memory:"),
    )
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["status"] == "degraded"
    assert payload["database"]["readable"] is True
    assert payload["database"]["writable"] is False
    assert payload["database"]["failedAt"] == "write.tempTable.insert.cleanup.rollback"
    assert "cleanup rollback failed" in payload["database"]["error"]


def test_dispatch_service_health_get_reports_savepoint_create_failure() -> None:
    class _SavepointCreateFailConnection:
        in_transaction = True

        def execute(self, sql: str):
            if sql == "SELECT 1":
                return None
            if sql == "SAVEPOINT ccatv_health_check":
                return None
            if sql == "CREATE TEMP TABLE IF NOT EXISTS ccatv_health_probe (v INTEGER)":
                raise sqlite3.OperationalError("temp store is full")
            if sql == "ROLLBACK TO ccatv_health_check":
                return None
            if sql == "RELEASE ccatv_health_check":
                return None
            return None

    context = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        persistence=SimpleNamespace(connection=_SavepointCreateFailConnection()),
        settings=SimpleNamespace(database_path=":memory:"),
    )
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["status"] == "degraded"
    assert payload["database"]["readable"] is True
    assert payload["database"]["writable"] is False
    assert payload["database"]["failedAt"] == "write.tempTable.create"


def test_dispatch_recording_worker_cycle_run(monkeypatch) -> None:
    context = _build_context()
    lock = StubLock()
    dispatcher = ServiceCommandDispatcher(context, worker_cycle_lock=lock)

    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.create_scheduler_worker",
        lambda *_args, **_kwargs: StubWorker(
            results=[
                OrchestratorResult(
                    job_id=10,
                    scheduler_state="completed",
                    recording_id=77,
                    recording_state="ready",
                    error=None,
                )
            ]
        ),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.worker.cycle.run",
        "payload": {
            "maxJobsPerCycle": 1,
            "outputDirectory": "/tmp",
        },
    })

    assert response["ok"] is True
    results = response["payload"]["results"]
    assert len(results) == 1
    assert results[0]["jobId"] == 10
    assert results[0]["schedulerState"] == "completed"
    assert lock.entered == 1


def test_dispatch_recording_worker_cycle_run_uses_defaults(monkeypatch) -> None:
    context = _build_context()
    lock = StubLock()
    dispatcher = ServiceCommandDispatcher(context, worker_cycle_lock=lock)
    captured: dict[str, object] = {}

    def _create_worker(*_args, **kwargs):
        captured.update(kwargs)
        return StubWorker(results=[])

    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.create_scheduler_worker",
        _create_worker,
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.worker.cycle.run",
        "payload": {},
    })

    assert response["ok"] is True
    assert response["payload"]["results"] == []
    assert captured["output_directory"] == "/tmp"
    assert captured["max_jobs_per_cycle"] is None


def test_service_commands_are_dispatchable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.create_scheduler_worker",
        lambda *_args, **_kwargs: StubWorker(results=[]),
    )

    async def _stub_run_sd_sync(**_kwargs):
        return SimpleNamespace(
            channels_upserted=1,
            programs_upserted=1,
            schedules_upserted=1,
            stale_schedules_pruned=0,
            ingest_run_id=1,
        )

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _stub_run_sd_sync)
    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "list_service_channel_name_map", lambda: {})
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout="<epg></epg>"),
    )
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.ingest_dvbstreamer_epg",
        lambda _connection, _raw, source, channel_name_map: SimpleNamespace(
            channels_upserted=0,
            programs_upserted=0,
            broadcasts_upserted=0,
            parsed_events=0,
            ingest_run_id=1,
            source=source,
        ),
    )

    runtime_store = RuntimeConfigStore(config_dir=tmp_path / "ccatv")
    recorder_store = TvRecorderConfigStore(config_dir=tmp_path / "dvbstreamer")
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.RuntimeConfigStore",
        lambda: runtime_store,
    )
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.TvRecorderConfigStore",
        lambda: recorder_store,
    )

    requests = [
        ("service.health.get", {}),
        ("service.info.get", {}),
        (
            "recording.schedule.create",
            {
                "channelName": "BBC TWO HD",
                "startAtUtc": "2026-05-25T21:00:00Z",
                "durationSeconds": 120,
            },
        ),
        ("recording.schedule.cancel", {"id": 1}),
        ("recording.delete", {"id": 1, "deleteFiles": False}),
        ("recording.schedule.list", {}),
        ("recording.metadata.backfill", {"dryRun": True, "limit": 1}),
        ("recording.worker.cycle.run", {}),
        (
            "metadata.guide.list",
            {
                "channel": "BBC TWO HD",
                "startAtUtc": "2026-05-25T20:00:00Z",
                "windowHours": 2,
            },
        ),
        (
            "metadata.ota.sync.run",
            {
                "grabCommand": "epgdata",
            },
        ),
        (
            "metadata.ota.sync.channel-names.backfill.run",
            {},
        ),
        (
            "metadata.sd.sync.run",
            {
                "lineupId": "UK-TEST",
                "windowHours": 24,
            },
        ),
        ("metadata.sd.sync.status.get", {}),
        (
            "runtime.setup.save",
            {
                "adapterCount": 4,
                "host": "druidmedia",
                "otaEpgChannelName": "BBC ONE East",
                "password": "secret",
                "sdLineupId": "UK-TEST",
                "username": "alice",
            },
        ),
    ]

    for command, payload in requests:
        response = dispatcher.dispatch({
            "apiVersion": API_VERSION,
            "command": command,
            "payload": payload,
        })
        if response["ok"] is False:
            assert response["error"]["code"] != "UNSUPPORTED_COMMAND"


def test_dispatch_metadata_sd_sync_run(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    stats = SimpleNamespace(
        channels_upserted=1,
        programs_upserted=2,
        schedules_upserted=3,
        stale_schedules_pruned=4,
        ingest_run_id=9,
    )

    async def _stub_run_sd_sync(**_kwargs):
        return stats

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _stub_run_sd_sync)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
            "windowHours": 24,
        },
    })

    assert response["ok"] is True
    sd_stats = response["payload"]["stats"]
    assert sd_stats["channelsUpserted"] == 1
    assert sd_stats["programsUpserted"] == 2
    assert sd_stats["schedulesUpserted"] == 3
    assert sd_stats["staleSchedulesPruned"] == 4
    assert sd_stats["ingestRunId"] == 9
    assert sd_stats["fullRefresh"] is False


def test_dispatch_metadata_ota_sync_run(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    raw_calls: list[str] = []
    start_commands: list[str] = []
    selected_channels: list[str] = []
    stop_commands: list[str] = []

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(
        context.tvrecorder,
        "list_service_channel_name_map",
        lambda: {"0x233a:0x1047:0x1047": "BBC ONE East"},
    )
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda name: selected_channels.append(name))
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda command: (
            raw_calls.append(command),
            SimpleNamespace(stdout=""),
        )[1],
    )
    monkeypatch.setattr(
        context.dvbctrl,
        "start_command",
        lambda command: (
            start_commands.append(command),
            _MockPopen("<epg></epg>"),
        )[1],
    )

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            stop_commands.append(command)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    def _stub_ingest(_connection, _raw_text, source, channel_name_map):
        assert channel_name_map == {"0x233a:0x1047:0x1047": "BBC ONE East"}
        return SimpleNamespace(
            channels_upserted=3,
            programs_upserted=9,
            broadcasts_upserted=27,
            parsed_events=27,
            ingest_run_id=22,
            source=source,
        )

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.ingest_dvbstreamer_epg",
        _stub_ingest,
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "grabCommand": "epgdata",
            "channelName": "BBC TWO HD",
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is True
    assert selected_channels == ["BBC TWO HD"]
    assert raw_calls == ["epgcapstart"]
    assert start_commands == ["epgdata"]
    assert stop_commands == ["epgcapstop"]
    stats = response["payload"]["stats"]
    assert stats["channelsUpserted"] == 3
    assert stats["programsUpserted"] == 9
    assert stats["broadcastsUpserted"] == 27
    assert stats["parsedEvents"] == 27
    assert stats["ingestRunId"] == 22


def test_dispatch_metadata_ota_channel_names_backfill_run(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    with context.persistence.connection:
        context.persistence.connection.execute(
            """
            INSERT INTO epg_channels(source, source_channel_id, display_name)
            VALUES(?, ?, ?)
            """,
            ("dvbstreamer_ota", "0x233a:0x1047:0x1047", "service 0x233a:0x1047:0x1047"),
        )

    monkeypatch.setattr(
        context.tvrecorder,
        "list_service_channel_name_map",
        lambda: {"0x233a:0x1047:0x1047": "BBC ONE East"},
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.channel-names.backfill.run",
        "payload": {},
    })

    assert response["ok"] is True
    stats = response["payload"]["stats"]
    assert stats["servicesResolved"] == 1
    assert stats["rowsUpdated"] == 1
    assert stats["syntheticBefore"] == 1
    assert stats["syntheticAfter"] == 0
    assert stats["totalChannels"] == 1

    row = context.persistence.connection.execute(
        """
        SELECT display_name
        FROM epg_channels
        WHERE source = ? AND source_channel_id = ?
        """,
        ("dvbstreamer_ota", "0x233a:0x1047:0x1047"),
    ).fetchone()
    assert row is not None
    assert row[0] == "BBC ONE East"


def test_dispatch_metadata_ota_channel_names_backfill_maps_service_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    def _raise_map_error():
        raise RuntimeError("serviceinfo failed")

    monkeypatch.setattr(
        context.tvrecorder,
        "list_service_channel_name_map",
        _raise_map_error,
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.channel-names.backfill.run",
        "payload": {},
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "OTA_CHANNEL_MAP_FAILED"
    assert response["error"]["retryable"] is True


def test_dispatch_metadata_ota_sync_sends_sigint_and_epgcapstop(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    popen = _MockPopen("<epg></epg>")
    stop_commands: list[str] = []

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        context.dvbctrl,
        "start_command",
        lambda _command: popen,
    )

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            stop_commands.append(command)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.ingest_dvbstreamer_epg",
        lambda _connection, _raw_text, source, channel_name_map: SimpleNamespace(
            channels_upserted=1,
            programs_upserted=1,
            broadcasts_upserted=1,
            parsed_events=1,
            ingest_run_id=1,
            source=source,
        ),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "grabCommand": "epgdata",
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is True
    assert popen.signals_sent == [signal.SIGINT]
    assert popen.communicate_timeouts == [5.0]
    assert popen.killed is False
    assert stop_commands == ["epgcapstop"]


def test_dispatch_metadata_ota_sync_maps_epgcapstop_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        context.dvbctrl,
        "start_command",
        lambda _command: _MockPopen("<epg></epg>"),
    )

    class _FailingStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            del command
            raise RuntimeError("stop failed")

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _FailingStopClient)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "grabCommand": "epgdata",
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "OTA_GRAB_FAILED"
    assert response["error"]["retryable"] is True
    assert "failed to stop OTA capture" in response["error"]["message"]


def test_dispatch_metadata_ota_sync_retries_when_epgcap_already_started(
    monkeypatch,
) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    run_raw_calls: list[str] = []
    stop_commands: list[str] = []

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )

    def _run_raw(command: str):
        run_raw_calls.append(command)
        if len(run_raw_calls) == 1:
            raise RuntimeError(
                "dvbctrl command failed (returncode=255): Already started!"
            )
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(context.tvrecorder, "run_raw", _run_raw)
    monkeypatch.setattr(
        context.dvbctrl,
        "start_command",
        lambda _command: _MockPopen("<epg></epg>"),
    )

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            stop_commands.append(command)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.ingest_dvbstreamer_epg",
        lambda _connection, _raw_text, source, channel_name_map: SimpleNamespace(
            channels_upserted=1,
            programs_upserted=1,
            broadcasts_upserted=1,
            parsed_events=1,
            ingest_run_id=1,
            source=source,
        ),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "grabCommand": "epgdata",
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is True
    assert run_raw_calls == ["epgcapstart", "epgcapstart"]
    assert stop_commands == ["epgcapstop", "epgcapstop"]


def test_dispatch_metadata_ota_sync_maps_nonzero_epgdata_exit(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        context.dvbctrl,
        "start_command",
        lambda _command: _MockPopen(
            "",
            returncode=1,
            stderr_data="decoder failed",
        ),
    )

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            del command
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "grabCommand": "epgdata",
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "OTA_GRAB_FAILED"
    assert response["error"]["retryable"] is True
    assert "returncode=1" in response["error"]["message"]
    assert "decoder failed" in response["error"]["message"]


def test_dispatch_metadata_ota_sync_maps_grab_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    stop_commands: list[str] = []

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout="", stderr="", returncode=0),
    )

    def _raise_start_error(_command: str):
        raise RuntimeError("dvbctrl not found")

    monkeypatch.setattr(context.dvbctrl, "start_command", _raise_start_error)

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            stop_commands.append(command)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "grabCommand": "epgdata",
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "OTA_GRAB_FAILED"
    assert response["error"]["retryable"] is True
    assert stop_commands == ["epgcapstop"]


def test_dispatch_metadata_ota_sync_maps_ingest_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        context.dvbctrl,
        "start_command",
        lambda _command: _MockPopen("<epg></epg>"),
    )

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            del command
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)

    def _raise_ingest_error(_connection, _raw_text, source, channel_name_map):
        del channel_name_map
        del source
        raise ValueError("broken epg")

    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.ingest_dvbstreamer_epg",
        _raise_ingest_error,
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "grabCommand": "epgdata",
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "OTA_INGEST_FAILED"
    assert response["error"]["retryable"] is True


def test_dispatch_metadata_ota_sync_frontend_lock_timeout(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=False),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "frontendLockTimeoutSeconds": 0.01,
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "OTA_GRAB_FAILED"


def test_dispatch_metadata_ota_sync_starts_dvbstreamer_when_not_running(
    monkeypatch,
) -> None:
    context = _build_context()
    start_calls = {"count": 0}
    context.dvbstreamer = SimpleNamespace(
        health_check=lambda: SimpleNamespace(state=DvbStreamerState.STOPPED),
        start=lambda: start_calls.__setitem__("count", start_calls["count"] + 1),
    )
    control_calls = {"count": 0}

    def _run_command(_command: str):
        control_calls["count"] += 1
        if control_calls["count"] == 1:
            raise RuntimeError("control unavailable")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    context.dvbctrl = SimpleNamespace(
        executable_path="dvbctrl",
        host="localhost",
        adapter_index=0,
        timeout_seconds=10.0,
        transient_retry_count=2,
        transient_retry_delay_seconds=0.2,
        run_command=_run_command,
        start_command=lambda _command: _MockPopen("<epg></epg>"),
    )
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout=""),
    )

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            del command
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.ingest_dvbstreamer_epg",
        lambda _connection, _raw_text, source, channel_name_map: SimpleNamespace(
            channels_upserted=1,
            programs_upserted=1,
            broadcasts_upserted=1,
            parsed_events=1,
            ingest_run_id=1,
            source=source,
        ),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {},
    })

    assert response["ok"] is True
    assert start_calls["count"] == 1


def test_dispatch_metadata_ota_sync_does_not_start_manager_when_control_ready(
    monkeypatch,
) -> None:
    context = _build_context()
    start_calls = {"count": 0}
    context.dvbstreamer = SimpleNamespace(
        health_check=lambda: SimpleNamespace(state=DvbStreamerState.STOPPED),
        start=lambda: start_calls.__setitem__("count", start_calls["count"] + 1),
    )
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(context.tvrecorder, "resolve_service_name", lambda name: name)
    monkeypatch.setattr(context.tvrecorder, "select_service", lambda _name: None)
    monkeypatch.setattr(
        context.tvrecorder,
        "frontend_status",
        lambda: SimpleNamespace(locked=True),
    )
    monkeypatch.setattr(
        context.tvrecorder,
        "run_raw",
        lambda _command: SimpleNamespace(stdout=""),
    )

    class _StubStopClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def run_command(self, command: str):
            del command
            return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("ccatv.app.service_dispatcher.DvbCtrlClient", _StubStopClient)
    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.ingest_dvbstreamer_epg",
        lambda _connection, _raw_text, source, channel_name_map: SimpleNamespace(
            channels_upserted=1,
            programs_upserted=1,
            broadcasts_upserted=1,
            parsed_events=1,
            ingest_run_id=1,
            source=source,
        ),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is True
    assert start_calls["count"] == 0


def test_dispatch_metadata_ota_sync_non_owner_does_not_start_manager(
    monkeypatch,
) -> None:
    context = _build_context()
    context.settings = SimpleNamespace(
        database_path=":memory:",
        ota_epg_channel_name="BBC TWO HD",
        dvbstreamer_manage_process=False,
    )
    start_calls = {"count": 0}
    context.dvbstreamer = SimpleNamespace(
        health_check=lambda: SimpleNamespace(state=DvbStreamerState.STOPPED),
        start=lambda: start_calls.__setitem__("count", start_calls["count"] + 1),
    )

    def _run_command(_command: str):
        raise RuntimeError("control unavailable")

    context.dvbctrl = SimpleNamespace(
        executable_path="dvbctrl",
        host="localhost",
        adapter_index=0,
        timeout_seconds=10.0,
        transient_retry_count=2,
        transient_retry_delay_seconds=0.2,
        run_command=_run_command,
        start_command=lambda _command: _MockPopen("<epg></epg>"),
    )

    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.ota.sync.run",
        "payload": {
            "captureSeconds": 0.01,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "OTA_GRAB_FAILED"
    assert "non-owner" in response["error"]["message"]
    assert start_calls["count"] == 0


def test_dispatch_metadata_sd_sync_run_with_full_refresh(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)
    captured: dict[str, object] = {}

    async def _stub_run_sd_sync(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            channels_upserted=1,
            programs_upserted=1,
            schedules_upserted=1,
            stale_schedules_pruned=1,
            ingest_run_id=10,
        )

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _stub_run_sd_sync)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
            "clearExisting": True,
        },
    })

    assert response["ok"] is True
    assert captured["clear_existing"] is True
    assert response["payload"]["stats"]["fullRefresh"] is True


def test_dispatch_metadata_sd_sync_maps_auth_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    async def _raise_auth_error(**_kwargs):
        raise SchedulesDirectAuthenticationError("bad credentials")

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _raise_auth_error)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "SD_AUTH_FAILED"
    assert response["error"]["retryable"] is False


def test_dispatch_metadata_sd_sync_maps_rate_limit_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    async def _raise_rate_limit(**_kwargs):
        raise SchedulesDirectRateLimitError("too many requests", retry_after_seconds=42)

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _raise_rate_limit)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "SD_RATE_LIMITED"
    assert response["error"]["retryable"] is True
    assert response["error"]["details"]["retryAfterSeconds"] == 42


def test_dispatch_metadata_sd_sync_maps_timeout(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    async def _slow_sync(**_kwargs):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _slow_sync)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
            "timeoutSeconds": 0.001,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "SD_SYNC_TIMEOUT"
    assert response["error"]["retryable"] is True
    assert response["error"]["details"]["timeoutSeconds"] == 0.001


def test_dispatch_metadata_sd_sync_maps_upstream_transport_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    async def _raise_transport_error(**_kwargs):
        raise SchedulesDirectTransportError("network unavailable")

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _raise_transport_error)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "SD_UPSTREAM_ERROR"
    assert response["error"]["retryable"] is True
    assert response["error"]["details"]["errorType"] == "transport"


def test_dispatch_metadata_sd_sync_maps_upstream_api_error(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    async def _raise_api_error(**_kwargs):
        raise SchedulesDirectApiError(7020, "upstream unavailable")

    monkeypatch.setattr(dispatcher, "_run_sd_sync", _raise_api_error)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "SD_UPSTREAM_ERROR"
    assert response["error"]["retryable"] is True
    assert response["error"]["details"]["errorType"] == "api"
    assert response["error"]["details"]["providerCode"] == 7020


def test_dispatch_returns_cancelled_error_when_stop_requested() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context, should_stop=lambda: True)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "recording.worker.cycle.run",
        "payload": {
            "outputDirectory": "/tmp",
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "COMMAND_CANCELLED"
    assert response["error"]["retryable"] is True


def test_dispatch_metadata_sd_sync_rejects_non_positive_timeout() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "metadata.sd.sync.run",
        "payload": {
            "lineupId": "UK-TEST",
            "timeoutSeconds": 0,
        },
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_run_coroutine_blocking_uses_thread_when_loop_running(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    async def _sample_coroutine():
        return "ok"

    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.asyncio.get_running_loop",
        lambda: object(),
    )
    result = dispatcher._run_coroutine_blocking(
        _sample_coroutine(), timeout_seconds=1.0
    )

    assert result == "ok"


def test_dispatch_generic_service_command_error_surfaces(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    monkeypatch.setattr(
        dispatcher,
        "_dispatch_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ServiceCommandError(code="TEST", message="boom", retryable=False)
        ),
    )

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "TEST"


def test_dispatch_invalid_request_returns_validation_error() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": "invalid",
        "command": "service.health.get",
        "payload": {},
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_dispatch_unsupported_command_returns_error() -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

    response = dispatcher.dispatch({
        "apiVersion": API_VERSION,
        "command": "unknown.command",
        "payload": {},
    })

    assert response["ok"] is False
    assert response["error"]["code"] == "UNSUPPORTED_COMMAND"


def test_dispatch_recording_worker_cycle_run_serializes_concurrent_calls(
    monkeypatch,
) -> None:
    context = _build_context()
    lock = threading.Lock()
    dispatcher = ServiceCommandDispatcher(context, worker_cycle_lock=lock)

    hold_first_cycle = threading.Event()
    first_cycle_started = threading.Event()

    class _BlockingWorker:
        def run_cycle(self):
            first_cycle_started.set()
            hold_first_cycle.wait(timeout=1.0)
            return []

    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.create_scheduler_worker",
        lambda *_args, **_kwargs: _BlockingWorker(),
    )

    thread_results: list[dict[str, object]] = []

    def _run_dispatch() -> None:
        thread_results.append(
            dispatcher.dispatch({
                "apiVersion": API_VERSION,
                "command": "recording.worker.cycle.run",
                "payload": {
                    "outputDirectory": "/tmp",
                },
            })
        )

    first = threading.Thread(target=_run_dispatch)
    second = threading.Thread(target=_run_dispatch)

    first.start()
    assert first_cycle_started.wait(timeout=1.0) is True
    second.start()

    time.sleep(0.05)
    assert len(thread_results) == 0

    hold_first_cycle.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert len(thread_results) == 2
    assert all(result["ok"] is True for result in thread_results)


def test_run_coroutine_blocking_stops_when_shutdown_requested(monkeypatch) -> None:
    stop_requested = {"value": False}
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(
        context,
        should_stop=lambda: stop_requested["value"],
    )

    async def _slow_coroutine():
        await asyncio.sleep(0.2)
        return "done"

    monkeypatch.setattr(
        "ccatv.app.service_dispatcher.asyncio.get_running_loop",
        lambda: object(),
    )

    def _trigger_stop() -> None:
        time.sleep(0.05)
        stop_requested["value"] = True

    stopper = threading.Thread(target=_trigger_stop)
    stopper.start()
    try:
        with pytest.raises(ServiceCommandError) as exc:
            dispatcher._run_coroutine_blocking(_slow_coroutine(), timeout_seconds=5.0)
    finally:
        stopper.join(timeout=1.0)

    assert exc.value.code == "COMMAND_CANCELLED"
    assert "shutdown" in exc.value.message
