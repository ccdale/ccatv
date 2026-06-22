from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from http import client as http_client
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccatv.app.service_daemon import (
    IPC_MAX_REQUEST_BYTES,
    _extract_broadcast_utc,
    _handle_ipc_request,
    _run_broadcast_time_healthcheck,
    main,
    run_http_server,
    run_ipc_server,
    run_service_daemon,
)
from ccatv.tvrecorder.orchestrator import OrchestratorResult


@dataclass(slots=True)
class StubWorker:
    ran_cycle: bool = False
    cycle_count: int = 0
    fail_first_cycle: bool = False
    cycle_results: list[OrchestratorResult] = field(default_factory=list)
    now_utc_values: list[str] = field(default_factory=list)

    def run_cycle(self, *, now_utc: str | None = None):
        self.ran_cycle = True
        self.cycle_count += 1
        if now_utc is not None:
            self.now_utc_values.append(now_utc)
        if self.fail_first_cycle and self.cycle_count == 1:
            raise RuntimeError("cycle failed")
        return self.cycle_results


@dataclass(slots=True)
class StubContext:
    logger: logging.Logger
    settings: object = field(
        default_factory=lambda: SimpleNamespace(ota_epg_channel_name="BBC TWO HD")
    )
    dvbstreamer: object = field(default_factory=lambda: StubDvbStreamer())
    dvbctrl: object | None = None
    tvrecorder: object | None = None
    adapter_pool: object | None = None


@dataclass(slots=True)
class StubIdleSlot:
    adapter_index: int
    capture_controller: object
    dvbstreamer: object = field(default_factory=object)


class StubDvbStreamerState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(slots=True)
class StubManagedDvbStreamer:
    state: StubDvbStreamerState

    def status(self):
        return SimpleNamespace(state=self.state)


@dataclass(slots=True)
class StubAdapterPool:
    slots: list[StubIdleSlot]
    in_use_count: int = 0
    removed: list[int] = field(default_factory=list)

    def idle_slots_snapshot(self) -> tuple[StubIdleSlot, ...]:
        return tuple(self.slots)

    def disable_idle_slot(self, adapter_index: int):
        for slot in list(self.slots):
            if slot.adapter_index == adapter_index:
                self.slots.remove(slot)
                self.removed.append(adapter_index)
                return slot
        return None


@dataclass(slots=True)
class StubProbeService:
    fail_probe: bool = False
    filters: list[str] = field(default_factory=lambda: ["<Primary>"])
    probe_calls: int = 0

    def list_service_filters(self, *, include_primary: bool = False):
        if include_primary:
            return list(self.filters)
        return [name for name in self.filters if name != "<Primary>"]

    def run_raw(self, command: str):
        self.probe_calls += 1
        if self.fail_probe and command == "lsmuxes":
            raise RuntimeError("probe failed")
        return object()

    def stats(self):
        return object()


@dataclass(slots=True)
class StubCaptureController:
    service: StubProbeService


@dataclass(slots=True)
class StubDvbStreamer:
    started: int = 0
    fail_start: bool = False

    def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("dvbstreamer failed to launch")
        return SimpleNamespace(state="running", pid=1234)


@dataclass(slots=True)
class StubLock:
    entered: int = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False


def _start_http_server(
    context,
    *,
    auth_token: str = "test-token",
    max_requests: int = 1,
) -> tuple[threading.Thread, int]:
    listened_port: dict[str, int] = {}
    ready = threading.Event()

    def _on_listening(port: int) -> None:
        listened_port["value"] = port
        ready.set()

    thread = threading.Thread(
        target=run_http_server,
        kwargs={
            "context": context,
            "bind_host": "127.0.0.1",
            "port": 0,
            "auth_token": auth_token,
            "max_requests": max_requests,
            "on_listening": _on_listening,
        },
        daemon=True,
    )
    thread.start()

    if not ready.wait(timeout=2.0):
        raise AssertionError("HTTP server did not become ready in time")
    return thread, listened_port["value"]


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


