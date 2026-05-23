from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ccatv.metadata.schedules_direct_contract import (
    GuideDataSource,
    GuideIngestionService,
    GuideRepository,
    GuideSyncWindow,
    SchedulesDirectClient,
    SDGuideSnapshot,
    SDProgram,
    SDScheduleEntry,
    SDStation,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quality_flags_json(entry: SDScheduleEntry) -> str | None:
    payload = {
        "is_live": entry.is_live,
        "audio_properties": list(entry.audio_properties),
        "video_properties": list(entry.video_properties),
    }
    if (
        not payload["is_live"]
        and not payload["audio_properties"]
        and not payload["video_properties"]
    ):
        return None
    return json.dumps(payload, sort_keys=True)


def _schedule_hash(lineup_id: str, entry: SDScheduleEntry) -> str:
    identity = (
        f"{lineup_id}|{entry.station_id}|{entry.program_id}|"
        f"{_to_utc_iso(entry.start_utc)}|{entry.duration_seconds}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SDGuideIngestStats:
    channels_upserted: int
    programs_upserted: int
    schedules_upserted: int
    stale_schedules_pruned: int
    ingest_run_id: int


@dataclass(frozen=True, slots=True)
class SDGuideIngestResult:
    snapshot: SDGuideSnapshot
    stats: SDGuideIngestStats


@dataclass(slots=True)
class SqliteGuideRepository(GuideRepository):
    connection: sqlite3.Connection

    async def upsert_stations(
        self, source: GuideDataSource, stations: list[SDStation]
    ) -> int:
        unique = {station.station_id: station for station in stations}
        for station in unique.values():
            metadata = json.dumps({"channel": station.channel}, sort_keys=True)
            self.connection.execute(
                """
                INSERT INTO epg_channels(
                    source,
                    source_channel_id,
                    display_name,
                    callsign,
                    logical_channel_number,
                    metadata_json
                )
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_channel_id)
                DO UPDATE SET
                    display_name = excluded.display_name,
                    callsign = excluded.callsign,
                    logical_channel_number = excluded.logical_channel_number,
                    metadata_json = excluded.metadata_json
                """,
                (
                    source.value,
                    station.station_id,
                    station.name,
                    station.callsign,
                    station.channel,
                    metadata,
                ),
            )
        return len(unique)

    async def upsert_programs(
        self, source: GuideDataSource, programs: list[SDProgram]
    ) -> int:
        unique = {program.program_id: program for program in programs}
        for program in unique.values():
            metadata = json.dumps(
                {
                    "genres": list(program.genres),
                    "artwork_urls": list(program.artwork_urls),
                },
                sort_keys=True,
            )
            update_result = self.connection.execute(
                """
                UPDATE epg_programs
                SET title = ?,
                    subtitle = ?,
                    description_long = ?,
                    original_air_date = ?,
                    genre_primary = ?,
                    metadata_json = ?
                WHERE source = ? AND source_program_id = ?
                """,
                (
                    program.title,
                    program.episode_title,
                    program.description,
                    program.original_air_date.isoformat()
                    if program.original_air_date is not None
                    else None,
                    program.genres[0] if program.genres else None,
                    metadata,
                    source.value,
                    program.program_id,
                ),
            )
            if update_result.rowcount > 0:
                continue

            self.connection.execute(
                """
                INSERT INTO epg_programs(
                    source,
                    source_program_id,
                    title,
                    subtitle,
                    description_long,
                    original_air_date,
                    genre_primary,
                    metadata_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.value,
                    program.program_id,
                    program.title,
                    program.episode_title,
                    program.description,
                    program.original_air_date.isoformat()
                    if program.original_air_date is not None
                    else None,
                    program.genres[0] if program.genres else None,
                    metadata,
                ),
            )
        return len(unique)

    async def upsert_schedules(
        self,
        source: GuideDataSource,
        lineup_id: str,
        schedules: list[SDScheduleEntry],
    ) -> int:
        if not schedules:
            return 0

        station_ids = sorted({entry.station_id for entry in schedules})
        program_ids = sorted({entry.program_id for entry in schedules})

        channel_map = {
            str(row[0]): int(row[1])
            for row in self.connection.execute(
                f"""
                SELECT source_channel_id, id
                FROM epg_channels
                WHERE source = ? AND source_channel_id IN ({",".join("?" for _ in station_ids)})
                """,
                (source.value, *station_ids),
            ).fetchall()
        }
        program_map = {
            str(row[0]): int(row[1])
            for row in self.connection.execute(
                f"""
                SELECT source_program_id, id
                FROM epg_programs
                WHERE source = ? AND source_program_id IN ({",".join("?" for _ in program_ids)})
                """,
                (source.value, *program_ids),
            ).fetchall()
        }

        upserted = 0
        for entry in schedules:
            channel_id = channel_map.get(entry.station_id)
            program_id = program_map.get(entry.program_id)
            if channel_id is None or program_id is None:
                continue

            metadata_json = json.dumps({"lineup_id": lineup_id}, sort_keys=True)
            self.connection.execute(
                """
                INSERT INTO epg_broadcasts(
                    channel_id,
                    program_id,
                    start_utc,
                    stop_utc,
                    duration_seconds,
                    is_new,
                    is_repeat,
                    quality_flags_json,
                    source_schedule_hash,
                    metadata_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, start_utc)
                DO UPDATE SET
                    program_id = excluded.program_id,
                    stop_utc = excluded.stop_utc,
                    duration_seconds = excluded.duration_seconds,
                    is_new = excluded.is_new,
                    is_repeat = excluded.is_repeat,
                    quality_flags_json = excluded.quality_flags_json,
                    source_schedule_hash = excluded.source_schedule_hash,
                    metadata_json = excluded.metadata_json
                """,
                (
                    channel_id,
                    program_id,
                    _to_utc_iso(entry.start_utc),
                    _to_utc_iso(entry.end_utc),
                    entry.duration_seconds,
                    1 if entry.is_new else 0,
                    0 if entry.is_new else 1,
                    _quality_flags_json(entry),
                    _schedule_hash(lineup_id, entry),
                    metadata_json,
                ),
            )
            upserted += 1

        return upserted

    async def prune_expired_schedules(
        self,
        source: GuideDataSource,
        lineup_id: str,
        before_utc: datetime,
    ) -> int:
        result = self.connection.execute(
            """
            DELETE FROM epg_broadcasts
            WHERE start_utc < ?
              AND channel_id IN (
                  SELECT id
                  FROM epg_channels
                  WHERE source = ?
              )
              AND json_extract(metadata_json, '$.lineup_id') = ?
            """,
            (_to_utc_iso(before_utc), source.value, lineup_id),
        )
        return int(result.rowcount)


@dataclass(slots=True)
class SchedulesDirectIngestionService(GuideIngestionService):
    client: SchedulesDirectClient
    repository: GuideRepository
    source: GuideDataSource = GuideDataSource.SCHEDULES_DIRECT
    seed_window_hours: int = 72

    async def seed_lineup(self, lineup_id: str) -> SDGuideSnapshot:
        now = _utc_now()
        window = GuideSyncWindow(
            start_utc=now,
            end_utc=now + timedelta(hours=self.seed_window_hours),
        )
        result = await self._sync(lineup_id=lineup_id, window=window)
        return result.snapshot

    async def sync_incremental(
        self, lineup_id: str, window: GuideSyncWindow
    ) -> SDGuideSnapshot:
        result = await self._sync(lineup_id=lineup_id, window=window)
        return result.snapshot

    async def sync_incremental_with_stats(
        self, *, lineup_id: str, window: GuideSyncWindow
    ) -> SDGuideIngestStats:
        result = await self._sync(lineup_id=lineup_id, window=window)
        return result.stats

    async def _sync(
        self, *, lineup_id: str, window: GuideSyncWindow
    ) -> SDGuideIngestResult:
        if not isinstance(self.repository, SqliteGuideRepository):
            raise TypeError(
                "SchedulesDirectIngestionService currently requires SqliteGuideRepository"
            )

        connection = self.repository.connection
        started_at = _utc_now()
        with connection:
            connection.execute(
                """
                UPDATE epg_ingest_runs
                SET finished_at_utc = ?,
                    status = 'failed',
                    message = 'stale running run superseded by a new ingest'
                WHERE source = ? AND status = 'running'
                """,
                (_to_utc_iso(started_at), self.source.value),
            )
            cursor = connection.execute(
                """
                INSERT INTO epg_ingest_runs(source, started_at_utc, status)
                VALUES(?, ?, ?)
                """,
                (self.source.value, _to_utc_iso(started_at), "running"),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("failed to create ingest run")
            ingest_run_id = int(cursor.lastrowid)

        try:
            stations = await self.client.get_lineup_stations(lineup_id)
            schedules = await self.client.get_schedules(lineup_id, window)
            program_ids = sorted({entry.program_id for entry in schedules})
            programs = await self.client.get_programs(program_ids)

            with connection:
                channels_upserted = await self.repository.upsert_stations(
                    self.source, stations
                )
                programs_upserted = await self.repository.upsert_programs(
                    self.source, programs
                )
                schedules_upserted = await self.repository.upsert_schedules(
                    self.source,
                    lineup_id,
                    schedules,
                )
                stale_schedules_pruned = await self.repository.prune_expired_schedules(
                    self.source,
                    lineup_id,
                    before_utc=window.start_utc,
                )

                stats = SDGuideIngestStats(
                    channels_upserted=channels_upserted,
                    programs_upserted=programs_upserted,
                    schedules_upserted=schedules_upserted,
                    stale_schedules_pruned=stale_schedules_pruned,
                    ingest_run_id=ingest_run_id,
                )
                finished_at = _utc_now()
                connection.execute(
                    """
                    UPDATE epg_ingest_runs
                    SET finished_at_utc = ?,
                        status = ?,
                        stats_json = ?
                    WHERE id = ?
                    """,
                    (
                        _to_utc_iso(finished_at),
                        "ok",
                        json.dumps(
                            {
                                "lineup_id": lineup_id,
                                "channels_upserted": stats.channels_upserted,
                                "programs_upserted": stats.programs_upserted,
                                "schedules_upserted": stats.schedules_upserted,
                                "stale_schedules_pruned": stats.stale_schedules_pruned,
                            },
                            sort_keys=True,
                        ),
                        ingest_run_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO epg_source_checkpoints(
                        source,
                        last_successful_ingest_utc,
                        metadata_json
                    )
                    VALUES(?, ?, ?)
                    ON CONFLICT(source)
                    DO UPDATE SET
                        last_successful_ingest_utc = excluded.last_successful_ingest_utc,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        self.source.value,
                        _to_utc_iso(finished_at),
                        json.dumps(
                            {
                                "last_run_id": ingest_run_id,
                                "lineup_id": lineup_id,
                                "window_start_utc": _to_utc_iso(window.start_utc),
                                "window_end_utc": _to_utc_iso(window.end_utc),
                            },
                            sort_keys=True,
                        ),
                    ),
                )
            snapshot = SDGuideSnapshot(
                lineup_id=lineup_id,
                fetched_at_utc=finished_at,
                stations=tuple(stations),
                schedules=tuple(schedules),
                programs=tuple(programs),
            )
            return SDGuideIngestResult(snapshot=snapshot, stats=stats)
        except Exception as exc:
            with connection:
                connection.execute(
                    """
                    UPDATE epg_ingest_runs
                    SET finished_at_utc = ?,
                        status = ?,
                        message = ?
                    WHERE id = ?
                    """,
                    (_to_utc_iso(_utc_now()), "failed", str(exc), ingest_run_id),
                )
            raise


__all__ = [
    "SDGuideIngestResult",
    "SDGuideIngestStats",
    "SchedulesDirectIngestionService",
    "SqliteGuideRepository",
]
