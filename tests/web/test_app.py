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
                        "epgName": "BBC TWO HD",
                        "callsign": "BBC2",
                        "logicalChannelNumber": "2",
                        "source": "dvbstreamer_ota",
                        "sourceChannelId": "200",
                        "dvbstreamerServiceName": None,
                        "favoriteChannel": False,
                        "guideName": "BBC TWO HD",
                        "guideLogicalChannelNumber": "2",
                        "broadcasterName": None,
                        "schedulesDirectName": "BBC TWO HD",
                        "sourceVariants": [
                            {
                                "source": "dvbstreamer_ota",
                                "name": "BBC TWO HD",
                                "sourceChannelId": "200",
                                "callsign": "BBC2",
                                "logicalChannelNumber": "2",
                            }
                        ],
                    }
                ]
            }
        if command == "metadata.channels.dvbservices.list":
            return {
                "available": True,
                "error": None,
                "services": ["BBC TWO HD", "QUEST", "5 HD"],
            }
        if command == "metadata.channels.service-name.set":
            return {
                "channelName": str(payload.get("channelName")),
                "updatedRows": 1,
            }
        if command == "metadata.channels.favorite.set":
            return {
                "channelName": str(payload.get("channelName")),
                "favorite": bool(payload.get("favorite")),
                "updatedRows": 1,
            }
        if command == "metadata.channels.lineup.set":
            return {
                "epgChannelName": str(payload.get("epgChannelName")),
                "broadcasterName": payload.get("broadcasterName"),
                "schedulesDirectName": payload.get("schedulesDirectName"),
                "guideName": payload.get("guideName"),
                "guideLogicalChannelNumber": payload.get("guideLogicalChannelNumber"),
                "action": "saved",
                "updatedRows": 1,
            }
        if command == "metadata.series.recording.list":
            return {
                "subscriptions": [
                    {
                        "seriesRef": "example.org/series-1",
                        "enabled": True,
                    }
                ]
            }
        if command == "metadata.series.recording.set":
            return {
                "seriesRef": str(payload.get("seriesRef")),
                "enabled": bool(payload.get("enabled")),
                "autoSchedule": {"scheduled": 0, "skipped": 0},
            }
        if command == "recording.schedule.list":
            return {"jobs": []}
        if command == "recording.list":
            return {
                "recordings": [
                    {
                        "id": 1,
                        "channelName": "BBC TWO HD",
                        "outputPath": "/tmp/bbc2.ts",
                        "state": "capture_completed",
                        "startedAtUtc": "2026-05-25T20:00:00Z",
                        "endedAtUtc": "2026-05-25T21:00:00Z",
                    }
                ]
            }
        if command == "recording.delete":
            return {
                "id": int(payload.get("id", 0)),
                "deleteFiles": bool(payload.get("deleteFiles", True)),
                "outputPath": "/tmp/bbc2.ts",
                "fileDelete": {
                    "deleted": ["/tmp/bbc2.ts"],
                    "missing": ["/tmp/bbc2.nfo"],
                    "errors": [],
                },
            }
        if command == "recording.schedule.create":
            return {"job": {"id": 1, "state": "scheduled"}}
        if command == "recording.schedule.cancel":
            return {"job": {"id": int(payload.get("id", 0)), "state": "cancelled"}}
        if command == "metadata.guide.list":
            return {
                "channel": "BBC TWO HD",
                "window": {
                    "startAtUtc": "2026-05-25T20:00:00Z",
                    "endAtUtc": "2026-05-25T22:00:00Z",
                },
                "programs": [
                    {
                        "channelName": "BBC TWO HD",
                        "startAtUtc": "2026-05-25T20:00:00Z",
                        "stopAtUtc": "2026-05-25T21:00:00Z",
                        "durationSeconds": 3600,
                        "title": "Example",
                        "description": "Example description",
                        "genre": "Drama",
                    }
                ],
            }
        if command == "metadata.films.list":
            return {
                "window": {
                    "startAtUtc": "2026-05-25T20:00:00Z",
                    "endAtUtc": "2026-06-01T20:00:00Z",
                },
                "filters": {
                    "channelScope": "favourites",
                    "minDurationHours": 1.5,
                    "maxDurationHours": 3.5,
                },
                "films": [
                    {
                        "channelName": "Film4",
                        "startAtUtc": "2026-05-25T20:00:00Z",
                        "stopAtUtc": "2026-05-25T22:00:00Z",
                        "durationSeconds": 7200,
                        "title": "Film Example",
                        "description": "Example feature film",
                        "source": "dvbstreamer_ota",
                        "contentRef": "example.org/content-1",
                    }
                ],
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
    assert "Use the record switch below to schedule or cancel" in body
    assert "7-day timeline guide" in body
    assert "arrow keys" in body
    assert "Favourite channels only" in body
    assert "Search title, description, channel, or genre" in body
    assert "guide-search-input" in body
    assert "guide-search-scope" in body
    assert "guide-search-button" in body
    assert "guide-search-clear" in body
    assert "Searches the full 7-day guide database." in body
    assert "guide-search-results" in body
    assert "record-badge" in body
    assert "job.programStartAtUtc || job.startAtUtc" in body
    assert "job.programStopAtUtc || job.startAtUtc" in body
    assert "Channel Manager" in body
    assert 'href="/channel-manager"' in body
    assert "Recordings" in body
    assert 'href="/recordings"' in body
    assert "Upcoming Films" in body
    assert 'href="/upcoming-films"' in body
    assert "header-expand-btn" in body
    assert "hero-drawer" in body
    assert "health-pill" in body
    assert stub.calls == []


def test_channel_manager_route_serves_browser_ui(monkeypatch) -> None:
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

    response = client.get("/channel-manager")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Channel Manager" in body
    assert "Probable dvbstreamer Service" in body
    assert stub.calls == []


def test_recordings_page_serves_browser_ui(monkeypatch) -> None:
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

    response = client.get("/recordings")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Recordings" in body
    assert "Upcoming recordings" in body
    assert "Back to Guide" in body
    assert "Upcoming Films" in body
    assert "health-pill" in body
    assert stub.calls == []


def test_upcoming_films_page_serves_browser_ui(monkeypatch) -> None:
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

    response = client.get("/upcoming-films")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Upcoming Films" in body
    assert "Chronological list" in body
    assert "Record" in body
    assert "channel-scope-select" in body
    assert "Other showings" in body
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


def test_dvbservices_list_route_forwards_command(monkeypatch) -> None:
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

    response = client.get("/api/dvbservices")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["payload"]["available"] is True
    assert stub.calls == [("metadata.channels.dvbservices.list", {})]


def test_recordings_list_route_forwards_command(monkeypatch) -> None:
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

    response = client.get("/api/recordings")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["payload"]["recordings"][0]["channelName"] == "BBC TWO HD"
    assert stub.calls == [("recording.list", {})]


def test_recordings_delete_route_forwards_command(monkeypatch) -> None:
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

    response = client.delete("/api/recordings/42")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["payload"]["id"] == 42
    assert stub.calls == [(
        "recording.delete",
        {"id": 42, "deleteFiles": False},
    )]


def test_recordings_delete_route_ignores_json_body(monkeypatch) -> None:
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

    response = client.delete("/api/recordings/7", json=["invalid"])

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [(
        "recording.delete",
        {"id": 7, "deleteFiles": False},
    )]


def test_channel_mapping_route_forwards_payload(monkeypatch) -> None:
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

    response = client.post(
        "/api/channels/mapping",
        json={"channelName": "Quest", "serviceName": "QUEST"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [
        ("metadata.channels.service-name.set", {"channelName": "Quest", "serviceName": "QUEST"})
    ]


def test_channel_favorite_route_forwards_payload(monkeypatch) -> None:
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

    response = client.post(
        "/api/channels/favorite",
        json={"channelName": "Quest", "favorite": True},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [
        ("metadata.channels.favorite.set", {"channelName": "Quest", "favorite": True})
    ]


def test_channel_lineup_route_forwards_payload(monkeypatch) -> None:
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

    response = client.post(
        "/api/channels/lineup",
        json={
            "epgChannelName": "ITV1 HD",
            "broadcasterName": "ITV1",
            "schedulesDirectName": "ITV1 HD (Meridian, Anglia)",
            "guideName": "ITV1",
            "guideLogicalChannelNumber": "3",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [
        (
            "metadata.channels.lineup.set",
            {
                "epgChannelName": "ITV1 HD",
                "broadcasterName": "ITV1",
                "schedulesDirectName": "ITV1 HD (Meridian, Anglia)",
                "guideName": "ITV1",
                "guideLogicalChannelNumber": "3",
            },
        )
    ]


def test_channel_favorite_route_rejects_non_object_json(monkeypatch) -> None:
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

    response = client.post("/api/channels/favorite", json=["bad"]) 

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == []


def test_series_recording_list_route_forwards_command(monkeypatch) -> None:
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

    response = client.get("/api/series-recordings")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [
        ("metadata.series.recording.list", {}),
    ]


def test_series_recording_set_route_forwards_payload(monkeypatch) -> None:
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

    response = client.post(
        "/api/series-recordings",
        json={"seriesRef": "example.org/series-1", "enabled": True},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [
        (
            "metadata.series.recording.set",
            {"seriesRef": "example.org/series-1", "enabled": True},
        )
    ]


def test_series_recording_set_route_rejects_non_object_json(monkeypatch) -> None:
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

    response = client.post("/api/series-recordings", json=["bad"])

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == []


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
            "programTitle": None,
            "programDescription": None,
            "programStartAtUtc": None,
            "programStopAtUtc": None,
            "programContentRef": None,
            "programSeriesRef": None,
        },
    )]


def test_schedule_cancel_route_forwards_command(monkeypatch) -> None:
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

    response = client.delete("/api/schedules/42")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [(
        "recording.schedule.cancel",
        {"id": 42},
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


def test_upcoming_films_forwards_query_params(monkeypatch) -> None:
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
        "/api/upcoming-films?startAtUtc=2026-05-25T20:00:00Z&windowHours=48&minDurationHours=1.5&maxDurationHours=3.5"
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert stub.calls == [
        (
            "metadata.films.list",
            {
                "channelScope": "favourites",
                "startAtUtc": "2026-05-25T20:00:00Z",
                "windowHours": 48.0,
                "minDurationHours": 1.5,
                "maxDurationHours": 3.5,
            },
        )
    ]


def test_upcoming_films_rejects_non_numeric_window_hours(monkeypatch) -> None:
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

    response = client.get("/api/upcoming-films?windowHours=abc")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == []


def test_upcoming_films_rejects_invalid_channel_scope(monkeypatch) -> None:
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

    response = client.get("/api/upcoming-films?channelScope=invalid")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert stub.calls == []


def test_guide_search_queries_all_channels_and_filters_matches(monkeypatch) -> None:
    @dataclass(slots=True)
    class _SearchStub:
        calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

        def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append((command, payload))
            if command == "metadata.channels.list":
                return {
                    "channels": [
                        {"name": "BBC TWO HD", "favoriteChannel": True},
                        {"name": "Film4", "favoriteChannel": False},
                    ]
                }
            if command == "metadata.guide.list":
                channel_name = str(payload.get("channel"))
                if channel_name == "BBC TWO HD":
                    return {
                        "programs": [
                            {
                                "channelName": "BBC TWO HD",
                                "startAtUtc": "2026-05-25T20:00:00Z",
                                "stopAtUtc": "2026-05-25T21:00:00Z",
                                "durationSeconds": 3600,
                                "title": "Example Match",
                                "description": "Contains keyword",
                                "genre": "Drama",
                            }
                        ]
                    }
                return {
                    "programs": [
                        {
                            "channelName": "Film4",
                            "startAtUtc": "2026-05-25T20:00:00Z",
                            "stopAtUtc": "2026-05-25T22:00:00Z",
                            "durationSeconds": 7200,
                            "title": "No Hit",
                            "description": "Nothing relevant",
                            "genre": "Movie",
                        }
                    ]
                }
            return {}

        def close(self) -> None:
            return None

    stub = _SearchStub()
    monkeypatch.setattr("ccatv.web.app.create_service_client", lambda **_kwargs: stub)

    app = create_app(service_host="127.0.0.1", service_port=8787, service_auth_token="token")
    client = app.test_client()

    response = client.get(
        "/api/guide/search?q=keyword&channelScope=all&startAtUtc=2026-05-25T19:00:00Z&windowHours=24"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    payload = body["payload"]
    assert payload["channelScope"] == "all"
    assert payload["channelsSearched"] == 2
    assert len(payload["programs"]) == 1
    assert payload["programs"][0]["channelName"] == "BBC TWO HD"


def test_guide_search_favourites_scope_only_queries_favourite_channels(monkeypatch) -> None:
    @dataclass(slots=True)
    class _SearchStub:
        calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

        def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
            self.calls.append((command, payload))
            if command == "metadata.channels.list":
                return {
                    "channels": [
                        {"name": "BBC TWO HD", "favoriteChannel": True},
                        {"name": "Film4", "favoriteChannel": False},
                    ]
                }
            if command == "metadata.guide.list":
                return {
                    "programs": [
                        {
                            "channelName": "BBC TWO HD",
                            "startAtUtc": "2026-05-25T20:00:00Z",
                            "stopAtUtc": "2026-05-25T21:00:00Z",
                            "durationSeconds": 3600,
                            "title": "Keyword Hit",
                            "description": "keyword",
                            "genre": "Drama",
                        }
                    ]
                }
            return {}

        def close(self) -> None:
            return None

    stub = _SearchStub()
    monkeypatch.setattr("ccatv.web.app.create_service_client", lambda **_kwargs: stub)

    app = create_app(service_host="127.0.0.1", service_port=8787, service_auth_token="token")
    client = app.test_client()

    response = client.get("/api/guide/search?q=keyword&channelScope=favourites")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["payload"]["channelsSearched"] == 1
    guide_calls = [call for call in stub.calls if call[0] == "metadata.guide.list"]
    assert len(guide_calls) == 1
    assert guide_calls[0][1]["channel"] == "BBC TWO HD"


def test_guide_search_validates_query_and_scope(monkeypatch) -> None:
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

    missing_q = client.get("/api/guide/search")
    assert missing_q.status_code == 400
    assert missing_q.get_json()["error"]["code"] == "VALIDATION_ERROR"

    bad_scope = client.get("/api/guide/search?q=test&channelScope=weird")
    assert bad_scope.status_code == 400
    assert bad_scope.get_json()["error"]["code"] == "VALIDATION_ERROR"


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
