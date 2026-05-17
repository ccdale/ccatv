"""Contracts and domain models for Schedules Direct guide integration.

This module intentionally defines interfaces and data models only.
Concrete HTTP clients and persistence implementations should depend on
these contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable


class SchedulesDirectError(Exception):
    """Base class for Schedules Direct domain failures."""


class SchedulesDirectTransportError(SchedulesDirectError):
    """Raised when network/transport errors prevent API communication."""


class SchedulesDirectAuthenticationError(SchedulesDirectError):
    """Raised when credentials are rejected or token refresh fails."""


class SchedulesDirectRateLimitError(SchedulesDirectError):
    """Raised when API rate limits are reached."""

    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class SchedulesDirectApiError(SchedulesDirectError):
    """Raised for API-level failures with a provider code and message."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code


class GuideDataSource(str, Enum):
    """Guide source identifier used in normalized persistence records."""

    SCHEDULES_DIRECT = "schedules_direct"


@dataclass(slots=True, frozen=True)
class SDCredentials:
    """Authentication credentials for Schedules Direct."""

    username: str
    password: str


@dataclass(slots=True, frozen=True)
class SDAccountStatus:
    """Account and service health details returned by Schedules Direct."""

    account_expires_utc: datetime | None = None
    max_lineups: int | None = None
    provider_message: str | None = None


@dataclass(slots=True, frozen=True)
class SDLineup:
    """Lineup metadata used to select and sync channel listings."""

    lineup_id: str
    name: str
    transport: str | None = None
    country: str | None = None
    postal_code: str | None = None


@dataclass(slots=True, frozen=True)
class SDStation:
    """Station metadata from an active lineup."""

    station_id: str
    callsign: str
    name: str
    channel: str | None = None


@dataclass(slots=True, frozen=True)
class SDProgram:
    """Program metadata referenced by schedule entries."""

    program_id: str
    title: str
    episode_title: str | None = None
    description: str | None = None
    original_air_date: date | None = None
    genres: tuple[str, ...] = ()
    artwork_urls: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SDScheduleEntry:
    """A single station program timeslot."""

    station_id: str
    program_id: str
    start_utc: datetime
    end_utc: datetime
    duration_seconds: int
    is_new: bool = False
    is_live: bool = False
    audio_properties: tuple[str, ...] = ()
    video_properties: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class GuideSyncWindow:
    """Time window used for incremental schedule synchronization."""

    start_utc: datetime
    end_utc: datetime


@dataclass(slots=True, frozen=True)
class SDGuideSnapshot:
    """Provider payload normalized into app-level sync units."""

    lineup_id: str
    fetched_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stations: tuple[SDStation, ...] = ()
    schedules: tuple[SDScheduleEntry, ...] = ()
    programs: tuple[SDProgram, ...] = ()


@runtime_checkable
class SchedulesDirectClient(Protocol):
    """Contract for provider communication and payload retrieval."""

    async def authenticate(self, credentials: SDCredentials) -> None:
        """Authenticate and cache provider session state."""

    async def get_account_status(self) -> SDAccountStatus:
        """Return account status and provider-side service details."""

    async def list_lineups(self, country: str, postal_code: str) -> list[SDLineup]:
        """Return candidate lineups for a location lookup."""

    async def get_lineup_stations(self, lineup_id: str) -> list[SDStation]:
        """Return all stations for the configured lineup."""

    async def get_schedules(
        self,
        lineup_id: str,
        window: GuideSyncWindow,
    ) -> list[SDScheduleEntry]:
        """Return schedule slots for a lineup and sync window."""

    async def get_programs(self, program_ids: list[str]) -> list[SDProgram]:
        """Resolve program metadata records for referenced IDs."""

    async def close(self) -> None:
        """Release network/session resources."""


@runtime_checkable
class GuideRepository(Protocol):
    """Persistence contract for normalized guide data."""

    async def upsert_stations(
        self, source: GuideDataSource, stations: list[SDStation]
    ) -> int:
        """Insert or update stations and return affected row count."""

    async def upsert_programs(
        self, source: GuideDataSource, programs: list[SDProgram]
    ) -> int:
        """Insert or update programs and return affected row count."""

    async def upsert_schedules(
        self,
        source: GuideDataSource,
        lineup_id: str,
        schedules: list[SDScheduleEntry],
    ) -> int:
        """Insert or update schedule entries and return affected row count."""

    async def prune_expired_schedules(
        self,
        source: GuideDataSource,
        lineup_id: str,
        before_utc: datetime,
    ) -> int:
        """Remove stale schedules and return deleted row count."""


@runtime_checkable
class ChannelGuideMapper(Protocol):
    """Mapping contract between local channel identity and provider stations."""

    async def map_service_to_station(
        self,
        adapter_index: int,
        service_name: str,
        logical_channel: str | None = None,
    ) -> str | None:
        """Return station_id for a local service, or None if unmapped."""


@runtime_checkable
class GuideIngestionService(Protocol):
    """High-level orchestration contract for guide sync jobs."""

    async def seed_lineup(self, lineup_id: str) -> SDGuideSnapshot:
        """Perform initial lineup seed for stations, schedules, and programs."""

    async def sync_incremental(
        self, lineup_id: str, window: GuideSyncWindow
    ) -> SDGuideSnapshot:
        """Perform incremental sync for a constrained time window."""


__all__ = [
    "ChannelGuideMapper",
    "GuideDataSource",
    "GuideIngestionService",
    "GuideRepository",
    "GuideSyncWindow",
    "SDAccountStatus",
    "SDCredentials",
    "SDGuideSnapshot",
    "SDLineup",
    "SDProgram",
    "SDScheduleEntry",
    "SDStation",
    "SchedulesDirectApiError",
    "SchedulesDirectAuthenticationError",
    "SchedulesDirectClient",
    "SchedulesDirectError",
    "SchedulesDirectRateLimitError",
    "SchedulesDirectTransportError",
]
