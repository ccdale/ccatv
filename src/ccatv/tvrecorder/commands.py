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

def addsf_command(sfname: str) -> DvbCtrlCommand:
    """Build a quoted-safe `addsf` add service filter command."""
    return DvbCtrlCommand(name="addsf", args=(sfname, "null://"))

def rmsf_command(sfname: str) -> DvbCtrlCommand:
    """Build a quoted-safe `rmsf` remove service filter command."""
    return DvbCtrlCommand(name="rmsf", args=(sfname,))

def lssfs_command() -> DvbCtrlCommand:
    """Build the `lssfs` command to list service filters."""
    return DvbCtrlCommand(name="lssfs")

def setsf_command(sfname: str, service_name: str) -> DvbCtrlCommand:
    """Build a quoted-safe `setsf` set service to be filtered command."""
    return DvbCtrlCommand(name="setsf", args=(sfname, service_name))

def setsfmrl_command(sfname: str, mrl: str) -> DvbCtrlCommand:
    """Build a quoted-safe `setsfmrl` set service filter output MRL command."""
    return DvbCtrlCommand(name="setsfmrl", args=(sfname, mrl))

def getsfmrl_command(sfname: str) -> DvbCtrlCommand:
    """Build a quoted-safe `getsfmrl` get service filter output MRL command."""
    return DvbCtrlCommand(name="getsfmrl", args=(sfname,))

def setsfavsonly_command(sfname: str, status: str = "on") -> DvbCtrlCommand:
    """Build a quoted-safe `setsfavsonly` set service filter to AVS (audio/video/subtitle) only command."""
    return DvbCtrlCommand(name="setsffavsonly", args=(sfname, status))

def getsfavsonly_command(sfname: str) -> DvbCtrlCommand:
    """Build a quoted-safe `getsfavsonly` get service filter AVS (audio/video/subtitle) only status command."""
    return DvbCtrlCommand(name="getsffavsonly", args=(sfname,))

__all__ = [
    "DvbCtrlCommand",
    "current_command",
    "festatus_command",
    "lsservices_command",
    "serviceinfo_command",
    "select_command",
    "stats_command",
    "addsf_command",
    "rmsf_command",
    "lssfs_command",
    "setsf_command",
    "setsfmrl_command",
    "getsfmrl_command",
    "setsfavsonly_command",
    "getsfavsonly_command",
]
