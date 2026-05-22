from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TextIO

from ccatv.tvrecorder.config import (
    DvbCtrlCredentials,
    TvRecorderConfig,
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
    store: TvRecorderConfigStore = TvRecorderConfigStore()


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser."""
    parser = argparse.ArgumentParser(prog="ccatv")
    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser(
        "setup",
        help="Store local dvbstreamer/dvbctrl credentials",
    )
    setup_parser.add_argument("--username", help="dvbctrl username")
    setup_parser.set_defaults(handler=run_setup)
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

    path = deps.store.save(
        TvRecorderConfig(
            dvbctrl_credentials=DvbCtrlCredentials(
                password=password,
                username=username,
            )
        )
    )
    print(f"Saved dvbstreamer credentials to {path}", file=deps.stdout)
    return 0


__all__ = [
    "CliDependencies",
    "build_parser",
    "main",
    "run_setup",
    "setup_main",
]
