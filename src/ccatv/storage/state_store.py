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
    program_content_ref: str | None = None
    program_series_ref: str | None = None


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
    program_content_ref: str | None = None
    program_series_ref: str | None = None


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
        program_content_ref: str | None = None,
        program_series_ref: str | None = None,
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
                    program_stop_at_utc,
                    program_content_ref,
                    program_series_ref
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    program_content_ref,
                    program_series_ref,
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
                    program_stop_at_utc,
                    program_content_ref,
                    program_series_ref
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
            program_content_ref=row[10],
            program_series_ref=row[11],
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
                    program_stop_at_utc,
                    program_content_ref,
                    program_series_ref
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
                program_content_ref=row[10],
                program_series_ref=row[11],
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
        program_content_ref: str | None = None,
        program_series_ref: str | None = None,
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
                    program_stop_at_utc,
                    program_content_ref,
                    program_series_ref
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    program_content_ref,
                    program_series_ref,
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
                    program_stop_at_utc,
                    program_content_ref,
                    program_series_ref
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
            program_content_ref=row[9],
            program_series_ref=row[10],
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
                    program_stop_at_utc,
                    program_content_ref,
                    program_series_ref
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
                program_content_ref=row[9],
                program_series_ref=row[10],
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
                WHERE lower(trim(display_name)) = lower(trim(?))
                """,
                (display_name,),
            ).fetchall()
        for row in rows:
            if bool(row[0]):
                return True
        return False

    def set_favorite_channel(self, display_name: str, favorite: bool) -> int:
        """Set favorite flag for all rows matching display_name case-insensitively."""
        with self._lock:
            result = self.connection.execute(
                """
                UPDATE epg_channels
                SET favorite_channel = ?
                WHERE lower(trim(display_name)) = lower(trim(?))
                """,
                (1 if favorite else 0, display_name),
            )
            self.connection.commit()
            return result.rowcount

    def list_channel_lineup_overrides(
        self,
    ) -> dict[str, dict[str, str | None]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT
                    epg_channel_name,
                    broadcaster_name,
                    schedules_direct_name,
                    guide_display_name,
                    guide_logical_channel_number
                FROM channel_lineup_overrides
                """
            ).fetchall()
        overrides: dict[str, dict[str, str | None]] = {}
        for row in rows:
            epg_name = str(row[0]).strip()
            if not epg_name:
                continue
            overrides[epg_name.casefold()] = {
                "epgChannelName": epg_name,
                "broadcasterName": str(row[1]).strip() if row[1] is not None else None,
                "schedulesDirectName": (
                    str(row[2]).strip() if row[2] is not None else None
                ),
                "guideName": str(row[3]).strip() if row[3] is not None else None,
                "guideLogicalChannelNumber": (
                    str(row[4]).strip() if row[4] is not None else None
                ),
            }
        return overrides

    def set_channel_lineup_override(
        self,
        *,
        epg_channel_name: str,
        broadcaster_name: str | None,
        schedules_direct_name: str | None,
        guide_name: str | None,
        guide_logical_channel_number: str | None,
    ) -> dict[str, object]:
        normalized_epg_name = epg_channel_name.strip()
        normalized_broadcaster = (
            broadcaster_name.strip() if isinstance(broadcaster_name, str) else None
        )
        normalized_sd = (
            schedules_direct_name.strip()
            if isinstance(schedules_direct_name, str)
            else None
        )
        normalized_guide_name = guide_name.strip() if isinstance(guide_name, str) else None
        normalized_guide_lcn = (
            guide_logical_channel_number.strip()
            if isinstance(guide_logical_channel_number, str)
            else None
        )

        normalized_broadcaster = normalized_broadcaster or None
        normalized_sd = normalized_sd or None
        normalized_guide_name = normalized_guide_name or None
        normalized_guide_lcn = normalized_guide_lcn or None

        with self._lock:
            if (
                normalized_broadcaster is None
                and normalized_sd is None
                and normalized_guide_name is None
                and normalized_guide_lcn is None
            ):
                deleted = self.connection.execute(
                    """
                    DELETE FROM channel_lineup_overrides
                    WHERE epg_channel_name = ?
                    """,
                    (normalized_epg_name,),
                ).rowcount
                self.connection.commit()
                return {
                    "action": "cleared",
                    "updatedRows": int(deleted),
                }

            self.connection.execute(
                """
                INSERT INTO channel_lineup_overrides(
                    epg_channel_name,
                    broadcaster_name,
                    schedules_direct_name,
                    guide_display_name,
                    guide_logical_channel_number,
                    updated_at_utc
                )
                VALUES(?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(epg_channel_name)
                DO UPDATE SET
                    broadcaster_name = excluded.broadcaster_name,
                    schedules_direct_name = excluded.schedules_direct_name,
                    guide_display_name = excluded.guide_display_name,
                    guide_logical_channel_number = excluded.guide_logical_channel_number,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    normalized_epg_name,
                    normalized_broadcaster,
                    normalized_sd,
                    normalized_guide_name,
                    normalized_guide_lcn,
                ),
            )
            self.connection.commit()
            return {
                "action": "saved",
                "updatedRows": 1,
            }

    def list_series_recording_subscriptions(self) -> list[str]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT series_ref
                FROM series_recording_subscriptions
                WHERE enabled = 1
                ORDER BY series_ref COLLATE NOCASE
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def set_series_recording_subscription(self, series_ref: str, enabled: bool) -> None:
        normalized = series_ref.strip()
        if not normalized:
            raise ValueError("series_ref must be a non-empty string")
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO series_recording_subscriptions(
                    series_ref,
                    enabled,
                    created_at_utc,
                    updated_at_utc
                )
                VALUES(?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(series_ref)
                DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (normalized, 1 if enabled else 0),
            )
            self.connection.commit()

    def has_recorded_content_ref(self, content_ref: str) -> bool:
        normalized = content_ref.strip()
        if not normalized:
            return False
        with self._lock:
            row = self.connection.execute(
                """
                SELECT 1
                FROM recorded_content_refs
                WHERE content_ref = ?
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return row is not None

    def mark_recorded_content_ref(
        self,
        *,
        content_ref: str,
        series_ref: str | None,
        title: str | None,
        recording_id: int | None,
    ) -> None:
        normalized = content_ref.strip()
        if not normalized:
            return
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO recorded_content_refs(
                    content_ref,
                    series_ref,
                    title,
                    recording_id,
                    recorded_at_utc
                )
                VALUES(?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(content_ref)
                DO UPDATE SET
                    series_ref = COALESCE(excluded.series_ref, recorded_content_refs.series_ref),
                    title = COALESCE(excluded.title, recorded_content_refs.title),
                    recording_id = COALESCE(excluded.recording_id, recorded_content_refs.recording_id),
                    recorded_at_utc = excluded.recorded_at_utc
                """,
                (
                    normalized,
                    series_ref.strip() if isinstance(series_ref, str) and series_ref.strip() else None,
                    title.strip() if isinstance(title, str) and title.strip() else None,
                    recording_id,
                ),
            )
            self.connection.commit()
