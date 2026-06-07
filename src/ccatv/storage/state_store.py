from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field

_UNCHANGED = object()


@dataclass(frozen=True, slots=True)
class RecordingStateRecord:
    id: int
    channel_name: str
    output_path: str
    state: str
    started_at_utc: str | None
    ended_at_utc: str | None
    program_title: str | None
    program_description: str | None
    program_start_at_utc: str | None
    program_stop_at_utc: str | None


@dataclass(frozen=True, slots=True)
class SchedulerJobRecord:
    id: int
    channel_name: str
    start_at_utc: str
    duration_seconds: int
    state: str
    program_title: str | None
    program_description: str | None
    program_start_at_utc: str | None
    program_stop_at_utc: str | None


@dataclass(slots=True)
class PersistenceStore:
    connection: sqlite3.Connection
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def create_recording(
        self,
        *,
        channel_name: str,
        output_path: str,
        state: str,
        started_at_utc: str | None = None,
        ended_at_utc: str | None = None,
        program_title: str | None = None,
        program_description: str | None = None,
        program_start_at_utc: str | None = None,
        program_stop_at_utc: str | None = None,
    ) -> RecordingStateRecord:
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO recordings(
                    channel_name,
                    output_path,
                    state,
                    started_at_utc,
                    ended_at_utc,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_name,
                    output_path,
                    state,
                    started_at_utc,
                    ended_at_utc,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc,
                ),
            )
            self.connection.commit()
            return self.get_recording(int(cursor.lastrowid), required=True)

    def get_recording(
        self,
        recording_id: int,
        *,
        required: bool = False,
    ) -> RecordingStateRecord | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                    id,
                    channel_name,
                    output_path,
                    state,
                    started_at_utc,
                    ended_at_utc,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc
                FROM recordings
                WHERE id = ?
                """,
                (recording_id,),
            ).fetchone()
        if row is None:
            if required:
                raise ValueError(f"recording id not found: {recording_id}")
            return None
        return RecordingStateRecord(
            id=int(row[0]),
            channel_name=str(row[1]),
            output_path=str(row[2]),
            state=str(row[3]),
            started_at_utc=row[4],
            ended_at_utc=row[5],
            program_title=row[6],
            program_description=row[7],
            program_start_at_utc=row[8],
            program_stop_at_utc=row[9],
        )

    def list_recordings(self) -> list[RecordingStateRecord]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT
                    id,
                    channel_name,
                    output_path,
                    state,
                    started_at_utc,
                    ended_at_utc,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc
                FROM recordings
                ORDER BY id
                """
            ).fetchall()
        return [
            RecordingStateRecord(
                id=int(row[0]),
                channel_name=str(row[1]),
                output_path=str(row[2]),
                state=str(row[3]),
                started_at_utc=row[4],
                ended_at_utc=row[5],
                program_title=row[6],
                program_description=row[7],
                program_start_at_utc=row[8],
                program_stop_at_utc=row[9],
            )
            for row in rows
        ]

    def update_recording_state(
        self,
        recording_id: int,
        *,
        state: str,
        ended_at_utc: str | None | object = _UNCHANGED,
    ) -> RecordingStateRecord:
        with self._lock:
            if ended_at_utc is _UNCHANGED:
                result = self.connection.execute(
                    """
                    UPDATE recordings
                    SET state = ?
                    WHERE id = ?
                    """,
                    (state, recording_id),
                )
            else:
                result = self.connection.execute(
                    """
                    UPDATE recordings
                    SET state = ?, ended_at_utc = ?
                    WHERE id = ?
                    """,
                    (state, ended_at_utc, recording_id),
                )
            if result.rowcount == 0:
                raise ValueError(f"recording id not found: {recording_id}")
            self.connection.commit()
            return self.get_recording(recording_id, required=True)

    def update_recording_program_snapshot(
        self,
        recording_id: int,
        *,
        program_title: str | None,
        program_description: str | None,
        program_start_at_utc: str | None,
        program_stop_at_utc: str | None,
    ) -> RecordingStateRecord:
        with self._lock:
            result = self.connection.execute(
                """
                UPDATE recordings
                SET
                    program_title = ?,
                    program_description = ?,
                    program_start_at_utc = ?,
                    program_stop_at_utc = ?
                WHERE id = ?
                """,
                (
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc,
                    recording_id,
                ),
            )
            if result.rowcount == 0:
                raise ValueError(f"recording id not found: {recording_id}")
            self.connection.commit()
            return self.get_recording(recording_id, required=True)

    def delete_recording(self, recording_id: int) -> RecordingStateRecord:
        with self._lock:
            existing = self.get_recording(recording_id, required=True)
            result = self.connection.execute(
                """
                DELETE FROM recordings
                WHERE id = ?
                """,
                (recording_id,),
            )
            if result.rowcount == 0:
                raise ValueError(f"recording id not found: {recording_id}")
            self.connection.commit()
            return existing

    def create_scheduler_job(
        self,
        *,
        channel_name: str,
        start_at_utc: str,
        duration_seconds: int,
        state: str,
        program_title: str | None = None,
        program_description: str | None = None,
        program_start_at_utc: str | None = None,
        program_stop_at_utc: str | None = None,
    ) -> SchedulerJobRecord:
        with self._lock:
            cursor = self.connection.execute(
                """
                INSERT INTO scheduler_jobs(
                    channel_name,
                    start_at_utc,
                    duration_seconds,
                    state,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_name,
                    start_at_utc,
                    duration_seconds,
                    state,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc,
                ),
            )
            self.connection.commit()
            return self.get_scheduler_job(int(cursor.lastrowid), required=True)

    def get_scheduler_job(
        self,
        job_id: int,
        *,
        required: bool = False,
    ) -> SchedulerJobRecord | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                    id,
                    channel_name,
                    start_at_utc,
                    duration_seconds,
                    state,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc
                FROM scheduler_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            if required:
                raise ValueError(f"scheduler job id not found: {job_id}")
            return None
        return SchedulerJobRecord(
            id=int(row[0]),
            channel_name=str(row[1]),
            start_at_utc=str(row[2]),
            duration_seconds=int(row[3]),
            state=str(row[4]),
            program_title=row[5],
            program_description=row[6],
            program_start_at_utc=row[7],
            program_stop_at_utc=row[8],
        )

    def list_scheduler_jobs(self) -> list[SchedulerJobRecord]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT
                    id,
                    channel_name,
                    start_at_utc,
                    duration_seconds,
                    state,
                    program_title,
                    program_description,
                    program_start_at_utc,
                    program_stop_at_utc
                FROM scheduler_jobs
                ORDER BY id
                """
            ).fetchall()
        return [
            SchedulerJobRecord(
                id=int(row[0]),
                channel_name=str(row[1]),
                start_at_utc=str(row[2]),
                duration_seconds=int(row[3]),
                state=str(row[4]),
                program_title=row[5],
                program_description=row[6],
                program_start_at_utc=row[7],
                program_stop_at_utc=row[8],
            )
            for row in rows
        ]

    def update_scheduler_job_state(
        self, job_id: int, *, state: str
    ) -> SchedulerJobRecord:
        with self._lock:
            result = self.connection.execute(
                """
                UPDATE scheduler_jobs
                SET state = ?
                WHERE id = ?
                """,
                (state, job_id),
            )
            if result.rowcount == 0:
                raise ValueError(f"scheduler job id not found: {job_id}")
            self.connection.commit()
            return self.get_scheduler_job(job_id, required=True)

    def get_dvbstreamer_service_name(self, display_name: str) -> str | None:
        """Return the stored dvbstreamer service name for an EPG channel display name.

        Returns None if no mapping is set or the channel is not found.
        Multiple rows with the same display_name are tolerated; the first
        non-null, non-empty value wins.
        """
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT dvbstreamer_service_name
                FROM epg_channels
                WHERE display_name = ?
                """,
                (display_name,),
            ).fetchall()
        for row in rows:
            value = row[0]
            if value is not None:
                stripped = str(value).strip()
                if stripped:
                    return stripped
        return None

    def set_dvbstreamer_service_name(
        self, display_name: str, service_name: str | None
    ) -> int:
        """Set the dvbstreamer service name for all EPG channels with the given display name.

        Passing None clears the mapping.  Returns the number of rows updated.
        """
        normalised = service_name.strip() if service_name is not None else None
        with self._lock:
            result = self.connection.execute(
                """
                UPDATE epg_channels
                SET dvbstreamer_service_name = ?
                WHERE display_name = ?
                """,
                (normalised or None, display_name),
            )
            self.connection.commit()
            return result.rowcount

    def get_favorite_channel(self, display_name: str) -> bool:
        """Return whether an EPG channel display name is marked as a favorite."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT favorite_channel
                FROM epg_channels
                WHERE display_name = ?
                """,
                (display_name,),
            ).fetchall()
        for row in rows:
            return bool(row[0])
        return False

    def set_favorite_channel(self, display_name: str, favorite: bool) -> int:
        """Set favorite flag for all EPG channel rows matching display_name."""
        with self._lock:
            result = self.connection.execute(
                """
                UPDATE epg_channels
                SET favorite_channel = ?
                WHERE display_name = ?
                """,
                (1 if favorite else 0, display_name),
            )
            self.connection.commit()
            return result.rowcount
