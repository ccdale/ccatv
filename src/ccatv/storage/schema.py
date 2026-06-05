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
    Migration(
        version=2,
        name="epg_schema_v2",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS epg_channels (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                source_channel_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                callsign TEXT,
                logical_channel_number TEXT,
                icon_url TEXT,
                metadata_json TEXT,
                UNIQUE(source, source_channel_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS epg_programs (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                source_program_id TEXT,
                title TEXT NOT NULL,
                subtitle TEXT,
                description_short TEXT,
                description_long TEXT,
                original_air_date TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                episode_id_onscreen TEXT,
                genre_primary TEXT,
                metadata_json TEXT
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_epg_programs_source_program_id
            ON epg_programs(source, source_program_id)
            WHERE source_program_id IS NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS epg_broadcasts (
                id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                program_id INTEGER NOT NULL,
                start_utc TEXT NOT NULL,
                stop_utc TEXT,
                duration_seconds INTEGER,
                is_new INTEGER,
                is_repeat INTEGER,
                quality_flags_json TEXT,
                source_schedule_hash TEXT,
                metadata_json TEXT,
                FOREIGN KEY(channel_id) REFERENCES epg_channels(id),
                FOREIGN KEY(program_id) REFERENCES epg_programs(id),
                UNIQUE(channel_id, start_utc)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_epg_broadcasts_start_utc
            ON epg_broadcasts(start_utc)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_epg_broadcasts_channel_start
            ON epg_broadcasts(channel_id, start_utc)
            """,
        ),
    ),
    Migration(
        version=3,
        name="epg_ingest_tracking_v3",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS epg_ingest_runs (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                finished_at_utc TEXT,
                status TEXT NOT NULL,
                message TEXT,
                stats_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS epg_source_checkpoints (
                source TEXT PRIMARY KEY,
                last_successful_ingest_utc TEXT,
                last_source_version TEXT,
                metadata_json TEXT
            )
            """,
        ),
    ),
    Migration(
        version=4,
        name="epg_channel_dvbstreamer_name_v4",
        statements=(
            """
            ALTER TABLE epg_channels
            ADD COLUMN dvbstreamer_service_name TEXT
            """,
        ),
    ),
    Migration(
        version=5,
        name="epg_channel_favorite_flag_v5",
        statements=(
            """
            ALTER TABLE epg_channels
            ADD COLUMN favorite_channel INTEGER NOT NULL DEFAULT 0
            """,
        ),
    ),
    Migration(
        version=6,
        name="recording_program_snapshot_v6",
        statements=(
            """
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_title TEXT
            """,
            """
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_description TEXT
            """,
            """
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_start_at_utc TEXT
            """,
            """
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_stop_at_utc TEXT
            """,
            """
            ALTER TABLE recordings
            ADD COLUMN program_title TEXT
            """,
            """
            ALTER TABLE recordings
            ADD COLUMN program_description TEXT
            """,
            """
            ALTER TABLE recordings
            ADD COLUMN program_start_at_utc TEXT
            """,
            """
            ALTER TABLE recordings
            ADD COLUMN program_stop_at_utc TEXT
            """,
        ),
    ),
)


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    # WAL mode allows concurrent reads from recording threads while the main
    # thread writes scheduler state; writes are still serialised by SQLite.
    connection.execute("PRAGMA journal_mode = WAL")
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
    connection.commit()

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
