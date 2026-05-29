from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event

from ccatv import __version__
from ccatv.app.bootstrap import AppContext, bootstrap_app, close_app_context
from ccatv.app.recorder_worker import create_scheduler_worker
from ccatv.app.service_dispatcher import ServiceCommandDispatcher

IPC_MAX_REQUEST_BYTES = 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccatv-service",
        description=(
            "Run the ccatv service daemon skeleton. "
            "Current implementation manages recorder scheduler cycles; "
            "API transport endpoints are planned next."
        ),
    )
    parser.add_argument(
        "--max-jobs-per-cycle",
        type=int,
        default=None,
        help="maximum due jobs to execute in a single scheduler cycle",
    )
    parser.add_argument(
        "--output-directory",
        default="/tmp",
        help="directory for recording output files",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=5.0,
        help="poll interval between scheduler cycles",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="execute one scheduler cycle then exit",
    )
    parser.add_argument(
        "--dispatch-command-json",
        default=None,
        help=(
            "execute one service command envelope JSON string and exit; "
            "use for M1 in-process command validation"
        ),
    )
    parser.add_argument(
        "--socket-path",
        default=None,
        help=(
            "unix socket path for local JSON request/response transport; "
            "when set, daemon serves service command envelopes over IPC"
        ),
    )
    parser.add_argument(
        "--http-bind-host",
        default=None,
        help=(
            "bind host for HTTP JSON transport (disabled by default); "
            "for example 127.0.0.1 or 0.0.0.0"
        ),
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8787,
        help="port for HTTP JSON transport when --http-bind-host is set",
    )
    parser.add_argument(
        "--http-auth-token",
        default=None,
        help=(
            "Bearer token required by HTTP JSON transport; "
            "required when --http-bind-host is set"
        ),
    )
    return parser


def _build_dispatcher(context: AppContext, *, should_stop: Callable[[], bool]):
    return ServiceCommandDispatcher(
        context,
        should_stop=should_stop,
        worker_cycle_lock=getattr(context, "worker_cycle_lock", None),
    )


def _handle_ipc_request(
    raw_payload: bytes, dispatcher: ServiceCommandDispatcher
) -> bytes:
    try:
        request_text = raw_payload.decode("utf-8")
    except UnicodeDecodeError:
        response = {
            "apiVersion": "v1alpha1",
            "requestId": None,
            "ok": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "request must be valid UTF-8 JSON",
                "retryable": False,
                "details": {},
            },
        }
        return json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"

    try:
        request = json.loads(request_text)
    except json.JSONDecodeError as exc:
        response = {
            "apiVersion": "v1alpha1",
            "requestId": None,
            "ok": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"invalid JSON request: {exc}",
                "retryable": False,
                "details": {},
            },
        }
        return json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"

    if not isinstance(request, dict):
        response = {
            "apiVersion": "v1alpha1",
            "requestId": None,
            "ok": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "request must decode to an object",
                "retryable": False,
                "details": {},
            },
        }
        return json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"

    request_id = request.get("requestId")

    try:
        response = dispatcher.dispatch(request)
    except Exception as exc:
        response = {
            "apiVersion": "v1alpha1",
            "requestId": request_id,
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": f"dispatcher failure: {exc}",
                "retryable": True,
                "details": {},
            },
        }

    try:
        return json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        fallback = {
            "apiVersion": "v1alpha1",
            "requestId": request_id,
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "response serialization failed",
                "retryable": True,
                "details": {},
            },
        }
        return json.dumps(fallback, sort_keys=True).encode("utf-8") + b"\n"


