from __future__ import annotations

from dataclasses import dataclass

from ccatv.playback.backend import PlaybackBackend


@dataclass(frozen=True, slots=True)
class PlaybackSessionState:
    status: str
    current_url: str | None
    volume_percent: int


class PlaybackSessionService:
    """Stateful orchestration layer above playback backend primitives."""

    def __init__(
        self,
        backend: PlaybackBackend,
        *,
        initial_volume_percent: int = 100,
    ) -> None:
        if initial_volume_percent < 0 or initial_volume_percent > 100:
            raise ValueError("initial_volume_percent must be in range 0..100")
        self._backend = backend
        self._state = PlaybackSessionState(
            status="idle",
            current_url=None,
            volume_percent=initial_volume_percent,
        )

    def get_state(self) -> PlaybackSessionState:
        return self._state

    def open(self, url: str, *, auto_play: bool = True) -> PlaybackSessionState:
        if not url.strip():
            raise ValueError("url must be a non-empty string")
        self._backend.open(url.strip())
        next_status = "playing" if auto_play else "paused"
        if auto_play:
            self._backend.play()
        else:
            self._backend.pause()
        self._state = PlaybackSessionState(
            status=next_status,
            current_url=url.strip(),
            volume_percent=self._state.volume_percent,
        )
        return self._state

    def play(self) -> PlaybackSessionState:
        self._backend.play()
        self._state = PlaybackSessionState(
            status="playing",
            current_url=self._state.current_url,
            volume_percent=self._state.volume_percent,
        )
        return self._state

    def pause(self) -> PlaybackSessionState:
        self._backend.pause()
        self._state = PlaybackSessionState(
            status="paused",
            current_url=self._state.current_url,
            volume_percent=self._state.volume_percent,
        )
        return self._state

    def stop(self) -> PlaybackSessionState:
        self._backend.stop()
        self._state = PlaybackSessionState(
            status="stopped",
            current_url=self._state.current_url,
            volume_percent=self._state.volume_percent,
        )
        return self._state

    def set_volume(self, percent: int) -> PlaybackSessionState:
        if percent < 0 or percent > 100:
            raise ValueError("percent must be in range 0..100")
        self._backend.set_volume(percent)
        self._state = PlaybackSessionState(
            status=self._state.status,
            current_url=self._state.current_url,
            volume_percent=percent,
        )
        return self._state

    def close(self) -> None:
        self._backend.close()
