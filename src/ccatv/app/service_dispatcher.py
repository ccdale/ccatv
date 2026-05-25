from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

from ccatv import __app_name__, __version__
from ccatv.app.bootstrap import AppContext
from ccatv.app.recorder_worker import create_scheduler_worker
from ccatv.metadata import SchedulesDirectHttpClient
from ccatv.metadata.schedules_direct_contract import (
    GuideSyncWindow,
    SchedulesDirectAuthenticationError,
    SchedulesDirectRateLimitError,
)
from ccatv.metadata.schedules_direct_ingest import (
    SchedulesDirectIngestionService,
    SqliteGuideRepository,
)
from ccatv.metadata.schedules_direct_runtime import (
    SchedulesDirectCredentialStore,
    SchedulesDirectTokenCacheStore,
)
from ccatv.storage import initialize_database

API_VERSION = "v1alpha1"

SERVICE_CAPABILITIES = [
    "service.health",
    "service.info",
    "recording.schedule",
    "recording.worker.cycle",
    "metadata.sd.sync",
]

SERVICE_COMMANDS = [
    "service.health.get",
    "service.info.get",
    "recording.schedule.create",
    "recording.schedule.list",
    "recording.worker.cycle.run",
    "metadata.sd.sync.run",
    "metadata.sd.sync.status.get",
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
        if command == "recording.schedule.create":
            return self._recording_schedule_create(payload)
        if command == "recording.schedule.list":
            return self._recording_schedule_list(payload)
        if command == "recording.worker.cycle.run":
            return self._recording_worker_cycle_run(payload)
        if command == "metadata.sd.sync.run":
            return self._metadata_sd_sync_run(payload)
        if command == "metadata.sd.sync.status.get":
            return self._metadata_sd_sync_status_get(payload)
        raise RuntimeError(f"unreachable dispatch branch for command: {command}")

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

        try:
            job = self._context.tvrecorder.schedule_recording(
                channel_name=channel_name.strip(),
                start_at_utc=start_at_utc.strip(),
                duration_seconds=duration_seconds,
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
                }
                for job in jobs
            ]
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

        self._raise_if_stopping()

        try:
            stats = self._run_coroutine_blocking(
                self._run_sd_sync(
                    lineup_id=lineup_id.strip(),
                    seed=seed,
                    window_hours=float(window_hours),
                    credentials_path=credentials_path,
                    database_path=database_path,
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
            }
        }

    def _metadata_sd_sync_status_get(self, payload: dict[str, object]) -> dict[str, object]:
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
