from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ccatv.app.service_daemon import run_service_daemon
from ccatv.tvrecorder.orchestrator import OrchestratorResult


@dataclass(slots=True)
class StubWorker:
    ran_cycle: bool = False
    cycle_count: int = 0
    cycle_results: list[OrchestratorResult] = field(default_factory=list)

    def run_cycle(self):
        self.ran_cycle = True
        self.cycle_count += 1
        return self.cycle_results


@dataclass(slots=True)
class StubContext:
    logger: logging.Logger


def test_run_service_daemon_once(monkeypatch) -> None:
    worker = StubWorker(
        cycle_results=[
            OrchestratorResult(
                job_id=12,
                scheduler_state="completed",
                recording_id=91,
                recording_state="ready",
                error=None,
            )
        ]
    )
    context = StubContext(logger=logging.getLogger("test.daemon.once"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=True,
    )

    assert result == 0
    assert worker.ran_cycle is True
    assert worker.cycle_count == 1


def test_run_service_daemon_forever_runs_cycles(monkeypatch) -> None:
    worker = StubWorker()
    context = StubContext(logger=logging.getLogger("test.daemon.forever"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    call_count = {"value": 0}

    def _fake_sleep(_seconds: float) -> None:
        call_count["value"] += 1
        if call_count["value"] >= 1:
            raise KeyboardInterrupt()

    monkeypatch.setattr("ccatv.app.service_daemon.time.sleep", _fake_sleep)

    try:
        run_service_daemon(
            context,
            output_directory="/tmp",
            max_jobs_per_cycle=1,
            poll_interval_seconds=5.0,
            run_once=False,
        )
    except KeyboardInterrupt:
        pass

    assert worker.cycle_count >= 1
