from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from ccatv.metadata.schedules_direct_contract import (
    GuideDataSource,
    GuideSyncWindow,
    SDProgram,
    SDScheduleEntry,
    SDStation,
)
from ccatv.metadata.schedules_direct_ingest import (
    SchedulesDirectIngestionService,
    SqliteGuideRepository,
)
from ccatv.storage import initialize_database


@dataclass(slots=True)
class StubSchedulesDirectClient:
    stations: list[SDStation]
    schedules: list[SDScheduleEntry]
    programs: list[SDProgram]
    fail_on_stations: bool = False

    async def authenticate(self, credentials) -> None:
        del credentials

    async def get_account_status(self):
        return None

    async def list_lineups(self, country: str, postal_code: str):
        del country, postal_code
        return []

    async def get_lineup_stations(self, lineup_id: str) -> list[SDStation]:
        del lineup_id
        if self.fail_on_stations:
            raise RuntimeError("station lookup failed")
        return self.stations

    async def get_schedules(
        self,
        lineup_id: str,
        window: GuideSyncWindow,
    ) -> list[SDScheduleEntry]:
        del lineup_id, window
        return self.schedules

    async def get_programs(self, program_ids: list[str]) -> list[SDProgram]:
        del program_ids
        return self.programs

    async def close(self) -> None:
        return None


def test_sync_incremental_writes_epg_rows(tmp_path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    repository = SqliteGuideRepository(connection=connection)

    start_utc = datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc)
    client = StubSchedulesDirectClient(
        stations=[
            SDStation(
                station_id="101",
                callsign="BBC1",
                name="BBC One",
                channel="1",
            )
        ],
        schedules=[
            SDScheduleEntry(
                station_id="101",
                program_id="EP0001",
                start_utc=start_utc,
                end_utc=start_utc + timedelta(minutes=30),
                duration_seconds=1800,
                is_new=True,
            )
        ],
        programs=[
            SDProgram(
                program_id="EP0001",
                title="Morning News",
                genres=("News",),
            )
        ],
    )
    service = SchedulesDirectIngestionService(client=client, repository=repository)

    stats = asyncio.run(
        service.sync_incremental_with_stats(
            lineup_id="UK-TEST",
            window=GuideSyncWindow(
                start_utc=datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc),
                end_utc=datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
            ),
        )
    )

    channel_count = connection.execute("SELECT COUNT(*) FROM epg_channels").fetchone()
    program_count = connection.execute("SELECT COUNT(*) FROM epg_programs").fetchone()
    broadcast_count = connection.execute(
        "SELECT COUNT(*) FROM epg_broadcasts"
    ).fetchone()
    run_row = connection.execute(
        """
        SELECT status, stats_json
        FROM epg_ingest_runs
        WHERE id = ?
        """,
        (stats.ingest_run_id,),
    ).fetchone()

    assert channel_count is not None
    assert program_count is not None
    assert broadcast_count is not None
    assert channel_count[0] == 1
    assert program_count[0] == 1
    assert broadcast_count[0] == 1
    assert stats.channels_upserted == 1
    assert stats.programs_upserted == 1
    assert stats.schedules_upserted == 1
    assert run_row is not None
    assert run_row[0] == "ok"
    assert run_row[1] is not None


