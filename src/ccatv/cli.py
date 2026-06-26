from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from queue import Queue
from threading import Thread
from typing import TextIO

from ccatv.app.service_client import (
    ServiceClient,
    ServiceClientError,
    create_local_service_client,
)
from ccatv.runtime_config import (
    RuntimeConfig,
    RuntimeConfigError,
    RuntimeConfigStore,
)
from ccatv.tvrecorder.config import (
    TvRecorderConfigStore,
)

PromptFn = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class CliDependencies:
    """Injected side-effect helpers for CLI execution and tests."""

    input_fn: PromptFn = input
    password_fn: PromptFn = getpass.getpass
    stderr: TextIO = sys.stderr
    stdout: TextIO = sys.stdout
    runtime_store: RuntimeConfigStore = RuntimeConfigStore()
    service_client_factory: Callable[[], ServiceClient] = create_local_service_client
    store: TvRecorderConfigStore = TvRecorderConfigStore()


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser."""
    parser = argparse.ArgumentParser(prog="ccatv")
    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser(
        "setup",
        help="Store local dvbstreamer/dvbctrl credentials",
    )
    setup_parser.add_argument("--adapter-count", type=int, help="number of adapters")
    setup_parser.add_argument("--host", help="dvbstreamer/dvbctrl host")
    setup_parser.add_argument(
        "--ota-epg-channel-name",
        help="default OTA EPG channel name for sync operations",
    )
    setup_parser.add_argument(
        "--sd-lineup-id",
        help="default Schedules Direct lineup id for CLI sync commands",
    )
    setup_parser.add_argument("--username", help="dvbctrl username")
    setup_parser.set_defaults(handler=run_setup)

    sync_parser = subparsers.add_parser(
        "epg-sync-sd",
        help="Sync EPG metadata from Schedules Direct",
    )
    sync_parser.add_argument(
        "--lineup-id",
        required=False,
        help=(
            "Schedules Direct lineup identifier "
            "(default: CCATV_SD_LINEUP_ID or runtime config sd_lineup_id)"
        ),
    )
    sync_parser.add_argument(
        "--window-hours",
        type=float,
        default=24.0,
        help="incremental sync window size in hours (run-once mode)",
    )
    sync_parser.add_argument(
        "--seed",
        action="store_true",
        help="run initial seed window instead of incremental sync",
    )
    sync_parser.add_argument(
        "--run-forever",
        action="store_true",
        help="run periodic sync cycles forever",
    )
    sync_parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1800.0,
        help="sleep interval between sync cycles in run-forever mode",
    )
    sync_parser.add_argument(
        "--database-path",
        default=None,
        help="override sqlite database path",
    )
    sync_parser.add_argument(
        "--credentials-path",
        default=None,
        help="override schedulesdirect credentials file path",
    )
    sync_parser.set_defaults(handler=run_epg_sync_sd)

    ota_sync_parser = subparsers.add_parser(
        "epg-sync-ota",
        help="Grab and ingest OTA EPG metadata from dvbstreamer",
    )
    ota_sync_parser.add_argument(
        "--grab-command",
        default="epgdata",
        help="raw dvbctrl command used to fetch EPG payload (default: epgdata)",
    )
    ota_sync_parser.add_argument(
        "--channel-name",
        default=None,
        help=(
            "channel to select before OTA capture "
            "(default: runtime config or CCATV_OTA_EPG_CHANNEL_NAME)"
        ),
    )
    ota_sync_parser.add_argument(
        "--capture-seconds",
        type=float,
        default=10.0,
        help="seconds to capture OTA epgdata stream before stopping (default: 10)",
    )
    ota_sync_parser.add_argument(
        "--database-path",
        default=None,
        help="override sqlite database path",
    )
    ota_sync_parser.set_defaults(handler=run_epg_sync_ota)

    ota_multimux_parser = subparsers.add_parser(
        "epg-sync-ota-multimux",
        help="Grab OTA EPG from one representative TV channel per DVB mux",
    )
    ota_multimux_parser.add_argument(
        "--grab-command",
        default="epgdata",
        help="raw dvbctrl command used to fetch EPG payload (default: epgdata)",
    )
    ota_multimux_parser.add_argument(
        "--capture-seconds",
        type=float,
        default=900.0,
        help="seconds to capture epgdata per mux (default: 900)",
    )
    ota_multimux_parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="retry attempts per mux if dvbstreamer is busy (default: 3)",
    )
    ota_multimux_parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=300.0,
        help="seconds to wait between retries (default: 300)",
    )
    ota_multimux_parser.add_argument(
        "--frontend-lock-timeout-seconds",
        type=float,
        default=30.0,
        help="seconds to wait for tuner lock after service select (default: 30)",
    )
    ota_multimux_parser.add_argument(
        "--database-path",
        default=None,
        help="override sqlite database path",
    )
    ota_multimux_parser.set_defaults(handler=run_epg_sync_ota_multimux)

    ota_backfill_parser = subparsers.add_parser(
        "epg-ota-backfill-channel-names",
        help="Backfill OTA channel display names from dvbstreamer serviceinfo",
    )
    ota_backfill_parser.add_argument(
        "--database-path",
        default=None,
        help="override sqlite database path",
    )
    ota_backfill_parser.set_defaults(handler=run_epg_ota_backfill_channel_names)

    sd_daily_parser = subparsers.add_parser(
        "epg-sync-sd-daily",
        help="Run the daily Schedules Direct rolling-window sync (14 days)",
    )
    sd_daily_parser.add_argument(
        "--lineup-id",
        required=False,
        help=(
            "Schedules Direct lineup identifier "
            "(default: CCATV_SD_LINEUP_ID or runtime config sd_lineup_id)"
        ),
    )
    sd_daily_parser.add_argument(
        "--database-path",
        default=None,
        help="override sqlite database path",
    )
    sd_daily_parser.add_argument(
        "--credentials-path",
        default=None,
        help="override schedulesdirect credentials file path",
    )
    sd_daily_parser.set_defaults(handler=run_epg_sync_sd_daily)

    sd_full_parser = subparsers.add_parser(
        "epg-sync-sd-full",
        help="Run a manual full Schedules Direct refresh (14-day window)",
    )
    sd_full_parser.add_argument(
        "--lineup-id",
        required=False,
        help=(
            "Schedules Direct lineup identifier "
            "(default: CCATV_SD_LINEUP_ID or runtime config sd_lineup_id)"
        ),
    )
    sd_full_parser.add_argument(
        "--database-path",
        default=None,
        help="override sqlite database path",
    )
    sd_full_parser.add_argument(
        "--credentials-path",
        default=None,
        help="override schedulesdirect credentials file path",
    )
    sd_full_parser.set_defaults(handler=run_epg_sync_sd_full)

    channel_map_parser = subparsers.add_parser(
        "channel-map",
        help="Set or clear the dvbstreamer service name for an EPG channel",
    )
    channel_map_parser.add_argument(
        "channel_name",
        metavar="CHANNEL_NAME",
        help="EPG display name of the channel (e.g. 'Quest')",
    )
    channel_map_parser.add_argument(
        "service_name",
        metavar="SERVICE_NAME",
        nargs="?",
        default=None,
        help="dvbstreamer service name to map to (omit or pass '' to clear)",
    )
    channel_map_parser.set_defaults(handler=run_channel_map)

    recordings_backfill_parser = subparsers.add_parser(
        "recordings-backfill-metadata",
        help="Backfill missing recording programme metadata from EPG and NFO",
    )
    recordings_backfill_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum number of recordings to scan",
    )
    recordings_backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be updated without writing changes",
    )
    recordings_backfill_parser.set_defaults(handler=run_recordings_backfill_metadata)

    status_parser = subparsers.add_parser(
        "status",
        help="Show current recording status",
    )
    status_parser.set_defaults(handler=run_status)

    return parser


def main(argv: Sequence[str] | None = None, deps: CliDependencies | None = None) -> int:
    """Run the main ccatv command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 1

    return args.handler(args, deps=deps or CliDependencies())


