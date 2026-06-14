from __future__ import annotations

import io
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from ccatv.app.service_client import ServiceClientError
from ccatv.cli import CliDependencies, main, run_channel_map, run_setup, setup_main
from ccatv.runtime_config import RuntimeConfig, RuntimeConfigStore
from ccatv.tvrecorder.config import (
    DvbCtrlCredentials,
    TvRecorderConfig,
    TvRecorderConfigStore,
)


class _SetupStubServiceClient:
    def __init__(
        self,
        *,
        runtime_store: RuntimeConfigStore,
        store: TvRecorderConfigStore,
        failure: ServiceClientError | None = None,
    ) -> None:
        self.closed = False
        self.executed: list[tuple[str, dict[str, object]]] = []
        self._failure = failure
        self._runtime_store = runtime_store
        self._store = store

    def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        self.executed.append((command, payload))
        if self._failure is not None:
            raise self._failure

        assert command == "runtime.setup.save"

        adapter_count = int(payload["adapterCount"])
        host = str(payload["host"])
        ota_epg_channel_name = str(payload["otaEpgChannelName"])
        sd_lineup_id_raw = payload.get("sdLineupId")
        sd_lineup_id = None if sd_lineup_id_raw is None else str(sd_lineup_id_raw)
        username = str(payload["username"])
        password = str(payload["password"])

        credentials_path = self._store.save(
            TvRecorderConfig(
                dvbctrl_credentials=DvbCtrlCredentials(
                    password=password,
                    username=username,
                )
            )
        )
        runtime_path = self._runtime_store.save(
            RuntimeConfig(
                dvb_adapter_count=adapter_count,
                dvbstreamer_host=host,
                ota_epg_channel_name=ota_epg_channel_name,
                sd_lineup_id=sd_lineup_id,
            )
        )
        return {
            "credentialsPath": str(credentials_path),
            "runtimeConfigPath": str(runtime_path),
        }

    def close(self) -> None:
        self.closed = True


