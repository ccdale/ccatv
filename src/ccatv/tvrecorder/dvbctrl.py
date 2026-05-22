from __future__ import annotations

import shlex
import subprocess
import time
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
    transient_retry_count: int = 2
    transient_retry_delay_seconds: float = 0.2

    def run_command(self, command: str) -> DvbCtrlResult:
        """Execute a dvbctrl command string and return process output."""
        command_parts = shlex.split(command)
        args = [
            self.executable_path,
            "-h",
            self.host,
            "-a",
            str(self.adapter_index),
        ]
        args.extend(command_parts)
        for attempt in range(self.transient_retry_count + 1):
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
                if attempt < self.transient_retry_count:
                    self._sleep_before_retry(attempt)
                    continue
                raise DvbCtrlTimeoutError(
                    f"dvbctrl command timed out after {self.timeout_seconds} seconds"
                ) from exc

            result = DvbCtrlResult(
                command=tuple(args),
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            if result.returncode == 0:
                return result
            if (
                attempt < self.transient_retry_count
                and _is_transient_command_failure(result.stderr)
            ):
                self._sleep_before_retry(attempt)
                continue
            raise DvbCtrlCommandError(
                "dvbctrl command failed "
                f"(returncode={result.returncode}): {result.stderr.strip()}"
            )

        raise DvbCtrlCommandError("dvbctrl command failed after retries")

    def _sleep_before_retry(self, attempt: int) -> None:
        if self.transient_retry_delay_seconds <= 0:
            return
        delay = self.transient_retry_delay_seconds * (2**attempt)
        time.sleep(delay)


def _is_transient_command_failure(stderr: str) -> bool:
    message = stderr.lower()
    transient_markers = (
        "connection refused",
        "connection reset",
        "connection timed out",
        "network is unreachable",
        "no route to host",
        "resource temporarily unavailable",
        "temporary failure",
        "timed out",
        "try again",
    )
    return any(marker in message for marker in transient_markers)
