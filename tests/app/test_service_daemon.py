from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ccatv.app.service_daemon import run_service_daemon
from ccatv.tvrecorder.orchestrator import OrchestratorResult


@dataclass(slots=True)
class StubWorker:
    ran_cycle: bool = False
    cycle_count: int = 0
    fail_first_cycle: bool = False
    cycle_results: list[OrchestratorResult] = field(default_factory=list)

    def run_cycle(self):
        self.ran_cycle = True
        self.cycle_count += 1
        if self.fail_first_cycle and self.cycle_count == 1:
            raise RuntimeError("cycle failed")
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

    stop_after_first_cycle = {"value": False}

    def _should_stop() -> bool:
        return stop_after_first_cycle["value"]

    def _fake_sleep(_seconds: float) -> None:
        stop_after_first_cycle["value"] = True

    monkeypatch.setattr("ccatv.app.service_daemon.time.sleep", _fake_sleep)

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=False,
        should_stop=_should_stop,
    )

    assert result == 0
    assert worker.cycle_count >= 1


def test_run_service_daemon_forever_continues_after_cycle_error(monkeypatch) -> None:
    worker = StubWorker(fail_first_cycle=True)
    context = StubContext(logger=logging.getLogger("test.daemon.forever.error"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    sleep_calls = {"count": 0}

    def _should_stop() -> bool:
        return sleep_calls["count"] >= 1

    def _fake_sleep(_seconds: float) -> None:
        sleep_calls["count"] += 1

    monkeypatch.setattr("ccatv.app.service_daemon.time.sleep", _fake_sleep)

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=False,
        should_stop=_should_stop,
    )

    assert result == 0
    assert worker.cycle_count >= 1