def setup_main(
    argv: Sequence[str] | None = None,
    deps: CliDependencies | None = None,
) -> int:
    """Run the dedicated setup entrypoint."""
    setup_argv = list(sys.argv[1:] if argv is None else argv)
    return main(["setup", *setup_argv], deps=deps)


def status_main(
    argv: Sequence[str] | None = None,
    deps: CliDependencies | None = None,
) -> int:
    """Run the dedicated status entrypoint."""
    status_argv = list(sys.argv[1:] if argv is None else argv)
    return main(["status", *status_argv], deps=deps)


def run_setup(args: argparse.Namespace, deps: CliDependencies) -> int:
    """Prompt for dvbctrl credentials and persist them in userconfig.json."""
    try:
        runtime_defaults = deps.runtime_store.load()
    except RuntimeConfigError:
        runtime_defaults = RuntimeConfig()

    username = (args.username or deps.input_fn("Dvbctrl username: ")).strip()
    if not username:
        print("Username is required.", file=deps.stderr)
        return 2

    password = deps.password_fn("Dvbctrl password: ")
    if not password:
        print("Password is required.", file=deps.stderr)
        return 2

    password_confirm = deps.password_fn("Confirm dvbctrl password: ")
    if password != password_confirm:
        print("Passwords did not match.", file=deps.stderr)
        return 2

    host_arg = getattr(args, "host", None)
    if host_arg is None:
        host = runtime_defaults.dvbstreamer_host
    else:
        host = str(host_arg).strip()
        if not host:
            print("Host cannot be empty.", file=deps.stderr)
            return 2

    adapter_count = getattr(args, "adapter_count", None)
    if adapter_count is None:
        adapter_count = runtime_defaults.dvb_adapter_count
    if adapter_count < 1:
        print("Adapter count must be greater than 0.", file=deps.stderr)
        return 2

    ota_epg_channel_name_arg = getattr(args, "ota_epg_channel_name", None)
    if ota_epg_channel_name_arg is None:
        ota_epg_channel_name = runtime_defaults.ota_epg_channel_name
    else:
        ota_epg_channel_name = str(ota_epg_channel_name_arg).strip()
        if not ota_epg_channel_name:
            print("OTA EPG channel name cannot be empty.", file=deps.stderr)
            return 2

    sd_lineup_id_arg = getattr(args, "sd_lineup_id", None)
    if sd_lineup_id_arg is None:
        sd_lineup_id = runtime_defaults.sd_lineup_id
    else:
        sd_lineup_id = str(sd_lineup_id_arg).strip()
        if not sd_lineup_id:
            print("Schedules Direct lineup id cannot be empty.", file=deps.stderr)
            return 2

    client = deps.service_client_factory()
    try:
        response_payload = client.execute(
            "runtime.setup.save",
            {
                "adapterCount": int(adapter_count),
                "host": host,
                "otaEpgChannelName": ota_epg_channel_name,
                "password": password,
                "sdLineupId": sd_lineup_id,
                "username": username,
            },
        )
        credentials_path = response_payload.get("credentialsPath")
        runtime_path = response_payload.get("runtimeConfigPath")
        if not isinstance(credentials_path, str) or not credentials_path:
            raise RuntimeError("runtime.setup.save returned malformed credentialsPath")
        if not isinstance(runtime_path, str) or not runtime_path:
            raise RuntimeError("runtime.setup.save returned malformed runtimeConfigPath")
    except ServiceClientError as exc:
        print(f"Setup failed: {exc.message}", file=deps.stderr)
        return 2
    except Exception as exc:
        print(f"Setup failed: {exc}", file=deps.stderr)
        return 2
    finally:
        client.close()

    print(f"Saved dvbstreamer credentials to {credentials_path}", file=deps.stdout)
    print(f"Saved ccatv runtime config to {runtime_path}", file=deps.stdout)
    return 0


