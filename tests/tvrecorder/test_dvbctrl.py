from __future__ import annotations

import subprocess

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
    client = DvbCtrlClient(timeout_seconds=0.01)

    def _run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", _run)

    with pytest.raises(DvbCtrlTimeoutError):
        client.run_command("stats")


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
