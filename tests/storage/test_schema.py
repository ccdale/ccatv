from __future__ import annotations

from pathlib import Path

from ccatv.storage import apply_migrations, initialize_database


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
