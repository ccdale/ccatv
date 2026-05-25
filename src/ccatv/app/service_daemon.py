from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from collections.abc import Callable, Sequence
from threading import Event

from ccatv.app.bootstrap import AppContext, bootstrap_app, close_app_context
from ccatv.app.recorder_worker import create_scheduler_worker
from ccatv.app.service_dispatcher import ServiceCommandDispatcher


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
    return parser


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
        "service daemon started (api_transport=planned, poll_interval_seconds=%s)",
        poll_interval_seconds,
    )

    if run_once:
        if worker_cycle_lock is None:
            results = worker.run_cycle()
        else:
            with worker_cycle_lock:
                results = worker.run_cycle()
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
            response = ServiceCommandDispatcher(
                context,
                should_stop=stop_requested.is_set,
                worker_cycle_lock=getattr(context, "worker_cycle_lock", None),
            ).dispatch(request)
            print(json.dumps(response, sort_keys=True))
            return 0

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
