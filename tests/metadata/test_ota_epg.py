from __future__ import annotations

from pathlib import Path

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

        channel_count = connection.execute("SELECT COUNT(*) FROM epg_channels").fetchone()
        program_count = connection.execute("SELECT COUNT(*) FROM epg_programs").fetchone()
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

        channel_count = connection.execute("SELECT COUNT(*) FROM epg_channels").fetchone()
        program_count = connection.execute("SELECT COUNT(*) FROM epg_programs").fetchone()
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
    finally:
        connection.close()
