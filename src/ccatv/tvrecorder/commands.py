from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DvbCtrlCommand:
    """Typed dvbctrl command representation."""

    name: str
    args: tuple[str, ...] = ()

    def render(self) -> str:
        """Render as a shell-safe dvbctrl command string."""
        parts = [self.name, *self.args]
        return " ".join(shlex.quote(part) for part in parts)


def current_command() -> DvbCtrlCommand:
    """Build the `current` command."""
    return DvbCtrlCommand(name="current")


def stats_command() -> DvbCtrlCommand:
    """Build the `stats` command."""
    return DvbCtrlCommand(name="stats")


def festatus_command() -> DvbCtrlCommand:
    """Build the `festatus` command."""
    return DvbCtrlCommand(name="festatus")


def select_command(service_name: str) -> DvbCtrlCommand:
    """Build a quoted-safe `select` command for a service name."""
    return DvbCtrlCommand(name="select", args=(service_name,))


def lsservices_command() -> DvbCtrlCommand:
    """Build the `lsservices` command."""
    return DvbCtrlCommand(name="lsservices")
    
def serviceinfo_command(service_name: str) -> DvbCtrlCommand:
    """Build a quoted-safe `serviceinfo` command for a service name."""
    return DvbCtrlCommand(name="serviceinfo", args=(service_name,))


__all__ = [
    "DvbCtrlCommand",
    "current_command",
    "festatus_command",
    "lsservices_command",
    "serviceinfo_command",
    "select_command",
    "stats_command",
]
