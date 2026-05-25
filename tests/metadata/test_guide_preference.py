from __future__ import annotations

from ccatv.metadata import (
    GuideBroadcastCandidate,
    select_preferred_broadcast,
    sort_by_preference,
    source_priority,
)


def test_select_preferred_broadcast_prefers_ota_over_sd() -> None:
    candidates = [
        GuideBroadcastCandidate(
            source="schedules_direct",
            source_channel_id="101",
            start_utc="2026-05-25T20:00:00Z",
            stop_utc="2026-05-25T20:30:00Z",
            title="News",
        ),
        GuideBroadcastCandidate(
            source="dvbstreamer_ota",
            source_channel_id="1:2:3",
            start_utc="2026-05-25T20:00:00Z",
            stop_utc="2026-05-25T20:30:00Z",
            title="News",
        ),
    ]

    selected = select_preferred_broadcast(candidates)

    assert selected is not None
    assert selected.source == "dvbstreamer_ota"


def test_select_preferred_broadcast_falls_back_to_sd() -> None:
    candidates = [
        GuideBroadcastCandidate(
            source="schedules_direct",
            source_channel_id="101",
            start_utc="2026-05-25T20:00:00Z",
            stop_utc="2026-05-25T20:30:00Z",
            title="News",
        )
    ]

    selected = select_preferred_broadcast(candidates)

    assert selected is not None
    assert selected.source == "schedules_direct"


def test_select_preferred_broadcast_empty_list_returns_none() -> None:
    selected = select_preferred_broadcast([])

    assert selected is None


def test_select_preferred_broadcast_keeps_order_for_equal_priority() -> None:
    first = GuideBroadcastCandidate(
        source="dvbstreamer_ota",
        source_channel_id="1:2:3",
        start_utc="2026-05-25T20:00:00Z",
        stop_utc="2026-05-25T20:30:00Z",
        title="News",
    )
    second = GuideBroadcastCandidate(
        source="dvbstreamer_ota",
        source_channel_id="4:5:6",
        start_utc="2026-05-25T20:00:00Z",
        stop_utc="2026-05-25T20:30:00Z",
        title="News",
    )

    selected = select_preferred_broadcast([first, second])

    assert selected is first


def test_sort_by_preference_orders_known_sources_before_unknown() -> None:
    candidates = [
        GuideBroadcastCandidate(
            source="custom_provider",
            source_channel_id="alpha",
            start_utc="2026-05-25T20:00:00Z",
            stop_utc=None,
            title="Film",
        ),
        GuideBroadcastCandidate(
            source="schedules_direct",
            source_channel_id="101",
            start_utc="2026-05-25T20:00:00Z",
            stop_utc="2026-05-25T21:00:00Z",
            title="Film",
        ),
        GuideBroadcastCandidate(
            source="dvbstreamer_ota",
            source_channel_id="1:2:3",
            start_utc="2026-05-25T20:00:00Z",
            stop_utc="2026-05-25T21:00:00Z",
            title="Film",
        ),
    ]

    ordered = sort_by_preference(candidates)

    assert [candidate.source for candidate in ordered] == [
        "dvbstreamer_ota",
        "schedules_direct",
        "custom_provider",
    ]


def test_source_priority_unknown_is_lower_priority_than_known() -> None:
    assert source_priority("dvbstreamer_ota") < source_priority("schedules_direct")
    assert source_priority("schedules_direct") < source_priority("custom_provider")
