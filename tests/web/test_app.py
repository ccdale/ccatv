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
        if command == "recording.schedule.list":
            return {"jobs": []}
        if command == "recording.schedule.create":
            return {"job": {"id": 1, "state": "scheduled"}}
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
