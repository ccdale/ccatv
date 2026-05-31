from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
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

API_VERSION = "v1alpha1"

SERVICE_CAPABILITIES = [
    "service.health",
    "service.info",
    "recording",
    "recording.schedule",
    "recording.worker.cycle",
    "metadata.channels",
    "metadata.guide",
    "metadata.ota.sync",
    "metadata.sd.sync",
    "runtime.setup",
]

SERVICE_COMMANDS = [
    "service.health.get",
    "service.info.get",
    "recording.list",
    "recording.delete",
    "recording.schedule.create",
    "recording.schedule.list",
    "recording.metadata.backfill",
    "recording.worker.cycle.run",
    "metadata.channels.list",
    "metadata.channels.dvbservices.list",
    "metadata.channels.favorite.set",
    "metadata.channels.service-name.set",
    "metadata.guide.list",
    "metadata.ota.sync.run",
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
            return {
                "apiVersion": API_VERSION,
                "requestId": request_id,
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
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
        if command == "recording.schedule.create":
            return self._recording_schedule_create(payload)
        if command == "recording.schedule.list":
            return self._recording_schedule_list(payload)
        if command == "recording.metadata.backfill":
            return self._recording_metadata_backfill(payload)
        if command == "recording.worker.cycle.run":
            return self._recording_worker_cycle_run(payload)
        if command == "metadata.channels.list":
            return self._metadata_channels_list(payload)
        if command == "metadata.channels.dvbservices.list":
            return self._metadata_channels_dvbservices_list(payload)
        if command == "metadata.channels.favorite.set":
            return self._metadata_channels_favorite_set(payload)
        if command == "metadata.channels.service-name.set":
            return self._metadata_channels_service_name_set(payload)
        if command == "metadata.guide.list":
            return self._metadata_guide_list(payload)
        if command == "metadata.ota.sync.run":
            return self._metadata_ota_sync_run(payload)
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
            )
        except ValueError as exc:
            raise ServiceCommandError(
                code="VALIDATION_ERROR",
                message=str(exc),
            ) from exc

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
                }
                for job in jobs
            ]
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

        channels_by_name: dict[str, dict[str, object]] = {}
        for row in rows:
            display_name = str(row[2]).strip()
            dvb_name = str(row[5]).strip() if row[5] is not None else None
            channel = {
                "name": display_name,
                "callsign": str(row[3]) if row[3] is not None else None,
                "logicalChannelNumber": str(row[4]) if row[4] is not None else None,
                "source": str(row[0]),
                "sourceChannelId": str(row[1]),
                "dvbstreamerServiceName": dvb_name or None,
                "favoriteChannel": bool(row[6]),
            }
            key = display_name.casefold()
            current = channels_by_name.get(key)
            if current is None or source_priority(channel["source"]) < source_priority(
                str(current["source"])
            ):
                channels_by_name[key] = channel

        return {
            "channels": sorted(
                channels_by_name.values(),
                key=lambda channel: (
                    not bool(channel["favoriteChannel"]),
                    channel["logicalChannelNumber"] is None,
                    channel["logicalChannelNumber"] or "",
                    str(channel["name"]).casefold(),
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
                p.description_long
            FROM epg_broadcasts AS b
            JOIN epg_channels AS c ON c.id = b.channel_id
            JOIN epg_programs AS p ON p.id = b.program_id
            WHERE b.start_utc >= ?
              AND b.start_utc < ?
              AND (
                lower(c.display_name) = lower(?)
                OR lower(COALESCE(c.callsign, '')) = lower(?)
                OR lower(COALESCE(c.logical_channel_number, '')) = lower(?)
              )
            ORDER BY b.start_utc ASC
            """,
            (start_utc, end_utc, channel_value, channel_value, channel_value),
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
                }
                for row in rows
            ],
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

        return {
            "stats": {
                "channelsUpserted": stats.channels_upserted,
                "programsUpserted": stats.programs_upserted,
                "schedulesUpserted": stats.schedules_upserted,
                "staleSchedulesPruned": stats.stale_schedules_pruned,
                "ingestRunId": stats.ingest_run_id,
                "fullRefresh": clear_existing,
            }
        }

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

        channel_name = payload.get("channelName", "BBC TWO HD")
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

        return {
            "stats": {
                "channelsUpserted": stats.channels_upserted,
                "programsUpserted": stats.programs_upserted,
                "broadcastsUpserted": stats.broadcasts_upserted,
                "parsedEvents": stats.parsed_events,
                "ingestRunId": stats.ingest_run_id,
            }
        }

    def _ensure_ota_control_ready(self) -> None:
        logger = self._context.logger
        dvbctrl = getattr(self._context, "dvbctrl", None)

        if dvbctrl is not None:
            try:
                dvbctrl.run_command("stats")
                return
            except Exception:
                pass

        manager = getattr(self._context, "dvbstreamer", None)
        if manager is None:
            return

        try:
            status = manager.health_check()
            state = getattr(status, "state", None)
            if state != DvbStreamerState.RUNNING:
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
        self._context.tvrecorder.run_raw("epgcapstart")

        result_queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def _read_stream() -> None:
            try:
                result_queue.put((True, self._context.tvrecorder.run_raw(grab_command)))
            except Exception as exc:
                result_queue.put((False, exc))

        stream_thread = Thread(target=_read_stream, daemon=True)
        stream_thread.start()

        try:
            deadline = time.monotonic() + capture_seconds
            while stream_thread.is_alive() and time.monotonic() < deadline:
                self._raise_if_stopping()
                time.sleep(0.25)
        finally:
            self._stop_ota_capture_with_separate_client()

        stream_thread.join(timeout=5.0)
        if stream_thread.is_alive():
            raise RuntimeError(
                "epgdata stream did not finish after epgcapstop; capture timed out"
            )
        if result_queue.empty():
            raise RuntimeError("epgdata capture did not produce any result")

        ok, payload = result_queue.get()
        if ok:
            return payload
        raise payload

    def _stop_ota_capture_with_separate_client(self) -> None:
        dvbctrl = getattr(self._context, "dvbctrl", None)
        if dvbctrl is None:
            self._context.tvrecorder.run_raw("epgcapstop")
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
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            self._raise_if_stopping()
            try:
                status = self._context.tvrecorder.frontend_status()
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