def test_run_setup_persists_credentials(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    store = TvRecorderConfigStore(config_dir=tmp_path)
    stub_client = _SetupStubServiceClient(runtime_store=runtime_store, store=store)
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=runtime_store,
        stderr=stderr,
        stdout=stdout,
        store=store,
        service_client_factory=lambda: stub_client,
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
    assert runtime.ota_epg_channel_name == "BBC TWO HD"
    assert stub_client.closed is True
    assert stub_client.executed


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
    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    store = TvRecorderConfigStore(config_dir=tmp_path)
    stub_client = _SetupStubServiceClient(runtime_store=runtime_store, store=store)
    deps = CliDependencies(
        input_fn=lambda prompt: "ignored",
        password_fn=lambda prompt: next(prompts),
        runtime_store=runtime_store,
        stderr=stderr,
        stdout=stdout,
        store=store,
        service_client_factory=lambda: stub_client,
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
    store = TvRecorderConfigStore(config_dir=tmp_path)
    stub_client = _SetupStubServiceClient(runtime_store=runtime_store, store=store)
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=runtime_store,
        stderr=stderr,
        stdout=stdout,
        store=store,
        service_client_factory=lambda: stub_client,
    )

    exit_code = run_setup(
        Namespace(adapter_count=None, host=None, username=None), deps=deps
    )

    assert exit_code == 0
    runtime = RuntimeConfigStore(config_dir=tmp_path).load()
    assert runtime.dvbstreamer_host == "druidmedia"
    assert runtime.dvb_adapter_count == 4


def test_run_setup_persists_ota_epg_channel_override(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    store = TvRecorderConfigStore(config_dir=tmp_path)
    stub_client = _SetupStubServiceClient(runtime_store=runtime_store, store=store)
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=runtime_store,
        stderr=stderr,
        stdout=stdout,
        store=store,
        service_client_factory=lambda: stub_client,
    )

    exit_code = run_setup(
        Namespace(
            adapter_count=1,
            host="localhost",
            ota_epg_channel_name="BBC ONE East",
            username="alice",
        ),
        deps=deps,
    )

    assert exit_code == 0
    runtime = RuntimeConfigStore(config_dir=tmp_path).load()
    assert runtime.ota_epg_channel_name == "BBC ONE East"


def test_run_setup_persists_sd_lineup_id_override(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    store = TvRecorderConfigStore(config_dir=tmp_path)
    stub_client = _SetupStubServiceClient(runtime_store=runtime_store, store=store)
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=runtime_store,
        stderr=stderr,
        stdout=stdout,
        store=store,
        service_client_factory=lambda: stub_client,
    )

    exit_code = run_setup(
        Namespace(
            adapter_count=1,
            host="localhost",
            ota_epg_channel_name="BBC ONE East",
            sd_lineup_id="UK-TEST",
            username="alice",
        ),
        deps=deps,
    )

    assert exit_code == 0
    runtime = RuntimeConfigStore(config_dir=tmp_path).load()
    assert runtime.sd_lineup_id == "UK-TEST"


def test_run_setup_surfaces_service_error(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    prompts = iter(["secret", "secret"])
    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    store = TvRecorderConfigStore(config_dir=tmp_path)
    stub_client = _SetupStubServiceClient(
        runtime_store=runtime_store,
        store=store,
        failure=ServiceClientError(
            code="VALIDATION_ERROR",
            message="bad host",
            retryable=False,
        ),
    )
    deps = CliDependencies(
        input_fn=lambda prompt: "alice",
        password_fn=lambda prompt: next(prompts),
        runtime_store=runtime_store,
        stderr=stderr,
        stdout=stdout,
        store=store,
        service_client_factory=lambda: stub_client,
    )

    exit_code = run_setup(
        Namespace(adapter_count=1, host="localhost", username="alice"),
        deps=deps,
    )

    assert exit_code == 2
    assert "Setup failed: bad host" in stderr.getvalue()
    assert stdout.getvalue() == ""
    assert stub_client.closed is True


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
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []
            self.closed = False

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "stats": {
                    "channelsUpserted": 1,
                    "programsUpserted": 1,
                    "schedulesUpserted": 1,
                    "staleSchedulesPruned": 0,
                    "ingestRunId": 7,
                }
            }

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()

    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(
        [
            "epg-sync-sd",
            "--lineup-id",
            "UK-TEST",
            "--window-hours",
            "24",
            "--database-path",
            str(tmp_path / "ccatv.sqlite3"),
            "--credentials-path",
            str(tmp_path / "tvrecorder.json"),
        ],
        deps=deps,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Schedules Direct sync complete" in stdout.getvalue()
    assert stub_client.closed is True
    assert len(stub_client.executed) == 1
    command, payload = stub_client.executed[0]
    assert command == "metadata.sd.sync.run"
    assert payload["lineupId"] == "UK-TEST"
    assert payload["windowHours"] == 24.0


def test_epg_sync_sd_command_rejects_invalid_window(
    tmp_path: Path,
) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {"stats": {}}

        def close(self) -> None:
            return None

    stub_client = _StubServiceClient()

    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

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


def test_epg_sync_ota_command_runs_once(tmp_path: Path) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []
            self.closed = False

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "stats": {
                    "channelsUpserted": 5,
                    "programsUpserted": 42,
                    "broadcastsUpserted": 96,
                    "parsedEvents": 96,
                    "ingestRunId": 33,
                }
            }

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(
        [
            "epg-sync-ota",
            "--grab-command",
            "epgdata",
            "--channel-name",
            "BBC TWO HD",
            "--capture-seconds",
            "5",
            "--database-path",
            str(tmp_path / "ccatv.sqlite3"),
        ],
        deps=deps,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "OTA EPG sync starting..." in stdout.getvalue()
    assert "OTA EPG sync complete" in stdout.getvalue()
    assert stub_client.closed is True
    assert stub_client.executed == [
        (
            "metadata.ota.sync.run",
            {
                "grabCommand": "epgdata",
                "channelName": "BBC TWO HD",
                "captureSeconds": 5.0,
                "databasePath": str(tmp_path / "ccatv.sqlite3"),
            },
        )
    ]


def test_epg_sync_ota_uses_runtime_default_channel_when_omitted(tmp_path: Path) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {"stats": {"channelsUpserted": 1, "programsUpserted": 1, "broadcastsUpserted": 1, "parsedEvents": 1, "ingestRunId": 1}}

        def close(self) -> None:
            return None

    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    runtime_store.save(
        RuntimeConfig(
            dvb_adapter_count=1,
            dvbstreamer_host="localhost",
            ota_epg_channel_name="BBC ONE East",
        )
    )
    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        runtime_store=runtime_store,
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["epg-sync-ota", "--capture-seconds", "5"], deps=deps)

    assert exit_code == 0
    assert stub_client.executed == [
        (
            "metadata.ota.sync.run",
            {
                "grabCommand": "epgdata",
                "channelName": "BBC ONE East",
                "captureSeconds": 5.0,
            },
        )
    ]


def test_epg_sync_ota_rejects_non_positive_capture_seconds() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(stdout=stdout, stderr=stderr)

    exit_code = main(
        ["epg-sync-ota", "--capture-seconds", "0"],
        deps=deps,
    )

    assert exit_code == 2
    assert "--capture-seconds must be greater than 0" in stderr.getvalue()


def test_epg_ota_backfill_channel_names_command_runs_once(tmp_path: Path) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []
            self.closed = False

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "stats": {
                    "servicesResolved": 18,
                    "rowsUpdated": 81,
                    "syntheticBefore": 81,
                    "syntheticAfter": 0,
                    "totalChannels": 99,
                }
            }

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(
        [
            "epg-ota-backfill-channel-names",
            "--database-path",
            str(tmp_path / "ccatv.sqlite3"),
        ],
        deps=deps,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "OTA channel-name backfill complete" in stdout.getvalue()
    assert stub_client.closed is True
    assert stub_client.executed == [
        (
            "metadata.ota.sync.channel-names.backfill.run",
            {
                "databasePath": str(tmp_path / "ccatv.sqlite3"),
            },
        )
    ]


def test_epg_ota_backfill_channel_names_surfaces_service_error() -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            del command, payload
            raise ServiceClientError(
                code="OTA_CHANNEL_MAP_FAILED",
                message="serviceinfo failed",
                retryable=True,
            )

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["epg-ota-backfill-channel-names"], deps=deps)

    assert exit_code == 2
    assert "OTA channel-name backfill failed: serviceinfo failed" in stderr.getvalue()
    assert stdout.getvalue() == ""
    assert stub_client.closed is True


