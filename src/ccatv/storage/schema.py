from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY,
                channel_name TEXT NOT NULL,
                output_path TEXT NOT NULL,
                state TEXT NOT NULL,
                started_at_utc TEXT,
                ended_at_utc TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS scheduler_jobs (
                id INTEGER PRIMARY KEY,
                channel_name TEXT NOT NULL,
                start_at_utc TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                state TEXT NOT NULL
            )
            """,
        ),
    ),
)


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def apply_migrations(connection: sqlite3.Connection) -> int:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    rows = connection.execute("SELECT version FROM schema_migrations")
    applied_versions = {row[0] for row in rows.fetchall()}
    applied_count = 0

    for migration in MIGRATIONS:
        if migration.version in applied_versions:
            continue
        try:
            connection.execute("BEGIN")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES(?, ?)",
                (migration.version, migration.name),
            )
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        applied_count += 1

    return applied_count


def initialize_database(path: Path) -> sqlite3.Connection:
    """Initialize schema and return an open connection.

    Caller is responsible for closing the returned connection.
    """
    connection = open_database(path)
    try:
        apply_migrations(connection)
    except Exception:
        connection.close()
        raise
    return connection
