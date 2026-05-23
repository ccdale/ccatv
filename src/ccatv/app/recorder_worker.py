from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from ccatv.app.bootstrap import AppContext, bootstrap_app
from ccatv.tvrecorder.orchestrator import SchedulerWorker, build_recording_output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccatv-recorder-worker",
        description="Poll scheduler jobs and execute due recordings.",
    )
    parser.add_argument(
        "--max-jobs-per-cycle",
        type=int,
        default=None,
        help="maximum due jobs to execute in a single poll cycle",
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
        help="poll interval between due-job scans (run-forever mode)",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="execute one due-job poll cycle then exit",
    )
    return parser


def create_scheduler_worker(
    context: AppContext,
    *,
    output_directory: str,
    max_jobs_per_cycle: int | None,
    poll_interval_seconds: float,
) -> SchedulerWorker:
    return SchedulerWorker(
        orchestrator=context.recorder_orchestrator,
        output_path_builder=lambda job: build_recording_output_path(
            directory=output_directory,
            job=job,
        ),
        max_jobs_per_cycle=max_jobs_per_cycle,
        poll_interval_seconds=poll_interval_seconds,
    )


def run_scheduler_worker(
    context: AppContext,
    *,
    output_directory: str,
    max_jobs_per_cycle: int | None,
    poll_interval_seconds: float,
    run_once: bool,
) -> int:
    worker = create_scheduler_worker(
        context,
        output_directory=output_directory,
        max_jobs_per_cycle=max_jobs_per_cycle,
        poll_interval_seconds=poll_interval_seconds,
    )
    logger = context.logger
    if run_once:
        results = worker.run_cycle()
        logger.info(
            "scheduler worker completed one cycle with %d due jobs", len(results)
        )
        for result in results:
            logger.info(
                "job_id=%s scheduler_state=%s recording_id=%s recording_state=%s error=%s",
                result.job_id,
                result.scheduler_state,
                result.recording_id,
                result.recording_state,
                result.error,
            )
        return 0

    logger.info(
        "starting recorder scheduler worker (poll_interval_seconds=%s)",
        poll_interval_seconds,
    )
    worker.run_forever()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.poll_interval_seconds <= 0:
        parser.error("--poll-interval-seconds must be greater than 0")
    if args.max_jobs_per_cycle is not None and args.max_jobs_per_cycle < 1:
        parser.error("--max-jobs-per-cycle must be at least 1 when provided")

    context = bootstrap_app()
    logging.getLogger("ccatv").debug(
        "recorder worker bootstrapped with db=%s",
        context.settings.database_path,
    )
    return run_scheduler_worker(
        context,
        output_directory=args.output_directory,
        max_jobs_per_cycle=args.max_jobs_per_cycle,
        poll_interval_seconds=args.poll_interval_seconds,
        run_once=args.run_once,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