def _run_epg_sync_sd_once(args: argparse.Namespace, deps: CliDependencies) -> int:
    if args.window_hours <= 0:
        raise ValueError("--window-hours must be greater than 0")

    client = deps.service_client_factory()
    try:
        payload = {
            "lineupId": args.lineup_id,
            "seed": bool(args.seed),
            "windowHours": float(args.window_hours),
        }
        if args.database_path:
            payload["databasePath"] = str(args.database_path)
        if args.credentials_path:
            payload["credentialsPath"] = str(args.credentials_path)

        response_payload = client.execute("metadata.sd.sync.run", payload)
        stats = response_payload.get("stats")
        if not isinstance(stats, dict):
            raise RuntimeError("metadata.sd.sync.run returned malformed stats payload")

        print(
            (
                "Schedules Direct sync complete "
                f"(lineup={args.lineup_id}, "
                f"channels={stats.get('channelsUpserted')}, "
                f"programs={stats.get('programsUpserted')}, "
                f"schedules={stats.get('schedulesUpserted')}, "
                f"pruned={stats.get('staleSchedulesPruned')}, "
                f"run_id={stats.get('ingestRunId')})"
            ),
            file=deps.stdout,
        )
    finally:
        client.close()
    return 0


def run_epg_sync_sd(args: argparse.Namespace, deps: CliDependencies) -> int:
    lineup_id = _resolve_sd_lineup_id(args.lineup_id, deps.runtime_store)
    if lineup_id is None:
        print(
            (
                "Schedules Direct lineup id is required; pass --lineup-id, "
                "set CCATV_SD_LINEUP_ID, or configure setup --sd-lineup-id."
            ),
            file=deps.stderr,
        )
        return 2
    args.lineup_id = lineup_id

    if args.run_forever and args.poll_interval_seconds <= 0:
        print("--poll-interval-seconds must be greater than 0", file=deps.stderr)
        return 2

    if not args.run_forever:
        try:
            return _run_epg_sync_sd_once(args, deps)
        except Exception as exc:
            print(f"EPG sync failed: {exc}", file=deps.stderr)
            return 2

    if args.window_hours <= 0:
        print("--window-hours must be greater than 0", file=deps.stderr)
        return 2

    client = deps.service_client_factory()
    payload = {
        "lineupId": args.lineup_id,
        "seed": bool(args.seed),
        "windowHours": float(args.window_hours),
    }
    if args.database_path:
        payload["databasePath"] = str(args.database_path)
    if args.credentials_path:
        payload["credentialsPath"] = str(args.credentials_path)

    async def _run_forever() -> None:
        while True:
            try:
                response_payload = client.execute("metadata.sd.sync.run", payload)
                stats = response_payload.get("stats")
                if not isinstance(stats, dict):
                    raise RuntimeError(
                        "metadata.sd.sync.run returned malformed stats payload"
                    )

                print(
                    (
                        "Schedules Direct sync complete "
                        f"(lineup={args.lineup_id}, "
                        f"channels={stats.get('channelsUpserted')}, "
                        f"programs={stats.get('programsUpserted')}, "
                        f"schedules={stats.get('schedulesUpserted')}, "
                        f"pruned={stats.get('staleSchedulesPruned')}, "
                        f"run_id={stats.get('ingestRunId')})"
                    ),
                    file=deps.stdout,
                )
            except ServiceClientError as exc:
                if not exc.retryable:
                    print(
                        (
                            "EPG sync cycle failed with non-retryable error: "
                            f"{exc.message}"
                        ),
                        file=deps.stderr,
                    )
                    raise
                print(f"EPG sync cycle failed: {exc}", file=deps.stderr)
            except Exception as exc:
                print(f"EPG sync cycle failed: {exc}", file=deps.stderr)
            await asyncio.sleep(args.poll_interval_seconds)

    try:
        _run_async_blocking(_run_forever())
    except ServiceClientError as exc:
        print(f"EPG sync failed: {exc.message}", file=deps.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=deps.stderr)
        return 2
    finally:
        client.close()
    return 0