def test_run_service_daemon_starts_dvbstreamer_before_cycle(monkeypatch) -> None:
    worker = StubWorker()
    dvbstreamer = StubDvbStreamer()
    context = StubContext(
        logger=logging.getLogger("test.daemon.start_dvbstreamer"),
        dvbstreamer=dvbstreamer,
    )

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
    assert dvbstreamer.started == 1
    assert worker.cycle_count == 1


def test_run_service_daemon_starts_all_adapter_slots_before_cycle(monkeypatch) -> None:
    worker = StubWorker()
    primary = StubDvbStreamer()
    secondary = StubDvbStreamer()
    context = StubContext(
        logger=logging.getLogger("test.daemon.start_all_adapters"),
        dvbstreamer=primary,
        adapter_pool=SimpleNamespace(
            slots=[
                SimpleNamespace(
                    adapter_index=0,
                    dvbstreamer=primary,
                    capture_controller=SimpleNamespace(service=StubProbeService()),
                ),
                SimpleNamespace(
                    adapter_index=1,
                    dvbstreamer=secondary,
                    capture_controller=SimpleNamespace(service=StubProbeService()),
                ),
            ]
        ),
    )

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
    assert primary.started == 1
    assert secondary.started == 1
    assert worker.cycle_count == 1


def test_run_service_daemon_returns_error_when_dvbstreamer_fails_to_start(
    monkeypatch,
) -> None:
    worker = StubWorker()
    context = StubContext(
        logger=logging.getLogger("test.daemon.start_dvbstreamer.error"),
        dvbstreamer=StubDvbStreamer(fail_start=True),
    )

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

    assert result == 1
    assert worker.cycle_count == 0


def test_run_service_daemon_once_returns_error_on_cycle_failure(monkeypatch) -> None:
    worker = StubWorker(fail_first_cycle=True)
    context = StubContext(logger=logging.getLogger("test.daemon.once.error"))

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

    assert result == 1
    assert worker.ran_cycle is True
    assert worker.cycle_count == 1


def test_run_service_daemon_uses_context_worker_lock(monkeypatch) -> None:
    worker = StubWorker()
    lock = StubLock()
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.lock"),
        worker_cycle_lock=lock,
    )

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
    assert worker.cycle_count == 1
    assert lock.entered == 1


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


def test_run_service_daemon_daily_metadata_sync_runs_sequential_steps(
    monkeypatch,
) -> None:
    worker = StubWorker()
    context = StubContext(
        logger=logging.getLogger("test.daemon.daily.sync"),
        settings=SimpleNamespace(ota_epg_channel_name="BBC ONE East"),
    )
    dispatch_calls: list[tuple[str, dict[str, object]]] = []

    class _StubDispatcher:
        def dispatch(self, request):
            dispatch_calls.append((request["command"], request.get("payload", {})))
            return {
                "apiVersion": "v1alpha1",
                "ok": True,
                "payload": {},
            }

    class _FixedDateTime:
        @classmethod
        def now(cls):
            del cls
            return datetime(2026, 5, 31, 3, 5, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )
    monkeypatch.setattr(
        "ccatv.app.service_daemon._build_dispatcher",
        lambda *_args, **_kwargs: _StubDispatcher(),
    )
    monkeypatch.setattr("ccatv.app.service_daemon.datetime", _FixedDateTime)

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
        enable_daily_metadata_sync=True,
        daily_metadata_sync_time="03:00",
        sd_lineup_id="UK-TEST",
        should_stop=_should_stop,
    )

    assert result == 0
    assert worker.cycle_count >= 1
    assert dispatch_calls == [
        (
            "metadata.ota.multimux.sync.run",
            {
                "grabCommand": "epgdata",
                "captureSeconds": 900.0,
                "maxRetries": 3,
                "retryDelaySeconds": 300.0,
            },
        ),
        (
            "metadata.sd.sync.run",
            {
                "clearExisting": False,
                "lineupId": "UK-TEST",
                "windowHours": 336,
            },
        ),
    ]


