from __future__ import annotations

import subprocess
import time

import pytest

from ccatv.tvrecorder.dvbctrl import (
    DvbCtrlClient,
    DvbCtrlCommandError,
    DvbCtrlExecutableNotFound,
    DvbCtrlTimeoutError,
)


def test_run_command_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DvbCtrlClient(executable_path="dvbctrl", host="localhost", adapter_index=1)

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _run)

    result = client.run_command("current")

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.command == ("dvbctrl", "-h", "localhost", "-a", "1", "current")


def test_run_command_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DvbCtrlClient()

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=2,
            stdout="",
            stderr="bad command",
        )

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(DvbCtrlCommandError, match="returncode=2"):
        client.run_command("stats")


def test_run_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DvbCtrlClient(timeout_seconds=0.01, transient_retry_count=0)

    def _run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(DvbCtrlTimeoutError):
        client.run_command("stats")


def test_run_command_retries_timeout_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DvbCtrlClient(
        timeout_seconds=0.01,
        transient_retry_count=2,
        transient_retry_delay_seconds=0.01,
    )
    calls = {"count": 0}

    def _run(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    sleep_calls: list[float] = []

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    result = client.run_command("stats")

    assert result.returncode == 0
    assert calls["count"] == 2
    assert sleep_calls == [0.01]


def test_run_command_timeout_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DvbCtrlClient(
        timeout_seconds=0.01,
        transient_retry_count=2,
        transient_retry_delay_seconds=0.1,
    )
    calls = {"count": 0}

    def _run(*args, **kwargs):
        calls["count"] += 1
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    sleep_calls: list[float] = []

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(DvbCtrlTimeoutError):
        client.run_command("stats")

    assert calls["count"] == 3
    assert sleep_calls == [0.1, 0.2]


def test_run_command_retries_transient_command_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DvbCtrlClient(transient_retry_count=2, transient_retry_delay_seconds=0.01)
    calls = {"count": 0}

    def _run(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=1,
                stdout="",
                stderr="connection refused",
            )
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    sleep_calls: list[float] = []

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    result = client.run_command("current")

    assert result.returncode == 0
    assert calls["count"] == 2
    assert sleep_calls == [0.01]


def test_run_command_transient_error_exhausts_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DvbCtrlClient(transient_retry_count=2, transient_retry_delay_seconds=0.1)
    calls = {"count": 0}

    def _run(*args, **kwargs):
        calls["count"] += 1
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="connection refused",
        )

    sleep_calls: list[float] = []

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(DvbCtrlCommandError, match="returncode=1"):
        client.run_command("stats")

    assert calls["count"] == 3
    assert sleep_calls == [0.1, 0.2]


def test_run_command_does_not_retry_non_transient_command_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DvbCtrlClient(transient_retry_count=3, transient_retry_delay_seconds=0.01)
    calls = {"count": 0}

    def _run(*args, **kwargs):
        calls["count"] += 1
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=2,
            stdout="",
            stderr="bad command",
        )

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(DvbCtrlCommandError, match="returncode=2"):
        client.run_command("stats")

    assert calls["count"] == 1


def test_run_command_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DvbCtrlClient(executable_path="does-not-exist")

    def _run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(DvbCtrlExecutableNotFound):
        client.run_command("current")


def test_run_command_omits_inline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DvbCtrlClient()

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _run)

    result = client.run_command("current")

    assert result.command == (
        "dvbctrl",
        "-h",
        "localhost",
        "-a",
        "0",
        "current",
    )
