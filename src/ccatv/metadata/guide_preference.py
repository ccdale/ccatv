from __future__ import annotations

from dataclasses import dataclass

SOURCE_PRIORITY = {
    "dvbstreamer_ota": 0,
    "schedules_direct": 1,
}


@dataclass(frozen=True, slots=True)
class GuideBroadcastCandidate:
    """Comparable guide candidate for a single logical program slot."""

    source: str
    source_channel_id: str
    start_utc: str
    stop_utc: str | None
    title: str
    description: str | None = None


def source_priority(source: str) -> int:
    """Return source priority; unknown sources are lower priority than known ones."""
    return SOURCE_PRIORITY.get(source, 99)


def select_preferred_broadcast(
    candidates: list[GuideBroadcastCandidate],
) -> GuideBroadcastCandidate | None:
    """Select the best candidate for a single slot, preferring OTA over SD.

    Candidates with equal source priority preserve incoming list order.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: source_priority(candidate.source))


def _candidates_agree(
    primary: GuideBroadcastCandidate,
    secondary: GuideBroadcastCandidate,
) -> bool:
    return (
        primary.title == secondary.title
        and primary.start_utc == secondary.start_utc
        and primary.stop_utc == secondary.stop_utc
    )


def select_preferred_broadcast_merged(
    candidates: list[GuideBroadcastCandidate],
) -> GuideBroadcastCandidate | None:
    """Select OTA-first candidate and merge richer data from agreeing fallbacks.

    The highest-priority source remains authoritative for identity fields. Optional
    fields are only filled from lower-priority candidates when title/start/stop
    match exactly.
    """
    preferred = select_preferred_broadcast(candidates)
    if preferred is None:
        return None

    merged_description = preferred.description
    for candidate in sort_by_preference(candidates):
        if candidate is preferred:
            continue
        if not _candidates_agree(preferred, candidate):
            continue
        if merged_description is None and candidate.description is not None:
            merged_description = candidate.description

    if merged_description == preferred.description:
        return preferred

    return GuideBroadcastCandidate(
        source=preferred.source,
        source_channel_id=preferred.source_channel_id,
        start_utc=preferred.start_utc,
        stop_utc=preferred.stop_utc,
        title=preferred.title,
        description=merged_description,
    )


def sort_by_preference(
    candidates: list[GuideBroadcastCandidate],
) -> list[GuideBroadcastCandidate]:
    """Return candidates sorted from highest to lowest preference."""
    return sorted(candidates, key=lambda candidate: source_priority(candidate.source))


__all__ = [
    "GuideBroadcastCandidate",
    "SOURCE_PRIORITY",
    "select_preferred_broadcast",
    "select_preferred_broadcast_merged",
    "sort_by_preference",
    "source_priority",
]