def test_run_service_daemon_daily_metadata_sync_requires_lineup(monkeypatch) -> None:
    worker = StubWorker()
    context = StubContext(logger=logging.getLogger("test.daemon.daily.lineup"))

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    result = run_service_daemon(
        context,
        output_directory="/tmp",
        max_jobs_per_cycle=1,
        poll_interval_seconds=5.0,
        run_once=False,
        enable_daily_metadata_sync=True,
        daily_metadata_sync_time="03:00",
        sd_lineup_id=None,
    )

    assert result == 1
    assert worker.cycle_count == 0


def test_run_service_daemon_idle_clock_skew_compensates_cycle_time(
    monkeypatch,
) -> None:
    worker = StubWorker()
    context = StubContext(logger=logging.getLogger("test.daemon.idle.clock"))

    class _StubDvbCtrl:
        def run_command(self, command: str):
            if command == "stats":
                return SimpleNamespace(stdout="Packets=1\n")
            assert command == "date"
            # 30 seconds ahead of local clock in explicit UTC format.
            return SimpleNamespace(stdout="2026-06-06T03:00:30Z\n")

    context.dvbctrl = _StubDvbCtrl()

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    tick = {"count": 0}
    time_values = [
        datetime(2026, 6, 6, 3, 0, 0, tzinfo=timezone.utc).timestamp(),
        datetime(2026, 6, 6, 3, 0, 0, tzinfo=timezone.utc).timestamp(),
        datetime(2026, 6, 6, 3, 1, 5, tzinfo=timezone.utc).timestamp(),
        datetime(2026, 6, 6, 3, 1, 5, tzinfo=timezone.utc).timestamp(),
    ]

    def _fake_time() -> float:
        if time_values:
            return time_values.pop(0)
        return datetime(2026, 6, 6, 3, 1, 5, tzinfo=timezone.utc).timestamp()

    def _should_stop() -> bool:
        return tick["count"] >= 2

    def _fake_sleep(_seconds: float) -> None:
        tick["count"] += 1

    monkeypatch.setattr("ccatv.app.service_daemon.time.time", _fake_time)
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
    assert worker.now_utc_values[0] == "2026-06-06T03:00:00Z"
    # Next cycle should include +30 second compensation from broadcast time skew.
    assert worker.now_utc_values[1] == "2026-06-06T03:00:30Z"


def test_run_service_daemon_idle_adapter_probe_removes_failed_slot(monkeypatch) -> None:
    worker = StubWorker()
    bad_service = StubProbeService(fail_probe=True)
    good_service = StubProbeService(fail_probe=False)
    pool = StubAdapterPool(
        slots=[
            StubIdleSlot(adapter_index=0, capture_controller=StubCaptureController(service=bad_service)),
            StubIdleSlot(adapter_index=1, capture_controller=StubCaptureController(service=good_service)),
        ]
    )
    context = StubContext(logger=logging.getLogger("test.daemon.idle.adapters"))
    context.adapter_pool = pool

    class _StubDvbCtrl:
        def run_command(self, command: str):
            if command == "stats":
                return SimpleNamespace(stdout="Packets=1\n")
            assert command == "date"
            return SimpleNamespace(stdout="2026-06-06T03:00:00Z\n")

    context.dvbctrl = _StubDvbCtrl()

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    ticks = {"count": 0}

    def _should_stop() -> bool:
        return ticks["count"] >= 1

    def _fake_sleep(_seconds: float) -> None:
        ticks["count"] += 1

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
    assert pool.removed == [0]
    assert [slot.adapter_index for slot in pool.slots] == [1]


