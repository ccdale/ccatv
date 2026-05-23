from __future__ import annotations

import io
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccatv.cli import CliDependencies, main, run_setup, setup_main
from ccatv.metadata.schedules_direct_contract import (
    SDCredentials,
    SDProgram,
    SDScheduleEntry,
    SDStation,
)
from ccatv.runtime_config import RuntimeConfig, RuntimeConfigStore
from ccatv.storage import initialize_database
from ccatv.tvrecorder.config import TvRecorderConfigStore


def test_run_setup_persists_credentials(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=RuntimeConfigStore(config_dir=tmp_path),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = run_setup(
        Namespace(adapter_count=None, host=None, username=None), deps=deps
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Saved dvbstreamer credentials" in stdout.getvalue()
    saved = TvRecorderConfigStore(config_dir=tmp_path).load()
    assert saved.dvbctrl_credentials is not None
    assert saved.dvbctrl_credentials.username == "alice"
    assert saved.dvbctrl_credentials.password == "secret"
    runtime = RuntimeConfigStore(config_dir=tmp_path).load()
    assert runtime.dvbstreamer_host == "localhost"
    assert runtime.dvb_adapter_count == 1


def test_run_setup_rejects_mismatched_passwords(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "different"])
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=RuntimeConfigStore(config_dir=tmp_path),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = run_setup(
        Namespace(adapter_count=None, host=None, username=None), deps=deps
    )

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
        runtime_store=RuntimeConfigStore(config_dir=tmp_path),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = setup_main(["--username", "alice"], deps=deps)

    assert exit_code == 0
    saved = TvRecorderConfigStore(config_dir=tmp_path).load()
    assert saved.dvbctrl_credentials is not None
    assert saved.dvbctrl_credentials.username == "alice"


def test_run_setup_rejects_invalid_adapter_count(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=RuntimeConfigStore(config_dir=tmp_path),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = run_setup(
        Namespace(adapter_count=0, host="druidmedia", username="alice"),
        deps=deps,
    )

    assert exit_code == 2
    assert "Adapter count must be greater than 0." in stderr.getvalue()


def test_run_setup_rejects_whitespace_host(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=RuntimeConfigStore(config_dir=tmp_path),
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = run_setup(
        Namespace(adapter_count=1, host="   ", username="alice"),
        deps=deps,
    )

    assert exit_code == 2
    assert "Host cannot be empty." in stderr.getvalue()


def test_run_setup_preserves_runtime_defaults_when_host_not_provided(
    tmp_path: Path,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    runtime_store.save(
        RuntimeConfig(
            dvb_adapter_count=4,
            dvbstreamer_host="druidmedia",
        )
    )
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=runtime_store,
        stderr=stderr,
        stdout=stdout,
        store=TvRecorderConfigStore(config_dir=tmp_path),
    )

    exit_code = run_setup(
        Namespace(adapter_count=None, host=None, username=None), deps=deps
    )

    assert exit_code == 0
    runtime = RuntimeConfigStore(config_dir=tmp_path).load()
    assert runtime.dvbstreamer_host == "druidmedia"
    assert runtime.dvb_adapter_count == 4


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


def test_epg_sync_sd_command_runs_once(tmp_path: Path, monkeypatch) -> None:
    class _StubCredentialStore:
        def __init__(self, path: Path | None = None) -> None:
            self.path = path

        def load(self) -> SDCredentials:
            return SDCredentials(username="alice", password="secret")

    class _StubClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def authenticate(self, credentials: SDCredentials) -> None:
            assert credentials.username == "alice"

        async def get_lineup_stations(self, lineup_id: str) -> list[SDStation]:
            assert lineup_id == "UK-TEST"
            return [
                SDStation(
                    station_id="101",
                    callsign="BBC1",
                    name="BBC One",
                    channel="1",
                )
            ]

        async def get_schedules(self, lineup_id, window) -> list[SDScheduleEntry]:
            del lineup_id
            assert window.end_utc > window.start_utc
            start_utc = datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc)
            return [
                SDScheduleEntry(
                    station_id="101",
                    program_id="EP0001",
                    start_utc=start_utc,
                    end_utc=start_utc + timedelta(minutes=30),
                    duration_seconds=1800,
                )
            ]

        async def get_programs(self, program_ids: list[str]) -> list[SDProgram]:
            assert program_ids == ["EP0001"]
            return [SDProgram(program_id="EP0001", title="Morning News")]

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "ccatv.cli.SchedulesDirectCredentialStore", _StubCredentialStore
    )
    monkeypatch.setattr("ccatv.cli.SchedulesDirectHttpClient", _StubClient)

    db_path = tmp_path / "ccatv.sqlite3"
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(stdout=stdout, stderr=stderr)

    exit_code = main(
        [
            "epg-sync-sd",
            "--lineup-id",
            "UK-TEST",
            "--window-hours",
            "24",
            "--database-path",
            str(db_path),
            "--credentials-path",
            str(tmp_path / "tvrecorder.json"),
        ],
        deps=deps,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Schedules Direct sync complete" in stdout.getvalue()

    connection = initialize_database(db_path)
    try:
        row = connection.execute(
            "SELECT status FROM epg_ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row[0] == "ok"


def test_epg_sync_sd_command_rejects_invalid_window(
    tmp_path: Path, monkeypatch
) -> None:
    class _StubCredentialStore:
        def __init__(self, path: Path | None = None) -> None:
            self.path = path

        def load(self) -> SDCredentials:
            return SDCredentials(username="alice", password="secret")

    class _StubClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def authenticate(self, credentials: SDCredentials) -> None:
            del credentials

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "ccatv.cli.SchedulesDirectCredentialStore", _StubCredentialStore
    )
    monkeypatch.setattr("ccatv.cli.SchedulesDirectHttpClient", _StubClient)

    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(stdout=stdout, stderr=stderr)

    exit_code = main(
        [
            "epg-sync-sd",
            "--lineup-id",
            "UK-TEST",
            "--window-hours",
            "0",
            "--database-path",
            str(tmp_path / "ccatv.sqlite3"),
        ],
        deps=deps,
    )

    assert exit_code == 2
    assert "--window-hours must be greater than 0" in stderr.getvalue()
