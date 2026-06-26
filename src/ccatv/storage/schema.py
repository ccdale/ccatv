from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
import re


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
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_content_ref TEXT
            """,
            """
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_series_ref TEXT
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
            """
            ALTER TABLE recordings
            ADD COLUMN program_content_ref TEXT
            """,
            """
            ALTER TABLE recordings
            ADD COLUMN program_series_ref TEXT
            """,
        ),
    ),
    Migration(
        version=7,
        name="series_recording_subscriptions_v7",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS series_recording_subscriptions (
                series_ref TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS recorded_content_refs (
                content_ref TEXT PRIMARY KEY,
                series_ref TEXT,
                title TEXT,
                recording_id INTEGER,
                recorded_at_utc TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=8,
        name="recording_program_refs_backfill_v8",
        statements=(
            """
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_content_ref TEXT
            """,
            """
            ALTER TABLE scheduler_jobs
            ADD COLUMN program_series_ref TEXT
            """,
            """
            ALTER TABLE recordings
            ADD COLUMN program_content_ref TEXT
            """,
            """
            ALTER TABLE recordings
            ADD COLUMN program_series_ref TEXT
            """,
        ),
    ),
    Migration(
        version=9,
        name="channel_lineup_overrides_v9",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS channel_lineup_overrides (
                epg_channel_name TEXT PRIMARY KEY COLLATE NOCASE,
                broadcaster_name TEXT,
                schedules_direct_name TEXT,
                guide_display_name TEXT,
                guide_logical_channel_number TEXT,
                updated_at_utc TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version=10,
        name="channel_groups_v10",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS channel_groups (
                group_id INTEGER PRIMARY KEY,
                group_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                group_logical_channel_number TEXT,
                preferred_recording_source TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """,
            """
            ALTER TABLE epg_channels
            ADD COLUMN channel_group_id INTEGER
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_epg_channels_group_id
            ON epg_channels(channel_group_id)
            """,
        ),
    ),
    Migration(
        version=11,
        name="serviceinfo_cache_v11",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS serviceinfo_cache (
                service_name TEXT PRIMARY KEY,
                raw_output TEXT NOT NULL,
                has_media_pid INTEGER NOT NULL,
                is_radio INTEGER NOT NULL,
                fetched_at_utc TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_serviceinfo_cache_fetched_at_utc
            ON serviceinfo_cache(fetched_at_utc)
            """,
        ),
    ),
    Migration(
        version=12,
        name="epg_channel_radio_flag_v12",
        statements=(
            """
            ALTER TABLE epg_channels
            ADD COLUMN is_radio_channel INTEGER
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_epg_channels_is_radio_channel
            ON epg_channels(is_radio_channel)
            """,
        ),
    ),
    Migration(
        version=13,
        name="epg_channel_hd_flag_and_service_cache_hd_v13",
        statements=(
            """
            ALTER TABLE epg_channels
            ADD COLUMN is_hd_channel INTEGER
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_epg_channels_is_hd_channel
            ON epg_channels(is_hd_channel)
            """,
            """
            ALTER TABLE serviceinfo_cache
            ADD COLUMN is_hd_channel INTEGER
            """,
        ),
    ),
)


def normalise_channel_name(name: str) -> str:
    """Normalise channel name for variant detection.
    
    Removes spaces and converts to lowercase to enable matching variants
    like "BBC One" with "BBC One HD" or "BBC One regional".
    
    Example:
        normalise_channel_name("BBC One HD") == "bbconehd"
    """
    return name.replace(" ", "").lower()


def find_channel_variants(channel_name: str, all_names: list[str]) -> list[str]:
    """Find all channels that are variants of the given channel.
    
    Uses bidirectional startswith check so "BBC One" matches both
    "BBC One HD" and vice versa.
    """
    normalised = normalise_channel_name(channel_name)
    variants = []
    for name in all_names:
        norm_other = normalise_channel_name(name)
        if norm_other.startswith(normalised) or normalised.startswith(norm_other):
            variants.append(name)
    return variants


_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+(?P<column>\w+)\b",
    re.IGNORECASE,
)


def _column_exists(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(str(row[1]).lower() == column_name.lower() for row in rows)


def _should_skip_statement(connection: sqlite3.Connection, statement: str) -> bool:
    match = _ADD_COLUMN_RE.match(statement)
    if match is None:
        return False
    table_name = match.group("table")
    column_name = match.group("column")
    return _column_exists(connection, table_name, column_name)


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
                if _should_skip_statement(connection, statement):
                    continue
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
