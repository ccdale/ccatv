from __future__ import annotations

import io
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from ccatv.cli import CliDependencies, main, run_setup, setup_main
from ccatv.tvrecorder.config import TvRecorderConfigStore


def test_run_setup_persists_credentials(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = run_setup(Namespace(username=None), deps=deps)

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Saved tvrecorder configuration" in stdout.getvalue()
    saved = TvRecorderConfigStore(config_dir=tmp_path).load()
    assert saved.dvbctrl_credentials is not None
    assert saved.dvbctrl_credentials.username == "alice"
    assert saved.dvbctrl_credentials.password == "secret"


def test_run_setup_rejects_mismatched_passwords(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "different"])
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = run_setup(Namespace(username=None), deps=deps)

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert "Passwords did not match." in stderr.getvalue()
    assert TvRecorderConfigStore(config_dir=tmp_path).load().dvbctrl_credentials is None


def test_setup_main_routes_to_setup_command(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    deps = CliDependencies(
        input_fn=lambda prompt: "ignored",
        password_fn=lambda prompt: next(prompts),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = setup_main(["--username", "alice"], deps=deps)

    assert exit_code == 0
    saved = TvRecorderConfigStore(config_dir=tmp_path).load()
    assert saved.dvbctrl_credentials is not None
    assert saved.dvbctrl_credentials.username == "alice"


def test_setup_main_uses_process_argv_when_no_args_provided(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ccatv-setup", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        setup_main()

    assert exc_info.value.code == 0


def test_main_without_subcommand_returns_usage_error() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(stdout=stdout, stderr=stderr)

    exit_code = main([], deps=deps)

    assert exit_code == 1