def run_ipc_server(
    context: AppContext,
    *,
    socket_path: str,
    should_stop: Callable[[], bool] | None = None,
    max_requests: int | None = None,
) -> int:
    logger = context.logger
    stop_predicate = should_stop or (lambda: False)
    dispatcher = _build_dispatcher(context, should_stop=stop_predicate)

    target_path = Path(socket_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()

    requests_served = 0
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(target_path))
        os.chmod(target_path, 0o600)
        server.listen(16)
        server.settimeout(0.5)
        logger.info("service daemon IPC transport listening on %s", target_path)

        while not stop_predicate():
            if max_requests is not None and requests_served >= max_requests:
                break
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue

            try:
                connection.settimeout(5.0)
                with connection:
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        block = connection.recv(4096)
                        if not block:
                            break
                        chunks.append(block)
                        total += len(block)
                        if total > IPC_MAX_REQUEST_BYTES:
                            response = {
                                "apiVersion": "v1alpha1",
                                "requestId": None,
                                "ok": False,
                                "error": {
                                    "code": "VALIDATION_ERROR",
                                    "message": "request too large",
                                    "retryable": False,
                                    "details": {
                                        "maxBytes": IPC_MAX_REQUEST_BYTES,
                                    },
                                },
                            }
                            connection.sendall(
                                json.dumps(response, sort_keys=True).encode("utf-8")
                                + b"\n"
                            )
                            break

                    if total <= IPC_MAX_REQUEST_BYTES:
                        request_bytes = b"".join(chunks).strip()
                        if not request_bytes:
                            response = {
                                "apiVersion": "v1alpha1",
                                "requestId": None,
                                "ok": False,
                                "error": {
                                    "code": "VALIDATION_ERROR",
                                    "message": "request body is empty",
                                    "retryable": False,
                                    "details": {},
                                },
                            }
                            connection.sendall(
                                json.dumps(response, sort_keys=True).encode("utf-8")
                                + b"\n"
                            )
                        else:
                            response_bytes = _handle_ipc_request(
                                request_bytes, dispatcher
                            )
                            connection.sendall(response_bytes)
            except (OSError, socket.timeout) as exc:
                logger.warning("service daemon IPC connection failed: %s", exc)
            except Exception:
                logger.exception("service daemon IPC connection unexpected failure")

            requests_served += 1

    finally:
        server.close()
        if target_path.exists():
            target_path.unlink()
    return 0


