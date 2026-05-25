from __future__ import annotations

import json
import logging
from http import client as http_client
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
    run_http_server,
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


def test_handle_ipc_request_maps_dispatcher_exception_to_error() -> None:
    class _StubDispatcher:
        def dispatch(self, _request):
            raise RuntimeError("boom")

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "explode-1",
        "payload": {},
    }

    response_bytes = _handle_ipc_request(
        json.dumps(request).encode("utf-8"),
        _StubDispatcher(),
    )
    response = json.loads(response_bytes.decode("utf-8"))

    assert response["ok"] is False
    assert response["requestId"] == "explode-1"
    assert response["error"]["code"] == "INTERNAL_ERROR"
    assert "dispatcher failure" in response["error"]["message"]


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


def test_main_rejects_http_without_auth_token() -> None:
    with pytest.raises(SystemExit):
        main([
            "--http-bind-host",
            "127.0.0.1",
        ])


def test_main_rejects_http_and_socket_together() -> None:
    with pytest.raises(SystemExit):
        main([
            "--socket-path",
            "/tmp/ccatv.sock",
            "--http-bind-host",
            "127.0.0.1",
            "--http-auth-token",
            "test-token",
        ])


def test_run_http_server_handles_authenticated_command_request(
    monkeypatch, tmp_path: Path
) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http"),
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
                    "echoCommand": request.get("command"),
                },
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind(("127.0.0.1", 0))
    http_port = port_socket.getsockname()[1]
    port_socket.close()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": http_port,
            "auth_token": "test-token",
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    time.sleep(0.05)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    request_payload = {
        "apiVersion": "v1alpha1",
        "command": "service.info.get",
        "requestId": "http-1",
        "payload": {},
    }
    connection.request(
        "POST",
        "/api/v1/command",
        body=json.dumps(request_payload),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 200
    assert decoded["ok"] is True
    assert decoded["requestId"] == "http-1"
    assert decoded["payload"]["echoCommand"] == "service.info.get"


def test_run_http_server_rejects_missing_bearer_token(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.auth"),
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

    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind(("127.0.0.1", 0))
    http_port = port_socket.getsockname()[1]
    port_socket.close()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": http_port,
            "auth_token": "test-token",
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    time.sleep(0.05)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request(
        "POST",
        "/api/v1/command",
        body=json.dumps(
            {
                "apiVersion": "v1alpha1",
                "command": "service.health.get",
                "requestId": "http-unauth",
                "payload": {},
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 401
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_run_http_server_handles_authenticated_health_get(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.health"),
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
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind(("127.0.0.1", 0))
    http_port = port_socket.getsockname()[1]
    port_socket.close()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": http_port,
            "auth_token": "test-token",
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    time.sleep(0.05)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request(
        "GET",
        "/health",
        headers={"Authorization": "Bearer test-token"},
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 200
    assert decoded["ok"] is True
    assert decoded["payload"]["status"] == "ok"


def test_run_http_server_rejects_empty_request_body(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.empty"),
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
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind(("127.0.0.1", 0))
    http_port = port_socket.getsockname()[1]
    port_socket.close()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": http_port,
            "auth_token": "test-token",
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    time.sleep(0.05)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request(
        "POST",
        "/api/v1/command",
        body="",
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
            "Content-Length": "0",
        },
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 400
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "VALIDATION_ERROR"
    assert decoded["error"]["message"] == "request body is empty"


def test_run_http_server_rejects_negative_content_length(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.neg.length"),
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
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind(("127.0.0.1", 0))
    http_port = port_socket.getsockname()[1]
    port_socket.close()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": http_port,
            "auth_token": "test-token",
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    time.sleep(0.05)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(2.0)
        raw.connect(("127.0.0.1", http_port))
        raw.sendall(
            b"POST /api/v1/command HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: -5\r\n"
            b"Connection: close\r\n\r\n"
        )
        chunks: list[bytes] = []
        while True:
            block = raw.recv(4096)
            if not block:
                break
            chunks.append(block)
        response_raw = b"".join(chunks)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert b"400" in response_raw
    assert b"Content-Length must be >= 0" in response_raw


def test_run_http_server_rejects_non_integer_content_length(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.bad.length"),
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
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_socket.bind(("127.0.0.1", 0))
    http_port = port_socket.getsockname()[1]
    port_socket.close()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": http_port,
            "auth_token": "test-token",
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    time.sleep(0.05)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(2.0)
        raw.connect(("127.0.0.1", http_port))
        raw.sendall(
            b"POST /api/v1/command HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: abc\r\n"
            b"Connection: close\r\n\r\n"
        )
        chunks: list[bytes] = []
        while True:
            block = raw.recv(4096)
            if not block:
                break
            chunks.append(block)
        response_raw = b"".join(chunks)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert b"400" in response_raw
    assert b"Content-Length must be an integer" in response_raw


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


def test_run_ipc_server_rejects_whitespace_only_request(tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc.empty.whitespace"),
        worker_cycle_lock=StubLock(),
    )
    socket_path = tmp_path / "ccatv-empty-whitespace.sock"

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
        client.sendall(b"   \n   ")
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
