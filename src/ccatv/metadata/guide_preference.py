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
    """Select the best candidate for a single slot, preferring OTA over SD."""
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: source_priority(candidate.source))


def sort_by_preference(
    candidates: list[GuideBroadcastCandidate],
) -> list[GuideBroadcastCandidate]:
    """Return candidates sorted from highest to lowest preference."""
    return sorted(candidates, key=lambda candidate: source_priority(candidate.source))


__all__ = [
    "GuideBroadcastCandidate",
    "SOURCE_PRIORITY",
    "select_preferred_broadcast",
    "sort_by_preference",
    "source_priority",
]
