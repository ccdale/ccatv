# EPG Migration Plan (v2-v4)

Status: proposed next migrations after initial v1 scaffold
Date: 2026-05-22

This plan converts the mapping notes into concrete sqlite migration steps.

References:
- [EPG Mapping Notes](epg-mapping-notes.md)
- [Storage Schema](../src/ccatv/storage/schema.py)

## Goals

1. Add normalized EPG persistence for channels, programs, and broadcast instances.
2. Keep migration steps small and reversible in logic (sqlite-compatible DDL strategy).
3. Preserve existing recording/scheduler scaffold while introducing EPG tables.

## Current Baseline

Migration v1 exists and creates:
- recordings
- scheduler_jobs
- schema_migrations

No EPG metadata tables exist yet.

## Proposed Migration Sequence

## Migration v2: Channels + Programs + Broadcasts

### New tables

1. `epg_channels`
- `id INTEGER PRIMARY KEY`
- `source TEXT NOT NULL`
- `source_channel_id TEXT NOT NULL`
- `display_name TEXT NOT NULL`
- `callsign TEXT`
- `logical_channel_number TEXT`
- `icon_url TEXT`
- `metadata_json TEXT`
- unique: `(source, source_channel_id)`

2. `epg_programs`
- `id INTEGER PRIMARY KEY`
- `source TEXT NOT NULL`
- `source_program_id TEXT`
- `title TEXT NOT NULL`
- `subtitle TEXT`
- `description_short TEXT`
- `description_long TEXT`
- `original_air_date TEXT`
- `season_number INTEGER`
- `episode_number INTEGER`
- `episode_id_onscreen TEXT`
- `genre_primary TEXT`
- `metadata_json TEXT`
- unique (partial semantics in ingestion): `(source, source_program_id)` when source_program_id is present

3. `epg_broadcasts`
- `id INTEGER PRIMARY KEY`
- `channel_id INTEGER NOT NULL`
- `program_id INTEGER NOT NULL`
- `start_utc TEXT NOT NULL`
- `stop_utc TEXT`
- `duration_seconds INTEGER`
- `is_new INTEGER`
- `is_repeat INTEGER`
- `quality_flags_json TEXT`
- `source_schedule_hash TEXT`
- `metadata_json TEXT`
- foreign keys:
  - `channel_id -> epg_channels(id)`
  - `program_id -> epg_programs(id)`
- unique: `(channel_id, start_utc)`

### New indexes

- `idx_epg_broadcasts_start_utc` on `epg_broadcasts(start_utc)`
- `idx_epg_broadcasts_channel_start` on `epg_broadcasts(channel_id, start_utc)`
- `idx_epg_programs_source_program_id` on `epg_programs(source, source_program_id)`

### Notes

- Keep all timestamps as UTC ISO text (for now).
- Do not yet alter existing recording/scheduler tables.

## Migration v3: Ingestion Run Tracking + Retention Support

### New tables

1. `epg_ingest_runs`
- `id INTEGER PRIMARY KEY`
- `source TEXT NOT NULL`
- `started_at_utc TEXT NOT NULL`
- `finished_at_utc TEXT`
- `status TEXT NOT NULL` (`running`, `ok`, `failed`)
- `message TEXT`
- `stats_json TEXT`

2. `epg_source_checkpoints`
- `source TEXT PRIMARY KEY`
- `last_successful_ingest_utc TEXT`
- `last_source_version TEXT`
- `metadata_json TEXT`

### Optional columns

- add `last_seen_ingest_run_id INTEGER` to `epg_broadcasts`
- used to efficiently prune stale broadcasts after an ingest completes

## Migration v4: Recording/Scheduler Linkage to EPG

### Table adjustments

1. `recordings`
- add nullable `broadcast_id INTEGER`
- add nullable `program_id INTEGER`

2. `scheduler_jobs`
- add nullable `broadcast_id INTEGER`
- add nullable `channel_id INTEGER`
- add nullable `program_id INTEGER`

### Foreign keys

- `broadcast_id -> epg_broadcasts(id)`
- `program_id -> epg_programs(id)`
- `channel_id -> epg_channels(id)`

### Notes

- Keep linkage nullable in first step to avoid breaking existing workflows.
- Populate links in service layer as scheduling/recording features are wired.

## Implementation Strategy in Code

For each migration version in [Storage Schema](../src/ccatv/storage/schema.py):

1. Add a `Migration(version=N, name=..., statements=(...))` entry.
2. Keep DDL split into individual statements (one statement per tuple item).
3. Avoid sqlite-unsupported `ALTER` patterns where possible; if needed, use table-copy pattern in a dedicated migration.
4. Add tests proving:
- schema creation
- idempotency
- rollback when migration insert fails
- expected tables/indexes exist after initialization

## Test Plan Per Migration

## v2 tests

1. New tables exist.
2. Unique constraints enforce `(source, source_channel_id)` and `(channel_id, start_utc)`.
3. Foreign keys reject invalid channel/program references.

## v3 tests

1. Ingest run rows can be created and finalized.
2. Checkpoint upsert works for repeated source runs.
3. Optional stale-row pruning helper behavior is deterministic.

## v4 tests

1. New nullable FK columns exist.
2. Existing recording rows remain valid without links.
3. Linking a recording to a broadcast/program works and enforces FK integrity.

## Open Decisions Before v2 Build

1. Source enum values: exact literals to standardize now (`xmltv`, `schedules_direct`, `dvbstreamer_xmltv`).
2. Whether to keep `source_program_id` uniqueness strict in schema or soft in ingestion logic.
3. Retention window defaults for `epg_broadcasts` pruning.
4. Whether ratings/credits should get normalized child tables in v2 or stay in metadata_json until later.

## Suggested Next Work Item

Implement migration v2 only, with tests and no ingestion parser yet. This gives stable storage primitives while keeping scope small.