def test_sync_incremental_prunes_stale_broadcasts_for_lineup(tmp_path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    repository = SqliteGuideRepository(connection=connection)

    connection.execute(
        """
        INSERT INTO epg_channels(source, source_channel_id, display_name)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "101", "BBC One"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "EP0001", "Morning News"),
    )
    channel_id_row = connection.execute(
        "SELECT id FROM epg_channels WHERE source = ? AND source_channel_id = ?",
        ("schedules_direct", "101"),
    ).fetchone()
    program_id_row = connection.execute(
        "SELECT id FROM epg_programs WHERE source = ? AND source_program_id = ?",
        ("schedules_direct", "EP0001"),
    ).fetchone()
    assert channel_id_row is not None
    assert program_id_row is not None

    connection.execute(
        """
        INSERT INTO epg_broadcasts(channel_id, program_id, start_utc, metadata_json)
        VALUES(?, ?, ?, ?)
        """,
        (
            int(channel_id_row[0]),
            int(program_id_row[0]),
            "2026-05-20T00:00:00Z",
            '{"lineup_id":"UK-TEST"}',
        ),
    )
    connection.commit()

    client = StubSchedulesDirectClient(stations=[], schedules=[], programs=[])
    service = SchedulesDirectIngestionService(client=client, repository=repository)

    stats = asyncio.run(
        service.sync_incremental_with_stats(
            lineup_id="UK-TEST",
            window=GuideSyncWindow(
                start_utc=datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc),
                end_utc=datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
            ),
        )
    )

    remaining = connection.execute("SELECT COUNT(*) FROM epg_broadcasts").fetchone()
    assert remaining is not None
    assert remaining[0] == 0
    assert stats.stale_schedules_pruned == 1


def test_sync_incremental_marks_ingest_failed_on_error(tmp_path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    repository = SqliteGuideRepository(connection=connection)
    client = StubSchedulesDirectClient(
        stations=[],
        schedules=[],
        programs=[],
        fail_on_stations=True,
    )
    service = SchedulesDirectIngestionService(client=client, repository=repository)

    with pytest.raises(RuntimeError, match="station lookup failed"):
        asyncio.run(
            service.sync_incremental_with_stats(
                lineup_id="UK-TEST",
                window=GuideSyncWindow(
                    start_utc=datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc),
                    end_utc=datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
                ),
            )
        )

    run_row = connection.execute(
        """
        SELECT status, message
        FROM epg_ingest_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert run_row is not None
    assert run_row[0] == "failed"
    assert "station lookup failed" in str(run_row[1])


def test_list_preferred_broadcasts_merges_sd_description_into_ota(tmp_path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    repository = SqliteGuideRepository(connection=connection)

    connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        ("dvbstreamer_ota", "1:2:3", "BBC One", "BBC1", "1"),
    )
    connection.execute(
        """
        INSERT INTO epg_channels(
            source,
            source_channel_id,
            display_name,
            callsign,
            logical_channel_number
        )
        VALUES(?, ?, ?, ?, ?)
        """,
        ("schedules_direct", "101", "BBC One", "BBC1", "1"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title, description_long)
        VALUES(?, ?, ?, ?)
        """,
        ("dvbstreamer_ota", "OTA-P1", "Evening News", None),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title, description_long)
        VALUES(?, ?, ?, ?)
        """,
        ("schedules_direct", "SD-P1", "Evening News", "Long SD synopsis"),
    )

    ota_channel_id = int(
        connection.execute(
            "SELECT id FROM epg_channels WHERE source = ? AND source_channel_id = ?",
            ("dvbstreamer_ota", "1:2:3"),
        ).fetchone()[0]
    )
    sd_channel_id = int(
        connection.execute(
            "SELECT id FROM epg_channels WHERE source = ? AND source_channel_id = ?",
            ("schedules_direct", "101"),
        ).fetchone()[0]
    )
    ota_program_id = int(
        connection.execute(
            "SELECT id FROM epg_programs WHERE source = ? AND source_program_id = ?",
            ("dvbstreamer_ota", "OTA-P1"),
        ).fetchone()[0]
    )
    sd_program_id = int(
        connection.execute(
            "SELECT id FROM epg_programs WHERE source = ? AND source_program_id = ?",
            ("schedules_direct", "SD-P1"),
        ).fetchone()[0]
    )

    connection.execute(
        """
        INSERT INTO epg_broadcasts(channel_id, program_id, start_utc, stop_utc)
        VALUES(?, ?, ?, ?)
        """,
        (
            ota_channel_id,
            ota_program_id,
            "2026-05-24T20:00:00Z",
            "2026-05-24T20:30:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO epg_broadcasts(channel_id, program_id, start_utc, stop_utc)
        VALUES(?, ?, ?, ?)
        """,
        (sd_channel_id, sd_program_id, "2026-05-24T20:00:00Z", "2026-05-24T20:30:00Z"),
    )
    connection.commit()

    results = repository.list_preferred_broadcasts(
        window_start_utc=datetime(2026, 5, 24, 19, 0, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 5, 24, 21, 0, tzinfo=timezone.utc),
    )

    assert len(results) == 1
    assert results[0].source == "dvbstreamer_ota"
    assert results[0].description == "Long SD synopsis"


def test_list_preferred_broadcasts_keeps_disagreeing_slots_separate(tmp_path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    repository = SqliteGuideRepository(connection=connection)

    connection.execute(
        """
        INSERT INTO epg_channels(source, source_channel_id, display_name)
        VALUES(?, ?, ?)
        """,
        ("dvbstreamer_ota", "1:2:3", "BBC One"),
    )
    connection.execute(
        """
        INSERT INTO epg_channels(source, source_channel_id, display_name)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "101", "BBC One"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title, description_long)
        VALUES(?, ?, ?, ?)
        """,
        ("dvbstreamer_ota", "OTA-P1", "Evening News", None),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title, description_long)
        VALUES(?, ?, ?, ?)
        """,
        ("schedules_direct", "SD-P1", "Evening Weather", "Long SD synopsis"),
    )

    ota_channel_id = int(
        connection.execute(
            "SELECT id FROM epg_channels WHERE source = ? AND source_channel_id = ?",
            ("dvbstreamer_ota", "1:2:3"),
        ).fetchone()[0]
    )
    sd_channel_id = int(
        connection.execute(
            "SELECT id FROM epg_channels WHERE source = ? AND source_channel_id = ?",
            ("schedules_direct", "101"),
        ).fetchone()[0]
    )
    ota_program_id = int(
        connection.execute(
            "SELECT id FROM epg_programs WHERE source = ? AND source_program_id = ?",
            ("dvbstreamer_ota", "OTA-P1"),
        ).fetchone()[0]
    )
    sd_program_id = int(
        connection.execute(
            "SELECT id FROM epg_programs WHERE source = ? AND source_program_id = ?",
            ("schedules_direct", "SD-P1"),
        ).fetchone()[0]
    )

    connection.execute(
        """
        INSERT INTO epg_broadcasts(channel_id, program_id, start_utc, stop_utc)
        VALUES(?, ?, ?, ?)
        """,
        (
            ota_channel_id,
            ota_program_id,
            "2026-05-24T20:00:00Z",
            "2026-05-24T20:30:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO epg_broadcasts(channel_id, program_id, start_utc, stop_utc)
        VALUES(?, ?, ?, ?)
        """,
        (sd_channel_id, sd_program_id, "2026-05-24T20:00:00Z", "2026-05-24T20:30:00Z"),
    )
    connection.commit()

    results = repository.list_preferred_broadcasts(
        window_start_utc=datetime(2026, 5, 24, 19, 0, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 5, 24, 21, 0, tzinfo=timezone.utc),
    )

    assert len(results) == 2
    assert sorted(result.title for result in results) == [
        "Evening News",
        "Evening Weather",
    ]


def test_upsert_schedules_replaces_only_overlapping_slots(tmp_path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    repository = SqliteGuideRepository(connection=connection)

    connection.execute(
        """
        INSERT INTO epg_channels(source, source_channel_id, display_name)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "101", "BBC One"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "OLD-1", "Old Show"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "OLD-2", "Later Show"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "NEW-1", "Updated Show"),
    )
    connection.commit()

    first_start = datetime(2026, 5, 24, 20, 0, tzinfo=timezone.utc)
    second_start = datetime(2026, 5, 24, 21, 0, tzinfo=timezone.utc)

    asyncio.run(
        repository.upsert_schedules(
            GuideDataSource.SCHEDULES_DIRECT,
            "UK-TEST",
            [
                SDScheduleEntry(
                    station_id="101",
                    program_id="OLD-1",
                    start_utc=first_start,
                    end_utc=first_start + timedelta(minutes=60),
                    duration_seconds=3600,
                ),
                SDScheduleEntry(
                    station_id="101",
                    program_id="OLD-2",
                    start_utc=second_start,
                    end_utc=second_start + timedelta(minutes=60),
                    duration_seconds=3600,
                ),
            ],
        )
    )

    asyncio.run(
        repository.upsert_schedules(
            GuideDataSource.SCHEDULES_DIRECT,
            "UK-TEST",
            [
                SDScheduleEntry(
                    station_id="101",
                    program_id="NEW-1",
                    start_utc=first_start + timedelta(minutes=5),
                    end_utc=first_start + timedelta(minutes=65),
                    duration_seconds=3600,
                )
            ],
        )
    )

    rows = connection.execute(
        """
        SELECT b.start_utc, b.stop_utc, p.source_program_id
        FROM epg_broadcasts AS b
        JOIN epg_programs AS p ON p.id = b.program_id
        ORDER BY b.start_utc
        """
    ).fetchall()

    assert rows == [
        ("2026-05-24T20:05:00Z", "2026-05-24T21:05:00Z", "NEW-1"),
    ]


def test_upsert_schedules_keeps_adjacent_slots(tmp_path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    repository = SqliteGuideRepository(connection=connection)

    connection.execute(
        """
        INSERT INTO epg_channels(source, source_channel_id, display_name)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "101", "BBC One"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "OLD", "Current Show"),
    )
    connection.execute(
        """
        INSERT INTO epg_programs(source, source_program_id, title)
        VALUES(?, ?, ?)
        """,
        ("schedules_direct", "NEXT", "Next Show"),
    )
    connection.commit()

    start_utc = datetime(2026, 5, 24, 20, 0, tzinfo=timezone.utc)

    asyncio.run(
        repository.upsert_schedules(
            GuideDataSource.SCHEDULES_DIRECT,
            "UK-TEST",
            [
                SDScheduleEntry(
                    station_id="101",
                    program_id="OLD",
                    start_utc=start_utc,
                    end_utc=start_utc + timedelta(minutes=60),
                    duration_seconds=3600,
                )
            ],
        )
    )

    asyncio.run(
        repository.upsert_schedules(
            GuideDataSource.SCHEDULES_DIRECT,
            "UK-TEST",
            [
                SDScheduleEntry(
                    station_id="101",
                    program_id="NEXT",
                    start_utc=start_utc + timedelta(minutes=60),
                    end_utc=start_utc + timedelta(minutes=120),
                    duration_seconds=3600,
                )
            ],
        )
    )

    rows = connection.execute(
        """
        SELECT b.start_utc, b.stop_utc, p.source_program_id
        FROM epg_broadcasts AS b
        JOIN epg_programs AS p ON p.id = b.program_id
        ORDER BY b.start_utc
        """
    ).fetchall()

    assert rows == [
        ("2026-05-24T20:00:00Z", "2026-05-24T21:00:00Z", "OLD"),
        ("2026-05-24T21:00:00Z", "2026-05-24T22:00:00Z", "NEXT"),
    ]
