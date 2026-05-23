from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ccatv.app.recorder_worker import run_scheduler_worker
from ccatv.tvrecorder.orchestrator import OrchestratorResult


@dataclass(slots=True)
class StubWorker:
    ran_cycle: bool = False
    ran_forever: bool = False
    cycle_results: list[OrchestratorResult] = field(default_factory=list)

    def run_cycle(self):
        self.ran_cycle = True
        return self.cycle_results

    def run_forever(self):
        self.ran_forever = True


@dataclass(slots=True)
class StubContext:
    logger: logging.Logger
    recorder_orchestrator: object = object()


def test_run_scheduler_worker_once(monkeypatch) -> None:
    worker = StubWorker(
        cycle_results=[
            OrchestratorResult(
                job_id=10,
                scheduler_state="completed",
                recording_id=99,
                recording_state="ready",
                error=None,
            )
        ]
    )
    context = StubContext(logger=logging.getLogger("test.worker.once"))

    monkeypatch.setattr(
        "ccatv.app.recorder_worker.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    result = run_scheduler_worker(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=True,
    )

    assert result == 0
    assert worker.ran_cycle is True
    assert worker.ran_forever is False


def test_run_scheduler_worker_forever(monkeypatch) -> None:
    worker = StubWorker()
    context = StubContext(logger=logging.getLogger("test.worker.forever"))

    monkeypatch.setattr(
        "ccatv.app.recorder_worker.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    result = run_scheduler_worker(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=False,
    )

    assert result == 0
    assert worker.ran_cycle is False
    assert worker.ran_forever is True
