from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
    setup_parser.add_argument("--username", help="dvbctrl username")
    setup_parser.set_defaults(handler=run_setup)

    sync_parser = subparsers.add_parser(
        "epg-sync-sd",
        help="Sync EPG metadata from Schedules Direct",
    )
    sync_parser.add_argument(
        "--lineup-id",
        required=True,
        help="Schedules Direct lineup identifier",
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

    client = deps.service_client_factory()
    try:
        response_payload = client.execute(
            "runtime.setup.save",
            {
                "adapterCount": int(adapter_count),
                "host": host,
                "password": password,
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
        asyncio.run(_run_forever())
    except ServiceClientError as exc:
        print(f"EPG sync failed: {exc.message}", file=deps.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=deps.stderr)
        return 2
    finally:
        client.close()
    return 0


__all__ = [
    "CliDependencies",
    "build_parser",
    "main",
    "run_epg_sync_sd",
    "run_setup",
    "setup_main",
]
