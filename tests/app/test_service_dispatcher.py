from __future__ import annotations

import asyncio
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


def _build_context() -> SimpleNamespace:
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    persistence = PersistenceStore(connection=connection)
    tvrecorder = TvRecorderService(
        dvbctrl=SimpleNamespace(),
        persistence=persistence,
    )
    return SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        persistence=persistence,
        settings=SimpleNamespace(database_path=":memory:"),
        tvrecorder=tvrecorder,
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
            "startAtUtc": "2026-05-25T21:00:00Z",
            "durationSeconds": 1800,
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
        INSERT INTO epg_programs(source, source_program_id, title, description_long)
        VALUES(?, ?, ?, ?)
        """,
        ("schedules_direct", "p1", "Newsnight", "Late-night news and analysis"),
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
    assert response["payload"]["channels"] == [
        {
            "name": "BBC TWO HD",
            "callsign": "BBC2",
            "logicalChannelNumber": "2",
            "source": "dvbstreamer_ota",
            "sourceChannelId": "200",
            "dvbstreamerServiceName": None,
            "favoriteChannel": False,
        },
        {
            "name": "BBC FOUR",
            "callsign": "BBC4",
            "logicalChannelNumber": "9",
            "source": "schedules_direct",
            "sourceChannelId": "300",
            "dvbstreamerServiceName": None,
            "favoriteChannel": False,
        },
    ]


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


def test_dispatch_metadata_channels_dvbservices_list_returns_sorted_unique_services() -> None:
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
            "password": "secret",
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
            "password": "",
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
        ("recording.schedule.list", {}),
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
                "password": "secret",
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
