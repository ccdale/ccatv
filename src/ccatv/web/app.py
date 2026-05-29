from __future__ import annotations

import secrets
from collections.abc import Callable

from flask import Flask, jsonify, render_template, request, session

from ccatv.app.service_client import ServiceClient, ServiceClientError, create_service_client


def _json_error(*, code: str, message: str, status_code: int) -> tuple[dict[str, object], int]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
            "details": {},
        },
    }, status_code


def _session_authenticated() -> bool:
    return session.get("ccatv_web_authenticated") is True


def _status_for_service_error(code: str) -> int:
    if code == "VALIDATION_ERROR":
        return 400
    if code == "UNSUPPORTED_COMMAND":
        return 400
    if code == "COMMAND_CANCELLED":
        return 409
    if code == "SD_RATE_LIMITED":
        return 429
    if code == "SD_SYNC_TIMEOUT":
        return 504
    if code == "SD_UPSTREAM_ERROR":
        return 502
    if code == "SD_AUTH_FAILED":
        return 502
    if code == "AUTHENTICATION_REQUIRED":
        return 401
    if code == "TRANSPORT_ERROR":
        return 503
    if code == "INTERNAL_ERROR":
        return 502
    if code == "NOT_FOUND":
        return 404
    return 502


def _with_client(
    factory: Callable[[], ServiceClient],
    command: str,
    payload: dict[str, object],
) -> tuple[dict[str, object], int]:
    client = factory()
    try:
        response_payload = client.execute(command, payload)
    except ServiceClientError as exc:
        return {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details or {},
            },
        }, _status_for_service_error(exc.code)
    finally:
        client.close()
    return {"ok": True, "payload": response_payload}, 200


def create_app(
    *,
    service_host: str,
    service_port: int,
    service_auth_token: str,
    web_auth_token: str | None = None,
    session_secret: str | None = None,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = session_secret or secrets.token_urlsafe(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            auth_required=web_auth_token is not None,
            authenticated=web_auth_token is None or _session_authenticated(),
        )

    @app.get("/channel-manager")
    def channel_manager():
        return render_template(
            "channel_manager.html",
            auth_required=web_auth_token is not None,
            authenticated=web_auth_token is None or _session_authenticated(),
        )

    @app.get("/auth/session")
    def auth_session_status():
        return jsonify(
            {
                "ok": True,
                "payload": {
                    "authRequired": web_auth_token is not None,
                    "authenticated": web_auth_token is None or _session_authenticated(),
                },
            }
        )

    @app.post("/auth/session")
    def auth_session_create():
        if web_auth_token is None:
            session["ccatv_web_authenticated"] = True
            return jsonify(
                {
                    "ok": True,
                    "payload": {
                        "authRequired": False,
                        "authenticated": True,
                    },
                }
            )

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        token = body.get("token")
        if not isinstance(token, str) or not token.strip():
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="token must be a non-empty string",
                status_code=400,
            )
            return jsonify(response), status_code

        if token.strip() != web_auth_token:
            session.pop("ccatv_web_authenticated", None)
            response, status_code = _json_error(
                code="AUTHENTICATION_REQUIRED",
                message="missing or invalid web bearer token",
                status_code=401,
            )
            return jsonify(response), status_code

        session["ccatv_web_authenticated"] = True
        return jsonify(
            {
                "ok": True,
                "payload": {
                    "authRequired": True,
                    "authenticated": True,
                },
            }
        )

    @app.delete("/auth/session")
    def auth_session_delete():
        session.pop("ccatv_web_authenticated", None)
        return jsonify(
            {
                "ok": True,
                "payload": {
                    "authRequired": web_auth_token is not None,
                    "authenticated": False,
                },
            }
        )

    @app.before_request
    def _web_auth_guard():
        if not request.path.startswith("/api/"):
            return None
        if web_auth_token is None:
            return None
        if _session_authenticated():
            return None
        header = request.headers.get("Authorization")
        if header == f"Bearer {web_auth_token}":
            return None
        response, status_code = _json_error(
            code="AUTHENTICATION_REQUIRED",
            message="missing or invalid web bearer token",
            status_code=401,
        )
        return jsonify(response), status_code

    def _client_factory() -> ServiceClient:
        return create_service_client(
            http_host=service_host,
            http_port=service_port,
            http_auth_token=service_auth_token,
        )

    @app.get("/api/health")
    def api_health():
        response, status_code = _with_client(
            _client_factory,
            "service.health.get",
            {},
        )
        return jsonify(response), status_code

    @app.get("/api/service/info")
    def api_service_info():
        response, status_code = _with_client(
            _client_factory,
            "service.info.get",
            {},
        )
        return jsonify(response), status_code

    @app.get("/api/channels")
    def api_channel_list():
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.list",
            {},
        )
        return jsonify(response), status_code

    @app.get("/api/dvbservices")
    def api_dvbservices_list():
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.dvbservices.list",
            {},
        )
        return jsonify(response), status_code

    @app.post("/api/channels/mapping")
    def api_channel_mapping_set():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "channelName": body.get("channelName"),
            "serviceName": body.get("serviceName"),
        }
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.service-name.set",
            payload,
        )
        return jsonify(response), status_code

    @app.post("/api/channels/favorite")
    def api_channel_favorite_set():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "channelName": body.get("channelName"),
            "favorite": body.get("favorite"),
        }
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.favorite.set",
            payload,
        )
        return jsonify(response), status_code

    @app.get("/api/schedules")
    def api_schedule_list():
        payload: dict[str, object] = {}
        state_filter = request.args.get("state", default=None, type=str)
        if state_filter:
            payload["state"] = state_filter
        response, status_code = _with_client(
            _client_factory,
            "recording.schedule.list",
            payload,
        )
        return jsonify(response), status_code

    @app.get("/api/guide")
    def api_guide_list():
        channel = request.args.get("channel", default=None, type=str)
        if channel is None or not channel.strip():
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="query parameter 'channel' is required",
                status_code=400,
            )
            return jsonify(response), status_code

        payload: dict[str, object] = {"channel": channel.strip()}

        start_at_utc = request.args.get("startAtUtc", default=None, type=str)
        if start_at_utc is not None:
            if not start_at_utc.strip():
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'startAtUtc' cannot be empty",
                    status_code=400,
                )
                return jsonify(response), status_code
            payload["startAtUtc"] = start_at_utc.strip()

        window_hours = request.args.get("windowHours", default=None, type=str)
        if window_hours is not None:
            try:
                payload["windowHours"] = float(window_hours)
            except ValueError:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'windowHours' must be numeric",
                    status_code=400,
                )
                return jsonify(response), status_code

        response, status_code = _with_client(
            _client_factory,
            "metadata.guide.list",
            payload,
        )
        return jsonify(response), status_code

    @app.post("/api/schedules")
    def api_schedule_create():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "channelName": body.get("channelName"),
            "startAtUtc": body.get("startAtUtc"),
            "durationSeconds": body.get("durationSeconds"),
        }
        response, status_code = _with_client(
            _client_factory,
            "recording.schedule.create",
            payload,
        )
        return jsonify(response), status_code

    return app