def test_run_service_daemon_idle_adapter_probe_skips_non_primary_filters(
    monkeypatch,
) -> None:
    worker = StubWorker()
    busy_service = StubProbeService(filters=["<Primary>", "sports-filter"])
    pool = StubAdapterPool(
        slots=[
            StubIdleSlot(
                adapter_index=0,
                capture_controller=StubCaptureController(service=busy_service),
            ),
        ]
    )
    context = StubContext(logger=logging.getLogger("test.daemon.idle.adapters.busy"))
    context.adapter_pool = pool

    class _StubDvbCtrl:
        def run_command(self, command: str):
            if command == "stats":
                return SimpleNamespace(stdout="Packets=1\n")
            assert command == "date"
            return SimpleNamespace(stdout="2026-06-06T03:00:00Z\n")

    context.dvbctrl = _StubDvbCtrl()

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    ticks = {"count": 0}

    def _should_stop() -> bool:
        return ticks["count"] >= 1

    def _fake_sleep(_seconds: float) -> None:
        ticks["count"] += 1

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
    assert pool.removed == []
    assert busy_service.probe_calls == 0


def test_run_service_daemon_idle_adapter_probe_skips_stopped_slots(
    monkeypatch,
) -> None:
    worker = StubWorker()
    stopped_service = StubProbeService(fail_probe=True)
    running_service = StubProbeService(fail_probe=False)
    pool = StubAdapterPool(
        slots=[
            StubIdleSlot(
                adapter_index=0,
                capture_controller=StubCaptureController(service=stopped_service),
                dvbstreamer=StubManagedDvbStreamer(state=StubDvbStreamerState.STOPPED),
            ),
            StubIdleSlot(
                adapter_index=1,
                capture_controller=StubCaptureController(service=running_service),
                dvbstreamer=StubManagedDvbStreamer(state=StubDvbStreamerState.RUNNING),
            ),
        ]
    )
    context = StubContext(logger=logging.getLogger("test.daemon.idle.adapters.stopped"))
    context.adapter_pool = pool

    class _StubDvbCtrl:
        def run_command(self, command: str):
            if command == "stats":
                return SimpleNamespace(stdout="Packets=1\n")
            assert command == "date"
            return SimpleNamespace(stdout="2026-06-06T03:00:00Z\n")

    context.dvbctrl = _StubDvbCtrl()

    monkeypatch.setattr(
        "ccatv.app.service_daemon.create_scheduler_worker",
        lambda *_args, **_kwargs: worker,
    )

    ticks = {"count": 0}

    def _should_stop() -> bool:
        return ticks["count"] >= 1

    def _fake_sleep(_seconds: float) -> None:
        ticks["count"] += 1

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
    assert pool.removed == []
    assert stopped_service.probe_calls == 0
    assert running_service.probe_calls == 1


