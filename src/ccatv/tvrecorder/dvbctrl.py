from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


class DvbCtrlError(Exception):
    """Base error for dvbctrl adapter failures."""


class DvbCtrlExecutableNotFound(DvbCtrlError):
    """Raised when the dvbctrl binary is missing or not executable."""


class DvbCtrlTimeoutError(DvbCtrlError):
    """Raised when a dvbctrl command exceeds timeout."""


class DvbCtrlCommandError(DvbCtrlError):
    """Raised when dvbctrl returns a non-zero exit code."""


@dataclass(frozen=True, slots=True)
class DvbCtrlResult:
    """Structured subprocess result for dvbctrl command execution."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class DvbCtrlClient:
    """Subprocess-backed adapter for invoking dvbctrl commands."""

    executable_path: str = "dvbctrl"
    host: str = "localhost"
    adapter_index: int = 0
    timeout_seconds: float = 10.0

    def run_command(self, command: str) -> DvbCtrlResult:
        """Execute a dvbctrl command string and return process output."""
        command_parts = shlex.split(command)
        args = [
            self.executable_path,
            "-h",
            self.host,
            "-a",
            str(self.adapter_index),
            *command_parts,
        ]

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DvbCtrlExecutableNotFound(
                f"dvbctrl executable not found: {self.executable_path}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DvbCtrlTimeoutError(
                f"dvbctrl command timed out after {self.timeout_seconds} seconds"
            ) from exc

        result = DvbCtrlResult(
            command=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if result.returncode != 0:
            raise DvbCtrlCommandError(
                "dvbctrl command failed "
                f"(returncode={result.returncode}): {result.stderr.strip()}"
            )
        return result
