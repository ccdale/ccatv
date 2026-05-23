from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ccatv.metadata import ota_epg
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

        checkpoint_row = connection.execute(
            "SELECT metadata_json FROM epg_source_checkpoints WHERE source = ?",
            ("dvbstreamer_ota",),
        ).fetchone()
        assert checkpoint_row is not None
        assert checkpoint_row[0] is not None
        checkpoint_metadata = json.loads(checkpoint_row[0])
        assert checkpoint_metadata["last_run_id"] == second.ingest_run_id
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


def test_ingest_marks_run_failed_on_parse_error(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)

    def _raise_parse_error(raw_text: str) -> list[ota_epg.OtaEpgEvent]:
        raise ValueError("broken epg")

    monkeypatch.setattr(ota_epg, "parse_dvbstreamer_epg", _raise_parse_error)

    try:
        with pytest.raises(ValueError, match="broken epg"):
            ingest_dvbstreamer_epg(connection, "<epg>")

        run_row = connection.execute(
            """
            SELECT status, message, finished_at_utc
            FROM epg_ingest_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert run_row is not None
        assert run_row[0] == "failed"
        assert run_row[1] == "broken epg"
        assert run_row[2].endswith("Z")
    finally:
        connection.close()


def test_ingest_marks_stale_running_run_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO epg_ingest_runs(source, started_at_utc, status)
            VALUES(?, ?, ?)
            """,
            ("dvbstreamer_ota", "2026-05-23T00:00:00Z", "running"),
        )
        connection.commit()

        ingest_dvbstreamer_epg(connection, _fixture_text())

        stale_row = connection.execute(
            """
            SELECT status, message, finished_at_utc
            FROM epg_ingest_runs
            WHERE source = ? AND started_at_utc = ?
            """,
            ("dvbstreamer_ota", "2026-05-23T00:00:00Z"),
        ).fetchone()
        assert stale_row is not None
        assert stale_row[0] == "failed"
        assert stale_row[1] == "stale running run superseded by a new ingest"
        assert stale_row[2].endswith("Z")
    finally:
        connection.close()


def test_checkpoint_failure_keeps_run_terminal(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)

    def _raise_checkpoint_error(
        _connection,
        _source: str,
        _finished_at_utc: str,
        _stats,
    ) -> None:
        raise RuntimeError("checkpoint write failed")

    monkeypatch.setattr(ota_epg, "_upsert_source_checkpoint", _raise_checkpoint_error)

    try:
        with pytest.raises(RuntimeError, match="checkpoint write failed"):
            ingest_dvbstreamer_epg(connection, _fixture_text())

        run_row = connection.execute(
            """
            SELECT status, stats_json, finished_at_utc
            FROM epg_ingest_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert run_row is not None
        assert run_row[0] == "ok"
        assert run_row[1] is not None
        assert run_row[2].endswith("Z")
    finally:
        connection.close()


def test_finalize_failure_logs_warning_and_preserves_original_error(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)

    def _raise_parse_error(raw_text: str) -> list[ota_epg.OtaEpgEvent]:
        raise ValueError("broken epg")

    def _raise_finalize_error(
        _connection,
        _run_id: int,
        *,
        finished_at_utc: str,
        status: str,
        message: str | None,
        stats,
    ) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ota_epg, "parse_dvbstreamer_epg", _raise_parse_error)
    monkeypatch.setattr(ota_epg, "_finish_ingest_run", _raise_finalize_error)

    try:
        with caplog.at_level("WARNING", logger="ccatv.metadata.ota_epg"):
            with pytest.raises(ValueError, match="broken epg"):
                ingest_dvbstreamer_epg(connection, "<epg>")

        assert "failed to finalize ingest run failure state" in caplog.text
        assert "database is locked" in caplog.text
    finally:
        connection.close()
