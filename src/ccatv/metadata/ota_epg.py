from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

EVENT_OPEN_RE = re.compile(r"^<event\s+([^>]+)>$")
NEW_RE = re.compile(r"^<new\s+([^>]+)/>$")
DETAIL_RE = re.compile(r"^<detail\s+([^>]+)>(.*)</detail>$")
ATTR_RE = re.compile(r"(\w+)=[\"']([^\"']*)[\"']")


@dataclass(frozen=True, slots=True)
class OtaEpgEvent:
    channel_source_id: str
    event_source_id: str
    start_utc: str
    end_utc: str | None
    title: str
    description: str | None
    encrypted: bool | None = None


@dataclass(frozen=True, slots=True)
class OtaEpgIngestStats:
    channels_upserted: int
    programs_upserted: int
    broadcasts_upserted: int
    parsed_events: int
    channels_inserted: int = 0
    programs_inserted: int = 0
    broadcasts_inserted: int = 0
    broadcasts_updated: int = 0
    ingest_run_id: int | None = None


@dataclass(slots=True)
class _EventAggregate:
    channel_source_id: str
    event_source_id: str
    start_utc: str | None = None
    end_utc: str | None = None
    encrypted: bool | None = None
    details: dict[tuple[str, str], list[str]] = field(default_factory=dict)


def _parse_attrs(raw: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in ATTR_RE.finditer(raw)}


def _event_key(attrs: dict[str, str]) -> tuple[str, str]:
    channel_source_id = ":".join(
        [
            attrs.get("net", ""),
            attrs.get("ts", ""),
            attrs.get("source", ""),
        ]
    )
    event_source_id = ":".join(
        [
            attrs.get("net", ""),
            attrs.get("ts", ""),
            attrs.get("source", ""),
            attrs.get("event", ""),
        ]
    )
    return channel_source_id, event_source_id


