from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ccatv.playback.service import PlaybackSessionService


@dataclass(slots=True)
class _StubBackend:
    calls: list[tuple[str, object | None]] = field(default_factory=list)

    def open(self, url: str) -> None:
        self.calls.append(("open", url))

    def play(self) -> None:
        self.calls.append(("play", None))

    def pause(self) -> None:
        self.calls.append(("pause", None))

    def stop(self) -> None:
        self.calls.append(("stop", None))

    def set_volume(self, percent: int) -> None:
        self.calls.append(("set_volume", percent))

    def close(self) -> None:
        self.calls.append(("close", None))


def test_service_starts_idle() -> None:
    service = PlaybackSessionService(_StubBackend())

    state = service.get_state()

    assert state.status == "idle"
    assert state.current_url is None
    assert state.volume_percent == 100


def test_open_updates_state_and_autoplays() -> None:
    backend = _StubBackend()
    service = PlaybackSessionService(backend)

    state = service.open("http://example.test/live")

    assert state.status == "playing"
    assert state.current_url == "http://example.test/live"
    assert backend.calls == [
        ("open", "http://example.test/live"),
        ("play", None),
    ]


def test_open_can_start_paused() -> None:
    backend = _StubBackend()
    service = PlaybackSessionService(backend)

    state = service.open("http://example.test/live", auto_play=False)

    assert state.status == "paused"
    assert backend.calls == [
        ("open", "http://example.test/live"),
        ("pause", None),
    ]


def test_play_pause_stop_flow_updates_state() -> None:
    backend = _StubBackend()
    service = PlaybackSessionService(backend)
    service.open("http://example.test/live")

    paused = service.pause()
    playing = service.play()
    stopped = service.stop()

    assert paused.status == "paused"
    assert playing.status == "playing"
    assert stopped.status == "stopped"


def test_set_volume_updates_state() -> None:
    backend = _StubBackend()
    service = PlaybackSessionService(backend)

    state = service.set_volume(55)

    assert state.volume_percent == 55
    assert backend.calls == [("set_volume", 55)]


def test_close_delegates_to_backend() -> None:
    backend = _StubBackend()
    service = PlaybackSessionService(backend)

    service.close()

    assert backend.calls == [("close", None)]


@pytest.mark.parametrize("percent", [-1, 101])
def test_set_volume_validates_range(percent: int) -> None:
    service = PlaybackSessionService(_StubBackend())

    with pytest.raises(ValueError):
        service.set_volume(percent)


def test_open_validates_non_empty_url() -> None:
    service = PlaybackSessionService(_StubBackend())

    with pytest.raises(ValueError):
        service.open("   ")


def test_initial_volume_validates_range() -> None:
    with pytest.raises(ValueError):
        PlaybackSessionService(_StubBackend(), initial_volume_percent=-1)

    with pytest.raises(ValueError):
        PlaybackSessionService(_StubBackend(), initial_volume_percent=101)