def run_epg_sync_ota(args: argparse.Namespace, deps: CliDependencies) -> int:
    if args.capture_seconds <= 0:
        print("--capture-seconds must be greater than 0", file=deps.stderr)
        return 2

    channel_name = _resolve_ota_epg_channel_name(args.channel_name, deps.runtime_store)

    print("OTA EPG sync starting...", file=deps.stdout)

    # CLI commands should not attempt to own dvbstreamer; only the scheduler service does.
    import os
    os.environ.setdefault("CCATV_DVBSTREAMER_MANAGE_PROCESS", "0")

    client = deps.service_client_factory()
    try:
        payload = {
            "grabCommand": args.grab_command,
            "channelName": channel_name,
            "captureSeconds": float(args.capture_seconds),
        }
        if args.database_path:
            payload["databasePath"] = str(args.database_path)

        result = client.execute("metadata.ota.sync.run", payload)
        stats = result.get("stats")
        if not isinstance(stats, dict):
            raise RuntimeError("metadata.ota.sync.run returned malformed stats payload")
    except ServiceClientError as exc:
        print(f"OTA EPG sync failed: {exc.message}", file=deps.stderr)
        return 2
    except Exception as exc:
        print(f"OTA EPG sync failed: {exc}", file=deps.stderr)
        return 2
    finally:
        client.close()

    print(
        (
            "OTA EPG sync complete "
            f"(channels={stats.get('channelsUpserted')}, "
            f"programs={stats.get('programsUpserted')}, "
            f"broadcasts={stats.get('broadcastsUpserted')}, "
            f"parsed_events={stats.get('parsedEvents')}, "
            f"run_id={stats.get('ingestRunId')})"
        ),
        file=deps.stdout,
    )
    return 0


