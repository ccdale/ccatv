from __future__ import annotations

import subprocess

import pytest

from ccatv.tvrecorder.manager import (
    DvbStreamerConfig,
    DvbStreamerLaunchError,
    DvbStreamerManager,
    DvbStreamerState,
    DvbStreamerStopTimeout,
)


class _FakeStderr:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class _FakeProcess:
    def __init__(
        self,
        pid: int = 1234,
        poll_value: int | None = None,
        kill_clears_timeout: bool = True,
        wait_raises_timeout: bool = False,
        stderr_text: str = "",
    ) -> None:
        self.kill_clears_timeout = kill_clears_timeout
        self.pid = pid
        self._poll_value = poll_value
        self._wait_raises_timeout = wait_raises_timeout
        self.stderr = _FakeStderr(stderr_text)
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._poll_value

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_raises_timeout:
            raise subprocess.TimeoutExpired(cmd="dvbstreamer", timeout=timeout or 0)
        self._poll_value = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        if self.kill_clears_timeout:
            self._wait_raises_timeout = False
            self._poll_value = -9


def test_start_sets_running_state(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = _FakeProcess(poll_value=None)

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig())
    status = manager.start()

    assert status.state == DvbStreamerState.RUNNING
    assert status.pid == 1234


def test_start_uses_debug_flags_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(poll_value=None)
    captured: dict[str, object] = {}

    def _popen(*args, **kwargs):
        captured["args"] = args[0]
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig(adapter_index=0))
    status = manager.start()

    assert status.state == DvbStreamerState.RUNNING
    assert captured["args"] == [
        "dvbstreamer",
        "-d",
        "-a",
        "0",
        "-i",
        "127.0.0.1",
        "-o",
        "null://",
    ]


def test_start_raises_when_executable_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _popen(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig(executable_path="missing"))

    with pytest.raises(DvbStreamerLaunchError, match="executable not found"):
        manager.start()

    assert manager.status().state == DvbStreamerState.FAILED


def test_start_raises_on_immediate_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = _FakeProcess(poll_value=2, stderr_text="invalid args")

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig())

    with pytest.raises(DvbStreamerLaunchError, match="invalid args"):
        manager.start()

    assert manager.status().state == DvbStreamerState.FAILED


def test_stop_terminates_running_process(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = _FakeProcess(poll_value=None)

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig())
    manager.start()
    status = manager.stop()

    assert fake_process.terminated is True
    assert status.state == DvbStreamerState.STOPPED
    assert status.pid is None


def test_stop_returns_stopped_when_process_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(poll_value=None)

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig())
    manager.start()
    fake_process._poll_value = 0

    status = manager.stop()

    assert fake_process.terminated is False
    assert fake_process.killed is False
    assert status.state == DvbStreamerState.STOPPED
    assert status.pid is None


def test_stop_timeout_can_force_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = _FakeProcess(poll_value=None, wait_raises_timeout=True)

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig())
    manager.start()
    status = manager.stop(force_kill=True)

    assert fake_process.killed is True
    assert status.state == DvbStreamerState.STOPPED


def test_stop_timeout_without_force_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_process = _FakeProcess(poll_value=None, wait_raises_timeout=True)

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig(), stop_timeout_seconds=1.0)
    manager.start()

    with pytest.raises(DvbStreamerStopTimeout):
        manager.stop(force_kill=False)

    status = manager.health_check()

    assert status.state == DvbStreamerState.FAILED
    assert status.pid == 1234
    assert status.last_error is not None


def test_stop_force_kill_timeout_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(
        poll_value=None,
        kill_clears_timeout=False,
        wait_raises_timeout=True,
    )

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig(), stop_timeout_seconds=1.0)
    manager.start()

    with pytest.raises(DvbStreamerStopTimeout, match="after force-kill"):
        manager.stop(force_kill=True)

    status = manager.status()
    assert fake_process.killed is True
    assert status.state == DvbStreamerState.FAILED
    assert status.pid == 1234


def test_health_check_preserves_failed_state_after_stop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(
        poll_value=None,
        kill_clears_timeout=False,
        wait_raises_timeout=True,
    )

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig(), stop_timeout_seconds=1.0)
    manager.start()

    with pytest.raises(DvbStreamerStopTimeout):
        manager.stop(force_kill=True)

    status = manager.health_check()

    assert status.state == DvbStreamerState.FAILED
    assert status.last_error is not None


def test_health_check_refreshes_failed_error_after_force_kill_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(
        poll_value=None,
        kill_clears_timeout=False,
        wait_raises_timeout=True,
    )

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig(), stop_timeout_seconds=1.0)
    manager.start()

    with pytest.raises(DvbStreamerStopTimeout):
        manager.stop(force_kill=True)

    fake_process._poll_value = 9
    fake_process._wait_raises_timeout = False

    status = manager.health_check()

    assert status.state == DvbStreamerState.FAILED
    assert status.last_error == "dvbstreamer exited with returncode 9"
    assert status.pid is None


def test_health_check_returns_stopped_when_no_process() -> None:
    manager = DvbStreamerManager(config=DvbStreamerConfig())

    status = manager.health_check()

    assert status.state == DvbStreamerState.STOPPED
    assert status.pid is None


def test_health_check_marks_stopped_on_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(poll_value=None)

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig())
    manager.start()
    fake_process._poll_value = 0

    status = manager.health_check()

    assert status.state == DvbStreamerState.STOPPED
    assert status.last_error is None
    assert status.pid is None


def test_health_check_refreshes_failed_error_after_process_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(
        poll_value=None,
        wait_raises_timeout=True,
    )

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig(), stop_timeout_seconds=1.0)
    manager.start()

    with pytest.raises(DvbStreamerStopTimeout):
        manager.stop(force_kill=False)

    fake_process._poll_value = 5
    fake_process._wait_raises_timeout = False

    status = manager.health_check()

    assert status.state == DvbStreamerState.FAILED
    assert status.last_error == "dvbstreamer exited with returncode 5"
    assert status.pid is None


def test_health_check_marks_failed_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeProcess(poll_value=None)

    def _popen(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", _popen)

    manager = DvbStreamerManager(config=DvbStreamerConfig())
    manager.start()
    fake_process._poll_value = 3

    status = manager.health_check()

    assert status.state == DvbStreamerState.FAILED
    assert status.last_error == "dvbstreamer exited with returncode 3"
    assert status.pid is None
