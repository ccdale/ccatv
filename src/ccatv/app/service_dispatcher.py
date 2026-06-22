from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
import re
import signal as _signal
import sqlite3
import subprocess
from threading import Lock, Thread
from types import SimpleNamespace
import time
import xml.etree.ElementTree as ET

from ccatv import __app_name__, __version__
from ccatv.app.bootstrap import AppContext
from ccatv.app.recorder_worker import create_scheduler_worker
from ccatv.metadata import SchedulesDirectHttpClient
from ccatv.metadata.guide_preference import source_priority
from ccatv.metadata.ota_epg import ingest_dvbstreamer_epg
from ccatv.metadata.schedules_direct_contract import (
    GuideSyncWindow,
    SchedulesDirectApiError,
    SchedulesDirectAuthenticationError,
    SchedulesDirectRateLimitError,
    SchedulesDirectTransportError,
)
from ccatv.metadata.schedules_direct_ingest import (
    SchedulesDirectIngestionService,
    SqliteGuideRepository,
)
from ccatv.metadata.schedules_direct_runtime import (
    SchedulesDirectCredentialStore,
    SchedulesDirectTokenCacheStore,
)
from ccatv.runtime_config import RuntimeConfig, RuntimeConfigStore
from ccatv.storage import initialize_database
from ccatv.tvrecorder.config import (
    DvbCtrlCredentials,
    TvRecorderConfig,
    TvRecorderConfigStore,
)
from ccatv.tvrecorder.manager import DvbStreamerState
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient
from ccatv.tvrecorder.commands import serviceinfo_command

API_VERSION = "v1alpha1"
MIN_RECORDING_SECONDS = 30

SERVICE_CAPABILITIES = [
    "service.health",
    "service.info",
    "recording",
    "recording.schedule",
    "recording.worker.cycle",
    "metadata.channels",
    "metadata.guide",
    "metadata.films",
    "metadata.series.recording",
    "metadata.ota.sync",
    "metadata.ota.multimux.sync",
    "metadata.sd.sync",
    "runtime.setup",
]

SERVICE_COMMANDS = [
    "service.health.get",
    "service.info.get",
    "recording.list",
    "recording.delete",
    "recording.stop",
    "recording.schedule.create",
    "recording.schedule.cancel",
    "recording.schedule.list",
    "recording.metadata.backfill",
    "recording.status.get",
    "recording.worker.cycle.run",
    "metadata.channels.list",
    "metadata.channels.dvbservices.list",
    "metadata.channels.favorite.set",
    "metadata.channels.lineup.set",
    "metadata.channels.service-name.set",
    "metadata.guide.list",
    "metadata.films.list",
    "metadata.series.recording.list",
    "metadata.series.recording.set",
    "metadata.ota.sync.run",
    "metadata.ota.sync.channel-names.backfill.run",
    "metadata.ota.multimux.sync.run",
    "metadata.sd.sync.run",
    "metadata.sd.sync.status.get",
    "runtime.setup.save",
]


@dataclass(frozen=True, slots=True)
class ServiceCommandError(Exception):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, object] | None = None


