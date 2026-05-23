from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PostProcessingRequest:
    recording_id: int
    channel_name: str
    output_path: str


@dataclass(frozen=True, slots=True)
class PostProcessingResult:
    success: bool
    message: str | None = None


class PostProcessingRunner(Protocol):
    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        ...


@dataclass(slots=True)
class NoOpPostProcessingRunner:
    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        return PostProcessingResult(success=True, message="no post-processing configured")


__all__ = [
    "NoOpPostProcessingRunner",
    "PostProcessingRequest",
    "PostProcessingResult",
    "PostProcessingRunner",
]