def run_epg_sync_ota_multimux(args: argparse.Namespace, deps: CliDependencies) -> int:
    if args.capture_seconds <= 0:
        print("--capture-seconds must be greater than 0", file=deps.stderr)
        return 2
    if args.frontend_lock_timeout_seconds <= 0:
        print("--frontend-lock-timeout-seconds must be greater than 0", file=deps.stderr)
        return 2

    print("OTA multi-mux EPG sync starting...", file=deps.stdout)

    # CLI commands should not attempt to own dvbstreamer; only the scheduler service does.
    import os
    os.environ.setdefault("CCATV_DVBSTREAMER_MANAGE_PROCESS", "0")
    
    client = deps.service_client_factory()
    try:
        payload: dict[str, object] = {
            "grabCommand": args.grab_command,
            "captureSeconds": float(args.capture_seconds),
            "maxRetries": int(args.max_retries),
            "retryDelaySeconds": float(args.retry_delay_seconds),
            "frontendLockTimeoutSeconds": float(args.frontend_lock_timeout_seconds),
        }
        if args.database_path:
            payload["databasePath"] = str(args.database_path)

        result = client.execute("metadata.ota.multimux.sync.run", payload)
        stats = result.get("stats")
        if not isinstance(stats, dict):
            raise RuntimeError(
                "metadata.ota.multimux.sync.run returned malformed stats payload"
            )
    except ServiceClientError as exc:
        print(f"OTA multi-mux EPG sync failed: {exc.message}", file=deps.stderr)
        return 2
    except Exception as exc:
        print(f"OTA multi-mux EPG sync failed: {exc}", file=deps.stderr)
        return 2
    finally:
        client.close()

    print(
        (
            "OTA multi-mux EPG sync complete "
            f"(muxes_ok={stats.get('muxesOk')}, "
            f"muxes_failed={stats.get('muxesFailed')}, "
            f"channels={stats.get('channelsUpserted')}, "
            f"programs={stats.get('programsUpserted')}, "
            f"broadcasts={stats.get('broadcastsUpserted')})"
        ),
        file=deps.stdout,
    )
    return 0


