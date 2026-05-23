from __future__ import annotations

from pathlib import Path

import json

from ccatv.metadata.ota_epg import (
    ingest_dvbstreamer_epg,
    parse_dvbstreamer_epg,
)
from ccatv.storage import initialize_database


def _fixture_text() -> str:
    path = (
        Path(__file__).resolve().parent.parent
        / "integration"
        / "fixtures"
        / "epgdata_sample.txt"
    )
    return path.read_text(encoding="utf-8")


def test_parse_fixture_extracts_events() -> None:
    events = parse_dvbstreamer_epg(_fixture_text())

    assert len(events) > 50
    first = events[0]
    assert first.channel_source_id
    assert first.event_source_id
    assert first.start_utc.endswith("Z")
    assert first.title


def test_parse_fixture_keeps_truncated_descriptions() -> None:
    events = parse_dvbstreamer_epg(_fixture_text())

    truncated = [
        event
        for event in events
        if event.description is not None and "Bringing C" in event.description
    ]
    assert truncated


def test_ingest_fixture_populates_v2_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        stats = ingest_dvbstreamer_epg(connection, _fixture_text())

        channel_count = connection.execute(
            "SELECT COUNT(*) FROM epg_channels"
        ).fetchone()
        program_count = connection.execute(
            "SELECT COUNT(*) FROM epg_programs"
        ).fetchone()
        broadcast_count = connection.execute(
            "SELECT COUNT(*) FROM epg_broadcasts"
        ).fetchone()

        assert stats.parsed_events > 50
        assert channel_count is not None
        assert program_count is not None
        assert broadcast_count is not None
        assert channel_count[0] == stats.channels_upserted
        assert program_count[0] == stats.programs_upserted
        assert broadcast_count[0] == stats.broadcasts_upserted
    finally:
        connection.close()


def test_ingest_is_idempotent_for_same_source(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        first = ingest_dvbstreamer_epg(connection, _fixture_text())
        second = ingest_dvbstreamer_epg(connection, _fixture_text())

        channel_count = connection.execute(
            "SELECT COUNT(*) FROM epg_channels"
        ).fetchone()
        program_count = connection.execute(
            "SELECT COUNT(*) FROM epg_programs"
        ).fetchone()
        broadcast_count = connection.execute(
            "SELECT COUNT(*) FROM epg_broadcasts"
        ).fetchone()

        assert first.parsed_events == second.parsed_events
        assert channel_count is not None
        assert program_count is not None
        assert broadcast_count is not None
        assert channel_count[0] == first.channels_upserted
        assert program_count[0] == first.programs_upserted
        assert broadcast_count[0] == first.broadcasts_upserted
        assert first.channels_inserted == first.channels_upserted
        assert first.programs_inserted == first.programs_upserted
        assert second.channels_inserted == 0
        assert second.programs_inserted == 0
        assert second.broadcasts_inserted == 0
        assert second.broadcasts_updated == second.broadcasts_upserted
        assert first.ingest_run_id is not None
        assert second.ingest_run_id is not None
        assert second.ingest_run_id > first.ingest_run_id
    finally:
        connection.close()


def test_ingest_records_run_and_checkpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        stats = ingest_dvbstreamer_epg(connection, _fixture_text())

        run_row = connection.execute(
            """
            SELECT source, started_at_utc, finished_at_utc, status, stats_json
            FROM epg_ingest_runs
            WHERE id = ?
            """,
            (stats.ingest_run_id,),
        ).fetchone()
        checkpoint_row = connection.execute(
            """
            SELECT source, last_successful_ingest_utc, metadata_json
            FROM epg_source_checkpoints
            WHERE source = ?
            """,
            ("dvbstreamer_ota",),
        ).fetchone()

        assert run_row is not None
        assert run_row[0] == "dvbstreamer_ota"
        assert run_row[1].endswith("Z")
        assert run_row[2].endswith("Z")
        assert run_row[3] == "ok"
        assert run_row[4] is not None

        run_stats = json.loads(run_row[4])
        assert run_stats["parsed_events"] == stats.parsed_events
        assert run_stats["broadcasts_upserted"] == stats.broadcasts_upserted

        assert checkpoint_row is not None
        assert checkpoint_row[0] == "dvbstreamer_ota"
        assert checkpoint_row[1].endswith("Z")
        assert checkpoint_row[2] is not None

        checkpoint_metadata = json.loads(checkpoint_row[2])
        assert checkpoint_metadata["last_run_id"] == stats.ingest_run_id
    finally:
        connection.close()
