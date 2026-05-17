from __future__ import annotations

import shlex
from dataclasses import dataclass

from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlResult


@dataclass(frozen=True, slots=True)
class DvbCtrlCommand:
    """Typed dvbctrl command representation."""

    name: str
    args: tuple[str, ...] = ()

    def render(self) -> str:
        """Render as a shell-safe dvbctrl command string."""
        parts = [self.name, *self.args]
        return " ".join(shlex.quote(part) for part in parts)


@dataclass(frozen=True, slots=True)
class CurrentServiceStatus:
    """Parsed response from the `current` command."""

    service_name: str | None
    fields: dict[str, str]


@dataclass(frozen=True, slots=True)
class StatsSnapshot:
    """Parsed response from the `stats` command."""

    metrics: dict[str, int | float | str]


@dataclass(frozen=True, slots=True)
class FrontendStatus:
    """Parsed response from the `festatus` command."""

    locked: bool | None
    signal: int | None
    snr: int | None
    ber: int | None
    fields: dict[str, str]


class TvRecorderService:
    """Thin service facade over DvbCtrlClient command execution."""

    def __init__(self, dvbctrl: DvbCtrlClient) -> None:
        self._dvbctrl = dvbctrl

    def run_raw(self, command: str) -> DvbCtrlResult:
        """Run a raw dvbctrl command string."""
        return self._dvbctrl.run_command(command)

    def run(self, command: DvbCtrlCommand) -> DvbCtrlResult:
        """Run a typed dvbctrl command."""
        return self._dvbctrl.run_command(command.render())

    def select_service(self, service_name: str) -> DvbCtrlResult:
        """Select a primary service by name."""
        return self.run(DvbCtrlCommand(name="select", args=(service_name,)))

    def current(self) -> DvbCtrlResult:
        """Return currently selected service output."""
        return self.run(DvbCtrlCommand(name="current"))

    def current_status(self) -> CurrentServiceStatus:
        """Return parsed status from the current service output."""
        result = self.current()
        fields = _parse_kv_lines(result.stdout)
        service_name = _pick_current_service_name(result.stdout, fields)
        return CurrentServiceStatus(service_name=service_name, fields=fields)

    def stats(self) -> DvbCtrlResult:
        """Return current dvbstreamer statistics output."""
        return self.run(DvbCtrlCommand(name="stats"))

    def stats_snapshot(self) -> StatsSnapshot:
        """Return parsed numeric/string metrics from stats output."""
        result = self.stats()
        parsed = {
            key: _coerce_scalar(value)
            for key, value in _parse_kv_lines(result.stdout).items()
        }
        return StatsSnapshot(metrics=parsed)

    def festatus(self) -> DvbCtrlResult:
        """Return frontend status output."""
        return self.run(DvbCtrlCommand(name="festatus"))

    def frontend_status(self) -> FrontendStatus:
        """Return parsed lock/signal fields from frontend status output."""
        result = self.festatus()
        fields = _parse_kv_lines(result.stdout)
        lock_raw = _pick_value(fields, "lock", "locked", "status")
        signal_raw = _pick_value(fields, "signal", "signal strength")
        snr_raw = _pick_value(fields, "snr")
        ber_raw = _pick_value(fields, "ber")
        return FrontendStatus(
            locked=_parse_lock_value(lock_raw),
            signal=_parse_int(signal_raw),
            snr=_parse_int(snr_raw),
            ber=_parse_int(ber_raw),
            fields=fields,
        )


def _parse_kv_lines(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        else:
            continue
        parsed[key.strip().lower()] = value.strip()
    return parsed


def _pick_current_service_name(output: str, fields: dict[str, str]) -> str | None:
    for key in ("current", "service", "service name"):
        if key in fields and fields[key]:
            return fields[key]

    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _pick_value(fields: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        if key in fields:
            return fields[key]
    return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    for token in value.replace("%", " ").replace(",", " ").split():
        try:
            return int(token)
        except ValueError:
            continue
    return None


def _parse_lock_value(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "locked", "ok"}:
        return True
    if normalized in {"0", "false", "no", "off", "unlocked", "none"}:
        return False
    return None


def _coerce_scalar(value: str) -> int | float | str:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
