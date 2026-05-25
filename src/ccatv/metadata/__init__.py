"""Metadata integrations and guide ingestion contracts."""

from ccatv.metadata.guide_preference import (
    SOURCE_PRIORITY,
    GuideBroadcastCandidate,
    select_preferred_broadcast,
    sort_by_preference,
    source_priority,
)
from ccatv.metadata.schedules_direct_api import (
    JsonHttpTransport,
    SchedulesDirectHttpClient,
    UrlLibJsonTransport,
)
from ccatv.metadata.schedules_direct_ingest import (
    SchedulesDirectIngestionService,
    SDGuideIngestResult,
    SDGuideIngestStats,
    SqliteGuideRepository,
)
from ccatv.metadata.schedules_direct_runtime import (
    SchedulesDirectConfigError,
    SchedulesDirectCredentialStore,
    SchedulesDirectResponseCacheStore,
    SchedulesDirectTokenCacheStore,
    SDResponseCacheEntry,
    SDTokenCache,
)

__all__ = [
    "GuideBroadcastCandidate",
    "JsonHttpTransport",
    "SDGuideIngestResult",
    "SDGuideIngestStats",
    "SOURCE_PRIORITY",
    "SDResponseCacheEntry",
    "SDTokenCache",
    "SchedulesDirectConfigError",
    "SchedulesDirectCredentialStore",
    "SchedulesDirectHttpClient",
    "SchedulesDirectIngestionService",
    "SchedulesDirectResponseCacheStore",
    "SchedulesDirectTokenCacheStore",
    "SqliteGuideRepository",
    "UrlLibJsonTransport",
    "select_preferred_broadcast",
    "sort_by_preference",
    "source_priority",
]
