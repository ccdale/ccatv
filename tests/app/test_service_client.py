from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from ccatv.app.service_client import LocalInProcessServiceClient, ServiceClientError
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
