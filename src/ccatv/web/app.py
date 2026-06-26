from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timezone

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

    @app.get("/recordings")
    def recordings_page():
        return render_template(
            "recordings.html",
            auth_required=web_auth_token is not None,
            authenticated=web_auth_token is None or _session_authenticated(),
        )

    @app.get("/upcoming-films")
    def upcoming_films_page():
        return render_template(
            "upcoming_films.html",
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

    @app.post("/api/channels/lineup")
    def api_channel_lineup_set():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "epgChannelName": body.get("epgChannelName"),
            "broadcasterName": body.get("broadcasterName"),
            "schedulesDirectName": body.get("schedulesDirectName"),
            "guideName": body.get("guideName"),
            "guideLogicalChannelNumber": body.get("guideLogicalChannelNumber"),
        }
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.lineup.set",
            payload,
        )
        return jsonify(response), status_code

    @app.get("/api/series-recordings")
    def api_series_recording_list():
        response, status_code = _with_client(
            _client_factory,
            "metadata.series.recording.list",
            {},
        )
        return jsonify(response), status_code

    @app.post("/api/series-recordings")
    def api_series_recording_set():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "seriesRef": body.get("seriesRef"),
            "enabled": body.get("enabled"),
        }
        response, status_code = _with_client(
            _client_factory,
            "metadata.series.recording.set",
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

    @app.get("/api/recordings")
    def api_recordings_list():
        response, status_code = _with_client(
            _client_factory,
            "recording.list",
            {},
        )
        return jsonify(response), status_code

    @app.delete("/api/recordings/<int:recording_id>")
    def api_recording_delete(recording_id: int):
        payload = {
            "id": recording_id,
            # Recordings page delete is intentionally DB-only because
            # post-processing may have moved files to NAS.
            "deleteFiles": False,
        }
        response, status_code = _with_client(
            _client_factory,
            "recording.delete",
            payload,
        )
        return jsonify(response), status_code

    @app.post("/api/recordings/<int:recording_id>/stop")
    def api_recording_stop(recording_id: int):
        payload = {
            "id": recording_id,
        }
        response, status_code = _with_client(
            _client_factory,
            "recording.stop",
            payload,
        )
        return jsonify(response), status_code

    @app.get("/api/guide")
    def api_guide_list():
        """Return channel guide rows with explicit identity-vs-metadata semantics.

        Response entries expose both legacy top-level fields and grouped fields:
        - broadcasterRefs: stable broadcaster identity refs (contentRef, seriesRef)
        - episodeMetadata: descriptive organization metadata
          (season/episode/onscreen id/original air date/release year)

        These are intentionally separate concepts and should not be conflated.
        """
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

    @app.get("/api/guide/search")
    def api_guide_search():
        query = request.args.get("q", default=None, type=str)
        if query is None or not query.strip():
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="query parameter 'q' is required",
                status_code=400,
            )
            return jsonify(response), status_code

        channel_scope = request.args.get("channelScope", default="favourites", type=str)
        if channel_scope is None:
            channel_scope = "favourites"
        channel_scope_value = channel_scope.strip().lower()
        if channel_scope_value not in {"all", "favourites"}:
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="query parameter 'channelScope' must be 'all' or 'favourites'",
                status_code=400,
            )
            return jsonify(response), status_code

        start_at_utc = request.args.get("startAtUtc", default=None, type=str)
        if start_at_utc is not None and not start_at_utc.strip():
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="query parameter 'startAtUtc' cannot be empty",
                status_code=400,
            )
            return jsonify(response), status_code

        window_hours_raw = request.args.get("windowHours", default=None, type=str)
        if window_hours_raw is None:
            window_hours = 24 * 7
        else:
            try:
                window_hours = float(window_hours_raw)
            except ValueError:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'windowHours' must be numeric",
                    status_code=400,
                )
                return jsonify(response), status_code

        channels_response, channels_status = _with_client(
            _client_factory,
            "metadata.channels.list",
            {},
        )
        if channels_status != 200:
            return jsonify(channels_response), channels_status

        channels_payload = channels_response.get("payload", {})
        channels = channels_payload.get("channels", [])
        if not isinstance(channels, list):
            channels = []

        selected_channels: list[str] = []
        for item in channels:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            if channel_scope_value == "favourites" and not bool(item.get("favoriteChannel")):
                continue
            selected_channels.append(name.strip())

        selected_channels = list(dict.fromkeys(selected_channels))

        start_value = (
            start_at_utc.strip()
            if isinstance(start_at_utc, str) and start_at_utc.strip()
            else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        query_value = query.strip().lower()

        matches: list[dict[str, object]] = []
        for channel_name in selected_channels:
            guide_payload: dict[str, object] = {
                "channel": channel_name,
                "startAtUtc": start_value,
                "windowHours": window_hours,
            }
            guide_response, guide_status = _with_client(
                _client_factory,
                "metadata.guide.list",
                guide_payload,
            )
            if guide_status != 200:
                return jsonify(guide_response), guide_status

            payload = guide_response.get("payload", {})
            programs = payload.get("programs", [])
            if not isinstance(programs, list):
                continue
            for program in programs:
                if not isinstance(program, dict):
                    continue
                haystack = "\n".join(
                    [
                        str(program.get("title") or ""),
                        str(program.get("description") or ""),
                        str(program.get("channelName") or ""),
                        str(program.get("genre") or ""),
                    ]
                ).lower()
                if query_value in haystack:
                    matches.append(program)

        matches.sort(
            key=lambda program: (
                str(program.get("startAtUtc") or ""),
                str(program.get("channelName") or ""),
                str(program.get("title") or ""),
            )
        )

        return jsonify(
            {
                "ok": True,
                "payload": {
                    "query": query.strip(),
                    "channelScope": channel_scope_value,
                    "channelsSearched": len(selected_channels),
                    "window": {
                        "startAtUtc": start_value,
                        "windowHours": window_hours,
                    },
                    "programs": matches,
                },
            }
        )

    @app.get("/api/guide/audit")
    def api_guide_audit():
        """Return audit rows for stored metadata vs description-parsed metadata.

        Stored values include grouped fields mirroring `/api/guide` semantics:
        - stored.broadcasterRefs for broadcaster identity refs
        - stored.episodeMetadata for descriptive episode/year metadata
        """
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

        limit_value = request.args.get("limit", default=None, type=str)
        if limit_value is not None:
            try:
                parsed_limit = int(limit_value)
            except ValueError:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'limit' must be an integer",
                    status_code=400,
                )
                return jsonify(response), status_code
            if parsed_limit <= 0:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'limit' must be greater than 0",
                    status_code=400,
                )
                return jsonify(response), status_code
            payload["limit"] = parsed_limit

        offset_value = request.args.get("offset", default=None, type=str)
        if offset_value is not None:
            try:
                parsed_offset = int(offset_value)
            except ValueError:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'offset' must be an integer",
                    status_code=400,
                )
                return jsonify(response), status_code
            if parsed_offset < 0:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'offset' must be non-negative",
                    status_code=400,
                )
                return jsonify(response), status_code
            payload["offset"] = parsed_offset

        response, status_code = _with_client(
            _client_factory,
            "metadata.guide.audit.list",
            payload,
        )
        return jsonify(response), status_code

    @app.get("/api/upcoming-films")
    def api_upcoming_films():
        """Return upcoming films with separate broadcaster refs and episode metadata.

        Broadcaster refs drive identity/dedupe workflows; episode metadata is for
        descriptive organization in downstream libraries.
        """
        payload: dict[str, object] = {}

        channel_scope = request.args.get("channelScope", default="favourites", type=str)
        if channel_scope is None:
            channel_scope = "favourites"
        channel_scope_value = channel_scope.strip().lower()
        if channel_scope_value not in {"all", "favourites"}:
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="query parameter 'channelScope' must be 'all' or 'favourites'",
                status_code=400,
            )
            return jsonify(response), status_code
        payload["channelScope"] = channel_scope_value

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

        min_duration_hours = request.args.get(
            "minDurationHours", default=None, type=str
        )
        if min_duration_hours is not None:
            try:
                payload["minDurationHours"] = float(min_duration_hours)
            except ValueError:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'minDurationHours' must be numeric",
                    status_code=400,
                )
                return jsonify(response), status_code

        max_duration_hours = request.args.get(
            "maxDurationHours", default=None, type=str
        )
        if max_duration_hours is not None:
            try:
                payload["maxDurationHours"] = float(max_duration_hours)
            except ValueError:
                response, status_code = _json_error(
                    code="VALIDATION_ERROR",
                    message="query parameter 'maxDurationHours' must be numeric",
                    status_code=400,
                )
                return jsonify(response), status_code

        response, status_code = _with_client(
            _client_factory,
            "metadata.films.list",
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
            "programTitle": body.get("programTitle"),
            "programDescription": body.get("programDescription"),
            "programStartAtUtc": body.get("programStartAtUtc"),
            "programStopAtUtc": body.get("programStopAtUtc"),
            "programContentRef": body.get("programContentRef"),
            "programSeriesRef": body.get("programSeriesRef"),
        }
        response, status_code = _with_client(
            _client_factory,
            "recording.schedule.create",
            payload,
        )
        return jsonify(response), status_code

    @app.delete("/api/schedules/<int:job_id>")
    def api_schedule_cancel(job_id: int):
        response, status_code = _with_client(
            _client_factory,
            "recording.schedule.cancel",
            {"id": job_id},
        )
        return jsonify(response), status_code

    @app.get("/api/channels/groups")
    def api_channel_groups_list():
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.groups.list",
            {},
        )
        return jsonify(response), status_code

    @app.post("/api/channels/groups")
    def api_channel_groups_create():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "groupName": body.get("groupName"),
            "groupLogicalChannelNumber": body.get("groupLogicalChannelNumber"),
            "preferredRecordingSource": body.get("preferredRecordingSource"),
            "memberChannels": body.get("memberChannels", []),
        }
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.groups.create",
            payload,
        )
        return jsonify(response), status_code

    @app.patch("/api/channels/groups/<int:group_id>")
    def api_channel_groups_update(group_id: int):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "groupId": group_id,
            "groupName": body.get("groupName"),
            "groupLogicalChannelNumber": body.get("groupLogicalChannelNumber"),
            "preferredRecordingSource": body.get("preferredRecordingSource"),
        }
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.groups.update",
            payload,
        )
        return jsonify(response), status_code

    @app.delete("/api/channels/groups/<int:group_id>")
    def api_channel_groups_delete(group_id: int):
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.groups.delete",
            {"groupId": group_id},
        )
        return jsonify(response), status_code

    @app.post("/api/channels/groups/assign")
    def api_channel_groups_assign():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            response, status_code = _json_error(
                code="VALIDATION_ERROR",
                message="request JSON body must be an object",
                status_code=400,
            )
            return jsonify(response), status_code

        payload = {
            "source": body.get("source"),
            "sourceChannelId": body.get("sourceChannelId"),
            "groupId": body.get("groupId"),
        }
        response, status_code = _with_client(
            _client_factory,
            "metadata.channels.groups.assign",
            payload,
        )
        return jsonify(response), status_code

    return app
