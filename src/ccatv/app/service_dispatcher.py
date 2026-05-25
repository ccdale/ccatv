from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

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
        if command == "service.health.get":
            return self._service_health_get()
        if command == "recording.worker.cycle.run":
            return self._recording_worker_cycle_run(payload)
        if command == "metadata.sd.sync.run":
            return self._metadata_sd_sync_run(payload)
        raise ServiceCommandError(
            code="UNSUPPORTED_COMMAND",
            message=f"unsupported command: {command}",
        )

    def _service_health_get(self) -> dict[str, object]:
        db_path = self._context.settings.database_path
        db_reachable = True
        try:
            self._context.persistence.connection.execute("SELECT 1")
        except Exception:
            db_reachable = False

        return {
            "status": "ok" if db_reachable else "degraded",
            "timeUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "database": {
                "path": db_path,
                "reachable": db_reachable,
            },
            "recorder": {
                "workerEnabled": True,
            },
        }

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

    def _run_coroutine_blocking(self, coroutine, *, timeout_seconds: float):
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
        thread.join()
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
