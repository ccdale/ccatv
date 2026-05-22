# EPG Mapping Notes (XMLTV + Schedules Direct)

Status: planning notes for future metadata ingestion
Date: 2026-05-22

## Purpose

Capture a common internal data shape so ccatv can ingest guide data from:
- XMLTV exports (including dvbstreamer-generated XMLTV)
- Schedules Direct API payloads

This note is intentionally lightweight. It is meant to steer upcoming schema and ingestion work, not to lock down a final implementation.

## Source Characteristics

### XMLTV

Common record types:
- channel records
- programme records

Key traits:
- Channel identity via `channel/@id`
- Programme identity is often implicit by `(channel, start, stop, title)` unless an `episode-num` system gives a stable id
- Time values are string timestamps with offset (for example `YYYYMMDDHHMMSS +/-ZZZZ`)
- Rich optional metadata: title, sub-title, desc, date, categories, episode-num, credits, audio, subtitles, rating, previously-shown

### Schedules Direct (from prior tvrecord code)

Common entities in prior code:
- channel (`stationID`, names/callsign/channel number mapping)
- schedule rows (`programID`, `stationID`, `airDateTime`, `duration`, `md5`)
- program details (`title`, `episodeTitle150`, short/long descriptions, `originalAirDate`, metadata for season/episode)
- people and role mappings (cast/crew)

Key traits:
- Stable station and program identifiers are provided
- Schedule and program details are separate retrieval steps
- Time comes as ISO datetime strings and is converted to epoch in prior implementation

## Proposed Canonical Internal Model (Phase 1)

Use one normalized shape regardless of source.

### Channel

- source: enum (`xmltv`, `schedules_direct`, `dvbstreamer_xmltv`)
- source_channel_id: text (XMLTV `channel/@id` or SD `stationID`)
- display_name: text
- callsign: text nullable
- logical_channel_number: text nullable
- icon_url: text nullable
- metadata_json: text nullable

Recommended uniqueness:
- unique(source, source_channel_id)

### Program (content identity)

- source: enum
- source_program_id: text nullable (SD `programID`, XMLTV `episode-num` where suitable)
- title: text not null
- subtitle: text nullable
- description_short: text nullable
- description_long: text nullable
- original_air_date: text nullable (ISO date)
- season_number: integer nullable
- episode_number: integer nullable
- episode_id_onscreen: text nullable
- genre_primary: text nullable
- metadata_json: text nullable

Recommended uniqueness:
- when source_program_id exists: unique(source, source_program_id)
- fallback de-dup key can be derived in ingestion logic

### Broadcast / Schedule (airing instance)

- channel_ref -> Channel
- program_ref -> Program
- start_utc: text not null
- stop_utc: text nullable
- duration_seconds: integer nullable
- is_new: integer nullable (0/1)
- is_repeat: integer nullable (0/1)
- quality_flags_json: text nullable (audio/subtitles/rating etc)
- source_schedule_hash: text nullable (for SD md5-like change tracking)

Recommended uniqueness:
- unique(channel_ref, start_utc)

## Field Mapping Crosswalk

### XMLTV -> Canonical

Channel:
- `channel/@id` -> `source_channel_id`
- first useful `display-name` -> `display_name`
- `icon/@src` -> `icon_url`

Programme:
- `programme/@channel` -> `channel_ref` resolution key
- `programme/@start` -> `start_utc` (normalize to UTC)
- `programme/@stop` -> `stop_utc` (normalize to UTC)
- `title` -> `title`
- `sub-title` -> `subtitle`
- `desc` -> `description_long` (or short fallback)
- `date` -> `original_air_date`
- `category[*]` -> `genre_primary` + `metadata_json.categories`
- `episode-num[system=dd_progid]` -> `source_program_id` candidate
- `episode-num[system=onscreen]` -> `episode_id_onscreen`
- `previously-shown` present -> `is_repeat=1`
- audio/subtitles/rating blocks -> `quality_flags_json`

### Schedules Direct -> Canonical

Channel:
- `stationID` -> `source_channel_id`
- lineup/station names -> `display_name`
- callsign -> `callsign`
- channel number map -> `logical_channel_number`

Program details:
- `programID` -> `source_program_id`
- `titles[*]` extracted title -> `title`
- `episodeTitle150` -> `subtitle`
- short/long descriptions -> `description_short` / `description_long`
- `originalAirDate` -> `original_air_date`
- metadata season/episode -> `season_number` / `episode_number`

Schedule rows:
- `stationID` + `airDateTime` -> `channel_ref` + `start_utc`
- `duration` -> `duration_seconds`
- computed `stop_utc` from start + duration
- `md5` -> `source_schedule_hash`

## Time Handling Rules

- Persist UTC timestamps only in database storage fields.
- Keep source-original time string in metadata_json only if needed for debugging.
- Use ISO 8601 UTC text for now (`YYYY-MM-DDTHH:MM:SSZ`) to match current sqlite scaffolding style.

## Ingestion Strategy (suggested)

1. Parse source into transient canonical rows.
2. Upsert channels.
3. Upsert programs.
4. Upsert broadcasts by `(channel_ref, start_utc)`.
5. Mark stale broadcasts for expiry outside retention window.

## Immediate Schema Guidance

When adding metadata tables in upcoming migrations, prioritize:
- channels
- programs
- broadcasts

Keep recording and scheduler tables separate from EPG source tables, joined through stable channel/program references.

## Open Questions

- Do we want one `source` namespace per input feed instance (for example multiple XMLTV files)?
- Should `program` be source-scoped forever, or do we eventually dedupe cross-source content?
- What retention window should we use for historical broadcasts (for example 7 days past, 14 days future)?
- Which category taxonomy should be first-class versus stored only in metadata_json?