class ServiceCommandDispatcher:
    """Execute M1 service command envelope requests in-process."""

    def __init__(
        self,
        context: AppContext,
        *,
        should_stop: Callable[[], bool] | None = None,
        sd_sync_timeout_seconds: float = 300.0,
        worker_cycle_lock=None,
    ) -> None:
        self._context = context
        self._should_stop = should_stop or (lambda: False)
        self._sd_sync_timeout_seconds = sd_sync_timeout_seconds
        self._worker_cycle_lock = (
            worker_cycle_lock or getattr(context, "worker_cycle_lock", None) or Lock()
        )

    def dispatch(self, request: dict[str, object]) -> dict[str, object]:
        request_id = request.get("requestId")
        try:
            api_version = request.get("apiVersion")
            if api_version != API_VERSION:
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message=f"unsupported apiVersion: {api_version}",
                    details={"expected": API_VERSION},
                )

            command = request.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message="command must be a non-empty string",
                )

            payload = request.get("payload", {})
            if not isinstance(payload, dict):
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message="payload must be an object",
                )

            response_payload = self._dispatch_command(command.strip(), payload)
            return {
                "apiVersion": API_VERSION,
                "requestId": request_id,
                "ok": True,
                "payload": response_payload,
            }
        except ServiceCommandError as exc:
            return {
                "apiVersion": API_VERSION,
                "requestId": request_id,
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "details": exc.details or {},
                },
            }
        except Exception as exc:
            error_message = str(exc)
            if isinstance(exc, sqlite3.OperationalError) and "no such column:" in error_message:
                error_message = (
                    f"{error_message}. Database schema is out of date; run migrations by "
                    "restarting ccatv-service after upgrading."
                )
            return {
                "apiVersion": API_VERSION,
                "requestId": request_id,
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": error_message,
                    "retryable": False,
                    "details": {},
                },
            }

    def _dispatch_command(
        self, command: str, payload: dict[str, object]
    ) -> dict[str, object]:
        if command not in SERVICE_COMMANDS:
            raise ServiceCommandError(
                code="UNSUPPORTED_COMMAND",
                message=f"unsupported command: {command}",
            )
        if command == "service.health.get":
            return self._service_health_get()
        if command == "service.info.get":
            return self._service_info_get()
        if command == "recording.list":
            return self._recording_list(payload)
        if command == "recording.delete":
            return self._recording_delete(payload)
        if command == "recording.stop":
            return self._recording_stop(payload)
        if command == "recording.schedule.create":
            return self._recording_schedule_create(payload)
        if command == "recording.schedule.cancel":
            return self._recording_schedule_cancel(payload)
        if command == "recording.schedule.list":
            return self._recording_schedule_list(payload)
        if command == "recording.metadata.backfill":
            return self._recording_metadata_backfill(payload)
        if command == "recording.status.get":
            return self._recording_status_get(payload)
        if command == "recording.worker.cycle.run":
            return self._recording_worker_cycle_run(payload)
        if command == "metadata.channels.list":
            return self._metadata_channels_list(payload)
        if command == "metadata.channels.dvbservices.list":
            return self._metadata_channels_dvbservices_list(payload)
        if command == "metadata.channels.favorite.set":
            return self._metadata_channels_favorite_set(payload)
        if command == "metadata.channels.lineup.set":
            return self._metadata_channels_lineup_set(payload)
        if command == "metadata.channels.service-name.set":
            return self._metadata_channels_service_name_set(payload)
        if command == "metadata.guide.list":
            return self._metadata_guide_list(payload)
        if command == "metadata.films.list":
            return self._metadata_films_list(payload)
        if command == "metadata.series.recording.list":
            return self._metadata_series_recording_list(payload)
        if command == "metadata.series.recording.set":
            return self._metadata_series_recording_set(payload)
        if command == "metadata.ota.sync.run":
            return self._metadata_ota_sync_run(payload)
        if command == "metadata.ota.multimux.sync.run":
            return self._metadata_ota_multimux_sync_run(payload)
        if command == "metadata.ota.sync.channel-names.backfill.run":
            return self._metadata_ota_channel_names_backfill_run(payload)
        if command == "metadata.sd.sync.run":
            return self._metadata_sd_sync_run(payload)
        if command == "metadata.sd.sync.status.get":
            return self._metadata_sd_sync_status_get(payload)
        if command == "runtime.setup.save":
            return self._runtime_setup_save(payload)
        raise RuntimeError(f"unreachable dispatch branch for command: {command}")

    def _runtime_setup_save(self, payload: dict[str, object]) -> dict[str, object]:
        username = payload.get("username")
        if not isinstance(username, str) or not username.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="username must be a non-empty string",
            )

        password = payload.get("password")
        if not isinstance(password, str) or not password:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="password must be a non-empty string",
            )

        host = payload.get("host")
        if not isinstance(host, str) or not host.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="host must be a non-empty string",
            )

        adapter_count = payload.get("adapterCount")
        if not isinstance(adapter_count, int) or adapter_count < 1:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="adapterCount must be an integer greater than 0",
            )

        ota_epg_channel_name = payload.get("otaEpgChannelName")
        if not isinstance(ota_epg_channel_name, str) or not ota_epg_channel_name.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="otaEpgChannelName must be a non-empty string",
            )

        raw_sd_lineup_id = payload.get("sdLineupId")
        if raw_sd_lineup_id is None:
            sd_lineup_id: str | None = None
        elif isinstance(raw_sd_lineup_id, str) and raw_sd_lineup_id.strip():
            sd_lineup_id = raw_sd_lineup_id.strip()
        else:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="sdLineupId must be a non-empty string when provided",
            )

        credentials_path = TvRecorderConfigStore().save(
            TvRecorderConfig(
                dvbctrl_credentials=DvbCtrlCredentials(
                    password=password,
                    username=username.strip(),
                )
            )
        )
        runtime_path = RuntimeConfigStore().save(
            RuntimeConfig(
                dvb_adapter_count=adapter_count,
                dvbstreamer_host=host.strip(),
                ota_epg_channel_name=ota_epg_channel_name.strip(),
                sd_lineup_id=sd_lineup_id,
            )
        )

        return {
            "credentialsPath": str(credentials_path),
            "runtimeConfigPath": str(runtime_path),
        }

    def _service_health_get(self) -> dict[str, object]:
        db_path = self._context.settings.database_path
        db_status = self._probe_database_health()

        return {
            "status": "ok" if db_status["reachable"] else "degraded",
            "timeUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "database": {
                "path": db_path,
                "reachable": db_status["reachable"],
                "readable": db_status["readable"],
                "writable": db_status["writable"],
                "error": db_status["error"],
                "failedAt": db_status["failedAt"],
            },
            "recorder": {
                "workerEnabled": True,
            },
        }

    def _service_info_get(self) -> dict[str, object]:
        return {
            "appName": __app_name__,
            "appVersion": __version__,
            "apiVersion": API_VERSION,
            "capabilities": SERVICE_CAPABILITIES,
            "commands": SERVICE_COMMANDS,
        }

    def _probe_database_health(self) -> dict[str, object]:
        connection = self._context.persistence.connection
        readable = False
        writable = False
        error: str | None = None
        failed_at: str | None = None

        try:
            connection.execute("SELECT 1")
            readable = True
        except Exception as exc:
            failed_at = "read.select"
            error = str(exc)
            return {
                "reachable": False,
                "readable": readable,
                "writable": writable,
                "error": error,
                "failedAt": failed_at,
            }

        if getattr(connection, "in_transaction", False):
            writable, error, failed_at = self._probe_database_write_with_savepoint(
                connection,
            )
        else:
            writable, error, failed_at = self._probe_database_write_with_transaction(
                connection,
            )

        return {
            "reachable": readable and writable,
            "readable": readable,
            "writable": writable,
            "error": error,
            "failedAt": failed_at,
        }

    def _probe_database_write_with_savepoint(self, connection):
        savepoint_name = "ccatv_health_check"
        failed_at: str | None = None
        savepoint_started = False
        try:
            failed_at = "write.savepoint.begin"
            connection.execute(f"SAVEPOINT {savepoint_name}")
            savepoint_started = True
            failed_at = "write.tempTable.create"
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS ccatv_health_probe (v INTEGER)"
            )
            failed_at = "write.tempTable.insert"
            connection.execute("INSERT INTO ccatv_health_probe (v) VALUES (1)")
        except Exception as exc:
            error = str(exc)
            if savepoint_started:
                try:
                    connection.execute(f"ROLLBACK TO {savepoint_name}")
                except Exception as cleanup_exc:
                    return (
                        False,
                        f"{error}; cleanup rollback failed: {cleanup_exc}",
                        f"{failed_at}.cleanup.rollback",
                    )
                try:
                    connection.execute(f"RELEASE {savepoint_name}")
                except Exception as cleanup_exc:
                    return (
                        False,
                        f"{error}; cleanup release failed: {cleanup_exc}",
                        f"{failed_at}.cleanup.release",
                    )
            return False, str(exc), failed_at

        try:
            failed_at = "write.savepoint.rollback"
            connection.execute(f"ROLLBACK TO {savepoint_name}")
            failed_at = "write.savepoint.release"
            connection.execute(f"RELEASE {savepoint_name}")
            return True, None, None
        except Exception as exc:
            return False, str(exc), failed_at

    def _probe_database_write_with_transaction(self, connection):
        failed_at: str | None = None
        transaction_started = False
        try:
            failed_at = "write.transaction.begin"
            connection.execute("BEGIN")
            transaction_started = True
            failed_at = "write.tempTable.create"
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS ccatv_health_probe (v INTEGER)"
            )
            failed_at = "write.tempTable.insert"
            connection.execute("INSERT INTO ccatv_health_probe (v) VALUES (1)")
        except Exception as exc:
            error = str(exc)
            if transaction_started:
                try:
                    connection.execute("ROLLBACK")
                except Exception as cleanup_exc:
                    return (
                        False,
                        f"{error}; cleanup rollback failed: {cleanup_exc}",
                        f"{failed_at}.cleanup.rollback",
                    )
            return False, str(exc), failed_at

        try:
            failed_at = "write.transaction.rollback"
            connection.execute("ROLLBACK")
            return True, None, None
        except Exception as exc:
            return False, str(exc), failed_at

    def _recording_worker_cycle_run(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        self._raise_if_stopping()
        max_jobs = payload.get("maxJobsPerCycle")
        if max_jobs is not None:
            if not isinstance(max_jobs, int) or max_jobs < 1:
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message="maxJobsPerCycle must be a positive integer",
                )

        output_directory = payload.get("outputDirectory", "/tmp")
        if not isinstance(output_directory, str) or not output_directory.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="outputDirectory must be a non-empty string",
            )

        worker = create_scheduler_worker(
            self._context,
            output_directory=output_directory,
            max_jobs_per_cycle=max_jobs,
            poll_interval_seconds=5.0,
        )
        with self._worker_cycle_lock:
            self._raise_if_stopping()
            results = worker.run_cycle()
        return {
            "results": [
                {
                    "jobId": result.job_id,
                    "schedulerState": result.scheduler_state,
                    "recordingId": result.recording_id,
                    "recordingState": result.recording_state,
                    "error": result.error,
                }
                for result in results
            ]
        }

    def _recording_list(self, payload: dict[str, object]) -> dict[str, object]:
        del payload
        recordings = self._context.persistence.list_recordings()
        return {
            "recordings": [self._recording_summary_payload(recording) for recording in recordings]
        }

    def _recording_delete(self, payload: dict[str, object]) -> dict[str, object]:
        recording_id = payload.get("id")
        if not isinstance(recording_id, int) or recording_id < 1:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="id must be a positive integer",
            )

        delete_files = payload.get("deleteFiles", True)
        if not isinstance(delete_files, bool):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="deleteFiles must be a boolean when provided",
            )

        try:
            deleted = self._context.persistence.delete_recording(recording_id)
        except ValueError as exc:
            raise ServiceCommandError(
                code="NOT_FOUND",
                message=str(exc),
            ) from exc

        file_result = {
            "deleted": [],
            "missing": [],
            "errors": [],
        }
        if delete_files:
            file_result = self._delete_recording_files(deleted.output_path)

        return {
            "id": deleted.id,
            "outputPath": deleted.output_path,
            "deleteFiles": delete_files,
            "fileDelete": file_result,
        }

    def _recording_stop(self, payload: dict[str, object]) -> dict[str, object]:
        recording_id = payload.get("id")
        if not isinstance(recording_id, int) or recording_id < 1:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="id must be a positive integer",
            )

        # Get the recording to find its associated scheduler job
        try:
            recording = self._context.persistence.get_recording(recording_id)
        except ValueError as exc:
            raise ServiceCommandError(
                code="NOT_FOUND",
                message=str(exc),
            ) from exc

        # Check if the recording is currently running
        if recording.state != "recording":
            raise ServiceCommandError(
                code="INVALID_STATE",
                message=f"recording {recording_id} is not in 'recording' state (current state: {recording.state})",
            )

        # Find the scheduler job for this recording by matching channel and start time
        scheduler_jobs = self._context.persistence.list_scheduler_jobs()
        matching_job = None
        for job in scheduler_jobs:
            if (
                job.channel_name == recording.channel_name
                and job.state == "running"
            ):
                # This is a best-effort match; ideally we'd have a foreign key
                # from recordings to scheduler_jobs, but for now we match on
                # channel and running state
                matching_job = job
                break

        if matching_job is None:
            raise ServiceCommandError(
                code="NOT_FOUND",
                message=f"no running scheduler job found for recording {recording_id}",
            )

        # Request the orchestrator to stop the job early
        self._context.recorder_orchestrator.request_job_stop(matching_job.id)

        return {
            "recordingId": recording.id,
            "jobId": matching_job.id,
            "status": "stop_requested",
        }

    def _delete_recording_files(self, output_path: str) -> dict[str, list[object]]:
        targets = [
            Path(output_path),
            Path(output_path).with_suffix(".nfo"),
            Path(output_path).with_suffix(".edl"),
            Path(output_path).with_suffix(".txt"),
            Path(output_path).with_suffix(".log"),
        ]

        deleted: list[str] = []
        missing: list[str] = []
        errors: list[dict[str, str]] = []

        for path in targets:
            try:
                path.unlink()
                deleted.append(str(path))
            except FileNotFoundError:
                missing.append(str(path))
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})

        return {
            "deleted": deleted,
            "missing": missing,
            "errors": errors,
        }

    def _recording_summary_payload(self, recording) -> dict[str, object]:
        nfo_metadata = self._read_recording_nfo(recording.output_path)
        return {
            "id": recording.id,
            "channelName": recording.channel_name,
            "outputPath": recording.output_path,
            "state": recording.state,
            "startedAtUtc": recording.started_at_utc,
            "endedAtUtc": recording.ended_at_utc,
            "programTitle": recording.program_title
            or nfo_metadata["programTitle"]
            or Path(recording.output_path).stem
            or "Untitled recording",
            "description": recording.program_description or nfo_metadata["description"],
            "programStartAtUtc": recording.program_start_at_utc,
            "programStopAtUtc": recording.program_stop_at_utc,
            "fileSizeBytes": self._recording_file_size(recording.output_path),
        }

    def _recording_file_size(self, output_path: str) -> int | None:
        path = Path(output_path)
        try:
            if not path.is_file():
                return None
            return path.stat().st_size
        except OSError:
            return None

    def _read_recording_nfo(self, output_path: str) -> dict[str, str | None]:
        nfo_path = Path(output_path).with_suffix(".nfo")
        try:
            if not nfo_path.is_file():
                return {"programTitle": None, "description": None}
            root = ET.parse(nfo_path).getroot()
        except (ET.ParseError, OSError):
            return {"programTitle": None, "description": None}

        return {
            "programTitle": self._first_xml_text(
                root,
                "title",
                "showtitle",
                "programmeTitle",
                "programTitle",
            ),
            "description": self._first_xml_text(
                root,
                "plot",
                "description",
                "outline",
            ),
        }

    def _first_xml_text(self, root: ET.Element, *tags: str) -> str | None:
        for tag in tags:
            node = root.find(f".//{tag}")
            if node is None or node.text is None:
                continue
            value = node.text.strip()
            if value:
                return value
        return None

    def _recording_schedule_create(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        self._raise_if_stopping()

        channel_name = payload.get("channelName")
        if not isinstance(channel_name, str) or not channel_name.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="channelName must be a non-empty string",
            )

        start_at_utc = payload.get("startAtUtc")
        if not isinstance(start_at_utc, str) or not start_at_utc.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="startAtUtc must be a non-empty UTC timestamp string",
            )

        duration_seconds = payload.get("durationSeconds")
        if not isinstance(duration_seconds, int) or duration_seconds < 1:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="durationSeconds must be an integer greater than 0",
            )

        program_title = payload.get("programTitle")
        if program_title is not None and not isinstance(program_title, str):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="programTitle must be a string when provided",
            )

        program_description = payload.get("programDescription")
        if program_description is not None and not isinstance(program_description, str):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="programDescription must be a string when provided",
            )

        program_start_at_utc = payload.get("programStartAtUtc")
        if program_start_at_utc is not None and (
            not isinstance(program_start_at_utc, str) or not program_start_at_utc.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="programStartAtUtc must be a non-empty string when provided",
            )

        program_stop_at_utc = payload.get("programStopAtUtc")
        if program_stop_at_utc is not None and (
            not isinstance(program_stop_at_utc, str) or not program_stop_at_utc.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="programStopAtUtc must be a non-empty string when provided",
            )

        # For in-progress guide programmes, reject requests that are effectively
        # over rather than creating a job that will fail immediately.
        if isinstance(program_stop_at_utc, str):
            try:
                stop_dt = datetime.strptime(
                    program_stop_at_utc.strip(), "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message="programStopAtUtc must be an ISO-8601 UTC timestamp string",
                ) from exc

            now_dt = datetime.now(timezone.utc)
            remaining_seconds = int((stop_dt - now_dt).total_seconds())
            if remaining_seconds < MIN_RECORDING_SECONDS:
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message=(
                        "programme has already ended or has less than "
                        f"{MIN_RECORDING_SECONDS} seconds remaining"
                    ),
                )

        program_content_ref = payload.get("programContentRef")
        if program_content_ref is not None and (
            not isinstance(program_content_ref, str)
            or not program_content_ref.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="programContentRef must be a non-empty string when provided",
            )

        program_series_ref = payload.get("programSeriesRef")
        if program_series_ref is not None and (
            not isinstance(program_series_ref, str)
            or not program_series_ref.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="programSeriesRef must be a non-empty string when provided",
            )

        try:
            job = self._context.tvrecorder.schedule_recording(
                channel_name=channel_name.strip(),
                start_at_utc=start_at_utc.strip(),
                duration_seconds=duration_seconds,
                program_title=(
                    program_title.strip()
                    if isinstance(program_title, str) and program_title.strip()
                    else None
                ),
                program_description=(
                    program_description.strip()
                    if isinstance(program_description, str)
                    and program_description.strip()
                    else None
                ),
                program_start_at_utc=(
                    program_start_at_utc.strip()
                    if isinstance(program_start_at_utc, str)
                    else None
                ),
                program_stop_at_utc=(
                    program_stop_at_utc.strip()
                    if isinstance(program_stop_at_utc, str)
                    else None
                ),
                program_content_ref=(
                    program_content_ref.strip()
                    if isinstance(program_content_ref, str)
                    else None
                ),
                program_series_ref=(
                    program_series_ref.strip()
                    if isinstance(program_series_ref, str)
                    else None
                ),
            )
        except ValueError as exc:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message=str(exc),
            ) from exc

        all_jobs = self._context.persistence.list_scheduler_jobs()
        scheduled_count = sum(1 for item in all_jobs if item.state == "scheduled")
        running_count = sum(1 for item in all_jobs if item.state == "running")
        self._context.logger.info(
            "scheduler job created: job_id=%s channel=%s start_at_utc=%s duration_seconds=%s scheduled=%s running=%s",
            job.id,
            job.channel_name,
            job.start_at_utc,
            job.duration_seconds,
            scheduled_count,
            running_count,
        )

        return {
            "job": {
                "id": job.id,
                "state": job.state,
            }
        }

    def _recording_schedule_list(self, payload: dict[str, object]) -> dict[str, object]:
        state_filter = payload.get("state")
        if state_filter is not None and (
            not isinstance(state_filter, str) or not state_filter.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="state must be a non-empty string when provided",
            )

        target_state = state_filter.strip() if isinstance(state_filter, str) else None
        jobs = self._context.persistence.list_scheduler_jobs()
        if target_state is not None:
            jobs = [job for job in jobs if job.state == target_state]

        return {
            "jobs": [
                {
                    "id": job.id,
                    "channelName": job.channel_name,
                    "startAtUtc": job.start_at_utc,
                    "durationSeconds": job.duration_seconds,
                    "state": job.state,
                    "programTitle": job.program_title,
                    "programDescription": job.program_description,
                    "programStartAtUtc": job.program_start_at_utc,
                    "programStopAtUtc": job.program_stop_at_utc,
                    "programContentRef": job.program_content_ref,
                    "programSeriesRef": job.program_series_ref,
                }
                for job in jobs
            ]
        }

    def _recording_schedule_cancel(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        self._raise_if_stopping()

        job_id = payload.get("id")
        if not isinstance(job_id, int) or job_id < 1:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="id must be an integer greater than 0",
            )

        try:
            job = self._context.persistence.get_scheduler_job(job_id, required=True)
        except ValueError as exc:
            raise ServiceCommandError(
                code="NOT_FOUND",
                message=str(exc),
            ) from exc

        if job.state != "scheduled":
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="only scheduled jobs can be cancelled",
                details={"state": job.state},
            )

        cancelled = self._context.persistence.update_scheduler_job_state(
            job_id,
            state="cancelled",
        )
        self._context.logger.info(
            "scheduler job cancelled: job_id=%s channel=%s start_at_utc=%s",
            cancelled.id,
            cancelled.channel_name,
            cancelled.start_at_utc,
        )

        return {
            "job": {
                "id": cancelled.id,
                "state": cancelled.state,
            }
        }

    def _recording_metadata_backfill(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        limit = payload.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="limit must be an integer greater than 0 when provided",
            )

        dry_run = payload.get("dryRun", False)
        if not isinstance(dry_run, bool):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="dryRun must be a boolean when provided",
            )

        recordings = [
            recording
            for recording in self._context.persistence.list_recordings()
            if not recording.program_title
        ]
        if limit is not None:
            recordings = recordings[:limit]

        updated_from_epg = 0
        updated_from_nfo = 0
        unchanged = 0

        for recording in recordings:
            epg_match = self._match_recording_to_epg(recording)
            if epg_match is not None:
                if not dry_run:
                    self._context.persistence.update_recording_program_snapshot(
                        recording.id,
                        program_title=epg_match["title"],
                        program_description=epg_match["description"],
                        program_start_at_utc=epg_match["startAtUtc"],
                        program_stop_at_utc=epg_match["stopAtUtc"],
                    )
                updated_from_epg += 1
                continue

            nfo_metadata = self._read_recording_nfo(recording.output_path)
            if nfo_metadata["programTitle"]:
                if not dry_run:
                    self._context.persistence.update_recording_program_snapshot(
                        recording.id,
                        program_title=nfo_metadata["programTitle"],
                        program_description=nfo_metadata["description"],
                        program_start_at_utc=recording.program_start_at_utc,
                        program_stop_at_utc=recording.program_stop_at_utc,
                    )
                updated_from_nfo += 1
                continue

            unchanged += 1

        return {
            "dryRun": dry_run,
            "scanned": len(recordings),
            "updatedFromEpg": updated_from_epg,
            "updatedFromNfo": updated_from_nfo,
            "unchanged": unchanged,
        }

    def _recording_status_get(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Get current recording status (in-progress recordings)."""
        del payload
        connection = self._context.persistence.connection
        rows = connection.execute(
            """
            SELECT r.id, r.channel_name, r.program_title, r.started_at_utc,
                   j.id AS job_id
            FROM recordings r
            LEFT JOIN scheduler_jobs j
                ON j.channel_name = r.channel_name AND j.state = 'running'
            WHERE r.state = 'recording'
            """,
        ).fetchall()

        active_recordings = []
        now_dt = datetime.now(timezone.utc)
        for row in rows:
            rec_id, channel, program, started_at, job_id = row
            elapsed_seconds = 0
            if started_at:
                try:
                    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    elapsed_seconds = int((now_dt - start_dt).total_seconds())
                except (ValueError, AttributeError):
                    pass

            active_recordings.append(
                {
                    "recordingId": rec_id,
                    "jobId": job_id,
                    "channel": channel or "unknown",
                    "program": program or "untitled",
                    "elapsedSeconds": max(0, elapsed_seconds),
                }
            )

        next_scheduled_row = connection.execute(
            """
            SELECT id, channel_name, program_title, start_at_utc
            FROM scheduler_jobs
            WHERE state = 'scheduled'
              AND start_at_utc >= ?
            ORDER BY start_at_utc ASC
            LIMIT 1
            """,
            (now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchone()

        next_scheduled: dict[str, object] | None = None
        if next_scheduled_row is not None:
            next_scheduled = {
                "jobId": int(next_scheduled_row[0]),
                "channel": str(next_scheduled_row[1]) if next_scheduled_row[1] else "unknown",
                "program": str(next_scheduled_row[2]) if next_scheduled_row[2] else "untitled",
                "startAtUtc": str(next_scheduled_row[3]),
            }

        adapter_statuses = self._adapter_status_list()

        return {
            "isRecording": len(active_recordings) > 0,
            "activeCount": len(active_recordings),
            "activeRecordings": active_recordings,
            "nextScheduled": next_scheduled,
            "adapters": adapter_statuses,
        }

    def _adapter_status_list(self) -> list[dict[str, object]]:
        adapter_pool = getattr(self._context, "adapter_pool", None)
        if adapter_pool is None:
            return []

        idle_indexes = {
            int(slot.adapter_index) for slot in adapter_pool.idle_slots_snapshot()
        }
        statuses: list[dict[str, object]] = []
        for slot in sorted(adapter_pool.slots, key=lambda candidate: candidate.adapter_index):
            adapter_index = int(slot.adapter_index)
            in_use = adapter_index not in idle_indexes

            state_value = DvbStreamerState.STOPPED.value
            pid_value = None
            try:
                dvb_status = slot.dvbstreamer.health_check()
                raw_state = getattr(dvb_status, "state", DvbStreamerState.STOPPED)
                if isinstance(raw_state, DvbStreamerState):
                    state_value = raw_state.value
                else:
                    state_value = str(raw_state)
                pid_value = getattr(dvb_status, "pid", None)
            except Exception as exc:
                status = {
                    "adapterIndex": adapter_index,
                    "inUse": in_use,
                    "allocation": "in-use" if in_use else "free",
                    "dvbStreamerState": DvbStreamerState.FAILED.value,
                    "dvbStreamerPid": None,
                    "tunedService": None,
                    "frontend": {
                        "locked": None,
                        "signal": None,
                        "snr": None,
                        "ber": None,
                    },
                    "stats": None,
                    "error": str(exc),
                }
                statuses.append(status)
                continue

            status: dict[str, object] = {
                "adapterIndex": adapter_index,
                "inUse": in_use,
                "allocation": "in-use" if in_use else "free",
                "dvbStreamerState": state_value,
                "dvbStreamerPid": int(pid_value) if isinstance(pid_value, int) else None,
                "tunedService": None,
                "frontend": {
                    "locked": None,
                    "signal": None,
                    "snr": None,
                    "ber": None,
                },
                "stats": None,
                "error": None,
            }

            service = getattr(slot.capture_controller, "service", None)
            if service is None:
                statuses.append(status)
                continue

            try:
                current = service.current_status()
                frontend = service.frontend_status()
                stats_snapshot = service.stats_snapshot()
                status["tunedService"] = current.service_name
                status["frontend"] = {
                    "locked": frontend.locked,
                    "signal": frontend.signal,
                    "snr": frontend.snr,
                    "ber": frontend.ber,
                }
                status["stats"] = stats_snapshot.metrics
            except Exception as exc:
                status["error"] = str(exc)

            statuses.append(status)

        return statuses

    def _match_recording_to_epg(self, recording) -> dict[str, str | None] | None:
        start_utc = recording.started_at_utc or recording.program_start_at_utc
        if not isinstance(start_utc, str) or not start_utc.strip():
            return None

        rows = self._context.persistence.connection.execute(
            """
            SELECT
                b.start_utc,
                b.stop_utc,
                p.title,
                p.description_long,
                ABS(strftime('%s', b.start_utc) - strftime('%s', ?)) AS start_delta
            FROM epg_broadcasts AS b
            JOIN epg_channels AS c ON c.id = b.channel_id
            JOIN epg_programs AS p ON p.id = b.program_id
            WHERE lower(c.display_name) = lower(?)
              AND b.start_utc >= datetime(?, '-6 hours')
              AND b.start_utc <= datetime(?, '+6 hours')
            ORDER BY start_delta ASC, b.start_utc ASC
            LIMIT 1
            """,
            (start_utc, recording.channel_name, start_utc, start_utc),
        ).fetchall()
        if not rows:
            return None

        row = rows[0]
        return {
            "startAtUtc": str(row[0]),
            "stopAtUtc": str(row[1]) if row[1] is not None else None,
            "title": str(row[2]),
            "description": str(row[3]) if row[3] is not None else None,
        }

    def _metadata_channels_list(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        del payload

        rows = self._context.persistence.connection.execute(
            """
            SELECT source, source_channel_id, display_name, callsign,
                   logical_channel_number, dvbstreamer_service_name,
                   favorite_channel
            FROM epg_channels
            WHERE trim(display_name) != ''
            ORDER BY display_name COLLATE NOCASE ASC
            """
        ).fetchall()

        lineup_overrides = self._context.persistence.list_channel_lineup_overrides()

        channels_by_name: dict[str, dict[str, object]] = {}
        for row in rows:
            display_name = str(row[2]).strip()
            dvb_name = str(row[5]).strip() if row[5] is not None else None
            channel = {
                "name": display_name,
                "epgName": display_name,
                "callsign": str(row[3]) if row[3] is not None else None,
                "logicalChannelNumber": str(row[4]) if row[4] is not None else None,
                "source": str(row[0]),
                "sourceChannelId": str(row[1]),
                "dvbstreamerServiceName": dvb_name or None,
                "favoriteChannel": bool(row[6]),
                "sourceVariants": [
                    {
                        "source": str(row[0]),
                        "name": display_name,
                        "sourceChannelId": str(row[1]),
                        "callsign": str(row[3]) if row[3] is not None else None,
                        "logicalChannelNumber": (
                            str(row[4]) if row[4] is not None else None
                        ),
                    }
                ],
            }
            key = display_name.casefold()
            current = channels_by_name.get(key)
            if current is None:
                channels_by_name[key] = channel
                continue

            merged_favorite = bool(current["favoriteChannel"]) or bool(
                channel["favoriteChannel"]
            )

            preferred = current
            if source_priority(channel["source"]) < source_priority(
                str(current["source"])
            ):
                preferred = channel

            fallback = channel if preferred is current else current
            if preferred["callsign"] is None and fallback["callsign"] is not None:
                preferred["callsign"] = fallback["callsign"]
            if (
                preferred["logicalChannelNumber"] is None
                and fallback["logicalChannelNumber"] is not None
            ):
                preferred["logicalChannelNumber"] = fallback["logicalChannelNumber"]
            if (
                preferred["dvbstreamerServiceName"] is None
                and fallback["dvbstreamerServiceName"] is not None
            ):
                preferred["dvbstreamerServiceName"] = fallback[
                    "dvbstreamerServiceName"
                ]
            if isinstance(preferred.get("sourceVariants"), list) and isinstance(
                fallback.get("sourceVariants"), list
            ):
                seen_variants = {
                    (
                        str(item.get("source") or ""),
                        str(item.get("sourceChannelId") or ""),
                    )
                    for item in preferred["sourceVariants"]
                    if isinstance(item, dict)
                }
                for item in fallback["sourceVariants"]:
                    if not isinstance(item, dict):
                        continue
                    variant_key = (
                        str(item.get("source") or ""),
                        str(item.get("sourceChannelId") or ""),
                    )
                    if variant_key in seen_variants:
                        continue
                    preferred["sourceVariants"].append(item)
                    seen_variants.add(variant_key)

            preferred["favoriteChannel"] = merged_favorite
            channels_by_name[key] = preferred

        for channel in channels_by_name.values():
            override = lineup_overrides.get(str(channel["name"]).casefold())
            schedules_direct_name: str | None = None
            variants = channel.get("sourceVariants")
            if isinstance(variants, list):
                for item in variants:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("source") or "") != "schedules_direct":
                        continue
                    candidate = item.get("name")
                    if isinstance(candidate, str) and candidate.strip():
                        schedules_direct_name = candidate.strip()
                        break
            if isinstance(override, dict):
                channel["guideName"] = (
                    str(override.get("guideName") or "").strip() or channel["name"]
                )
                channel["guideLogicalChannelNumber"] = (
                    str(override.get("guideLogicalChannelNumber") or "").strip()
                    or channel["logicalChannelNumber"]
                )
                channel["broadcasterName"] = (
                    str(override.get("broadcasterName") or "").strip()
                    or channel["dvbstreamerServiceName"]
                )
                channel["schedulesDirectName"] = (
                    str(override.get("schedulesDirectName") or "").strip()
                    or schedules_direct_name
                )
            else:
                channel["guideName"] = channel["name"]
                channel["guideLogicalChannelNumber"] = channel["logicalChannelNumber"]
                channel["broadcasterName"] = channel["dvbstreamerServiceName"]
                channel["schedulesDirectName"] = schedules_direct_name

        return {
            "channels": sorted(
                channels_by_name.values(),
                key=lambda channel: (
                    not bool(channel["favoriteChannel"]),
                    channel["guideLogicalChannelNumber"] is None,
                    channel["guideLogicalChannelNumber"] or "",
                    str(channel["guideName"]).casefold(),
                ),
            )
        }

    def _metadata_channels_dvbservices_list(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        del payload
        try:
            services = self._context.tvrecorder.list_services()
        except Exception as exc:
            return {
                "available": False,
                "error": str(exc),
                "services": [],
            }

        deduped_by_name: dict[str, str] = {}
        for service in services:
            key = service.casefold()
            if key not in deduped_by_name:
                deduped_by_name[key] = service

        deduped = sorted(deduped_by_name.values(), key=lambda value: value.casefold())
        return {
            "available": True,
            "error": None,
            "services": deduped,
        }

    def _metadata_channels_favorite_set(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        channel_name = payload.get("channelName")
        if not isinstance(channel_name, str) or not channel_name.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="channelName must be a non-empty string",
            )

        favorite = payload.get("favorite")
        if not isinstance(favorite, bool):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="favorite must be a boolean",
            )

        updated = self._context.persistence.set_favorite_channel(
            channel_name.strip(),
            favorite,
        )
        if updated == 0:
            raise ServiceCommandError(
                code="NOT_FOUND",
                message=f"no EPG channel found with name: {channel_name.strip()!r}",
            )
        return {
            "channelName": channel_name.strip(),
            "favorite": favorite,
            "updatedRows": updated,
        }

    def _metadata_channels_lineup_set(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        epg_channel_name = payload.get("epgChannelName")
        if not isinstance(epg_channel_name, str) or not epg_channel_name.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="epgChannelName must be a non-empty string",
            )

        broadcaster_name = payload.get("broadcasterName")
        if broadcaster_name is not None and not isinstance(broadcaster_name, str):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="broadcasterName must be a string or null",
            )

        schedules_direct_name = payload.get("schedulesDirectName")
        if schedules_direct_name is not None and not isinstance(
            schedules_direct_name, str
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="schedulesDirectName must be a string or null",
            )

        guide_name = payload.get("guideName")
        if guide_name is not None and not isinstance(guide_name, str):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="guideName must be a string or null",
            )

        guide_lcn = payload.get("guideLogicalChannelNumber")
        if guide_lcn is not None and not isinstance(guide_lcn, str):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="guideLogicalChannelNumber must be a string or null",
            )

        exists = self._context.persistence.connection.execute(
            """
            SELECT 1
            FROM epg_channels
            WHERE lower(trim(display_name)) = lower(trim(?))
            LIMIT 1
            """,
            (epg_channel_name.strip(),),
        ).fetchone()
        if exists is None:
            raise ServiceCommandError(
                code="NOT_FOUND",
                message=f"no EPG channel found with name: {epg_channel_name.strip()!r}",
            )

        result = self._context.persistence.set_channel_lineup_override(
            epg_channel_name=epg_channel_name.strip(),
            broadcaster_name=broadcaster_name,
            schedules_direct_name=schedules_direct_name,
            guide_name=guide_name,
            guide_logical_channel_number=guide_lcn,
        )
        return {
            "epgChannelName": epg_channel_name.strip(),
            "broadcasterName": (
                broadcaster_name.strip()
                if isinstance(broadcaster_name, str) and broadcaster_name.strip()
                else None
            ),
            "schedulesDirectName": (
                schedules_direct_name.strip()
                if isinstance(schedules_direct_name, str)
                and schedules_direct_name.strip()
                else None
            ),
            "guideName": (
                guide_name.strip()
                if isinstance(guide_name, str) and guide_name.strip()
                else None
            ),
            "guideLogicalChannelNumber": (
                guide_lcn.strip()
                if isinstance(guide_lcn, str) and guide_lcn.strip()
                else None
            ),
            "action": str(result["action"]),
            "updatedRows": int(result["updatedRows"]),
        }

    def _metadata_channels_service_name_set(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        channel_name = payload.get("channelName")
        if not isinstance(channel_name, str) or not channel_name.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="channelName must be a non-empty string",
            )

        service_name = payload.get("serviceName")
        if service_name is not None and not isinstance(service_name, str):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="serviceName must be a string or null",
            )

        updated = self._context.persistence.set_dvbstreamer_service_name(
            channel_name.strip(),
            service_name if isinstance(service_name, str) else None,
        )
        if updated == 0:
            raise ServiceCommandError(
                code="NOT_FOUND",
                message=f"no EPG channel found with name: {channel_name.strip()!r}",
            )
        return {"channelName": channel_name.strip(), "updatedRows": updated}

    def _metadata_guide_list(self, payload: dict[str, object]) -> dict[str, object]:
        channel = payload.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="channel must be a non-empty string",
            )

        window_hours = payload.get("windowHours", 6)
        if not isinstance(window_hours, int | float) or window_hours <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="windowHours must be greater than 0",
            )

        start_at_utc = payload.get("startAtUtc")
        if start_at_utc is None:
            start = datetime.now(timezone.utc)
        elif isinstance(start_at_utc, str) and start_at_utc.strip():
            try:
                start = datetime.strptime(start_at_utc.strip(), "%Y-%m-%dT%H:%M:%SZ")
                start = start.replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message="startAtUtc must be an ISO-8601 UTC timestamp string",
                ) from exc
        else:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="startAtUtc must be a non-empty string when provided",
            )

        channel_value = channel.strip()
        start_utc = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = (start + timedelta(hours=float(window_hours))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        rows = self._context.persistence.connection.execute(
            """
            SELECT
                c.source,
                c.source_channel_id,
                c.display_name,
                c.callsign,
                c.logical_channel_number,
                b.start_utc,
                b.stop_utc,
                b.duration_seconds,
                p.title,
                p.description_long,
                p.genre_primary,
                json_extract(p.metadata_json, '$.contentRef') AS content_ref,
                json_extract(p.metadata_json, '$.seriesRef') AS series_ref
            FROM epg_broadcasts AS b
            JOIN epg_channels AS c ON c.id = b.channel_id
            JOIN epg_programs AS p ON p.id = b.program_id
            WHERE b.start_utc <= ?
              AND b.stop_utc > ?
              AND (
                lower(c.display_name) = lower(?)
                                OR replace(lower(c.display_name), ' ', '') = replace(lower(?), ' ', '')
                OR lower(COALESCE(c.callsign, '')) = lower(?)
                OR lower(COALESCE(c.logical_channel_number, '')) = lower(?)
              )
            ORDER BY b.start_utc ASC
            """,
                        (
                                end_utc,
                                start_utc,
                                channel_value,
                                channel_value,
                                channel_value,
                                channel_value,
                        ),
        ).fetchall()

        return {
            "channel": channel_value,
            "window": {
                "startAtUtc": start_utc,
                "endAtUtc": end_utc,
            },
            "programs": [
                {
                    "source": str(row[0]),
                    "sourceChannelId": str(row[1]),
                    "channelName": str(row[2]),
                    "callsign": str(row[3]) if row[3] is not None else None,
                    "logicalChannelNumber": (
                        str(row[4]) if row[4] is not None else None
                    ),
                    "startAtUtc": str(row[5]),
                    "stopAtUtc": str(row[6]) if row[6] is not None else None,
                    "durationSeconds": int(row[7]) if row[7] is not None else None,
                    "title": str(row[8]),
                    "description": str(row[9]) if row[9] is not None else None,
                    "genre": str(row[10]) if row[10] is not None else None,
                    "contentRef": str(row[11]) if row[11] is not None else None,
                    "seriesRef": str(row[12]) if row[12] is not None else None,
                }
                for row in rows
            ],
        }

    def _metadata_films_list(self, payload: dict[str, object]) -> dict[str, object]:
        channel_scope = payload.get("channelScope", "favourites")
        if not isinstance(channel_scope, str):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="channelScope must be 'all' or 'favourites'",
            )
        channel_scope_value = channel_scope.strip().lower()
        if channel_scope_value not in {"all", "favourites"}:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="channelScope must be 'all' or 'favourites'",
            )

        window_hours = payload.get("windowHours", 24 * 7)
        if not isinstance(window_hours, int | float) or window_hours <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="windowHours must be greater than 0",
            )

        min_duration_hours = payload.get("minDurationHours", 1.5)
        if not isinstance(min_duration_hours, int | float) or min_duration_hours <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="minDurationHours must be greater than 0",
            )

        max_duration_hours = payload.get("maxDurationHours", 3.5)
        if not isinstance(max_duration_hours, int | float) or max_duration_hours <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="maxDurationHours must be greater than 0",
            )

        if float(max_duration_hours) <= float(min_duration_hours):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="maxDurationHours must be greater than minDurationHours",
            )

        start_at_utc = payload.get("startAtUtc")
        if start_at_utc is None:
            start = datetime.now(timezone.utc)
        elif isinstance(start_at_utc, str) and start_at_utc.strip():
            try:
                start = datetime.strptime(start_at_utc.strip(), "%Y-%m-%dT%H:%M:%SZ")
                start = start.replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ServiceCommandError(
                    code="VALIDATION_ERROR",
                    message="startAtUtc must be an ISO-8601 UTC timestamp string",
                ) from exc
        else:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="startAtUtc must be a non-empty string when provided",
            )

        start_utc = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = (start + timedelta(hours=float(window_hours))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        min_duration_seconds = int(float(min_duration_hours) * 3600)
        max_duration_seconds = int(float(max_duration_hours) * 3600)

        rows = self._context.persistence.connection.execute(
            """
            SELECT
                c.source,
                c.source_channel_id,
                c.display_name,
                c.callsign,
                c.logical_channel_number,
                b.start_utc,
                b.stop_utc,
                b.duration_seconds,
                p.title,
                p.description_long,
                p.genre_primary,
                json_extract(p.metadata_json, '$.contentRef') AS content_ref,
                json_extract(p.metadata_json, '$.seriesRef') AS series_ref
            FROM epg_broadcasts AS b
            JOIN epg_channels AS c ON c.id = b.channel_id
            JOIN epg_programs AS p ON p.id = b.program_id
            WHERE b.start_utc >= ?
              AND b.start_utc < ?
              AND b.duration_seconds >= ?
              AND b.duration_seconds <= ?
            ORDER BY b.start_utc ASC,
                     c.display_name COLLATE NOCASE ASC,
                     p.title COLLATE NOCASE ASC
            """,
            (start_utc, end_utc, min_duration_seconds, max_duration_seconds),
        ).fetchall()

        favourite_names: set[str] = set()
        if channel_scope_value == "favourites":
            favorite_rows = self._context.persistence.connection.execute(
                """
                SELECT lower(trim(display_name))
                FROM epg_channels
                WHERE favorite_channel = 1
                  AND trim(display_name) != ''
                """
            ).fetchall()
            favourite_names = {
                str(row[0])
                for row in favorite_rows
                if row[0] is not None and str(row[0]).strip()
            }

        films_by_slot: dict[tuple[str, str, str, str], dict[str, object]] = {}
        service_eligibility_cache: dict[str, bool] = {}
        for row in rows:
            title = str(row[8])
            channel_name = str(row[2])
            if (
                channel_scope_value == "favourites"
                and channel_name.strip().casefold() not in favourite_names
            ):
                continue

            if not self._channel_is_eligible_for_films(
                channel_name,
                cache=service_eligibility_cache,
            ):
                continue

            stop_at_utc = str(row[6]) if row[6] is not None else ""
            dedupe_key = (
                channel_name.casefold(),
                str(row[5]),
                stop_at_utc,
                title.casefold(),
            )
            film = {
                "source": str(row[0]),
                "sourceChannelId": str(row[1]),
                "channelName": channel_name,
                "callsign": str(row[3]) if row[3] is not None else None,
                "logicalChannelNumber": str(row[4]) if row[4] is not None else None,
                "startAtUtc": str(row[5]),
                "stopAtUtc": str(row[6]) if row[6] is not None else None,
                "durationSeconds": int(row[7]) if row[7] is not None else None,
                "title": title,
                "description": str(row[9]) if row[9] is not None else None,
                "genre": str(row[10]) if row[10] is not None else None,
                "contentRef": str(row[11]) if row[11] is not None else None,
                "seriesRef": str(row[12]) if row[12] is not None else None,
            }

            current = films_by_slot.get(dedupe_key)
            if current is None or source_priority(film["source"]) < source_priority(
                str(current["source"])
            ):
                films_by_slot[dedupe_key] = film

        films = sorted(
            films_by_slot.values(),
            key=lambda film: (
                str(film["startAtUtc"]),
                str(film["channelName"]).casefold(),
                str(film["title"]).casefold(),
            ),
        )

        return {
            "window": {
                "startAtUtc": start_utc,
                "endAtUtc": end_utc,
            },
            "filters": {
                "channelScope": channel_scope_value,
                "minDurationHours": float(min_duration_hours),
                "maxDurationHours": float(max_duration_hours),
            },
            "films": films,
        }

    def _channel_is_eligible_for_films(
        self,
        channel_name: str,
        *,
        cache: dict[str, bool],
    ) -> bool:
        key = channel_name.strip().casefold()
        if key in cache:
            return cache[key]

        # Default to include when control-plane inspection is unavailable.
        eligible = True
        try:
            resolved_name = self._context.tvrecorder.resolve_service_name(channel_name)
            serviceinfo = self._context.tvrecorder.run(
                serviceinfo_command(resolved_name)
            ).stdout
            has_pid = self._serviceinfo_has_media_pid(serviceinfo)
            is_radio = self._serviceinfo_is_radio(serviceinfo)
            eligible = has_pid and not is_radio
        except Exception:
            eligible = True

        cache[key] = eligible
        return eligible

    def _serviceinfo_has_media_pid(self, raw: str) -> bool:
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.casefold()
            if "pid" not in lowered:
                continue
            if "service id" in lowered:
                continue
            if ":" in stripped:
                value = stripped.split(":", 1)[1].strip()
            else:
                value = stripped
            value_lower = value.casefold()
            if value_lower in {"none", "n/a", "-", "null", "no"}:
                continue
            if re.search(r"0x[0-9a-fA-F]+|\\b\\d+\\b", value):
                return True
        return False

    def _serviceinfo_is_radio(self, raw: str) -> bool:
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.casefold()
            if "type" in lowered and "radio" in lowered:
                return True
        return False

    def _metadata_series_recording_list(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        del payload
        series_refs = self._context.persistence.list_series_recording_subscriptions()
        return {
            "subscriptions": [
                {
                    "seriesRef": series_ref,
                    "enabled": True,
                }
                for series_ref in series_refs
            ]
        }

    def _metadata_series_recording_set(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        series_ref = payload.get("seriesRef")
        if not isinstance(series_ref, str) or not series_ref.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="seriesRef must be a non-empty string",
            )

        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="enabled must be a boolean",
            )

        normalized = series_ref.strip()
        self._context.persistence.set_series_recording_subscription(normalized, enabled)

        auto_schedule = {
            "scheduled": 0,
            "skipped": 0,
        }
        if enabled:
            auto_schedule = self._auto_schedule_series_recordings(
                only_series_refs={normalized}
            )

        return {
            "seriesRef": normalized,
            "enabled": enabled,
            "autoSchedule": auto_schedule,
        }

    def _auto_schedule_series_recordings(
        self,
        *,
        only_series_refs: set[str] | None = None,
    ) -> dict[str, int]:
        all_series_refs = set(self._context.persistence.list_series_recording_subscriptions())
        if only_series_refs is not None:
            all_series_refs = {ref for ref in all_series_refs if ref in only_series_refs}
        if not all_series_refs:
            return {"scheduled": 0, "skipped": 0}

        placeholders = ", ".join("?" for _ in all_series_refs)
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._context.persistence.connection.execute(
            f"""
            SELECT
                c.display_name,
                b.start_utc,
                b.stop_utc,
                b.duration_seconds,
                p.title,
                p.description_long,
                json_extract(p.metadata_json, '$.contentRef') AS content_ref,
                json_extract(p.metadata_json, '$.seriesRef') AS series_ref
            FROM epg_broadcasts AS b
            JOIN epg_channels AS c ON c.id = b.channel_id
            JOIN epg_programs AS p ON p.id = b.program_id
            WHERE b.start_utc >= ?
              AND json_extract(p.metadata_json, '$.seriesRef') IN ({placeholders})
            ORDER BY b.start_utc ASC, c.display_name COLLATE NOCASE ASC
            """,
            (now_utc, *sorted(all_series_refs)),
        ).fetchall()

        existing_jobs = self._context.persistence.list_scheduler_jobs()
        taken_content_refs = {
            job.program_content_ref
            for job in existing_jobs
            if isinstance(job.program_content_ref, str)
            and job.program_content_ref.strip()
            and job.state in {"scheduled", "running", "completed"}
        }
        taken_slots = {
            (
                job.channel_name.casefold(),
                job.program_start_at_utc or job.start_at_utc,
            )
            for job in existing_jobs
            if job.state in {"scheduled", "running", "completed"}
        }

        scheduled = 0
        skipped = 0
        service_eligibility_cache: dict[str, bool] = {}
        for row in rows:
            channel_name = str(row[0])
            if not self._channel_is_eligible_for_films(
                channel_name,
                cache=service_eligibility_cache,
            ):
                skipped += 1
                continue

            start_at_utc = str(row[1])
            slot_key = (channel_name.casefold(), start_at_utc)
            content_ref = str(row[6]).strip() if row[6] is not None else None
            series_ref = str(row[7]).strip() if row[7] is not None else None

            if slot_key in taken_slots:
                skipped += 1
                continue
            if content_ref and content_ref in taken_content_refs:
                skipped += 1
                continue
            if content_ref and self._context.persistence.has_recorded_content_ref(content_ref):
                skipped += 1
                continue

            duration_seconds: int | None = int(row[3]) if row[3] is not None else None
            if duration_seconds is None and row[2] is not None:
                try:
                    start_dt = datetime.strptime(start_at_utc, "%Y-%m-%dT%H:%M:%SZ")
                    stop_dt = datetime.strptime(str(row[2]), "%Y-%m-%dT%H:%M:%SZ")
                    duration_seconds = int((stop_dt - start_dt).total_seconds())
                except ValueError:
                    duration_seconds = None
            if duration_seconds is None or duration_seconds < MIN_RECORDING_SECONDS:
                skipped += 1
                continue

            try:
                self._context.tvrecorder.schedule_recording(
                    channel_name=channel_name,
                    start_at_utc=start_at_utc,
                    duration_seconds=duration_seconds,
                    program_title=str(row[4]) if row[4] is not None else None,
                    program_description=str(row[5]) if row[5] is not None else None,
                    program_start_at_utc=start_at_utc,
                    program_stop_at_utc=(str(row[2]) if row[2] is not None else None),
                    program_content_ref=content_ref,
                    program_series_ref=series_ref,
                )
            except Exception:
                skipped += 1
                continue

            scheduled += 1
            taken_slots.add(slot_key)
            if content_ref:
                taken_content_refs.add(content_ref)

        return {
            "scheduled": scheduled,
            "skipped": skipped,
        }

    def _metadata_sd_sync_run(self, payload: dict[str, object]) -> dict[str, object]:
        lineup_id = payload.get("lineupId")
        if not isinstance(lineup_id, str) or not lineup_id.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="lineupId must be a non-empty string",
            )

        seed = payload.get("seed", False)
        if not isinstance(seed, bool):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="seed must be a boolean",
            )

        window_hours = payload.get("windowHours", 24)
        if not isinstance(window_hours, int | float) or window_hours <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="windowHours must be greater than 0",
            )

        credentials_path = payload.get("credentialsPath")
        if credentials_path is not None and (
            not isinstance(credentials_path, str) or not credentials_path.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="credentialsPath must be a non-empty string when provided",
            )

        database_path = payload.get("databasePath")
        if database_path is not None and (
            not isinstance(database_path, str) or not database_path.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="databasePath must be a non-empty string when provided",
            )

        timeout_seconds = payload.get("timeoutSeconds", self._sd_sync_timeout_seconds)
        if not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="timeoutSeconds must be greater than 0",
            )

        clear_existing = payload.get("clearExisting", False)
        if not isinstance(clear_existing, bool):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="clearExisting must be a boolean",
            )

        self._raise_if_stopping()

        try:
            stats = self._run_coroutine_blocking(
                self._run_sd_sync(
                    lineup_id=lineup_id.strip(),
                    seed=seed,
                    window_hours=float(window_hours),
                    credentials_path=credentials_path,
                    database_path=database_path,
                    clear_existing=clear_existing,
                ),
                timeout_seconds=float(timeout_seconds),
            )
        except SchedulesDirectAuthenticationError as exc:
            raise ServiceCommandError(
                code="SD_AUTH_FAILED",
                message=str(exc),
                retryable=False,
            ) from exc
        except SchedulesDirectRateLimitError as exc:
            raise ServiceCommandError(
                code="SD_RATE_LIMITED",
                message=str(exc),
                retryable=True,
                details={
                    "retryAfterSeconds": exc.retry_after_seconds,
                },
            ) from exc
        except SchedulesDirectTransportError as exc:
            raise ServiceCommandError(
                code="SD_UPSTREAM_ERROR",
                message=str(exc),
                retryable=True,
                details={
                    "errorType": "transport",
                },
            ) from exc
        except SchedulesDirectApiError as exc:
            raise ServiceCommandError(
                code="SD_UPSTREAM_ERROR",
                message=str(exc),
                retryable=True,
                details={
                    "errorType": "api",
                    "providerCode": exc.code,
                },
            ) from exc
        except TimeoutError as exc:
            raise ServiceCommandError(
                code="SD_SYNC_TIMEOUT",
                message="metadata.sd.sync.run timed out",
                retryable=True,
                details={"timeoutSeconds": float(timeout_seconds)},
            ) from exc

        auto_schedule = self._auto_schedule_series_recordings()

        return {
            "stats": {
                "channelsUpserted": stats.channels_upserted,
                "programsUpserted": stats.programs_upserted,
                "schedulesUpserted": stats.schedules_upserted,
                "staleSchedulesPruned": stats.stale_schedules_pruned,
                "ingestRunId": stats.ingest_run_id,
                "fullRefresh": clear_existing,
                "seriesAutoScheduled": auto_schedule["scheduled"],
                "seriesAutoSkipped": auto_schedule["skipped"],
            }
        }

    @staticmethod
    def _parse_ts_id_from_serviceinfo(raw: str) -> str | None:
        """Extract transport-stream ID from dvbctrl serviceinfo output.

        The ``ID`` line looks like ``233a.6040.6e40``; we return the middle
        component formatted as ``0x6040``.
        """
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped.startswith("ID"):
                continue
            if ":" not in stripped:
                continue
            value = stripped.split(":", 1)[1].strip()
            parts = value.split(".")
            if len(parts) >= 3:
                return "0x" + parts[1].lower()
        return None

    def _metadata_ota_multimux_sync_run(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        """Capture OTA EPG from one representative TV channel per DVB mux.

        For each distinct transport stream discovered via dvbctrl lsservices /
        serviceinfo, picks the first non-radio, video-capable service, then
        runs a timed epgdata capture.  Retries each mux if dvbstreamer is
        transiently busy (e.g. a recording is in progress).
        """
        capture_seconds = payload.get("captureSeconds", 900.0)
        if not isinstance(capture_seconds, int | float) or capture_seconds <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="captureSeconds must be greater than 0",
            )

        grab_command = payload.get("grabCommand", "epgdata")
        if not isinstance(grab_command, str) or not grab_command.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="grabCommand must be a non-empty string when provided",
            )

        max_retries = payload.get("maxRetries", 3)
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="maxRetries must be a non-negative integer",
            )

        retry_delay_seconds = payload.get("retryDelaySeconds", 300.0)
        if not isinstance(retry_delay_seconds, int | float) or retry_delay_seconds < 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="retryDelaySeconds must be a non-negative number",
            )

        frontend_lock_timeout_seconds = payload.get("frontendLockTimeoutSeconds", 15.0)
        if (
            not isinstance(frontend_lock_timeout_seconds, int | float)
            or frontend_lock_timeout_seconds <= 0
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="frontendLockTimeoutSeconds must be greater than 0",
            )

        database_path = payload.get("databasePath")
        if database_path is not None and (
            not isinstance(database_path, str) or not database_path.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="databasePath must be a non-empty string when provided",
            )

        source = payload.get("source", "dvbstreamer_ota")
        if not isinstance(source, str) or not source.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="source must be a non-empty string when provided",
            )

        logger = self._context.logger
        self._raise_if_stopping()

        # ------------------------------------------------------------------ #
        # 1.  Discover one representative TV service per transport stream.     #
        # ------------------------------------------------------------------ #
        try:
            all_services = self._context.tvrecorder.list_services()
        except Exception as exc:
            raise ServiceCommandError(
                code="OTA_GRAB_FAILED",
                message=f"failed to list dvbstreamer services: {exc}",
                retryable=True,
            ) from exc

        mux_representative: dict[str, str] = {}  # ts_id -> service_name
        for svc in all_services:
            self._raise_if_stopping()
            ts_id = None
            try:
                info = self._context.tvrecorder.run(serviceinfo_command(svc)).stdout
                if self._serviceinfo_is_radio(info):
                    continue
                if not self._serviceinfo_has_media_pid(info):
                    continue
                ts_id = self._parse_ts_id_from_serviceinfo(info)
            except Exception as exc:
                logger.debug("serviceinfo failed for %r: %s", svc, exc)
                continue
            if ts_id and ts_id not in mux_representative:
                mux_representative[ts_id] = svc
                logger.debug("selected %r as representative for mux %s", svc, ts_id)

        if not mux_representative:
            raise ServiceCommandError(
                code="OTA_GRAB_FAILED",
                message="no eligible TV services found to use as mux representatives",
                retryable=True,
            )

        logger.info(
            "OTA multi-mux sync: found %d distinct muxes to capture",
            len(mux_representative),
        )

        # ------------------------------------------------------------------ #
        # 2.  Capture EPG from each mux with per-mux retry.                  #
        # ------------------------------------------------------------------ #
        target_connection = self._context.persistence.connection
        close_after = False
        if database_path is not None:
            target_connection = initialize_database(Path(database_path.strip()))
            close_after = True

        total_channels = 0
        total_programs = 0
        total_broadcasts = 0
        muxes_ok: list[str] = []
        muxes_failed: list[str] = []

        try:
            for ts_id, svc in mux_representative.items():
                self._raise_if_stopping()
                last_exc: Exception | None = None

                for attempt in range(max_retries + 1):
                    self._raise_if_stopping()
                    if attempt > 0:
                        logger.info(
                            "OTA multi-mux sync: retrying mux %s (attempt %d/%d) "
                            "after %.0fs delay",
                            ts_id,
                            attempt + 1,
                            max_retries + 1,
                            retry_delay_seconds,
                        )
                        deadline = time.monotonic() + float(retry_delay_seconds)
                        while time.monotonic() < deadline:
                            self._raise_if_stopping()
                            time.sleep(min(5.0, deadline - time.monotonic()))

                    capture_clients = self._ota_multimux_capture_clients()
                    if not capture_clients:
                        last_exc = RuntimeError(
                            "no capture clients available (all adapters busy?)"
                        )
                        logger.warning(
                            "OTA multi-mux sync: mux %s attempt %d failed: %s",
                            ts_id,
                            attempt + 1,
                            last_exc,
                        )
                        continue

                    for adapter_index, tvrecorder, dvbctrl, manager in capture_clients:
                        try:
                            self._ensure_ota_control_ready_with_clients(
                                dvbctrl=dvbctrl,
                                manager=manager,
                            )
                            resolved = tvrecorder.resolve_service_name(svc)
                            tvrecorder.select_service(resolved)
                            self._wait_for_frontend_lock_with_service(
                                tvrecorder=tvrecorder,
                                timeout_seconds=float(frontend_lock_timeout_seconds),
                            )
                            try:
                                channel_name_map = tvrecorder.list_service_channel_name_map()
                            except Exception as map_exc:
                                logger.warning(
                                    "failed to resolve channel name map for mux %s: %s",
                                    ts_id,
                                    map_exc,
                                )
                                channel_name_map = {}

                            grab_result = self._capture_ota_epg_stream_with_clients(
                                tvrecorder=tvrecorder,
                                dvbctrl=dvbctrl,
                                grab_command=grab_command.strip(),
                                capture_seconds=float(capture_seconds),
                            )
                            stats = ingest_dvbstreamer_epg(
                                target_connection,
                                grab_result.stdout,
                                channel_name_map=channel_name_map,
                                source=source.strip(),
                            )
                            total_channels += stats.channels_upserted
                            total_programs += stats.programs_upserted
                            total_broadcasts += stats.broadcasts_upserted
                            muxes_ok.append(ts_id)
                            logger.info(
                                "OTA multi-mux sync: mux %s ok "
                                "(service=%r adapter=%s channels=%d programs=%d broadcasts=%d)",
                                ts_id,
                                svc,
                                adapter_index,
                                stats.channels_upserted,
                                stats.programs_upserted,
                                stats.broadcasts_upserted,
                            )
                            last_exc = None
                            break
                        except Exception as exc:
                            last_exc = exc
                            logger.warning(
                                "OTA multi-mux sync: mux %s attempt %d adapter %s failed: %s",
                                ts_id,
                                attempt + 1,
                                adapter_index,
                                exc,
                            )

                    if last_exc is None:
                        break

                if last_exc is not None:
                    muxes_failed.append(ts_id)
                    logger.error(
                        "OTA multi-mux sync: mux %s failed after %d attempt(s), skipping",
                        ts_id,
                        max_retries + 1,
                    )
        finally:
            if close_after:
                target_connection.close()

        auto_schedule = self._auto_schedule_series_recordings()

        logger.info(
            "OTA multi-mux sync complete "
            "(muxes_ok=%d muxes_failed=%d channels=%d programs=%d broadcasts=%d)",
            len(muxes_ok),
            len(muxes_failed),
            total_channels,
            total_programs,
            total_broadcasts,
        )

        return {
            "stats": {
                "muxesAttempted": len(mux_representative),
                "muxesOk": len(muxes_ok),
                "muxesFailed": len(muxes_failed),
                "channelsUpserted": total_channels,
                "programsUpserted": total_programs,
                "broadcastsUpserted": total_broadcasts,
                "seriesAutoScheduled": auto_schedule["scheduled"],
                "seriesAutoSkipped": auto_schedule["skipped"],
            }
        }

    def _ota_multimux_capture_clients(self) -> list[tuple[int, object, object, object]]:
        """Return candidate (adapter, tvrecorder, dvbctrl, dvbstreamer) tuples.

        The primary adapter is always included.  Idle adapter-pool slots are
        appended so multimux capture can try another adapter before sleeping.
        """
        clients: list[tuple[int, object, object, object]] = []

        primary_tvrecorder = getattr(self._context, "tvrecorder", None)
        primary_dvbctrl = getattr(self._context, "dvbctrl", None)
        primary_manager = getattr(self._context, "dvbstreamer", None)
        if primary_tvrecorder is not None and primary_dvbctrl is not None:
            adapter_index = int(getattr(primary_dvbctrl, "adapter_index", 0))
            clients.append((adapter_index, primary_tvrecorder, primary_dvbctrl, primary_manager))

        adapter_pool = getattr(self._context, "adapter_pool", None)
        if adapter_pool is None:
            return clients

        for slot in adapter_pool.idle_slots_snapshot():
            capture_controller = getattr(slot, "capture_controller", None)
            tvrecorder = getattr(capture_controller, "service", None)
            if tvrecorder is None:
                continue
            dvbctrl = getattr(tvrecorder, "_dvbctrl", None)
            if dvbctrl is None:
                continue
            adapter_index = int(getattr(slot, "adapter_index", getattr(dvbctrl, "adapter_index", -1)))
            if any(existing_index == adapter_index for existing_index, *_rest in clients):
                continue
            manager = getattr(slot, "dvbstreamer", None)
            clients.append((adapter_index, tvrecorder, dvbctrl, manager))

        return clients

    def _metadata_ota_sync_run(self, payload: dict[str, object]) -> dict[str, object]:
        source = payload.get("source", "dvbstreamer_ota")
        if not isinstance(source, str) or not source.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="source must be a non-empty string when provided",
            )

        database_path = payload.get("databasePath")
        if database_path is not None and (
            not isinstance(database_path, str) or not database_path.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="databasePath must be a non-empty string when provided",
            )

        grab_command = payload.get("grabCommand", "epgdata")
        if not isinstance(grab_command, str) or not grab_command.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="grabCommand must be a non-empty string when provided",
            )

        default_ota_channel_name = getattr(
            self._context.settings,
            "ota_epg_channel_name",
            "BBC TWO HD",
        )
        channel_name = payload.get("channelName", default_ota_channel_name)
        if not isinstance(channel_name, str) or not channel_name.strip():
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="channelName must be a non-empty string when provided",
            )

        frontend_lock_timeout_seconds = payload.get("frontendLockTimeoutSeconds", 15.0)
        if (
            not isinstance(frontend_lock_timeout_seconds, int | float)
            or frontend_lock_timeout_seconds <= 0
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="frontendLockTimeoutSeconds must be greater than 0",
            )

        capture_seconds = payload.get("captureSeconds", 10.0)
        if not isinstance(capture_seconds, int | float) or capture_seconds <= 0:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="captureSeconds must be greater than 0",
            )

        logger = self._context.logger
        self._raise_if_stopping()

        self._ensure_ota_control_ready()

        resolved_channel = self._context.tvrecorder.resolve_service_name(
            channel_name.strip()
        )

        try:
            self._context.tvrecorder.select_service(resolved_channel)
            self._wait_for_frontend_lock(
                timeout_seconds=float(frontend_lock_timeout_seconds),
            )

            try:
                channel_name_map = self._context.tvrecorder.list_service_channel_name_map()
            except Exception as exc:
                logger.warning("failed to resolve OTA serviceinfo mapping: %s", exc)
                channel_name_map = {}

            grab_result = self._capture_ota_epg_stream(
                grab_command=grab_command.strip(),
                capture_seconds=float(capture_seconds),
            )
        except Exception as exc:
            logger.error(
                "OTA EPG grab failed (command=%s): %s",
                grab_command.strip(),
                exc,
            )
            raise ServiceCommandError(
                code="OTA_GRAB_FAILED",
                message=str(exc),
                retryable=True,
            ) from exc

        target_connection = self._context.persistence.connection
        close_after = False
        if database_path is not None:
            target_connection = initialize_database(Path(database_path.strip()))
            close_after = True

        try:
            stats = ingest_dvbstreamer_epg(
                target_connection,
                grab_result.stdout,
                channel_name_map=channel_name_map,
                source=source.strip(),
            )
        except Exception as exc:
            logger.error("OTA EPG ingest failed: %s", exc)
            raise ServiceCommandError(
                code="OTA_INGEST_FAILED",
                message=str(exc),
                retryable=True,
            ) from exc
        finally:
            if close_after:
                target_connection.close()

        logger.info(
            "OTA EPG sync complete (source=%s, channels=%d, programs=%d, broadcasts=%d, run_id=%s)",
            source.strip(),
            stats.channels_upserted,
            stats.programs_upserted,
            stats.broadcasts_upserted,
            stats.ingest_run_id,
        )

        auto_schedule = self._auto_schedule_series_recordings()

        return {
            "stats": {
                "channelsUpserted": stats.channels_upserted,
                "programsUpserted": stats.programs_upserted,
                "broadcastsUpserted": stats.broadcasts_upserted,
                "parsedEvents": stats.parsed_events,
                "ingestRunId": stats.ingest_run_id,
                "seriesAutoScheduled": auto_schedule["scheduled"],
                "seriesAutoSkipped": auto_schedule["skipped"],
            }
        }

    def _metadata_ota_channel_names_backfill_run(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        database_path = payload.get("databasePath")
        if database_path is not None and (
            not isinstance(database_path, str) or not database_path.strip()
        ):
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="databasePath must be a non-empty string when provided",
            )

        try:
            channel_name_map = self._context.tvrecorder.list_service_channel_name_map()
        except Exception as exc:
            raise ServiceCommandError(
                code="OTA_CHANNEL_MAP_FAILED",
                message=str(exc),
                retryable=True,
            ) from exc

        target_connection = self._context.persistence.connection
        close_after = False
        if database_path is not None:
            target_connection = initialize_database(Path(database_path.strip()))
            close_after = True

        try:
            synthetic_before = int(
                target_connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM epg_channels
                    WHERE source = 'dvbstreamer_ota'
                      AND display_name LIKE 'service 0x%'
                    """
                ).fetchone()[0]
            )
            total_channels = int(
                target_connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM epg_channels
                    WHERE source = 'dvbstreamer_ota'
                    """
                ).fetchone()[0]
            )

            updated_rows = 0
            with target_connection:
                for source_channel_id, service_name in channel_name_map.items():
                    updated_rows += target_connection.execute(
                        """
                        UPDATE epg_channels
                        SET display_name = ?
                        WHERE source = 'dvbstreamer_ota'
                          AND source_channel_id = ?
                          AND display_name != ?
                        """,
                        (service_name, source_channel_id, service_name),
                    ).rowcount

            synthetic_after = int(
                target_connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM epg_channels
                    WHERE source = 'dvbstreamer_ota'
                      AND display_name LIKE 'service 0x%'
                    """
                ).fetchone()[0]
            )
        finally:
            if close_after:
                target_connection.close()

        return {
            "stats": {
                "servicesResolved": len(channel_name_map),
                "rowsUpdated": updated_rows,
                "syntheticBefore": synthetic_before,
                "syntheticAfter": synthetic_after,
                "totalChannels": total_channels,
            }
        }

    def _ensure_ota_control_ready(self) -> None:
        self._ensure_ota_control_ready_with_clients(
            dvbctrl=getattr(self._context, "dvbctrl", None),
            manager=getattr(self._context, "dvbstreamer", None),
        )

    def _ensure_ota_control_ready_with_clients(self, *, dvbctrl, manager) -> None:
        logger = self._context.logger
        settings = getattr(self._context, "settings", None)
        allow_manager_start = bool(
            getattr(settings, "dvbstreamer_manage_process", True)
        )

        if dvbctrl is not None:
            try:
                dvbctrl.run_command("stats")
                return
            except Exception:
                pass

        if manager is None:
            return

        try:
            status = manager.health_check()
            state = getattr(status, "state", None)
            if state != DvbStreamerState.RUNNING:
                if not allow_manager_start:
                    raise RuntimeError(
                        "dvbstreamer control endpoint unavailable and this process "
                        "is configured as non-owner"
                    )
                manager.start()
                logger.info("OTA EPG sync started dvbstreamer manager")

            deadline = time.monotonic() + 5.0
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                self._raise_if_stopping()
                try:
                    if dvbctrl is None:
                        return
                    dvbctrl.run_command("stats")
                    return
                except Exception as exc:
                    last_error = exc
                time.sleep(0.25)

            if last_error is not None:
                raise RuntimeError(
                    "dvbctrl control endpoint did not become ready: "
                    f"{last_error}"
                )
            raise RuntimeError("dvbctrl control endpoint did not become ready")
        except Exception as exc:
            logger.error("OTA EPG sync failed starting dvbstreamer: %s", exc)
            raise ServiceCommandError(
                code="OTA_GRAB_FAILED",
                message=f"failed to start dvbstreamer: {exc}",
                retryable=True,
            ) from exc

    def _capture_ota_epg_stream(self, *, grab_command: str, capture_seconds: float):
        return self._capture_ota_epg_stream_with_clients(
            tvrecorder=getattr(self._context, "tvrecorder", None),
            dvbctrl=getattr(self._context, "dvbctrl", None),
            grab_command=grab_command,
            capture_seconds=capture_seconds,
        )

    def _capture_ota_epg_stream_with_clients(
        self,
        *,
        tvrecorder,
        dvbctrl,
        grab_command: str,
        capture_seconds: float,
    ):
        if dvbctrl is None:
            raise RuntimeError("dvbctrl not available for OTA EPG streaming capture")
        if tvrecorder is None:
            raise RuntimeError("tvrecorder not available for OTA EPG streaming capture")

        logger = self._context.logger
        proc = None
        capture_started = False
        stop_error: Exception | None = None
        try:
            try:
                tvrecorder.run_raw("epgcapstart")
            except Exception as exc:
                if "already started" not in str(exc).casefold():
                    raise
                # Stale capture state can survive previous failures; stop then retry once.
                logger.warning(
                    "OTA EPG capture already started; forcing epgcapstop before retry"
                )
                self._stop_ota_capture_with_separate_client_for(
                    dvbctrl=dvbctrl,
                    tvrecorder=tvrecorder,
                )
                tvrecorder.run_raw("epgcapstart")
            capture_started = True

            proc = dvbctrl.start_command(grab_command)

            deadline = time.monotonic() + capture_seconds
            while time.monotonic() < deadline:
                self._raise_if_stopping()
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.25, remaining))
        finally:
            if capture_started:
                try:
                    self._stop_ota_capture_with_separate_client_for(
                        dvbctrl=dvbctrl,
                        tvrecorder=tvrecorder,
                    )
                except Exception as exc:
                    stop_error = exc
            if proc is not None:
                try:
                    proc.send_signal(_signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass

        if proc is None:
            raise RuntimeError("failed to start OTA EPG grab command")

        try:
            stdout, stderr = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

        if stop_error is not None:
            raise RuntimeError(f"failed to stop OTA capture: {stop_error}") from stop_error

        allowed_returncodes = {0, 130, -int(_signal.SIGINT)}
        if proc.returncode not in allowed_returncodes:
            detail = (stderr or "").strip() or "no stderr"
            raise RuntimeError(
                "OTA EPG grab command failed "
                f"(returncode={proc.returncode}): {detail}"
            )

        return SimpleNamespace(stdout=stdout)

    def _stop_ota_capture_with_separate_client(self) -> None:
        self._stop_ota_capture_with_separate_client_for(
            dvbctrl=getattr(self._context, "dvbctrl", None),
            tvrecorder=getattr(self._context, "tvrecorder", None),
        )

    def _stop_ota_capture_with_separate_client_for(self, *, dvbctrl, tvrecorder) -> None:
        if dvbctrl is None:
            if tvrecorder is None:
                raise RuntimeError("tvrecorder not available for OTA EPG stop")
            tvrecorder.run_raw("epgcapstop")
            return

        stop_client = DvbCtrlClient(
            executable_path=dvbctrl.executable_path,
            host=dvbctrl.host,
            adapter_index=dvbctrl.adapter_index,
            timeout_seconds=dvbctrl.timeout_seconds,
            transient_retry_count=dvbctrl.transient_retry_count,
            transient_retry_delay_seconds=dvbctrl.transient_retry_delay_seconds,
        )
        stop_client.run_command("epgcapstop")

    def _wait_for_frontend_lock(self, *, timeout_seconds: float) -> None:
        self._wait_for_frontend_lock_with_service(
            tvrecorder=getattr(self._context, "tvrecorder", None),
            timeout_seconds=timeout_seconds,
        )

    def _wait_for_frontend_lock_with_service(self, *, tvrecorder, timeout_seconds: float) -> None:
        if tvrecorder is None:
            raise RuntimeError("tvrecorder not available for frontend lock polling")
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            self._raise_if_stopping()
            try:
                status = tvrecorder.frontend_status()
                if status.locked:
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)

        if last_error is not None:
            raise RuntimeError(
                "frontend did not lock before timeout "
                f"(last error: {last_error})"
            )
        raise RuntimeError("frontend did not lock before timeout")

    def _metadata_sd_sync_status_get(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        source = payload.get("source")
        if source is None:
            source_value = "schedules_direct"
        elif isinstance(source, str) and source.strip():
            source_value = source.strip()
        else:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="source must be a non-empty string when provided",
            )

        if source_value != "schedules_direct":
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message="source must be 'schedules_direct'",
            )

        connection = self._context.persistence.connection
        run_row = connection.execute(
            """
            SELECT id, status, finished_at_utc
            FROM epg_ingest_runs
            WHERE source = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (source_value,),
        ).fetchone()
        checkpoint_row = connection.execute(
            """
            SELECT last_successful_ingest_utc
            FROM epg_source_checkpoints
            WHERE source = ?
            """,
            (source_value,),
        ).fetchone()

        return {
            "lastRun": {
                "id": int(run_row[0]) if run_row is not None else None,
                "status": str(run_row[1]) if run_row is not None else None,
                "finishedAtUtc": run_row[2] if run_row is not None else None,
            },
            "checkpoint": {
                "lastSuccessfulIngestUtc": (
                    checkpoint_row[0] if checkpoint_row is not None else None
                )
            },
        }

    def _run_coroutine_blocking(self, coroutine, *, timeout_seconds: float):
        self._raise_if_stopping()

        async def _timed():
            return await asyncio.wait_for(coroutine, timeout=timeout_seconds)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._execute_with_asyncio_run(_timed())

        queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def _target() -> None:
            try:
                queue.put((True, self._execute_with_asyncio_run(_timed())))
            except Exception as exc:
                queue.put((False, exc))

        thread = Thread(target=_target, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(timeout=0.05)
            self._raise_if_stopping()

        if queue.empty():
            raise RuntimeError("async command execution did not return a result")

        ok, payload = queue.get()
        if ok:
            return payload
        raise payload

    def _execute_with_asyncio_run(self, coroutine):
        try:
            return asyncio.run(coroutine)
        except asyncio.TimeoutError as exc:
            raise TimeoutError() from exc

    def _raise_if_stopping(self) -> None:
        if self._should_stop():
            raise ServiceCommandError(
                code="COMMAND_CANCELLED",
                message="command cancelled because service shutdown was requested",
                retryable=True,
            )

    async def _run_sd_sync(
        self,
        *,
        lineup_id: str,
        seed: bool,
        window_hours: float,
        credentials_path: str | None,
        database_path: str | None,
        clear_existing: bool,
    ):
        credential_store = SchedulesDirectCredentialStore(
            path=Path(credentials_path) if credentials_path else None
        )
        self._raise_if_stopping()
        credentials = credential_store.load()

        target_db_path = database_path or self._context.settings.database_path
        connection = initialize_database(Path(target_db_path))
        repository = SqliteGuideRepository(connection=connection)
        client = SchedulesDirectHttpClient(
            token_cache_store=SchedulesDirectTokenCacheStore(),
        )
        service = SchedulesDirectIngestionService(client=client, repository=repository)

        try:
            self._raise_if_stopping()
            await client.authenticate(credentials)
            if clear_existing:
                with connection:
                    connection.execute(
                        """
                        DELETE FROM epg_broadcasts
                        WHERE channel_id IN (
                            SELECT id
                            FROM epg_channels
                            WHERE source = 'schedules_direct'
                        )
                          AND (
                              json_extract(metadata_json, '$.lineup_id') = ?
                              OR json_extract(metadata_json, '$.lineup_id') IS NULL
                          )
                        """,
                        (lineup_id,),
                    )
            self._raise_if_stopping()
            now = datetime.now(timezone.utc)
            if seed:
                effective_window_hours = float(service.seed_window_hours)
            else:
                effective_window_hours = window_hours
            window = GuideSyncWindow(
                start_utc=now,
                end_utc=now + timedelta(hours=effective_window_hours),
            )
            self._raise_if_stopping()
            return await service.sync_incremental_with_stats(
                lineup_id=lineup_id,
                window=window,
            )
        finally:
            await client.close()
            connection.close()


__all__ = [
    "API_VERSION",
    "ServiceCommandDispatcher",
    "ServiceCommandError",
]