def run_epg_ota_backfill_channel_names(
    args: argparse.Namespace,
    deps: CliDependencies,
) -> int:
    client = deps.service_client_factory()
    try:
        payload: dict[str, object] = {}
        if args.database_path:
            payload["databasePath"] = str(args.database_path)

        result = client.execute(
            "metadata.ota.sync.channel-names.backfill.run",
            payload,
        )
        stats = result.get("stats")
        if not isinstance(stats, dict):
            raise RuntimeError(
                "metadata.ota.sync.channel-names.backfill.run returned malformed stats payload"
            )
    except ServiceClientError as exc:
        print(f"OTA channel-name backfill failed: {exc.message}", file=deps.stderr)
        return 2
    except Exception as exc:
        print(f"OTA channel-name backfill failed: {exc}", file=deps.stderr)
        return 2
    finally:
        client.close()

    print(
        (
            "OTA channel-name backfill complete "
            f"(services_resolved={stats.get('servicesResolved')}, "
            f"rows_updated={stats.get('rowsUpdated')}, "
            f"synthetic_before={stats.get('syntheticBefore')}, "
            f"synthetic_after={stats.get('syntheticAfter')}, "
            f"total_channels={stats.get('totalChannels')})"
        ),
        file=deps.stdout,
    )
    return 0


def _resolve_ota_epg_channel_name(
    explicit_channel_name: str | None,
    runtime_store: RuntimeConfigStore,
) -> str:
    if explicit_channel_name is not None:
        return explicit_channel_name

    env_channel_name = os.getenv("CCATV_OTA_EPG_CHANNEL_NAME")
    if env_channel_name is not None and env_channel_name.strip():
        return env_channel_name.strip()

    try:
        return runtime_store.load().ota_epg_channel_name
    except RuntimeConfigError:
        return RuntimeConfig().ota_epg_channel_name


def _resolve_sd_lineup_id(
    explicit_lineup_id: str | None,
    runtime_store: RuntimeConfigStore,
) -> str | None:
    if explicit_lineup_id is not None and explicit_lineup_id.strip():
        return explicit_lineup_id.strip()

    env_lineup_id = os.getenv("CCATV_SD_LINEUP_ID")
    if env_lineup_id is not None and env_lineup_id.strip():
        return env_lineup_id.strip()

    try:
        return runtime_store.load().sd_lineup_id
    except RuntimeConfigError:
        return RuntimeConfig().sd_lineup_id


def _run_epg_sync_sd_window(
    *,
    lineup_id: str,
    window_hours: float,
    clear_existing: bool,
    credentials_path: str | None,
    database_path: str | None,
    deps: CliDependencies,
) -> int:
    client = deps.service_client_factory()
    try:
        payload: dict[str, object] = {
            "lineupId": lineup_id,
            "seed": False,
            "windowHours": window_hours,
            "clearExisting": clear_existing,
        }
        if database_path:
            payload["databasePath"] = str(database_path)
        if credentials_path:
            payload["credentialsPath"] = str(credentials_path)

        response_payload = client.execute("metadata.sd.sync.run", payload)
        stats = response_payload.get("stats")
        if not isinstance(stats, dict):
            raise RuntimeError("metadata.sd.sync.run returned malformed stats payload")
    except ServiceClientError as exc:
        print(f"Schedules Direct sync failed: {exc.message}", file=deps.stderr)
        return 2
    except Exception as exc:
        print(f"Schedules Direct sync failed: {exc}", file=deps.stderr)
        return 2
    finally:
        client.close()

    mode = "full" if clear_existing else "daily"
    print(
        (
            f"Schedules Direct {mode} sync complete "
            f"(lineup={lineup_id}, "
            f"window_hours={window_hours}, "
            f"channels={stats.get('channelsUpserted')}, "
            f"programs={stats.get('programsUpserted')}, "
            f"schedules={stats.get('schedulesUpserted')}, "
            f"pruned={stats.get('staleSchedulesPruned')}, "
            f"run_id={stats.get('ingestRunId')})"
        ),
        file=deps.stdout,
    )
    return 0