def test_clock_healthcheck_treats_no_date_received_as_not_ready(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.daemon.clock.no_date")

    class _StubDvbCtrl:
        def run_command(self, command: str):
            assert command == "date"
            return SimpleNamespace(stdout="No date/time has been received!\n")

    context = StubContext(
        logger=logger,
        settings=SimpleNamespace(ota_epg_channel_name="BBC ONE East"),
    )
    context.dvbctrl = _StubDvbCtrl()

    with caplog.at_level(logging.DEBUG):
        skew = _run_broadcast_time_healthcheck(
            context=context,
            logger=logger,
            now_timestamp=datetime(2026, 6, 6, 3, 0, 0, tzinfo=timezone.utc).timestamp(),
            skew_threshold_seconds=60.0,
        )

    assert skew is None
    assert "not ready yet" in caplog.text
    assert "could not parse broadcast time" not in caplog.text


def test_clock_healthcheck_retries_after_idle_retune(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.daemon.clock.retry_retune")
    calls: list[str] = []

    class _StubDvbCtrl:
        def run_command(self, command: str):
            calls.append(command)
            assert command == "date"
            if len(calls) == 1:
                return SimpleNamespace(stdout="No date/time has been received!\n")
            return SimpleNamespace(stdout="2026-06-06T03:00:30Z\n")

    class _StubTvRecorder:
        def resolve_service_name(self, name: str) -> str:
            return name

        def select_service(self, name: str):
            assert name == "BBC ONE East"
            return object()

    context = StubContext(
        logger=logger,
        settings=SimpleNamespace(ota_epg_channel_name="BBC ONE East"),
    )
    context.dvbctrl = _StubDvbCtrl()
    context.tvrecorder = _StubTvRecorder()

    with caplog.at_level(logging.DEBUG):
        skew = _run_broadcast_time_healthcheck(
            context=context,
            logger=logger,
            now_timestamp=datetime(2026, 6, 6, 3, 0, 0, tzinfo=timezone.utc).timestamp(),
            skew_threshold_seconds=60.0,
        )

    assert skew == 30.0
    assert calls == ["date", "date"]
    assert "requested retune" in caplog.text


def test_clock_healthcheck_restarts_primary_dvbstreamer_when_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.daemon.clock.restart_primary")

    class _StubManaged:
        def __init__(self) -> None:
            self.running = False
            self.started = 0

        def health_check(self):
            state = "running" if self.running else "stopped"
            return SimpleNamespace(state=state)

        def start(self):
            self.running = True
            self.started += 1
            return SimpleNamespace(state="running", pid=777)

    managed = _StubManaged()

    class _StubDvbCtrl:
        def run_command(self, command: str):
            if not managed.running:
                raise RuntimeError("Failed to connect to host localhost port 54197")
            if command == "stats":
                return SimpleNamespace(stdout="Packets=1\n")
            assert command == "date"
            return SimpleNamespace(stdout="2026-06-06T03:00:30Z\n")

    context = StubContext(
        logger=logger,
        settings=SimpleNamespace(ota_epg_channel_name="BBC ONE East"),
    )
    context.dvbstreamer = managed
    context.dvbctrl = _StubDvbCtrl()

    with caplog.at_level(logging.WARNING):
        skew = _run_broadcast_time_healthcheck(
            context=context,
            logger=logger,
            now_timestamp=datetime(2026, 6, 6, 3, 0, 0, tzinfo=timezone.utc).timestamp(),
            skew_threshold_seconds=60.0,
        )

    assert skew == 30.0
    assert managed.started == 1
    assert "restarted primary dvbstreamer" in caplog.text


def test_extract_broadcast_utc_interprets_ctime_as_local_time(monkeypatch) -> None:
    class _FakeTime:
        @staticmethod
        def mktime(_tuple) -> float:
            return datetime(2026, 6, 6, 2, 0, 0, tzinfo=timezone.utc).timestamp()

    monkeypatch.setattr("ccatv.app.service_daemon.time", _FakeTime)

    parsed = _extract_broadcast_utc("Sat Jun  6 03:00:00 2026\n")

    assert parsed is not None
    assert parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-06-06T02:00:00Z"


def test_main_dispatch_command_json(monkeypatch, capsys) -> None:
    context = SimpleNamespace(
        settings=SimpleNamespace(database_path=":memory:"),
        persistence=SimpleNamespace(connection=SimpleNamespace(close=lambda: None)),
        dvbstreamer=SimpleNamespace(stop=lambda force_kill=True: None),
        logger=logging.getLogger("test.daemon.dispatch"),
    )

    class _StubDispatcher:
        def __init__(
            self,
            _context,
            *,
            should_stop=None,
            worker_cycle_lock=None,
        ) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr("ccatv.app.service_daemon.bootstrap_app", lambda: context)
    monkeypatch.setattr(
        "ccatv.app.service_daemon.close_app_context", lambda _context: None
    )
    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "abc-123",
        "payload": {},
    }
    result = main(["--dispatch-command-json", json.dumps(request)])

    assert result == 0
    output = capsys.readouterr().out.strip()
    response = json.loads(output)
    assert response["ok"] is True
    assert response["requestId"] == "abc-123"


def test_main_dispatch_command_json_passes_stop_predicate(monkeypatch, capsys) -> None:
    context = SimpleNamespace(
        settings=SimpleNamespace(database_path=":memory:"),
        persistence=SimpleNamespace(connection=SimpleNamespace(close=lambda: None)),
        dvbstreamer=SimpleNamespace(stop=lambda force_kill=True: None),
        logger=logging.getLogger("test.daemon.dispatch.stop"),
        worker_cycle_lock=StubLock(),
    )
    captured = {}

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            captured["should_stop"] = should_stop
            captured["worker_cycle_lock"] = worker_cycle_lock

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr("ccatv.app.service_daemon.bootstrap_app", lambda: context)
    monkeypatch.setattr(
        "ccatv.app.service_daemon.close_app_context", lambda _context: None
    )
    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "stop-1",
        "payload": {},
    }
    result = main(["--dispatch-command-json", json.dumps(request)])

    assert result == 0
    output = capsys.readouterr().out.strip()
    response = json.loads(output)
    assert response["ok"] is True
    assert callable(captured["should_stop"])
    assert captured["worker_cycle_lock"] is context.worker_cycle_lock