def run_http_server(
    context: AppContext,
    *,
    bind_host: str,
    port: int,
    auth_token: str,
    should_stop: Callable[[], bool] | None = None,
    max_requests: int | None = None,
    on_listening: Callable[[int], None] | None = None,
) -> int:
    logger = context.logger
    stop_predicate = should_stop or (lambda: False)
    dispatcher = _build_dispatcher(context, should_stop=stop_predicate)

    requests_served = 0

    def _status_from_response(response: dict[str, object]) -> HTTPStatus:
        if response.get("ok") is True:
            return HTTPStatus.OK
        error = response.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if code == "INTERNAL_ERROR":
                return HTTPStatus.INTERNAL_SERVER_ERROR
            if code == "AUTHENTICATION_REQUIRED":
                return HTTPStatus.UNAUTHORIZED
            if code == "NOT_FOUND":
                return HTTPStatus.NOT_FOUND
            if code == "COMMAND_CANCELLED":
                return HTTPStatus.CONFLICT
            if code == "SD_RATE_LIMITED":
                return HTTPStatus.TOO_MANY_REQUESTS
            if code == "SD_SYNC_TIMEOUT":
                return HTTPStatus.GATEWAY_TIMEOUT
            if code in {"SD_UPSTREAM_ERROR", "SD_AUTH_FAILED"}:
                return HTTPStatus.BAD_GATEWAY
        return HTTPStatus.BAD_REQUEST

    class _Handler(BaseHTTPRequestHandler):
        server_version = "ccatv-service"
        protocol_version = "HTTP/1.1"

        def _json_response(self, *, status: HTTPStatus, body: dict[str, object]) -> None:
            payload = json.dumps(body, sort_keys=True).encode("utf-8") + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _is_authorized(self) -> bool:
            header = self.headers.get("Authorization")
            return header == f"Bearer {auth_token}"

        def _record_request(self) -> None:
            nonlocal requests_served
            requests_served += 1

        def do_GET(self) -> None:  # noqa: N802
            self._record_request()
            if self.path != "/health":
                self._json_response(
                    status=HTTPStatus.NOT_FOUND,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "NOT_FOUND",
                            "message": "unknown endpoint",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
                return

            if not self._is_authorized():
                self._json_response(
                    status=HTTPStatus.UNAUTHORIZED,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "AUTHENTICATION_REQUIRED",
                            "message": "missing or invalid bearer token",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
                return

            response = dispatcher.dispatch(
                {
                    "apiVersion": "v1alpha1",
                    "command": "service.health.get",
                    "payload": {},
                    "requestId": None,
                }
            )
            status = _status_from_response(response)
            self._json_response(status=status, body=response)

        def do_POST(self) -> None:  # noqa: N802
            self._record_request()
            if self.path != "/api/v1/command":
                self._json_response(
                    status=HTTPStatus.NOT_FOUND,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "NOT_FOUND",
                            "message": "unknown endpoint",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
                return

            if not self._is_authorized():
                self._json_response(
                    status=HTTPStatus.UNAUTHORIZED,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "AUTHENTICATION_REQUIRED",
                            "message": "missing or invalid bearer token",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
                return

            length_header = self.headers.get("Content-Length", "0")
            try:
                body_length = int(length_header)
            except ValueError:
                self._json_response(
                    status=HTTPStatus.BAD_REQUEST,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "Content-Length must be an integer",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
                return

            if body_length < 0:
                self._json_response(
                    status=HTTPStatus.BAD_REQUEST,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "Content-Length must be >= 0",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
                return

            if body_length == 0:
                self._json_response(
                    status=HTTPStatus.BAD_REQUEST,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "request body is empty",
                            "retryable": False,
                            "details": {},
                        },
                    },
                )
                return

            if body_length > IPC_MAX_REQUEST_BYTES:
                self._json_response(
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "request too large",
                            "retryable": False,
                            "details": {"maxBytes": IPC_MAX_REQUEST_BYTES},
                        },
                    },
                )
                return

            self.connection.settimeout(5.0)
            try:
                raw_body = self.rfile.read(body_length)
            except socket.timeout:
                self._json_response(
                    status=HTTPStatus.REQUEST_TIMEOUT,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "TRANSPORT_ERROR",
                            "message": "request body read timed out",
                            "retryable": True,
                            "details": {},
                        },
                    },
                )
                return
            except OSError as exc:
                self._json_response(
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    body={
                        "apiVersion": "v1alpha1",
                        "requestId": None,
                        "ok": False,
                        "error": {
                            "code": "TRANSPORT_ERROR",
                            "message": f"request body read failed: {exc}",
                            "retryable": True,
                            "details": {},
                        },
                    },
                )
                return
            response_bytes = _handle_ipc_request(raw_body, dispatcher)
            try:
                response = json.loads(response_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = {
                    "apiVersion": "v1alpha1",
                    "requestId": None,
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "response serialization failed",
                        "retryable": True,
                        "details": {},
                    },
                }
            status = _status_from_response(response)
            self._json_response(status=status, body=response)

        def log_message(self, _format: str, *_args) -> None:
            # Keep service logs in the app logger instead of stderr spam.
            return

    # Keep request handling on the same thread as the bootstrap-created
    # sqlite connection to avoid sqlite thread-affinity errors.
    server = HTTPServer((bind_host, port), _Handler)
    server.timeout = 0.5
    try:
        logger.info(
            "service daemon HTTP transport listening on %s:%s",
            bind_host,
            server.server_port,
        )
        if on_listening is not None:
            on_listening(server.server_port)
        while not stop_predicate():
            if max_requests is not None and requests_served >= max_requests:
                break
            server.handle_request()
    finally:
        server.server_close()
    return 0


