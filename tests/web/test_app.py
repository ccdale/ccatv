from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ccatv.app.service_client import ServiceClientError
from ccatv.web.app import create_app


@dataclass(slots=True)
class _StubServiceClient:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    fail_with: ServiceClientError | None = None

    def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((command, payload))
        if self.fail_with is not None:
            raise self.fail_with

        if command == "service.health.get":
            return {"status": "ok"}
        if command == "service.info.get":
            return {"appName": "ccatv", "apiVersion": "v1alpha1"}
        if command == "metadata.channels.list":
            return {
                "channels": [
                    {
                        "name": "BBC TWO HD",
                        "callsign": "BBC2",
                        "logicalChannelNumber": "2",
                        "source": "dvbstreamer_ota",
                        "sourceChannelId": "200",
                    }
                ]
            }
        if command == "recording.schedule.list":
            return {"jobs": []}
        if command == "recording.schedule.create":
            return {"job": {"id": 1, "state": "scheduled"}}
        if command == "metadata.guide.list":
            return {
                "channel": "BBC TWO HD",
                "window": {
                    "startAtUtc": "2026-05-25T20:00:00Z",
                    "endAtUtc": "2026-05-25T22:00:00Z",
                },
                "programs": [],
            }
        return {}

    def close(self) -> None:
        return None


def test_health_route_forwards_command(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [("service.health.get", {})]


def test_index_route_serves_browser_ui(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
    )
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Sign in" in body
    assert "Record programme" in body
    assert "Scheduled recordings" in body
    assert stub.calls == []


def test_session_status_reports_not_authenticated_when_token_required(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
    )
    client = app.test_client()

    response = client.get("/auth/session")

    assert response.status_code == 200
    payload = response.get_json()["payload"]
    assert payload["authRequired"] is True
    assert payload["authenticated"] is False


def test_session_login_and_cookie_allows_api_without_header(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
        session_secret="session-secret",
    )
    client = app.test_client()

    login_response = client.post("/auth/session", json={"token": "web-token"})
    assert login_response.status_code == 200
    assert login_response.get_json()["payload"]["authenticated"] is True

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [("service.health.get", {})]


def test_session_login_rejects_invalid_token(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
    )
    client = app.test_client()

    response = client.post("/auth/session", json={"token": "wrong-token"})

    assert response.status_code == 401
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_session_logout_revokes_cookie_auth(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
        session_secret="session-secret",
    )
    client = app.test_client()

    client.post("/auth/session", json={"token": "web-token"})
    first = client.get("/api/health")
    assert first.status_code == 200

    logout_response = client.delete("/auth/session")
    assert logout_response.status_code == 200

    second = client.get("/api/health")
    assert second.status_code == 401


def test_channel_list_route_forwards_command(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.get("/api/channels")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["payload"]["channels"][0]["name"] == "BBC TWO HD"
    assert stub.calls == [("metadata.channels.list", {})]


def test_schedule_list_forwards_state_query(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.get("/api/schedules?state=scheduled")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [(
        "recording.schedule.list",
        {"state": "scheduled"},
    )]


def test_schedule_create_route_validates_json_object(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.post("/api/schedules", json=["not", "an", "object"])

    assert response.status_code == 400
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == []


def test_schedule_create_maps_service_error(monkeypatch) -> None:
    stub = _StubServiceClient(
        fail_with=ServiceClientError(
            code="VALIDATION_ERROR",
            message="durationSeconds must be an integer greater than 0",
        )
    )
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.post(
        "/api/schedules",
        json={
            "channelName": "BBC ONE",
            "startAtUtc": "2026-05-25T20:00:00Z",
            "durationSeconds": 0,
        },
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == [(
        "recording.schedule.create",
        {
            "channelName": "BBC ONE",
            "startAtUtc": "2026-05-25T20:00:00Z",
            "durationSeconds": 0,
        },
    )]


def test_guide_list_forwards_query_params(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.get(
        "/api/guide?channel=BBC%20TWO%20HD&startAtUtc=2026-05-25T20:00:00Z&windowHours=2"
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [(
        "metadata.guide.list",
        {
            "channel": "BBC TWO HD",
            "startAtUtc": "2026-05-25T20:00:00Z",
            "windowHours": 2.0,
        },
    )]


def test_guide_list_requires_channel_query_param(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.get("/api/guide")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == []


def test_guide_list_rejects_non_numeric_window_hours(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.get("/api/guide?channel=BBC%20TWO%20HD&windowHours=abc")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == []


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("SD_RATE_LIMITED", 429),
        ("SD_SYNC_TIMEOUT", 504),
        ("SD_UPSTREAM_ERROR", 502),
        ("AUTHENTICATION_REQUIRED", 401),
        ("TRANSPORT_ERROR", 503),
    ],
)
def test_health_route_maps_domain_and_transport_errors(
    monkeypatch,
    error_code: str,
    expected_status: int,
) -> None:
    stub = _StubServiceClient(
        fail_with=ServiceClientError(
            code=error_code,
            message="simulated error",
            retryable=True,
        )
    )
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
    )
    client = app.test_client()

    response = client.get("/api/health")

    assert response.status_code == expected_status
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == error_code


def test_web_auth_guard_blocks_unauthenticated_requests(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
    )
    client = app.test_client()

    response = client.get("/api/health")

    assert response.status_code == 401
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert stub.calls == []


def test_web_auth_guard_allows_authenticated_requests(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
    )
    client = app.test_client()

    response = client.get(
        "/api/health",
        headers={"Authorization": "Bearer web-token"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert stub.calls == [("service.health.get", {})]


def test_web_auth_guard_allows_index_without_token(monkeypatch) -> None:
    stub = _StubServiceClient()
    monkeypatch.setattr(
        "ccatv.web.app.create_service_client",
        lambda **_kwargs: stub,
    )

    app = create_app(
        service_host="127.0.0.1",
        service_port=8787,
        service_auth_token="token",
        web_auth_token="web-token",
    )
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert stub.calls == []