def run_epg_sync_sd_daily(args: argparse.Namespace, deps: CliDependencies) -> int:
    lineup_id = _resolve_sd_lineup_id(args.lineup_id, deps.runtime_store)
    if lineup_id is None:
        print(
            (
                "Schedules Direct lineup id is required; pass --lineup-id, "
                "set CCATV_SD_LINEUP_ID, or configure setup --sd-lineup-id."
            ),
            file=deps.stderr,
        )
        return 2

    return _run_epg_sync_sd_window(
        lineup_id=lineup_id,
        window_hours=14 * 24,
        clear_existing=False,
        credentials_path=args.credentials_path,
        database_path=args.database_path,
        deps=deps,
    )


def run_epg_sync_sd_full(args: argparse.Namespace, deps: CliDependencies) -> int:
    lineup_id = _resolve_sd_lineup_id(args.lineup_id, deps.runtime_store)
    if lineup_id is None:
        print(
            (
                "Schedules Direct lineup id is required; pass --lineup-id, "
                "set CCATV_SD_LINEUP_ID, or configure setup --sd-lineup-id."
            ),
            file=deps.stderr,
        )
        return 2

    return _run_epg_sync_sd_window(
        lineup_id=lineup_id,
        window_hours=14 * 24,
        clear_existing=True,
        credentials_path=args.credentials_path,
        database_path=args.database_path,
        deps=deps,
    )


def run_channel_map(args: argparse.Namespace, deps: CliDependencies) -> int:
    """Set or clear the dvbstreamer service name mapping for an EPG channel."""
    channel_name = args.channel_name
    service_name = args.service_name or None  # empty string → clear

    client = deps.service_client_factory()
    try:
        payload = client.execute(
            "metadata.channels.service-name.set",
            {"channelName": channel_name, "serviceName": service_name},
        )
        updated = payload.get("updatedRows", 0)
        if service_name:
            print(
                f"Mapped {channel_name!r} → {service_name!r} ({updated} row(s) updated)",
                file=deps.stdout,
            )
        else:
            print(
                f"Cleared dvbstreamer service name for {channel_name!r} ({updated} row(s) updated)",
                file=deps.stdout,
            )
    except ServiceClientError as exc:
        print(f"channel-map failed: {exc.message}", file=deps.stderr)
        return 2
    except Exception as exc:
        print(f"channel-map failed: {exc}", file=deps.stderr)
        return 2
    finally:
        client.close()
    return 0


def run_recordings_backfill_metadata(
    args: argparse.Namespace,
    deps: CliDependencies,
) -> int:
    if args.limit is not None and args.limit < 1:
        print("--limit must be greater than 0", file=deps.stderr)
        return 2

    client = deps.service_client_factory()
    try:
        payload = {
            "dryRun": bool(args.dry_run),
            "limit": args.limit,
        }
        result = client.execute("recording.metadata.backfill", payload)
    except ServiceClientError as exc:
        print(f"recordings-backfill-metadata failed: {exc.message}", file=deps.stderr)
        return 2
    except Exception as exc:
        print(f"recordings-backfill-metadata failed: {exc}", file=deps.stderr)
        return 2
    finally:
        client.close()

    print(
        (
            "Backfill complete "
            f"(dry_run={result.get('dryRun')}, "
            f"scanned={result.get('scanned')}, "
            f"updated_from_epg={result.get('updatedFromEpg')}, "
            f"updated_from_nfo={result.get('updatedFromNfo')}, "
            f"unchanged={result.get('unchanged')})"
        ),
        file=deps.stdout,
    )
    return 0


