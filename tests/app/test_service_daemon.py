from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccatv.app.service_daemon import (
    IPC_MAX_REQUEST_BYTES,
    _handle_ipc_request,
    main,
    run_ipc_server,
    run_service_daemon,
)
from ccatv.tvrecorder.orchestrator import OrchestratorResult


@dataclass(slots=True)
class StubWorker:
    ran_cycle: bool = False
    cycle_count: int = 0
    fail_first_cycle: bool = False
    cycle_results: list[OrchestratorResult] = field(default_factory=list)

    def run_cycle(self):
        self.ran_cycle = True
        self.cycle_count += 1
        if self.fail_first_cycle and self.cycle_count == 1:
            raise RuntimeError("cycle failed")
        return self.cycle_results


@dataclass(slots=True)
class StubContext:
    logger: logging.Logger


@dataclass(slots=True)
class StubLock:
    entered: int = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def test_run_service_daemon_once(monkeypatch) -> None:
    worker = StubWorker(
        cycle_results=[
            OrchestratorResult(
                job_id=12,
                scheduler_state="completed",
                recording_id=91,
                recording_state="ready",
                error=None,
            )
        ]
    )
    context = StubContext(logger=logging.getLogger("test.daemon.once"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=True,
    )

    assert result == 0
    assert worker.ran_cycle is True
    assert worker.cycle_count == 1


def test_run_service_daemon_once_returns_error_on_cycle_failure(monkeypatch) -> None:
    worker = StubWorker(fail_first_cycle=True)
    context = StubContext(logger=logging.getLogger("test.daemon.once.error"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=True,
    )

    assert result == 1
    assert worker.ran_cycle is True
    assert worker.cycle_count == 1


def test_run_service_daemon_uses_context_worker_lock(monkeypatch) -> None:
    worker = StubWorker()
    lock = StubLock()
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.lock"),
        worker_cycle_lock=lock,
    )

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=True,
    )

    assert result == 0
    assert worker.cycle_count == 1
    assert lock.entered == 1


def test_run_service_daemon_forever_runs_cycles(monkeypatch) -> None:
    worker = StubWorker()
    context = StubContext(logger=logging.getLogger("test.daemon.forever"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    stop_after_first_cycle = {"value": False}

    def _should_stop() -> bool:
        return stop_after_first_cycle["value"]

    def _fake_sleep(_seconds: float) -> None:
        stop_after_first_cycle["value"] = True

    monkeypatch.setattr("ccatv.app.service_daemon.time.sleep", _fake_sleep)

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=False,
        should_stop=_should_stop,
    )

    assert result == 0
    assert worker.cycle_count >= 1


def test_run_service_daemon_forever_continues_after_cycle_error(monkeypatch) -> None:
    worker = StubWorker(fail_first_cycle=True)
    context = StubContext(logger=logging.getLogger("test.daemon.forever.error"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    sleep_calls = {"count": 0}

    def _should_stop() -> bool:
        return sleep_calls["count"] >= 1

    def _fake_sleep(_seconds: float) -> None:
        sleep_calls["count"] += 1

    monkeypatch.setattr("ccatv.app.service_daemon.time.sleep", _fake_sleep)

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=False,
        should_stop=_should_stop,
    )

    assert result == 0
    assert worker.cycle_count >= 1


def test_main_dispatch_command_json(monkeypatch, capsys) -> None:
    context = SimpleNamespace(
        settings=SimpleNamespace(database_path=":memory:"),
        persistence=SimpleNamespace(connection=SimpleNamespace(close=lambda: None)),
        dvbstreamer=SimpleNamespace(stop=lambda force_kill=True: None),
        logger=logging.getLogger("test.daemon.dispatch"),
    )

    class _StubDispatcher:
        def __init__(
            self,
            _context,
            *,
            should_stop=None,
            worker_cycle_lock=None,
        ) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr("ccatv.app.service_daemon.bootstrap_app", lambda: context)
    monkeypatch.setattr(
        "ccatv.app.service_daemon.close_app_context", lambda _context: None
    )
    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "abc-123",
        "payload": {},
    }
    result = main(["--dispatch-command-json", json.dumps(request)])

    assert result == 0
    output = capsys.readouterr().out.strip()
    response = json.loads(output)
    assert response["ok"] is True
    assert response["requestId"] == "abc-123"


def test_main_dispatch_command_json_passes_stop_predicate(monkeypatch, capsys) -> None:
    context = SimpleNamespace(
        settings=SimpleNamespace(database_path=":memory:"),
        persistence=SimpleNamespace(connection=SimpleNamespace(close=lambda: None)),
        dvbstreamer=SimpleNamespace(stop=lambda force_kill=True: None),
        logger=logging.getLogger("test.daemon.dispatch.stop"),
        worker_cycle_lock=StubLock(),
    )
    captured = {}

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            captured["should_stop"] = should_stop
            captured["worker_cycle_lock"] = worker_cycle_lock

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr("ccatv.app.service_daemon.bootstrap_app", lambda: context)
    monkeypatch.setattr(
        "ccatv.app.service_daemon.close_app_context", lambda _context: None
    )
    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "stop-1",
        "payload": {},
    }
    result = main(["--dispatch-command-json", json.dumps(request)])

    assert result == 0
    output = capsys.readouterr().out.strip()
    response = json.loads(output)
    assert response["ok"] is True
    assert callable(captured["should_stop"])
    assert captured["worker_cycle_lock"] is context.worker_cycle_lock


def test_handle_ipc_request_rejects_invalid_json() -> None:
    class _StubDispatcher:
        def dispatch(self, _request):
            return {"ok": True}

    response_bytes = _handle_ipc_request(b"{", _StubDispatcher())
    response = json.loads(response_bytes.decode("utf-8"))

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_run_ipc_server_handles_health_request(monkeypatch, tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {
                    "status": "ok",
                },
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    socket_path = tmp_path / "ccatv.sock"

    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={
            "context": context,
            "socket_path": str(socket_path),
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("socket did not become ready")

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "ipc-1",
        "payload": {},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(json.dumps(request).encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        response_raw = client.recv(4096)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    response = json.loads(response_raw.decode("utf-8"))
    assert response["ok"] is True
    assert response["requestId"] == "ipc-1"
    assert response["payload"]["status"] == "ok"


def test_main_rejects_socket_and_dispatch_json_together() -> None:
    with pytest.raises(SystemExit):
        main([
            "--socket-path",
            "/tmp/ccatv.sock",
            "--dispatch-command-json",
            "{}",
        ])


def test_run_ipc_server_rejects_empty_request(tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc.empty"),
        worker_cycle_lock=StubLock(),
    )
    socket_path = tmp_path / "ccatv-empty.sock"

    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={
            "context": context,
            "socket_path": str(socket_path),
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("socket did not become ready")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(b"\n")
        client.shutdown(socket.SHUT_WR)
        response_raw = client.recv(4096)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    response = json.loads(response_raw.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"
    assert response["error"]["message"] == "request body is empty"


def test_run_ipc_server_rejects_oversized_request(tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc.oversized"),
        worker_cycle_lock=StubLock(),
    )
    socket_path = tmp_path / "ccatv-oversized.sock"

    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={
            "context": context,
            "socket_path": str(socket_path),
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("socket did not become ready")

    oversized_payload = b"x" * (IPC_MAX_REQUEST_BYTES + 1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(oversized_payload)
        client.shutdown(socket.SHUT_WR)
        response_raw = client.recv(4096)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    response = json.loads(response_raw.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"
    assert response["error"]["message"] == "request too large"
    assert response["error"]["details"]["maxBytes"] == IPC_MAX_REQUEST_BYTES
