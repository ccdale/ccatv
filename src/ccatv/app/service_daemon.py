from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence

from ccatv.app.bootstrap import AppContext, bootstrap_app
from ccatv.app.recorder_worker import create_scheduler_worker


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
    return parser


def run_service_daemon(
    context: AppContext,
    *,
    output_directory: str,
    max_jobs_per_cycle: int | None,
    poll_interval_seconds: float,
    run_once: bool,
) -> int:
    logger = context.logger
    worker = create_scheduler_worker(
        context,
        output_directory=output_directory,
        max_jobs_per_cycle=max_jobs_per_cycle,
        poll_interval_seconds=poll_interval_seconds,
    )

    logger.info(
        "service daemon started (api_transport=planned, poll_interval_seconds=%s)",
        poll_interval_seconds,
    )

    if run_once:
        results = worker.run_cycle()
        logger.info("service daemon completed one cycle (jobs=%d)", len(results))
        return 0

    while True:
        results = worker.run_cycle()
        if results:
            logger.info("service daemon cycle completed with %d due jobs", len(results))
        time.sleep(poll_interval_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be greater than 0")
    if args.max_jobs_per_cycle is not None and args.max_jobs_per_cycle < 1:
        parser.error("--max-jobs-per-cycle must be at least 1 when provided")

    context = bootstrap_app()
    logging.getLogger("ccatv").debug(
        "service daemon bootstrapped with db=%s",
        context.settings.database_path,
    )
    return run_service_daemon(
        context,
        output_directory=args.output_directory,
        max_jobs_per_cycle=args.max_jobs_per_cycle,
        poll_interval_seconds=args.poll_interval_seconds,
        run_once=args.run_once,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