def test_epg_sync_sd_daily_uses_14_day_window() -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "stats": {
                    "channelsUpserted": 1,
                    "programsUpserted": 1,
                    "schedulesUpserted": 1,
                    "staleSchedulesPruned": 0,
                    "ingestRunId": 8,
                }
            }

        def close(self) -> None:
            return None

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["epg-sync-sd-daily", "--lineup-id", "UK-TEST"], deps=deps)

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Schedules Direct daily sync complete" in stdout.getvalue()
    assert stub_client.executed == [
        (
            "metadata.sd.sync.run",
            {
                "lineupId": "UK-TEST",
                "seed": False,
                "windowHours": 336,
                "clearExisting": False,
            },
        )
    ]


def test_epg_sync_sd_daily_uses_runtime_config_lineup_id(tmp_path: Path) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "stats": {
                    "channelsUpserted": 1,
                    "programsUpserted": 1,
                    "schedulesUpserted": 1,
                    "staleSchedulesPruned": 0,
                    "ingestRunId": 8,
                }
            }

        def close(self) -> None:
            return None

    runtime_store = RuntimeConfigStore(config_dir=tmp_path)
    runtime_store.save(
        RuntimeConfig(
            dvb_adapter_count=1,
            dvbstreamer_host="localhost",
            ota_epg_channel_name="BBC TWO HD",
            sd_lineup_id="UK-RUNTIME",
        )
    )

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        runtime_store=runtime_store,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["epg-sync-sd-daily"], deps=deps)

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert stub_client.executed == [
        (
            "metadata.sd.sync.run",
            {
                "lineupId": "UK-RUNTIME",
                "seed": False,
                "windowHours": 336,
                "clearExisting": False,
            },
        )
    ]


def test_epg_sync_sd_daily_uses_env_lineup_id(monkeypatch) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "stats": {
                    "channelsUpserted": 1,
                    "programsUpserted": 1,
                    "schedulesUpserted": 1,
                    "staleSchedulesPruned": 0,
                    "ingestRunId": 8,
                }
            }

        def close(self) -> None:
            return None

    monkeypatch.setenv("CCATV_SD_LINEUP_ID", "UK-ENV")

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["epg-sync-sd-daily"], deps=deps)

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert stub_client.executed == [
        (
            "metadata.sd.sync.run",
            {
                "lineupId": "UK-ENV",
                "seed": False,
                "windowHours": 336,
                "clearExisting": False,
            },
        )
    ]


