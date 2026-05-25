from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccatv.app.service_client import (
    HttpServiceClient,
    LocalInProcessServiceClient,
    ServiceClientError,
    UnixSocketServiceClient,
    create_service_client,
)
from ccatv.app.service_daemon import run_http_server, run_ipc_server
from ccatv.storage import PersistenceStore, apply_migrations
from ccatv.tvrecorder.service import TvRecorderService


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


def test_local_in_process_service_client_executes_command() -> None:
    client = LocalInProcessServiceClient(context=_build_context())

    payload = client.execute("service.info.get", {})

    assert payload["apiVersion"] == "v1alpha1"
    assert payload["appName"] == "ccatv"


def test_local_in_process_service_client_maps_service_errors() -> None:
    client = LocalInProcessServiceClient(context=_build_context())

    with pytest.raises(ServiceClientError) as exc_info:
        client.execute("unknown.command", {})

    assert exc_info.value.code == "UNSUPPORTED_COMMAND"


def test_local_in_process_service_client_close_delegates_shutdown(monkeypatch) -> None:
    client = LocalInProcessServiceClient(context=_build_context())
    called = {"count": 0}

    def _close_stub(_context) -> None:
        called["count"] += 1

    monkeypatch.setattr("ccatv.app.service_client.close_app_context", _close_stub)

    client.close()

    assert called["count"] == 1


# ---------------------------------------------------------------------------
# helpers shared by Unix socket tests
# ---------------------------------------------------------------------------

def _start_ipc_server(context, socket_path: Path, *, max_requests: int = 1) -> threading.Thread:
    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={"context": context, "socket_path": str(socket_path), "max_requests": max_requests},
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if socket_path.exists():
            return thread
        time.sleep(0.01)
    raise AssertionError("socket did not become ready in time")


def _start_http_server(
    context,
    *,
    auth_token: str = "test-token",
    max_requests: int = 1,
) -> tuple[threading.Thread, int]:
    listened_port: dict[str, int] = {}
    ready = threading.Event()

    def _on_listening(port: int) -> None:
        listened_port["value"] = port
        ready.set()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": 0,
            "auth_token": auth_token,
            "max_requests": max_requests,
            "on_listening": _on_listening,
        },
        daemon=True,
    )
    thread.start()

    if not ready.wait(timeout=2.0):
        raise AssertionError("HTTP server did not become ready in time")
    return thread, listened_port["value"]


# ---------------------------------------------------------------------------
# UnixSocketServiceClient
# ---------------------------------------------------------------------------

def test_unix_socket_service_client_executes_command(tmp_path: Path) -> None:
    context = _build_context()
    socket_path = tmp_path / "ccatv.sock"
    thread = _start_ipc_server(context, socket_path)

    client = UnixSocketServiceClient(socket_path=str(socket_path))
    payload = client.execute("service.info.get", {})

    thread.join(timeout=2.0)
    assert payload["apiVersion"] == "v1alpha1"
    assert payload["appName"] == "ccatv"


def test_unix_socket_service_client_maps_service_errors(tmp_path: Path) -> None:
    context = _build_context()
    socket_path = tmp_path / "ccatv.sock"
    thread = _start_ipc_server(context, socket_path)

    client = UnixSocketServiceClient(socket_path=str(socket_path))
    with pytest.raises(ServiceClientError) as exc_info:
        client.execute("unknown.command", {})

    thread.join(timeout=2.0)
    assert exc_info.value.code == "UNSUPPORTED_COMMAND"


def test_unix_socket_service_client_raises_transport_error_on_missing_socket() -> None:
    client = UnixSocketServiceClient(socket_path="/tmp/_ccatv_test_nonexistent.sock")

    with pytest.raises(ServiceClientError) as exc_info:
        client.execute("service.info.get", {})

    assert exc_info.value.code == "TRANSPORT_ERROR"
    assert exc_info.value.retryable is True


def test_unix_socket_service_client_close_is_noop() -> None:
    client = UnixSocketServiceClient(socket_path="/tmp/_ccatv_test.sock")
    client.close()  # must not raise


# ---------------------------------------------------------------------------
# create_service_client factory
# ---------------------------------------------------------------------------

def test_http_service_client_executes_command() -> None:
    context = _build_context()
    thread, http_port = _start_http_server(context)

    client = HttpServiceClient(
        host="127.0.0.1",
        port=http_port,
        auth_token="test-token",
    )
    payload = client.execute("service.info.get", {})

    thread.join(timeout=2.0)
    assert payload["apiVersion"] == "v1alpha1"
    assert payload["appName"] == "ccatv"


def test_http_service_client_maps_service_errors() -> None:
    context = _build_context()
    thread, http_port = _start_http_server(context)

    client = HttpServiceClient(
        host="127.0.0.1",
        port=http_port,
        auth_token="test-token",
    )
    with pytest.raises(ServiceClientError) as exc_info:
        client.execute("unknown.command", {})

    thread.join(timeout=2.0)
    assert exc_info.value.code == "UNSUPPORTED_COMMAND"


def test_http_service_client_rejects_invalid_token() -> None:
    context = _build_context()
    thread, http_port = _start_http_server(context)

    client = HttpServiceClient(
        host="127.0.0.1",
        port=http_port,
        auth_token="wrong-token",
    )
    with pytest.raises(ServiceClientError) as exc_info:
        client.execute("service.info.get", {})

    thread.join(timeout=2.0)
    assert exc_info.value.code == "AUTHENTICATION_REQUIRED"


def test_http_service_client_reports_non_json_status_context(monkeypatch) -> None:
    class _StubHttpResponse:
        status = 503

        def read(self) -> bytes:
            return b"<html>upstream failure</html>"

    class _StubHttpConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]):
            assert method == "POST"
            assert path == "/api/v1/command"
            assert headers["Authorization"] == "Bearer test-token"

        def getresponse(self):
            return _StubHttpResponse()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "ccatv.app.service_client._http_client.HTTPConnection",
        _StubHttpConnection,
    )

    client = HttpServiceClient(
        host="127.0.0.1",
        port=8787,
        auth_token="test-token",
    )
    with pytest.raises(ServiceClientError) as exc_info:
        client.execute("service.info.get", {})

    assert exc_info.value.code == "TRANSPORT_ERROR"
    assert exc_info.value.retryable is True
    assert "HTTP 503 response not valid JSON" in exc_info.value.message

def test_create_service_client_returns_unix_socket_client_when_path_given() -> None:
    client = create_service_client(socket_path="/tmp/_ccatv_test.sock")
    assert isinstance(client, UnixSocketServiceClient)
    assert client.socket_path == "/tmp/_ccatv_test.sock"


def test_create_service_client_returns_local_client_when_no_path(monkeypatch) -> None:
    monkeypatch.setattr("ccatv.app.service_client.bootstrap_app", _build_context)

    client = create_service_client()
    assert isinstance(client, LocalInProcessServiceClient)


def test_create_service_client_returns_http_client_when_host_given() -> None:
    client = create_service_client(
        http_host="127.0.0.1",
        http_port=8787,
        http_auth_token="test-token",
    )
    assert isinstance(client, HttpServiceClient)
    assert client.host == "127.0.0.1"
    assert client.port == 8787


def test_create_service_client_requires_http_auth_token() -> None:
    with pytest.raises(ValueError):
        create_service_client(http_host="127.0.0.1")