def test_main_logs_service_version_on_startup(monkeypatch, capsys) -> None:
    logged: list[tuple[object, ...]] = []

    class _Logger:
        def info(self, *args, **kwargs) -> None:
            logged.append(args)

    context = SimpleNamespace(
        settings=SimpleNamespace(database_path=":memory:"),
        persistence=SimpleNamespace(connection=SimpleNamespace(close=lambda: None)),
        dvbstreamer=SimpleNamespace(stop=lambda force_kill=True: None),
        logger=_Logger(),
    )

    class _StubDispatcher:
        def __init__(
            self,
            _context,
            *,
            should_stop=None,
            worker_cycle_lock=None,
        ) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr("ccatv.app.service_daemon.bootstrap_app", lambda: context)
    monkeypatch.setattr(
        "ccatv.app.service_daemon.close_app_context", lambda _context: None
    )
    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "log-1",
        "payload": {},
    }
    result = main(["--dispatch-command-json", json.dumps(request)])

    assert result == 0
    _ = capsys.readouterr()
    assert len(logged) == 1
    assert str(logged[0][0]).startswith("ccatv-service starting")


def test_handle_ipc_request_rejects_invalid_json() -> None:
    class _StubDispatcher:
        def dispatch(self, _request):
            return {"ok": True}

    response_bytes = _handle_ipc_request(b"{", _StubDispatcher())
    response = json.loads(response_bytes.decode("utf-8"))

    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"


def test_handle_ipc_request_maps_dispatcher_exception_to_error() -> None:
    class _StubDispatcher:
        def dispatch(self, _request):
            raise RuntimeError("boom")

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "explode-1",
        "payload": {},
    }

    response_bytes = _handle_ipc_request(
        json.dumps(request).encode("utf-8"),
        _StubDispatcher(),
    )
    response = json.loads(response_bytes.decode("utf-8"))

    assert response["ok"] is False
    assert response["requestId"] == "explode-1"
    assert response["error"]["code"] == "INTERNAL_ERROR"
    assert "dispatcher failure" in response["error"]["message"]


