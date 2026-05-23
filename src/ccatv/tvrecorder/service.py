from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ccatv.storage import PersistenceStore, RecordingStateRecord, SchedulerJobRecord
from ccatv.tvrecorder.commands import (
    DvbCtrlCommand,
    current_command,
    festatus_command,
    select_command,
    stats_command,
)
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlResult


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

    def __init__(
        self,
        dvbctrl: DvbCtrlClient,
        *,
        persistence: PersistenceStore | None = None,
    ) -> None:
        self._dvbctrl = dvbctrl
        self._persistence = persistence

    def run_raw(self, command: str) -> DvbCtrlResult:
        """Run a raw dvbctrl command string."""
        return self._dvbctrl.run_command(command)

    def run(self, command: DvbCtrlCommand) -> DvbCtrlResult:
        """Run a typed dvbctrl command."""
        return self._dvbctrl.run_command(command.render())

    def select_service(self, service_name: str) -> DvbCtrlResult:
        """Select a primary service by name."""
        return self.run(select_command(service_name))

    def current(self) -> DvbCtrlResult:
        """Return currently selected service output."""
        return self.run(current_command())

    def current_status(self) -> CurrentServiceStatus:
        """Return parsed status from the current service output."""
        result = self.current()
        fields = _parse_kv_lines(result.stdout)
        service_name = _pick_current_service_name(result.stdout, fields)
        return CurrentServiceStatus(service_name=service_name, fields=fields)

    def stats(self) -> DvbCtrlResult:
        """Return current dvbstreamer statistics output."""
        return self.run(stats_command())

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
        return self.run(festatus_command())

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

    def schedule_recording(
        self,
        *,
        channel_name: str,
        start_at_utc: str,
        duration_seconds: int,
    ) -> SchedulerJobRecord:
        return self._require_persistence().create_scheduler_job(
            channel_name=channel_name,
            start_at_utc=start_at_utc,
            duration_seconds=duration_seconds,
            state="scheduled",
        )

    def mark_scheduler_job_running(self, job_id: int) -> SchedulerJobRecord:
        return self._require_persistence().update_scheduler_job_state(
            job_id,
            state="running",
        )

    def mark_scheduler_job_completed(self, job_id: int) -> SchedulerJobRecord:
        return self._require_persistence().update_scheduler_job_state(
            job_id,
            state="completed",
        )

    def mark_scheduler_job_failed(self, job_id: int) -> SchedulerJobRecord:
        return self._require_persistence().update_scheduler_job_state(
            job_id,
            state="failed",
        )

    def begin_recording(
        self,
        *,
        channel_name: str,
        output_path: str,
        started_at_utc: str | None = None,
    ) -> RecordingStateRecord:
        return self._require_persistence().create_recording(
            channel_name=channel_name,
            output_path=output_path,
            state="recording",
            started_at_utc=started_at_utc or _now_utc_iso(),
        )

    def mark_recording_capture_completed(
        self,
        recording_id: int,
        *,
        ended_at_utc: str | None = None,
    ) -> RecordingStateRecord:
        return self._require_persistence().update_recording_state(
            recording_id,
            state="capture_completed",
            ended_at_utc=ended_at_utc or _now_utc_iso(),
        )

    def start_recording_post_processing(
        self, recording_id: int
    ) -> RecordingStateRecord:
        return self._require_persistence().update_recording_state(
            recording_id,
            state="post_processing",
        )

    def mark_recording_ready(self, recording_id: int) -> RecordingStateRecord:
        return self._require_persistence().update_recording_state(
            recording_id,
            state="ready",
        )

    def mark_recording_failed(
        self,
        recording_id: int,
        *,
        ended_at_utc: str | None = None,
    ) -> RecordingStateRecord:
        persistence = self._require_persistence()
        if ended_at_utc is None:
            existing = persistence.get_recording(recording_id, required=True)
            if existing.ended_at_utc is None:
                ended_at_utc = _now_utc_iso()
            else:
                return persistence.update_recording_state(
                    recording_id,
                    state="failed",
                )
        return persistence.update_recording_state(
            recording_id,
            state="failed",
            ended_at_utc=ended_at_utc,
        )

    def _require_persistence(self) -> PersistenceStore:
        if self._persistence is None:
            raise RuntimeError("persistence store is not configured")
        return self._persistence


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