def test_epg_sync_sd_full_uses_14_day_clear_existing() -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "stats": {
                    "channelsUpserted": 1,
                    "programsUpserted": 1,
                    "schedulesUpserted": 1,
                    "staleSchedulesPruned": 1,
                    "ingestRunId": 9,
                }
            }

        def close(self) -> None:
            return None

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["epg-sync-sd-full", "--lineup-id", "UK-TEST"], deps=deps)

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Schedules Direct full sync complete" in stdout.getvalue()
    assert stub_client.executed == [
        (
            "metadata.sd.sync.run",
            {
                "lineupId": "UK-TEST",
                "seed": False,
                "windowHours": 336,
                "clearExisting": True,
            },
        )
    ]


def test_epg_sync_sd_rejects_missing_lineup_id_when_not_configured() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    calls = {"factory": 0}

    def _factory():
        calls["factory"] += 1
        raise AssertionError("service client should not be created")

    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=_factory,
    )

    exit_code = main(["epg-sync-sd", "--window-hours", "24"], deps=deps)

    assert exit_code == 2
    assert "Schedules Direct lineup id is required" in stderr.getvalue()
    assert calls["factory"] == 0


def test_epg_sync_sd_full_rejects_missing_lineup_id_when_not_configured() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    calls = {"factory": 0}

    def _factory():
        calls["factory"] += 1
        raise AssertionError("service client should not be created")

    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=_factory,
    )

    exit_code = main(["epg-sync-sd-full"], deps=deps)

    assert exit_code == 2
    assert "Schedules Direct lineup id is required" in stderr.getvalue()
    assert calls["factory"] == 0


def test_recordings_backfill_metadata_command_runs() -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []
            self.closed = False

        def execute(
            self,
            command: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {
                "dryRun": True,
                "scanned": 10,
                "updatedFromEpg": 4,
                "updatedFromNfo": 2,
                "unchanged": 4,
            }

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(
        ["recordings-backfill-metadata", "--dry-run", "--limit", "25"],
        deps=deps,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Backfill complete" in stdout.getvalue()
    assert stub_client.closed is True
    assert stub_client.executed == [
        (
            "recording.metadata.backfill",
            {"dryRun": True, "limit": 25},
        )
    ]


def test_recordings_backfill_metadata_rejects_invalid_limit() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(stdout=stdout, stderr=stderr)

    exit_code = main(["recordings-backfill-metadata", "--limit", "0"], deps=deps)

    assert exit_code == 2
    assert "--limit must be greater than 0" in stderr.getvalue()


def test_epg_sync_sd_run_forever_rejects_invalid_window_without_client() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    calls = {"factory": 0}

    def _factory():
        calls["factory"] += 1
        raise AssertionError("service client should not be created")

    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=_factory,
    )

    exit_code = main(
        [
            "epg-sync-sd",
            "--lineup-id",
            "UK-TEST",
            "--run-forever",
            "--window-hours",
            "0",
        ],
        deps=deps,
    )

    assert exit_code == 2
    assert "--window-hours must be greater than 0" in stderr.getvalue()
    assert calls["factory"] == 0


def test_epg_sync_sd_run_forever_non_retryable_error_exits(monkeypatch) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            del command, payload
            raise ServiceClientError(
                code="SD_AUTH_FAILED",
                message="bad credentials",
                retryable=False,
            )

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()

    async def _sleep(_seconds: float) -> None:
        raise AssertionError("sleep should not run after non-retryable failure")

    monkeypatch.setattr("ccatv.cli.asyncio.sleep", _sleep)

    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(
        [
            "epg-sync-sd",
            "--lineup-id",
            "UK-TEST",
            "--run-forever",
            "--poll-interval-seconds",
            "1",
        ],
        deps=deps,
    )

    assert exit_code == 2
    assert "non-retryable" in stderr.getvalue()
    assert "EPG sync failed: bad credentials" in stderr.getvalue()
    assert stub_client.closed is True


def test_epg_sync_sd_run_forever_handles_running_event_loop(monkeypatch) -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            del command, payload
            raise ServiceClientError(
                code="SD_AUTH_FAILED",
                message="bad credentials",
                retryable=False,
            )

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()

    async def _sleep(_seconds: float) -> None:
        raise AssertionError("sleep should not run after non-retryable failure")

    monkeypatch.setattr("ccatv.cli.asyncio.sleep", _sleep)
    monkeypatch.setattr("ccatv.cli.asyncio.get_running_loop", lambda: object())

    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(
        [
            "epg-sync-sd",
            "--lineup-id",
            "UK-TEST",
            "--run-forever",
            "--poll-interval-seconds",
            "1",
        ],
        deps=deps,
    )

    assert exit_code == 2
    assert "EPG sync failed: bad credentials" in stderr.getvalue()
    assert stub_client.closed is True


def test_run_channel_map_sets_mapping() -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.closed = False
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {"channelName": "Quest", "updatedRows": 1}

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = run_channel_map(
        Namespace(channel_name="Quest", service_name="QUEST"),
        deps,
    )

    assert exit_code == 0
    assert stub_client.closed is True
    assert stub_client.executed == [
        (
            "metadata.channels.service-name.set",
            {"channelName": "Quest", "serviceName": "QUEST"},
        )
    ]
    assert "Mapped 'Quest'" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_main_channel_map_returns_error_on_service_failure() -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            del command, payload
            raise ServiceClientError(
                code="NOT_FOUND",
                message="no EPG channel found",
                retryable=False,
            )

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["channel-map", "Unknown", "UNKNOWN"], deps=deps)

    assert exit_code == 2
    assert "channel-map failed: no EPG channel found" in stderr.getvalue()
    assert stub_client.closed is True


