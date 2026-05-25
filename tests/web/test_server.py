from __future__ import annotations

import pytest

from ccatv.web.server import main


def test_main_requires_service_auth_token() -> None:
    with pytest.raises(SystemExit):
        main([
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            "5001",
            "--service-host",
            "127.0.0.1",
            "--service-port",
            "8787",
        ])


def test_main_accepts_web_auth_token_from_environment(monkeypatch) -> None:
    captured = {}

    class _StubApp:
        def run(self, host: str, port: int, debug: bool) -> None:
            captured["run"] = {
                "host": host,
                "port": port,
                "debug": debug,
            }

    def _create_app_stub(**kwargs):
        captured["kwargs"] = kwargs
        return _StubApp()

    monkeypatch.setenv("CCATV_SERVICE_AUTH_TOKEN", "service-token")
    monkeypatch.setenv("CCATV_WEB_AUTH_TOKEN", "web-token")
    monkeypatch.setattr("ccatv.web.server.create_app", _create_app_stub)

    result = main([
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        "5001",
        "--service-host",
        "127.0.0.1",
        "--service-port",
        "8787",
    ])

    assert result == 0
    assert captured["kwargs"]["service_host"] == "127.0.0.1"
    assert captured["kwargs"]["service_port"] == 8787
    assert captured["kwargs"]["service_auth_token"] == "service-token"
    assert captured["kwargs"]["web_auth_token"] == "web-token"
    assert captured["run"] == {"host": "127.0.0.1", "port": 5001, "debug": False}
