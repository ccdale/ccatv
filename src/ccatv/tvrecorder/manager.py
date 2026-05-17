from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum


class DvbStreamerError(Exception):
    """Base error for dvbstreamer process management failures."""


class DvbStreamerLaunchError(DvbStreamerError):
    """Raised when dvbstreamer cannot be started."""


class DvbStreamerStopTimeout(DvbStreamerError):
    """Raised when dvbstreamer does not stop within timeout."""


class DvbStreamerState(str, Enum):
    """Lifecycle states for managed dvbstreamer process."""

    FAILED = "failed"
    RUNNING = "running"
    STARTING = "starting"
    STOPPED = "stopped"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class DvbStreamerConfig:
    """Configuration for launching dvbstreamer."""

    adapter_index: int = 0
    bind_address: str = "127.0.0.1"
    executable_path: str = "dvbstreamer"
    extra_args: tuple[str, ...] = ()
    output_mrl: str = "null://"


@dataclass(frozen=True, slots=True)
class DvbStreamerStatus:
    """Current manager status snapshot."""

    state: DvbStreamerState
    pid: int | None
    last_error: str | None


@dataclass(slots=True)
class DvbStreamerManager:
    """Process owner for dvbstreamer start/stop/health lifecycle."""

    config: DvbStreamerConfig
    startup_timeout_seconds: float = 2.0
    stop_timeout_seconds: float = 5.0
    _last_error: str | None = field(default=None, init=False, repr=False)
    _process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _state: DvbStreamerState = field(
        default=DvbStreamerState.STOPPED,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._last_error = None
        self._process = None
        self._state = DvbStreamerState.STOPPED

    def start(self) -> DvbStreamerStatus:
        """Start dvbstreamer unless it is already running."""
        if self._process is not None and self._process.poll() is None:
            self._state = DvbStreamerState.RUNNING
            return self.status()

        self._state = DvbStreamerState.STARTING
        args = [
            self.config.executable_path,
            "-a",
            str(self.config.adapter_index),
            "-i",
            self.config.bind_address,
            "-o",
            self.config.output_mrl,
            *self.config.extra_args,
        ]

        try:
            process = subprocess.Popen(
                args,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            self._state = DvbStreamerState.FAILED
            self._last_error = (
                f"dvbstreamer executable not found: {self.config.executable_path}"
            )
            raise DvbStreamerLaunchError(self._last_error) from exc

        self._process = process
        if process.poll() is not None:
            self._state = DvbStreamerState.FAILED
            stderr = process.stderr.read().strip() if process.stderr else ""
            self._last_error = stderr or "dvbstreamer exited immediately"
            raise DvbStreamerLaunchError(self._last_error)

        self._last_error = None
        self._state = DvbStreamerState.RUNNING
        return self.status()

    def stop(self, force_kill: bool = True) -> DvbStreamerStatus:
        """Stop dvbstreamer if active; optionally force-kill on timeout."""
        if self._process is None:
            self._state = DvbStreamerState.STOPPED
            return self.status()

        if self._process.poll() is not None:
            self._process = None
            self._state = DvbStreamerState.STOPPED
            return self.status()

        self._state = DvbStreamerState.STOPPING
        self._process.terminate()

        try:
            self._process.wait(timeout=self.stop_timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            if not force_kill:
                self._state = DvbStreamerState.FAILED
                self._last_error = (
                    "dvbstreamer did not stop within "
                    f"{self.stop_timeout_seconds} seconds"
                )
                raise DvbStreamerStopTimeout(self._last_error) from exc
            self._process.kill()
            self._process.wait(timeout=self.stop_timeout_seconds)

        self._process = None
        self._state = DvbStreamerState.STOPPED
        return self.status()

    def health_check(self) -> DvbStreamerStatus:
        """Evaluate process state and map it to manager lifecycle status."""
        if self._process is None:
            self._state = DvbStreamerState.STOPPED
            return self.status()

        returncode = self._process.poll()
        if returncode is None:
            self._state = DvbStreamerState.RUNNING
            return self.status()

        self._process = None
        if returncode == 0:
            self._state = DvbStreamerState.STOPPED
            self._last_error = None
        else:
            self._state = DvbStreamerState.FAILED
            self._last_error = f"dvbstreamer exited with returncode {returncode}"
        return self.status()

    def status(self) -> DvbStreamerStatus:
        """Return immutable manager status snapshot."""
        pid = None
        if self._process is not None and self._process.poll() is None:
            pid = self._process.pid
        return DvbStreamerStatus(
            state=self._state, pid=pid, last_error=self._last_error
        )
