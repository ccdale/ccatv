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


def test_initialize_database_creates_expected_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ccatv.sqlite3"

    tables = _table_names(db_path)

    assert "schema_migrations" in tables
    assert "recordings" in tables
    assert "scheduler_jobs" in tables


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
    assert applied_versions[0] == 1


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
    assert applied_versions[0] == 1


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
            WHEN NEW.version = 2
            BEGIN
                SELECT RAISE(ABORT, 'blocked');
            END;
            """
        )

        migration_two = storage_schema.Migration(
            version=2,
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
