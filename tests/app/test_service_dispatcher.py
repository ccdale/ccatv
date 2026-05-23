from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

from ccatv.app.service_dispatcher import API_VERSION, ServiceCommandDispatcher
from ccatv.tvrecorder.orchestrator import OrchestratorResult


@dataclass(slots=True)
class StubWorker:
    results: list[OrchestratorResult]

    def run_cycle(self):
        return self.results


def _build_context() -> SimpleNamespace:
    connection = sqlite3.connect(":memory:")
    return SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        persistence=SimpleNamespace(connection=connection),
        settings=SimpleNamespace(database_path=":memory:"),
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
    assert payload["recorder"]["workerEnabled"] is True


def test_dispatch_recording_worker_cycle_run(monkeypatch) -> None:
    context = _build_context()
    dispatcher = ServiceCommandDispatcher(context)

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