def test_main_channel_map_clears_mapping_when_service_name_omitted() -> None:
    class _StubServiceClient:
        def __init__(self) -> None:
            self.closed = False
            self.executed: list[tuple[str, dict[str, object]]] = []

        def execute(
            self, command: str, payload: dict[str, object]
        ) -> dict[str, object]:
            self.executed.append((command, payload))
            return {"channelName": "Quest", "updatedRows": 1}

        def close(self) -> None:
            self.closed = True

    stub_client = _StubServiceClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["channel-map", "Quest"], deps=deps)

    assert exit_code == 0
    assert stub_client.closed is True
    assert stub_client.executed == [
        (
            "metadata.channels.service-name.set",
            {"channelName": "Quest", "serviceName": None},
        )
    ]
    assert "Cleared dvbstreamer service name for 'Quest'" in stdout.getvalue()
    assert stderr.getvalue() == ""


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


def test_main_status_no_recordings_in_progress() -> None:
    class _StubClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
            return {
                "isRecording": False,
                "activeCount": 0,
                "activeRecordings": [],
                "nextScheduled": None,
            }

        def close(self) -> None:
            self.closed = True

    stub_client = _StubClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["status"], deps=deps)

    assert exit_code == 0
    out = stdout.getvalue()
    assert "No recordings in progress." in out
    assert "No upcoming scheduled recordings." in out
    assert stderr.getvalue() == ""


def test_main_status_shows_next_scheduled_when_idle() -> None:
    class _StubClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
            return {
                "isRecording": False,
                "activeCount": 0,
                "activeRecordings": [],
                "nextScheduled": {
                    "jobId": 145,
                    "channel": "BBC TWO HD",
                    "program": "Gardeners' World",
                    "startAtUtc": "2026-06-14T14:30:00Z",
                },
            }

        def close(self) -> None:
            self.closed = True

    stub_client = _StubClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["status"], deps=deps)

    assert exit_code == 0
    out = stdout.getvalue()
    assert "No recordings in progress." in out
    assert "Next recording: [job=145] BBC TWO HD: Gardeners' World at 2026-06-14T14:30:00Z" in out
    assert stderr.getvalue() == ""


def test_main_status_shows_active_recordings() -> None:
    class _StubClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
            return {
                "isRecording": True,
                "activeCount": 1,
                "activeRecordings": [
                    {
                        "recordingId": 34,
                        "jobId": 112,
                        "channel": "BBC One East HD",
                        "program": "Points of View",
                        "elapsedSeconds": 1080,
                    }
                ],
            }

        def close(self) -> None:
            self.closed = True

    stub_client = _StubClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["status"], deps=deps)

    assert exit_code == 0
    out = stdout.getvalue()
    assert "Recording in progress (1 active)" in out
    assert "job=112" in out
    assert "BBC One East HD" in out
    assert "Points of View" in out
    assert "18m0s" in out
    assert stderr.getvalue() == ""


def test_main_status_returns_error_on_service_client_failure() -> None:
    class _StubClient:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
            raise ServiceClientError(
                code="SERVICE_UNAVAILABLE", message="connection refused"
            )

        def close(self) -> None:
            self.closed = True

    stub_client = _StubClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    deps = CliDependencies(
        stdout=stdout,
        stderr=stderr,
        service_client_factory=lambda: stub_client,
    )

    exit_code = main(["status"], deps=deps)

    assert exit_code == 1
    assert "Error:" in stderr.getvalue()
