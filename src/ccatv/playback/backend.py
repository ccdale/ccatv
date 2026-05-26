from __future__ import annotations

from typing import Protocol, runtime_checkable


class PlaybackError(RuntimeError):
    """Raised when a playback backend command fails."""


@runtime_checkable
class PlaybackBackend(Protocol):
    def open(self, url: str) -> None: ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def stop(self) -> None: ...

    def set_volume(self, percent: int) -> None: ...

    def close(self) -> None: ...
