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
from pathlib import Path
from threading import Event

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
    if args.max_jobs_per_cycle is not None and args.max_jobs_per_cycle < 1:
        parser.error("--max-jobs-per-cycle must be at least 1 when provided")
    if args.socket_path and args.dispatch_command_json is not None:
        parser.error("--socket-path cannot be combined with --dispatch-command-json")

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
