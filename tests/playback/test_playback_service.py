from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ccatv.playback.backend import PlaybackError
from ccatv.playback.service import PlaybackSessionService


@dataclass(slots=True)
class _StubBackend:
    calls: list[tuple[str, object | None]] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)

    def open(self, url: str) -> None:
        self.calls.append(("open", url))
        if "open" in self.fail_on:
            raise PlaybackError("open failed")

    def play(self) -> None:
        self.calls.append(("play", None))
        if "play" in self.fail_on:
            raise PlaybackError("play failed")

    def pause(self) -> None:
        self.calls.append(("pause", None))
        if "pause" in self.fail_on:
            raise PlaybackError("pause failed")

    def stop(self) -> None:
        self.calls.append(("stop", None))
        if "stop" in self.fail_on:
            raise PlaybackError("stop failed")

    def set_volume(self, percent: int) -> None:
        self.calls.append(("set_volume", percent))
        if "set_volume" in self.fail_on:
            raise PlaybackError("set_volume failed")

    def close(self) -> None:
        self.calls.append(("close", None))
        if "close" in self.fail_on:
            raise PlaybackError("close failed")


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
    assert paused.current_url == "http://example.test/live"
    assert playing.status == "playing"
    assert playing.current_url == "http://example.test/live"
    assert stopped.status == "stopped"
    assert stopped.current_url == "http://example.test/live"


def test_set_volume_updates_state() -> None:
    backend = _StubBackend()
    service = PlaybackSessionService(backend)

    state = service.set_volume(55)

    assert state.status == "idle"
    assert state.current_url is None
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


def test_open_play_failure_keeps_loaded_state() -> None:
    backend = _StubBackend(fail_on={"play"})
    service = PlaybackSessionService(backend)

    with pytest.raises(PlaybackError):
        service.open("http://example.test/live")

    state = service.get_state()
    assert state.status == "loaded"
    assert state.current_url == "http://example.test/live"


@pytest.mark.parametrize(
    ("method_name", "open_auto_play"),
    [("play", False), ("pause", True), ("stop", False)],
)
def test_failed_control_command_preserves_state(
    method_name: str,
    open_auto_play: bool,
) -> None:
    backend = _StubBackend(fail_on={method_name})
    service = PlaybackSessionService(backend)
    service.open("http://example.test/live", auto_play=open_auto_play)
    before = service.get_state()

    with pytest.raises(PlaybackError):
        getattr(service, method_name)()

    assert service.get_state() == before


def test_failed_set_volume_preserves_state() -> None:
    backend = _StubBackend(fail_on={"set_volume"})
    service = PlaybackSessionService(backend)
    service.open("http://example.test/live")
    before = service.get_state()

    with pytest.raises(PlaybackError):
        service.set_volume(10)

    assert service.get_state() == before