def run_service_daemon(
    context: AppContext,
    *,
    output_directory: str,
    max_jobs_per_cycle: int | None,
    poll_interval_seconds: float,
    run_once: bool,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    logger = context.logger
    stop_predicate = should_stop or (lambda: False)
    worker = create_scheduler_worker(
        context,
        output_directory=output_directory,
        max_jobs_per_cycle=max_jobs_per_cycle,
        poll_interval_seconds=poll_interval_seconds,
    )
    worker_cycle_lock = getattr(context, "worker_cycle_lock", None)

    logger.info(
        "service daemon started (mode=scheduler_loop, poll_interval_seconds=%s)",
        poll_interval_seconds,
    )

    if run_once:
        try:
            if worker_cycle_lock is None:
                results = worker.run_cycle()
            else:
                with worker_cycle_lock:
                    results = worker.run_cycle()
        except Exception:
            logger.exception("service daemon run-once cycle failed")
            return 1

        logger.info("service daemon completed one cycle (jobs=%d)", len(results))
        return 0

    while not stop_predicate():
        try:
            if worker_cycle_lock is None:
                results = worker.run_cycle()
            else:
                with worker_cycle_lock:
                    results = worker.run_cycle()
        except Exception:
            logger.exception("service daemon cycle failed")
            results = []
        if results:
            logger.info("service daemon cycle completed with %d due jobs", len(results))
        time.sleep(poll_interval_seconds)

    logger.info("service daemon stop requested")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be greater than 0")
    if args.http_port < 1 or args.http_port > 65535:
        parser.error("--http-port must be in range 1..65535")
    if args.max_jobs_per_cycle is not None and args.max_jobs_per_cycle < 1:
        parser.error("--max-jobs-per-cycle must be at least 1 when provided")
    if args.socket_path and args.dispatch_command_json is not None:
        parser.error("--socket-path cannot be combined with --dispatch-command-json")
    if args.socket_path and args.http_bind_host:
        parser.error("--socket-path cannot be combined with --http-bind-host")
    if args.http_bind_host and args.dispatch_command_json is not None:
        parser.error("--http-bind-host cannot be combined with --dispatch-command-json")
    if args.http_bind_host and not args.http_auth_token:
        parser.error("--http-auth-token is required when --http-bind-host is set")
    if args.http_auth_token and not args.http_bind_host:
        parser.error("--http-auth-token requires --http-bind-host")

    context = bootstrap_app()
    stop_requested = Event()

    def _request_stop(_signum: int, _frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    logging.getLogger("ccatv").debug(
        "service daemon bootstrapped with db=%s",
        context.settings.database_path,
    )
    context.logger.info("ccatv-service starting (version=%s)", __version__)
    try:
        if args.dispatch_command_json is not None:
            try:
                request = json.loads(args.dispatch_command_json)
            except json.JSONDecodeError as exc:
                parser.error(f"--dispatch-command-json must be valid JSON: {exc}")
            if not isinstance(request, dict):
                parser.error("--dispatch-command-json must decode to an object")
            response = _build_dispatcher(
                context,
                should_stop=stop_requested.is_set,
            ).dispatch(request)
            print(json.dumps(response, sort_keys=True))
            return 0

        if args.socket_path:
            return run_ipc_server(
                context,
                socket_path=args.socket_path,
                should_stop=stop_requested.is_set,
            )

        if args.http_bind_host:
            return run_http_server(
                context,
                bind_host=args.http_bind_host,
                port=args.http_port,
                auth_token=args.http_auth_token,
                should_stop=stop_requested.is_set,
            )

        return run_service_daemon(
            context,
            output_directory=args.output_directory,
            max_jobs_per_cycle=args.max_jobs_per_cycle,
            poll_interval_seconds=args.poll_interval_seconds,
            run_once=args.run_once,
            should_stop=stop_requested.is_set,
        )
    finally:
        close_app_context(context)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
