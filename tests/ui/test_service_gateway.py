from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccatv.app.service_daemon import run_ipc_server
from ccatv.storage import PersistenceStore, apply_migrations
from ccatv.tvrecorder.service import TvRecorderService
from ccatv.ui.service_gateway import GtkServiceGateway, create_gtk_service_gateway


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


def test_create_gtk_service_gateway_uses_socket_client() -> None:
    gateway = create_gtk_service_gateway(socket_path="/tmp/ccatv.sock")

    assert isinstance(gateway, GtkServiceGateway)
    assert gateway.socket_path == "/tmp/ccatv.sock"


def test_gateway_health_command_uses_ipc_transport(tmp_path: Path) -> None:
    context = _build_context()
    socket_path = tmp_path / "ccatv.sock"
    thread = _start_ipc_server(context, socket_path)

    gateway = create_gtk_service_gateway(socket_path=str(socket_path))
    payload = gateway.get_service_health()

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert payload["status"] in {"ok", "degraded"}


def test_gateway_create_schedule_forwards_expected_payload() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def _execute(command: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((command, payload))
        return {"job": {"id": 123, "state": "scheduled"}}

    gateway = GtkServiceGateway(
        socket_path="/tmp/ccatv.sock",
        _client_factory=lambda: SimpleNamespace(
            execute=_execute,
            close=lambda: None,
        ),
    )

    payload = gateway.create_schedule(
        channel_name="BBC ONE",
        start_at_utc="2026-05-25T20:00:00Z",
        duration_seconds=1800,
    )

    assert payload["job"]["id"] == 123
    assert calls == [
        (
            "recording.schedule.create",
            {
                "channelName": "BBC ONE",
                "startAtUtc": "2026-05-25T20:00:00Z",
                "durationSeconds": 1800,
            },
        )
    ]


def test_gateway_create_schedule_validates_inputs() -> None:
    gateway = GtkServiceGateway(
        socket_path="/tmp/ccatv.sock",
        _client_factory=lambda: SimpleNamespace(
            execute=lambda *_args, **_kwargs: {},
            close=lambda: None,
        ),
    )

    with pytest.raises(ValueError):
        gateway.create_schedule(
            channel_name="",
            start_at_utc="2026-05-25T20:00:00Z",
            duration_seconds=1200,
        )

    with pytest.raises(ValueError):
        gateway.create_schedule(
            channel_name="BBC ONE",
            start_at_utc="",
            duration_seconds=1200,
        )

    with pytest.raises(ValueError):
        gateway.create_schedule(
            channel_name="BBC ONE",
            start_at_utc="2026-05-25T20:00:00Z",
            duration_seconds=0,
        )


def test_gateway_list_schedules_validates_state() -> None:
    gateway = GtkServiceGateway(
        socket_path="/tmp/ccatv.sock",
        _client_factory=lambda: SimpleNamespace(
            execute=lambda *_args, **_kwargs: {},
            close=lambda: None,
        ),
    )

    with pytest.raises(ValueError):
        gateway.list_schedules(state="  ")