def _normalize_epg_timestamp(raw_value: str) -> str:
    parsed = datetime.strptime(raw_value, "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_detail(aggregate: _EventAggregate, name: str) -> str | None:
    english_variants = ("eng", "en")
    candidates: list[str] = []

    for lang in english_variants:
        candidates.extend(aggregate.details.get((name, lang), []))
    if candidates:
        return max(candidates, key=len)

    any_candidates: list[str] = []
    for (detail_name, _lang), values in aggregate.details.items():
        if detail_name == name:
            any_candidates.extend(values)
    if not any_candidates:
        return None
    return max(any_candidates, key=len)


def parse_dvbstreamer_epg(raw_text: str) -> list[OtaEpgEvent]:
    aggregates: dict[tuple[str, str], _EventAggregate] = {}
    current_event_attrs: dict[str, str] | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        open_match = EVENT_OPEN_RE.match(line)
        if open_match:
            current_event_attrs = _parse_attrs(open_match.group(1))
            continue

        if line == "</event>":
            current_event_attrs = None
            continue

        if current_event_attrs is None:
            continue

        channel_source_id, event_source_id = _event_key(current_event_attrs)
        aggregate = aggregates.setdefault(
            (channel_source_id, event_source_id),
            _EventAggregate(
                channel_source_id=channel_source_id,
                event_source_id=event_source_id,
            ),
        )

        new_match = NEW_RE.match(line)
        if new_match:
            attrs = _parse_attrs(new_match.group(1))
            start_raw = attrs.get("start")
            end_raw = attrs.get("end")
            ca_raw = attrs.get("ca")
            if start_raw:
                aggregate.start_utc = _normalize_epg_timestamp(start_raw)
            if end_raw:
                aggregate.end_utc = _normalize_epg_timestamp(end_raw)
            if ca_raw == "yes":
                aggregate.encrypted = True
            elif ca_raw == "no":
                aggregate.encrypted = False
            continue

        detail_match = DETAIL_RE.match(line)
        if detail_match:
            attrs = _parse_attrs(detail_match.group(1))
            text = detail_match.group(2)
            name = attrs.get("name", "")
            lang = attrs.get("lang", "")
            if name:
                aggregate.details.setdefault((name, lang), []).append(text)

    events: list[OtaEpgEvent] = []
    for aggregate in aggregates.values():
        if aggregate.start_utc is None:
            continue

        title = _select_detail(aggregate, "title")
        if not title:
            continue

        events.append(
            OtaEpgEvent(
                channel_source_id=aggregate.channel_source_id,
                event_source_id=aggregate.event_source_id,
                start_utc=aggregate.start_utc,
                end_utc=aggregate.end_utc,
                title=title,
                description=_select_detail(aggregate, "description"),
                encrypted=aggregate.encrypted,
            )
        )

    events.sort(
        key=lambda item: (item.channel_source_id, item.start_utc, item.event_source_id)
    )
    return events


def _duration_seconds(start_utc: str, end_utc: str | None) -> int | None:
    if end_utc is None:
        return None
    start = datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%SZ")
    end = datetime.strptime(end_utc, "%Y-%m-%dT%H:%M:%SZ")
    return int((end - start).total_seconds())


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _upsert_channel(
    connection: sqlite3.Connection, source: str, channel_source_id: str
) -> tuple[int, bool]:
    existing_row = connection.execute(
        "SELECT id FROM epg_channels WHERE source = ? AND source_channel_id = ?",
        (source, channel_source_id),
    ).fetchone()
    inserted = existing_row is None
    display_name = f"service {channel_source_id}"
    connection.execute(
        """
        INSERT INTO epg_channels(source, source_channel_id, display_name)
        VALUES(?, ?, ?)
        ON CONFLICT(source, source_channel_id)
        DO UPDATE SET display_name = excluded.display_name
        """,
        (source, channel_source_id, display_name),
    )
    row = connection.execute(
        "SELECT id FROM epg_channels WHERE source = ? AND source_channel_id = ?",
        (source, channel_source_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to upsert epg_channels row")
    return int(row[0]), inserted


def _upsert_program(
    connection: sqlite3.Connection, source: str, event: OtaEpgEvent
) -> tuple[int, bool]:
    update_result = connection.execute(
        """
        UPDATE epg_programs
        SET title = ?, description_long = ?
        WHERE source = ? AND source_program_id = ?
        """,
        (event.title, event.description, source, event.event_source_id),
    )
    if update_result.rowcount == 0:
        connection.execute(
            """
            INSERT INTO epg_programs(source, source_program_id, title, description_long)
            VALUES(?, ?, ?, ?)
            """,
            (source, event.event_source_id, event.title, event.description),
        )
        inserted = True
    else:
        inserted = False
    row = connection.execute(
        "SELECT id FROM epg_programs WHERE source = ? AND source_program_id = ?",
        (source, event.event_source_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to upsert epg_programs row")
    return int(row[0]), inserted


def _upsert_broadcast(
    connection: sqlite3.Connection,
    channel_id: int,
    program_id: int,
    event: OtaEpgEvent,
) -> bool:
    existing_row = connection.execute(
        "SELECT id FROM epg_broadcasts WHERE channel_id = ? AND start_utc = ?",
        (channel_id, event.start_utc),
    ).fetchone()
    inserted = existing_row is None

    flags = {}
    if event.encrypted is not None:
        flags["encrypted"] = event.encrypted

    connection.execute(
        """
        INSERT INTO epg_broadcasts(
            channel_id,
            program_id,
            start_utc,
            stop_utc,
            duration_seconds,
            quality_flags_json
        )
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, start_utc)
        DO UPDATE SET
            program_id = excluded.program_id,
            stop_utc = excluded.stop_utc,
            duration_seconds = excluded.duration_seconds,
            quality_flags_json = excluded.quality_flags_json
        """,
        (
            channel_id,
            program_id,
            event.start_utc,
            event.end_utc,
            _duration_seconds(event.start_utc, event.end_utc),
            json.dumps(flags, sort_keys=True) if flags else None,
        ),
    )

    return inserted


def _insert_ingest_run(connection: sqlite3.Connection, source: str, started_at_utc: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO epg_ingest_runs(source, started_at_utc, status)
        VALUES(?, ?, ?)
        """,
        (source, started_at_utc, "running"),
    )
    return int(cursor.lastrowid)


def _finish_ingest_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    finished_at_utc: str,
    status: str,
    message: str | None,
    stats: OtaEpgIngestStats | None,
) -> None:
    stats_json = None
    if stats is not None:
        stats_json = json.dumps(
            {
                "parsed_events": stats.parsed_events,
                "channels_upserted": stats.channels_upserted,
                "programs_upserted": stats.programs_upserted,
                "broadcasts_upserted": stats.broadcasts_upserted,
                "channels_inserted": stats.channels_inserted,
                "programs_inserted": stats.programs_inserted,
                "broadcasts_inserted": stats.broadcasts_inserted,
                "broadcasts_updated": stats.broadcasts_updated,
            },
            sort_keys=True,
        )

    connection.execute(
        """
        UPDATE epg_ingest_runs
        SET finished_at_utc = ?,
            status = ?,
            message = ?,
            stats_json = ?
        WHERE id = ?
        """,
        (finished_at_utc, status, message, stats_json, run_id),
    )


def _upsert_source_checkpoint(
    connection: sqlite3.Connection,
    source: str,
    finished_at_utc: str,
    stats: OtaEpgIngestStats,
) -> None:
    metadata_json = json.dumps(
        {
            "last_run_id": stats.ingest_run_id,
            "parsed_events": stats.parsed_events,
            "broadcasts_upserted": stats.broadcasts_upserted,
        },
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO epg_source_checkpoints(
            source,
            last_successful_ingest_utc,
            metadata_json
        )
        VALUES(?, ?, ?)
        ON CONFLICT(source)
        DO UPDATE SET
            last_successful_ingest_utc = excluded.last_successful_ingest_utc,
            metadata_json = excluded.metadata_json
        """,
        (source, finished_at_utc, metadata_json),
    )


def ingest_dvbstreamer_epg(
    connection: sqlite3.Connection,
    raw_text: str,
    *,
    source: str = "dvbstreamer_ota",
) -> OtaEpgIngestStats:
    started_at_utc = _now_utc_iso()
    with connection:
        ingest_run_id = _insert_ingest_run(connection, source, started_at_utc)

    events = parse_dvbstreamer_epg(raw_text)

    channel_ids: dict[str, int] = {}
    program_ids: dict[str, int] = {}
    channels_inserted = 0
    programs_inserted = 0
    broadcasts_inserted = 0
    broadcasts_updated = 0

    try:
        with connection:
            for event in events:
                if event.channel_source_id not in channel_ids:
                    channel_id, was_inserted = _upsert_channel(
                        connection,
                        source,
                        event.channel_source_id,
                    )
                    channel_ids[event.channel_source_id] = channel_id
                    if was_inserted:
                        channels_inserted += 1

                if event.event_source_id not in program_ids:
                    program_id, was_inserted = _upsert_program(
                        connection,
                        source,
                        event,
                    )
                    program_ids[event.event_source_id] = program_id
                    if was_inserted:
                        programs_inserted += 1

                was_inserted = _upsert_broadcast(
                    connection,
                    channel_ids[event.channel_source_id],
                    program_ids[event.event_source_id],
                    event,
                )
                if was_inserted:
                    broadcasts_inserted += 1
                else:
                    broadcasts_updated += 1

            finished_at_utc = _now_utc_iso()
            stats = OtaEpgIngestStats(
                channels_upserted=len(channel_ids),
                programs_upserted=len(program_ids),
                broadcasts_upserted=len(events),
                parsed_events=len(events),
                channels_inserted=channels_inserted,
                programs_inserted=programs_inserted,
                broadcasts_inserted=broadcasts_inserted,
                broadcasts_updated=broadcasts_updated,
                ingest_run_id=ingest_run_id,
            )
            _finish_ingest_run(
                connection,
                ingest_run_id,
                finished_at_utc=finished_at_utc,
                status="ok",
                message=None,
                stats=stats,
            )
            _upsert_source_checkpoint(
                connection,
                source,
                finished_at_utc,
                stats,
            )
            return stats
    except Exception as exc:
        with connection:
            _finish_ingest_run(
                connection,
                ingest_run_id,
                finished_at_utc=_now_utc_iso(),
                status="failed",
                message=str(exc),
                stats=None,
            )
        raise


def ingest_dvbstreamer_epg_file(
    connection: sqlite3.Connection,
    epg_file: Path,
    *,
    source: str = "dvbstreamer_ota",
) -> OtaEpgIngestStats:
    return ingest_dvbstreamer_epg(
        connection,
        epg_file.read_text(encoding="utf-8"),
        source=source,
    )


__all__ = [
    "OtaEpgEvent",
    "OtaEpgIngestStats",
    "ingest_dvbstreamer_epg",
    "ingest_dvbstreamer_epg_file",
    "parse_dvbstreamer_epg",
]
