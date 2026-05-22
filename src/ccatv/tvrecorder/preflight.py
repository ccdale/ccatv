from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass

from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlError


class WritePreflightError(Exception):
    """Raised when write-operation preflight checks fail."""


@dataclass(frozen=True, slots=True)
class WritePreflightResult:
    """Result of preflight checks before write operations."""

    host: str
    adapter_count: int
    online_adapters: tuple[int, ...]
    selected_adapter: int


@dataclass(slots=True)
class WritePreflightChecker:
    """Run host and adapter checks required before write operations."""

    host: str
    adapter_count: int
    preferred_adapter_index: int
    executable_path: str = "dvbctrl"
    timeout_seconds: float = 10.0
    probe_command: str = "lsmuxes"
    client_factory: Callable[[int], DvbCtrlClient] | None = None

    def check(self) -> WritePreflightResult:
        """Validate host resolution and available adapters for writes."""
        if self.adapter_count < 1:
            raise WritePreflightError("Adapter count must be greater than 0.")

        self._ensure_host_resolves()

        online_adapters: list[int] = []
        probe_errors: dict[int, str] = {}
        for adapter_index in range(self.adapter_count):
            try:
                client = self._build_client(adapter_index)
                client.run_command(self.probe_command)
            except DvbCtrlError as exc:
                probe_errors[adapter_index] = str(exc)
            else:
                online_adapters.append(adapter_index)

        if not online_adapters:
            details = "; ".join(
                f"adapter {idx}: {error}" for idx, error in probe_errors.items()
            )
            raise WritePreflightError(
                "No writable tuner path is available. "
                f"Host={self.host}, probed_adapters={self.adapter_count}. "
                f"Probe_errors={details}"
            )

        selected_adapter = online_adapters[0]
        if self.preferred_adapter_index in online_adapters:
            selected_adapter = self.preferred_adapter_index

        return WritePreflightResult(
            host=self.host,
            adapter_count=self.adapter_count,
            online_adapters=tuple(online_adapters),
            selected_adapter=selected_adapter,
        )

    def _build_client(self, adapter_index: int) -> DvbCtrlClient:
        if self.client_factory is not None:
            return self.client_factory(adapter_index)
        return DvbCtrlClient(
            executable_path=self.executable_path,
            host=self.host,
            adapter_index=adapter_index,
            timeout_seconds=self.timeout_seconds,
        )

    def _ensure_host_resolves(self) -> None:
        try:
            socket.getaddrinfo(self.host, None)
        except socket.gaierror as exc:
            raise WritePreflightError(
                f"Host '{self.host}' is not reachable: {exc}"
            ) from exc


__all__ = [
    "WritePreflightChecker",
    "WritePreflightError",
    "WritePreflightResult",
]
