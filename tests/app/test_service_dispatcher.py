from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from ccatv.app.service_dispatcher import (
    API_VERSION,
    ServiceCommandDispatcher,
    ServiceCommandError,
)
from ccatv.metadata.schedules_direct_contract import (
    SchedulesDirectAuthenticationError,
    SchedulesDirectRateLimitError,
)
from ccatv.tvrecorder.orchestrator import OrchestratorResult


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
    assert payload["database"]["readable"] is True
    assert payload["database"]["writable"] is True
    assert payload["database"]["error"] is None
    assert payload["database"]["failedAt"] is None
    assert payload["recorder"]["workerEnabled"] is True


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
