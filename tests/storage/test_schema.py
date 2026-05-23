from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ccatv.storage import apply_migrations, initialize_database
from ccatv.storage import schema as storage_schema


def _table_names(path: Path) -> set[str]:
    connection = initialize_database(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        connection.close()


def _index_names(path: Path) -> set[str]:
    connection = initialize_database(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        connection.close()


def test_initialize_database_creates_expected_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"

    tables = _table_names(db_path)

    assert "schema_migrations" in tables
    assert "recordings" in tables
    assert "scheduler_jobs" in tables
    assert "epg_channels" in tables
    assert "epg_programs" in tables
    assert "epg_broadcasts" in tables


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        applied_count = apply_migrations(connection)
        applied_versions = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()
    finally:
        connection.close()

    assert applied_count == 0
    assert applied_versions is not None
    assert applied_versions[0] == 2


def test_initialize_database_is_idempotent_for_same_path(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"

    first = initialize_database(db_path)
    first.close()

    second = initialize_database(db_path)
    try:
        applied_versions = second.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()
    finally:
        second.close()

    assert applied_versions is not None
    assert applied_versions[0] == 2


def test_initialize_database_closes_connection_on_migration_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    opened_connection: sqlite3.Connection | None = None

    def _open_database(path: Path) -> sqlite3.Connection:
        nonlocal opened_connection
        opened_connection = sqlite3.connect(path)
        return opened_connection

    def _raise_migration_error(connection: sqlite3.Connection) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(storage_schema, "open_database", _open_database)
    monkeypatch.setattr(storage_schema, "apply_migrations", _raise_migration_error)

    with pytest.raises(RuntimeError, match="boom"):
        storage_schema.initialize_database(db_path)

    assert opened_connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened_connection.execute("SELECT 1")


def test_apply_migrations_rolls_back_on_insert_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = storage_schema.open_database(db_path)
    try:
        apply_migrations(connection)
        connection.execute(
            """
            CREATE TRIGGER block_version_two
            BEFORE INSERT ON schema_migrations
            WHEN NEW.version = 999
            BEGIN
                SELECT RAISE(ABORT, 'blocked');
            END;
            """
        )

        migration_two = storage_schema.Migration(
            version=999,
            name="atomicity_probe",
            statements=("CREATE TABLE atomic_probe(id INTEGER PRIMARY KEY)",),
        )
        monkeypatch.setattr(
            storage_schema,
            "MIGRATIONS",
            storage_schema.MIGRATIONS + (migration_two,),
        )

        with pytest.raises(sqlite3.DatabaseError, match="blocked"):
            apply_migrations(connection)

        probe_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='atomic_probe'"
        ).fetchall()
        assert probe_rows == []
    finally:
        connection.close()


def test_epg_channel_unique_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO epg_channels(source, source_channel_id, display_name)
            VALUES(?, ?, ?)
            """,
            ("xmltv", "chan-1", "BBC TWO HD"),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO epg_channels(source, source_channel_id, display_name)
                VALUES(?, ?, ?)
                """,
                ("xmltv", "chan-1", "Duplicate"),
            )
    finally:
        connection.close()


def test_epg_broadcast_unique_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO epg_channels(source, source_channel_id, display_name)
            VALUES(?, ?, ?)
            """,
            ("xmltv", "chan-1", "BBC TWO HD"),
        )
        connection.execute(
            """
            INSERT INTO epg_programs(source, source_program_id, title)
            VALUES(?, ?, ?)
            """,
            ("xmltv", "prog-1", "News"),
        )

        channel_id = connection.execute(
            "SELECT id FROM epg_channels WHERE source_channel_id = ?",
            ("chan-1",),
        ).fetchone()
        program_id = connection.execute(
            "SELECT id FROM epg_programs WHERE source_program_id = ?",
            ("prog-1",),
        ).fetchone()
        assert channel_id is not None
        assert program_id is not None

        connection.execute(
            """
            INSERT INTO epg_broadcasts(channel_id, program_id, start_utc)
            VALUES(?, ?, ?)
            """,
            (channel_id[0], program_id[0], "2026-05-23T10:00:00Z"),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO epg_broadcasts(channel_id, program_id, start_utc)
                VALUES(?, ?, ?)
                """,
                (channel_id[0], program_id[0], "2026-05-23T10:00:00Z"),
            )
    finally:
        connection.close()


def test_epg_broadcast_foreign_keys_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO epg_broadcasts(channel_id, program_id, start_utc)
                VALUES(?, ?, ?)
                """,
                (999, 999, "2026-05-23T10:00:00Z"),
            )
    finally:
        connection.close()


def test_epg_programs_partial_index_allows_multiple_null_source_ids(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ccatv.sqlite3"
    connection = initialize_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO epg_programs(source, source_program_id, title)
            VALUES(?, ?, ?)
            """,
            ("xmltv", None, "Program A"),
        )
        connection.execute(
            """
            INSERT INTO epg_programs(source, source_program_id, title)
            VALUES(?, ?, ?)
            """,
            ("xmltv", None, "Program B"),
        )

        rows = connection.execute(
            "SELECT COUNT(*) FROM epg_programs WHERE source = ? AND source_program_id IS NULL",
            ("xmltv",),
        ).fetchone()
        assert rows is not None
        assert rows[0] == 2
    finally:
        connection.close()


def test_epg_schema_indexes_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"

    indexes = _index_names(db_path)

    assert "idx_epg_programs_source_program_id" in indexes
    assert "idx_epg_broadcasts_start_utc" in indexes
    assert "idx_epg_broadcasts_channel_start" in indexes
