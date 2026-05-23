from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecordingStateRecord:
    id: int
    channel_name: str
    output_path: str
    state: str
    started_at_utc: str | None
    ended_at_utc: str | None


@dataclass(frozen=True, slots=True)
class SchedulerJobRecord:
    id: int
    channel_name: str
    start_at_utc: str
    duration_seconds: int
    state: str


@dataclass(slots=True)
class PersistenceStore:
    connection: sqlite3.Connection

    def create_recording(
        self,
        *,
        channel_name: str,
        output_path: str,
        state: str,
        started_at_utc: str | None = None,
        ended_at_utc: str | None = None,
    ) -> RecordingStateRecord:
        cursor = self.connection.execute(
            """
            INSERT INTO recordings(
                channel_name,
                output_path,
                state,
                started_at_utc,
                ended_at_utc
            )
            VALUES(?, ?, ?, ?, ?)
            """,
            (channel_name, output_path, state, started_at_utc, ended_at_utc),
        )
        self.connection.commit()
        return self.get_recording(int(cursor.lastrowid), required=True)

    def get_recording(
        self,
        recording_id: int,
        *,
        required: bool = False,
    ) -> RecordingStateRecord | None:
        row = self.connection.execute(
            """
            SELECT id, channel_name, output_path, state, started_at_utc, ended_at_utc
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
        )

    def list_recordings(self) -> list[RecordingStateRecord]:
        rows = self.connection.execute(
            """
            SELECT id, channel_name, output_path, state, started_at_utc, ended_at_utc
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
            )
            for row in rows
        ]

    def update_recording_state(
        self,
        recording_id: int,
        *,
        state: str,
        ended_at_utc: str | None = None,
    ) -> RecordingStateRecord:
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

    def create_scheduler_job(
        self,
        *,
        channel_name: str,
        start_at_utc: str,
        duration_seconds: int,
        state: str,
    ) -> SchedulerJobRecord:
        cursor = self.connection.execute(
            """
            INSERT INTO scheduler_jobs(
                channel_name,
                start_at_utc,
                duration_seconds,
                state
            )
            VALUES(?, ?, ?, ?)
            """,
            (channel_name, start_at_utc, duration_seconds, state),
        )
        self.connection.commit()
        return self.get_scheduler_job(int(cursor.lastrowid), required=True)

    def get_scheduler_job(
        self,
        job_id: int,
        *,
        required: bool = False,
    ) -> SchedulerJobRecord | None:
        row = self.connection.execute(
            """
            SELECT id, channel_name, start_at_utc, duration_seconds, state
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
        )

    def list_scheduler_jobs(self) -> list[SchedulerJobRecord]:
        rows = self.connection.execute(
            """
            SELECT id, channel_name, start_at_utc, duration_seconds, state
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
            )
            for row in rows
        ]

    def update_scheduler_job_state(self, job_id: int, *, state: str) -> SchedulerJobRecord:
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