def test_run_ipc_server_handles_health_request(monkeypatch, tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {
                    "status": "ok",
                },
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    socket_path = tmp_path / "ccatv.sock"

    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={
            "context": context,
            "socket_path": str(socket_path),
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("socket did not become ready")

    request = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "ipc-1",
        "payload": {},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(json.dumps(request).encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        response_raw = client.recv(4096)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    response = json.loads(response_raw.decode("utf-8"))
    assert response["ok"] is True
    assert response["requestId"] == "ipc-1"
    assert response["payload"]["status"] == "ok"


def test_main_rejects_socket_and_dispatch_json_together() -> None:
    with pytest.raises(SystemExit):
        main([
            "--socket-path",
            "/tmp/ccatv.sock",
            "--dispatch-command-json",
            "{}",
        ])


def test_main_rejects_http_without_auth_token() -> None:
    with pytest.raises(SystemExit):
        main([
            "--http-bind-host",
            "127.0.0.1",
        ])


def test_main_rejects_http_and_socket_together() -> None:
    with pytest.raises(SystemExit):
        main([
            "--socket-path",
            "/tmp/ccatv.sock",
            "--http-bind-host",
            "127.0.0.1",
            "--http-auth-token",
            "test-token",
        ])


def test_run_http_server_handles_authenticated_command_request(
    monkeypatch, tmp_path: Path
) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {
                    "status": "ok",
                    "echoCommand": request.get("command"),
                },
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    request_payload = {
        "apiVersion": "v1alpha1",
        "command": "service.info.get",
        "requestId": "http-1",
        "payload": {},
    }
    connection.request(
        "POST",
        "/api/v1/command",
        body=json.dumps(request_payload),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 200
    assert decoded["ok"] is True
    assert decoded["requestId"] == "http-1"
    assert decoded["payload"]["echoCommand"] == "service.info.get"


def test_run_http_server_dispatches_with_threaded_transport(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.thread_affinity"),
        worker_cycle_lock=StubLock(),
    )

    class _ThreadedDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self._thread_ids: set[int] = set()

        def dispatch(self, request):
            self._thread_ids.add(threading.get_ident())
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher",
        _ThreadedDispatcher,
    )

    thread, http_port = _start_http_server(context)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    request_payload = {
        "apiVersion": "v1alpha1",
        "command": "service.health.get",
        "requestId": "thread-affinity-1",
        "payload": {},
    }
    connection.request(
        "POST",
        "/api/v1/command",
        body=json.dumps(request_payload),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 200
    assert decoded["ok"] is True
    assert decoded["requestId"] == "thread-affinity-1"


def test_run_http_server_rejects_missing_bearer_token(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.auth"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {
                    "status": "ok",
                },
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request(
        "POST",
        "/api/v1/command",
        body=json.dumps(
            {
                "apiVersion": "v1alpha1",
                "command": "service.health.get",
                "requestId": "http-unauth",
                "payload": {},
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 401
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_run_http_server_handles_authenticated_health_get(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.health"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request(
        "GET",
        "/health",
        headers={"Authorization": "Bearer test-token"},
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 200
    assert decoded["ok"] is True
    assert decoded["payload"]["status"] == "ok"


def test_run_http_server_rejects_unauthenticated_health_get(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.health.unauth"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request("GET", "/health")
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 401
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_run_http_server_maps_rate_limit_code_to_429(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.sd.rate_limit"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": False,
                "error": {
                    "code": "SD_RATE_LIMITED",
                    "message": "slow down",
                    "retryable": True,
                    "details": {"retryAfterSeconds": 30},
                },
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request(
        "POST",
        "/api/v1/command",
        body=json.dumps(
            {
                "apiVersion": "v1alpha1",
                "command": "metadata.sd.sync.run",
                "requestId": "http-rate-limit",
                "payload": {},
            }
        ),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 429
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "SD_RATE_LIMITED"


def test_run_http_server_rejects_empty_request_body(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.empty"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    connection = http_client.HTTPConnection("127.0.0.1", http_port, timeout=2.0)
    connection.request(
        "POST",
        "/api/v1/command",
        body="",
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
            "Content-Length": "0",
        },
    )
    response = connection.getresponse()
    response_body = response.read()
    connection.close()

    thread.join(timeout=2.0)
    assert not thread.is_alive()

    decoded = json.loads(response_body.decode("utf-8"))
    assert response.status == 400
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "VALIDATION_ERROR"
    assert decoded["error"]["message"] == "request body is empty"


def test_run_http_server_rejects_negative_content_length(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.neg.length"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(2.0)
        raw.connect(("127.0.0.1", http_port))
        raw.sendall(
            b"POST /api/v1/command HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: -5\r\n"
            b"Connection: close\r\n\r\n"
        )
        chunks: list[bytes] = []
        while True:
            block = raw.recv(4096)
            if not block:
                break
            chunks.append(block)
        response_raw = b"".join(chunks)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert b"400" in response_raw
    assert b"Content-Length must be >= 0" in response_raw


def test_run_http_server_rejects_non_integer_content_length(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.bad.length"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(2.0)
        raw.connect(("127.0.0.1", http_port))
        raw.sendall(
            b"POST /api/v1/command HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: abc\r\n"
            b"Connection: close\r\n\r\n"
        )
        chunks: list[bytes] = []
        while True:
            block = raw.recv(4096)
            if not block:
                break
            chunks.append(block)
        response_raw = b"".join(chunks)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert b"400" in response_raw
    assert b"Content-Length must be an integer" in response_raw


def test_run_http_server_rejects_oversized_request(monkeypatch) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.http.oversized"),
        worker_cycle_lock=StubLock(),
    )

    class _StubDispatcher:
        def __init__(self, _context, *, should_stop, worker_cycle_lock) -> None:
            self.context = _context

        def dispatch(self, request):
            return {
                "apiVersion": "v1alpha1",
                "requestId": request.get("requestId"),
                "ok": True,
                "payload": {"status": "ok"},
            }

    monkeypatch.setattr(
        "ccatv.app.service_daemon.ServiceCommandDispatcher", _StubDispatcher
    )

    thread, http_port = _start_http_server(context)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(2.0)
        raw.connect(("127.0.0.1", http_port))
        raw.sendall(
            b"POST /api/v1/command HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {IPC_MAX_REQUEST_BYTES + 1}\r\n".encode("utf-8")
            + b"Connection: close\r\n\r\n"
        )
        chunks: list[bytes] = []
        while True:
            block = raw.recv(4096)
            if not block:
                break
            chunks.append(block)
        response_raw = b"".join(chunks)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert b"413" in response_raw
    assert b"request too large" in response_raw


def test_run_ipc_server_rejects_empty_request(tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc.empty"),
        worker_cycle_lock=StubLock(),
    )
    socket_path = tmp_path / "ccatv-empty.sock"

    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={
            "context": context,
            "socket_path": str(socket_path),
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("socket did not become ready")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(b"\n")
        client.shutdown(socket.SHUT_WR)
        response_raw = client.recv(4096)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    response = json.loads(response_raw.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"
    assert response["error"]["message"] == "request body is empty"


def test_run_ipc_server_rejects_whitespace_only_request(tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc.empty.whitespace"),
        worker_cycle_lock=StubLock(),
    )
    socket_path = tmp_path / "ccatv-empty-whitespace.sock"

    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={
            "context": context,
            "socket_path": str(socket_path),
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("socket did not become ready")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(b"   \n   ")
        client.shutdown(socket.SHUT_WR)
        response_raw = client.recv(4096)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    response = json.loads(response_raw.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"
    assert response["error"]["message"] == "request body is empty"


def test_run_ipc_server_rejects_oversized_request(tmp_path: Path) -> None:
    context = SimpleNamespace(
        logger=logging.getLogger("test.daemon.ipc.oversized"),
        worker_cycle_lock=StubLock(),
    )
    socket_path = tmp_path / "ccatv-oversized.sock"

    thread = threading.Thread(
        target=run_ipc_server,
        kwargs={
            "context": context,
            "socket_path": str(socket_path),
            "max_requests": 1,
        },
        daemon=True,
    )
    thread.start()

    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("socket did not become ready")

    oversized_payload = b"x" * (IPC_MAX_REQUEST_BYTES + 1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(oversized_payload)
        client.shutdown(socket.SHUT_WR)
        response_raw = client.recv(4096)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    response = json.loads(response_raw.decode("utf-8"))
    assert response["ok"] is False
    assert response["error"]["code"] == "VALIDATION_ERROR"
    assert response["error"]["message"] == "request too large"
    assert response["error"]["details"]["maxBytes"] == IPC_MAX_REQUEST_BYTES