def run_status(args: argparse.Namespace, deps: CliDependencies) -> int:
    """Show current recording status."""
    try:
        del args
        client = deps.service_client_factory()
        result = client.execute("recording.status.get", {})
        is_recording = result.get("isRecording", False)
        active_count = result.get("activeCount", 0)
        active_recordings = result.get("activeRecordings", [])
        next_scheduled = result.get("nextScheduled")
        adapters = result.get("adapters", [])

        if not is_recording:
            print("No recordings in progress.", file=deps.stdout)
            if isinstance(next_scheduled, dict):
                job_id = next_scheduled.get("jobId")
                channel = next_scheduled.get("channel") or "unknown"
                program = next_scheduled.get("program") or "untitled"
                start_at_utc = next_scheduled.get("startAtUtc") or "unknown"
                print(
                    f"Next recording: [job={job_id}] {channel}: {program} at {start_at_utc}",
                    file=deps.stdout,
                )
            else:
                print("No upcoming scheduled recordings.", file=deps.stdout)
            _print_adapter_statuses(adapters, deps=deps)
            return 0

        print(f"Recording in progress ({active_count} active):", file=deps.stdout)
        for rec in active_recordings:
            job_id = rec.get("jobId")
            channel = rec.get("channel")
            program = rec.get("program")
            duration = rec.get("elapsedSeconds", 0)
            minutes = duration // 60
            seconds = duration % 60
            print(
                f"  [job={job_id}] {channel}: {program} ({minutes}m{seconds}s)",
                file=deps.stdout,
            )
        _print_adapter_statuses(adapters, deps=deps)
        return 0
    except ServiceClientError as exc:
        print(f"Error: {exc}", file=deps.stderr)
        return 1


def _print_adapter_statuses(adapters: object, *, deps: CliDependencies) -> None:
    if not isinstance(adapters, list) or not adapters:
        return

    print("Adapter status:", file=deps.stdout)
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        adapter_index = adapter.get("adapterIndex")
        allocation = adapter.get("allocation") or (
            "in-use" if adapter.get("inUse") else "free"
        )
        dvb_state = adapter.get("dvbStreamerState") or "unknown"
        tuned_service = adapter.get("tunedService") or "none"
        frontend = adapter.get("frontend") if isinstance(adapter.get("frontend"), dict) else {}
        locked = frontend.get("locked")
        signal = frontend.get("signal")
        snr = frontend.get("snr")
        ber = frontend.get("ber")

        signal_text = f", signal={signal}" if signal is not None else ""
        snr_text = f", snr={snr}" if snr is not None else ""
        ber_text = f", ber={ber}" if ber is not None else ""
        print(
            (
                f"  adapter={adapter_index} allocation={allocation} dvbstreamer={dvb_state} "
                f"tuned={tuned_service} lock={locked}{signal_text}{snr_text}{ber_text}"
            ),
            file=deps.stdout,
        )

        error = adapter.get("error")
        if isinstance(error, str) and error:
            print(f"    probe_error={error}", file=deps.stdout)


def _run_async_blocking(coroutine: object) -> object:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def _target() -> None:
        try:
            queue.put((True, asyncio.run(coroutine)))
        except Exception as exc:
            queue.put((False, exc))

    thread = Thread(target=_target, daemon=True)
    thread.start()
    thread.join()

    if queue.empty():
        raise RuntimeError("async CLI execution did not return a result")

    ok, payload = queue.get()
    if ok:
        return payload
    raise payload


__all__ = [
    "CliDependencies",
    "build_parser",
    "main",
    "run_channel_map",
    "run_epg_ota_backfill_channel_names",
    "run_epg_sync_ota",
    "run_epg_sync_sd_daily",
    "run_epg_sync_sd_full",
    "run_epg_sync_sd",
    "run_recordings_backfill_metadata",
    "run_setup",
    "run_status",
    "setup_main",
    "status_main",
]
