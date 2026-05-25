from __future__ import annotations

from collections.abc import Callable

from flask import Flask, jsonify, request

from ccatv.app.service_client import ServiceClient, ServiceClientError, create_service_client


def _status_for_service_error(code: str) -> int:
    if code == "VALIDATION_ERROR":
        return 400
    if code == "UNSUPPORTED_COMMAND":
        return 400
    if code == "AUTHENTICATION_REQUIRED":
        return 502
    if code == "TRANSPORT_ERROR":
        return 503
    if code == "INTERNAL_ERROR":
        return 502
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
) -> Flask:
    app = Flask(__name__)

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

    @app.post("/api/schedules")
    def api_schedule_create():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "request JSON body must be an object",
                            "retryable": False,
                            "details": {},
                        },
                    }
                ),
                400,
            )

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
