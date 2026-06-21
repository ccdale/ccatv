from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ccatv.storage import PersistenceStore, RecordingStateRecord, SchedulerJobRecord
from ccatv.tvrecorder.commands import (
    addsf_command,
    DvbCtrlCommand,
    current_command,
    festatus_command,
    lssfs_command,
    rmsf_command,
    setsfavsonly_command,
    setsf_command,
    setsfmrl_command,
    lsservices_command,
    select_command,
    serviceinfo_command,
    stats_command,
)
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlResult
from ccatv.tvrecorder.postprocess import (
    NoOpPostProcessingRunner,
    PostProcessingRequest,
    PostProcessingRunner,
)


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


@dataclass(frozen=True, slots=True)
class RecordingPaddingPolicy:
    post_finish_seconds: int = 900
    pre_start_seconds: int = 120


@dataclass(frozen=True, slots=True)
class RecordingHealthCheckPolicy:
    early_growth_checks: int = 3
    early_growth_interval_seconds: float = 2.0
    final_stability_checks: int = 2
    final_stability_interval_seconds: float = 2.0
    growth_min_bytes: int = 1
    periodic_growth_checks: int = 1
    periodic_growth_interval_seconds: float = 30.0


class TvRecorderService:
    """Thin service facade over DvbCtrlClient command execution."""

    def __init__(
        self,
        dvbctrl: DvbCtrlClient,
        *,
        persistence: PersistenceStore | None = None,
        post_processor: PostProcessingRunner | None = None,
        file_size_reader: Callable[[str], int | None] | None = None,
        health_policy: RecordingHealthCheckPolicy | None = None,
        padding_policy: RecordingPaddingPolicy | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._dvbctrl = dvbctrl
        self._persistence = persistence
        self._post_processor = post_processor or NoOpPostProcessingRunner()
        self._file_size_reader = file_size_reader or _read_file_size
        self._health_policy = health_policy or RecordingHealthCheckPolicy()
        self._padding_policy = padding_policy or RecordingPaddingPolicy()
        self._sleep_fn = sleep_fn or time.sleep

    def run_raw(self, command: str) -> DvbCtrlResult:
        """Run a raw dvbctrl command string."""
        return self._dvbctrl.run_command(command)

    def run(self, command: DvbCtrlCommand) -> DvbCtrlResult:
        """Run a typed dvbctrl command."""
        return self._dvbctrl.run_command(command.render())

    def list_services(self) -> list[str]:
        """Return all service names known to dvbstreamer."""
        result = self.run(lsservices_command())
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_service_channel_name_map(self) -> dict[str, str]:
        """Return OTA channel ids mapped to dvbstreamer service names."""
        channel_name_map: dict[str, str] = {}
        for service_name in self.list_services():
            result = self.run(serviceinfo_command(service_name))
            channel_source_id = _parse_serviceinfo_channel_source_id(result.stdout)
            if channel_source_id is None:
                continue
            channel_name_map[channel_source_id] = service_name
        return channel_name_map

    def resolve_service_name(self, name: str) -> str:
        """Return the exact dvbstreamer service name for the EPG channel *name*.

        Resolution order:
        1. DB mapping in ``epg_channels.dvbstreamer_service_name`` (explicit).
        2. Case-insensitive match against the live ``lsservices`` output.
        3. *name* unchanged as a last resort.
        """
        if self._persistence is not None:
            mapped = self._persistence.get_dvbstreamer_service_name(name)
            if mapped is not None:
                return mapped

        services = self.list_services()
        name_lower = name.casefold()
        for svc in services:
            if svc.casefold() == name_lower:
                return svc
        return name

    def select_service(self, service_name: str) -> DvbCtrlResult:
        """Select a primary service by name."""
        return self.run(select_command(service_name))

    def list_service_filters(self, *, include_primary: bool = False) -> list[str]:
        """List service filter names reported by dvbstreamer.

        By default this excludes the built-in ``<Primary>`` filter because
        service-filter control flow must not manipulate it.
        """
        result = self.run(lssfs_command())
        names = [
            _parse_service_filter_name(line)
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        if include_primary:
            return names
        return [name for name in names if not _is_primary_filter_name(name)]

    def add_service_filter(self, filter_name: str, output_mrl: str = "null://") -> DvbCtrlResult:
        """Create a service filter and set its initial output MRL."""
        _ensure_non_primary_filter_name(filter_name)
        created = self.run(addsf_command(filter_name))
        if output_mrl != "null://":
            self.set_service_filter_output(filter_name, output_mrl)
        return created

    def remove_service_filter(self, filter_name: str) -> DvbCtrlResult:
        """Remove a service filter by name."""
        _ensure_non_primary_filter_name(filter_name)
        return self.run(rmsf_command(filter_name))

    def set_service_filter_service(
        self,
        filter_name: str,
        service_name: str,
    ) -> DvbCtrlResult:
        """Bind a service filter to a dvbstreamer service."""
        _ensure_non_primary_filter_name(filter_name)
        return self.run(setsf_command(filter_name, service_name))

    def set_service_filter_output(self, filter_name: str, output_mrl: str) -> DvbCtrlResult:
        """Set the destination MRL for a service filter."""
        _ensure_non_primary_filter_name(filter_name)
        return self.run(setsfmrl_command(filter_name, output_mrl))

    def set_service_filter_avs_only(
        self,
        filter_name: str,
        status: str = "off",
    ) -> DvbCtrlResult:
        """Enable or disable AVS-only mode for a service filter."""
        _ensure_non_primary_filter_name(filter_name)
        return self.run(setsfavsonly_command(filter_name, status))

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
        lock_raw = _pick_value(fields, "lock", "locked", "status", "tuner status")
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
        program_title: str | None = None,
        program_description: str | None = None,
        program_start_at_utc: str | None = None,
        program_stop_at_utc: str | None = None,
        program_content_ref: str | None = None,
        program_series_ref: str | None = None,
    ) -> SchedulerJobRecord:
        padded_start_utc, padded_duration_seconds = (
            self.compute_padded_recording_window(
                start_at_utc=start_at_utc,
                duration_seconds=duration_seconds,
            )
        )
        return self._require_persistence().create_scheduler_job(
            channel_name=channel_name,
            start_at_utc=padded_start_utc,
            duration_seconds=padded_duration_seconds,
            state="scheduled",
            program_title=program_title,
            program_description=program_description,
            program_start_at_utc=program_start_at_utc,
            program_stop_at_utc=program_stop_at_utc,
            program_content_ref=program_content_ref,
            program_series_ref=program_series_ref,
        )

    def compute_padded_recording_window(
        self,
        *,
        start_at_utc: str,
        duration_seconds: int,
    ) -> tuple[str, int]:
        if duration_seconds < 1:
            raise ValueError("duration_seconds must be at least 1")

        start = _parse_utc_iso(start_at_utc)
        pre_start = self._padding_policy.pre_start_seconds
        post_finish = self._padding_policy.post_finish_seconds
        if pre_start < 0 or post_finish < 0:
            raise ValueError("padding policy values must be non-negative")

        padded_start = start - pre_start
        padded_duration = duration_seconds + pre_start + post_finish
        return _format_utc_iso(padded_start), padded_duration

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
        program_title: str | None = None,
        program_description: str | None = None,
        program_start_at_utc: str | None = None,
        program_stop_at_utc: str | None = None,
        program_content_ref: str | None = None,
        program_series_ref: str | None = None,
    ) -> RecordingStateRecord:
        return self._require_persistence().create_recording(
            channel_name=channel_name,
            output_path=output_path,
            state="recording",
            started_at_utc=started_at_utc or _now_utc_iso(),
            program_title=program_title,
            program_description=program_description,
            program_start_at_utc=program_start_at_utc,
            program_stop_at_utc=program_stop_at_utc,
            program_content_ref=program_content_ref,
            program_series_ref=program_series_ref,
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

    def run_recording_post_processing(self, recording_id: int) -> RecordingStateRecord:
        persistence = self._require_persistence()
        recording = persistence.get_recording(recording_id, required=True)
        self.start_recording_post_processing(recording_id)
        request = PostProcessingRequest(
            recording_id=recording.id,
            channel_name=recording.channel_name,
            output_path=recording.output_path,
            program_title=recording.program_title,
            program_description=recording.program_description,
            program_start_at_utc=recording.program_start_at_utc,
            program_stop_at_utc=recording.program_stop_at_utc,
        )

        try:
            result = self._post_processor.run(request)
        except Exception:
            self.mark_recording_failed(recording_id)
            raise

        if result.success:
            return self.mark_recording_ready(recording_id)
        return self.mark_recording_failed(recording_id)

    def mark_recording_ready(self, recording_id: int) -> RecordingStateRecord:
        persistence = self._require_persistence()
        updated = persistence.update_recording_state(
            recording_id,
            state="ready",
        )
        if (
            isinstance(updated.program_content_ref, str)
            and updated.program_content_ref.strip()
        ):
            persistence.mark_recorded_content_ref(
                content_ref=updated.program_content_ref,
                series_ref=updated.program_series_ref,
                title=updated.program_title,
                recording_id=updated.id,
            )
        return updated

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

    def verify_recording_output_growth(
        self,
        recording_id: int,
        *,
        checks: int,
        interval_seconds: float,
        min_growth_bytes: int = 1,
    ) -> RecordingStateRecord:
        if checks < 1:
            raise ValueError("checks must be at least 1")
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0")
        if min_growth_bytes < 1:
            raise ValueError("min_growth_bytes must be at least 1")

        persistence = self._require_persistence()
        recording = persistence.get_recording(recording_id, required=True)
        previous_size = self._file_size_reader(recording.output_path)
        if previous_size is None:
            return self.mark_recording_failed(recording_id)

        saw_growth = False
        for _ in range(checks):
            self._sleep_fn(interval_seconds)
            current_size = self._file_size_reader(recording.output_path)
            if current_size is None:
                return self.mark_recording_failed(recording_id)
            if current_size - previous_size >= min_growth_bytes:
                saw_growth = True
            previous_size = current_size

        if not saw_growth:
            return self.mark_recording_failed(recording_id)
        return persistence.get_recording(recording_id, required=True)

    def verify_recording_output_stable_after_stop(
        self,
        recording_id: int,
        *,
        checks: int = 2,
        interval_seconds: float = 2.0,
    ) -> RecordingStateRecord:
        if checks < 1:
            raise ValueError("checks must be at least 1")
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0")

        persistence = self._require_persistence()
        recording = persistence.get_recording(recording_id, required=True)
        previous_size = self._file_size_reader(recording.output_path)
        if previous_size is None:
            return self.mark_recording_failed(recording_id)

        for _ in range(checks):
            self._sleep_fn(interval_seconds)
            current_size = self._file_size_reader(recording.output_path)
            if current_size is None:
                return self.mark_recording_failed(recording_id)
            if current_size != previous_size:
                return self.mark_recording_failed(recording_id)
            previous_size = current_size

        return persistence.get_recording(recording_id, required=True)

    def verify_recording_output_growth_early(
        self,
        recording_id: int,
    ) -> RecordingStateRecord:
        return self.verify_recording_output_growth(
            recording_id,
            checks=self._health_policy.early_growth_checks,
            interval_seconds=self._health_policy.early_growth_interval_seconds,
            min_growth_bytes=self._health_policy.growth_min_bytes,
        )

    def verify_recording_output_growth_periodic(
        self,
        recording_id: int,
    ) -> RecordingStateRecord:
        return self.verify_recording_output_growth(
            recording_id,
            checks=self._health_policy.periodic_growth_checks,
            interval_seconds=self._health_policy.periodic_growth_interval_seconds,
            min_growth_bytes=self._health_policy.growth_min_bytes,
        )

    def verify_recording_output_stable_after_stop_default(
        self,
        recording_id: int,
    ) -> RecordingStateRecord:
        return self.verify_recording_output_stable_after_stop(
            recording_id,
            checks=self._health_policy.final_stability_checks,
            interval_seconds=self._health_policy.final_stability_interval_seconds,
        )

    def _require_persistence(self) -> PersistenceStore:
        if self._persistence is None:
            raise RuntimeError("persistence store is not configured")
        return self._persistence


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_iso(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


def _format_utc_iso(timestamp_seconds: int) -> str:
    return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _read_file_size(path: str) -> int | None:
    file_path = Path(path)
    try:
        return file_path.stat().st_size
    except FileNotFoundError:
        return None


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


def _parse_service_filter_name(line: str) -> str:
    stripped = line.strip()
    if ":" in stripped:
        head, tail = stripped.split(":", 1)
        if head.strip().casefold() in {"filter", "service filter", "name"}:
            return tail.strip()
    return stripped


def _is_primary_filter_name(name: str) -> bool:
    normalized = "".join(ch for ch in name.casefold() if ch not in {"<", ">", " ", "\t"})
    return normalized == "primary"


def _ensure_non_primary_filter_name(name: str) -> None:
    if _is_primary_filter_name(name):
        raise ValueError("service filter control flow must not target <Primary>")


def _parse_serviceinfo_channel_source_id(output: str) -> str | None:
    fields = _parse_kv_lines(output)
    raw_value = fields.get("id")
    if raw_value is None:
        return None

    parts = [part.strip() for part in raw_value.split(".") if part.strip()]
    if len(parts) != 3:
        return None

    normalized_parts: list[str] = []
    for part in parts:
        normalized = part.lower()
        if not normalized.startswith("0x"):
            normalized = f"0x{normalized}"
        normalized_parts.append(normalized)
    return ":".join(normalized_parts)


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
    if "no lock" in normalized or "unlocked" in normalized:
        return False
    if "lock" in normalized:
        return True
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
