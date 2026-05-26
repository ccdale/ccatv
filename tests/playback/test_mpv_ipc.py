from __future__ import annotations

import json

import pytest

from ccatv.playback import MpvIpcBackend, PlaybackError


class _StubSocket:
    def __init__(self, responses: list[bytes] | None = None, fail_connect: bool = False):
        self.responses = responses or []
        self.fail_connect = fail_connect
        self.sent: list[bytes] = []
        self.closed = False
        self.connected_to: str | None = None
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, socket_path: str) -> None:
        if self.fail_connect:
            raise OSError("connect failed")
        self.connected_to = socket_path

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _size: int) -> bytes:
        if self.responses:
            return self.responses.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


def test_open_sends_loadfile_command(monkeypatch) -> None:
    stub = _StubSocket([b'{"error":"success"}\n'])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")
    backend.open("http://example.test/stream")

    sent_payload = json.loads(stub.sent[0].decode("utf-8").strip())
    assert sent_payload["command"] == ["loadfile", "http://example.test/stream", "replace"]
    assert stub.connected_to == "/tmp/mpv.sock"
    assert stub.closed is True
    assert stub.timeout == backend.timeout_seconds


def test_open_rejects_empty_url() -> None:
    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")

    with pytest.raises(ValueError):
        backend.open("   ")


@pytest.mark.parametrize(
    ("method_name", "expected_command"),
    [
        ("play", ["set_property", "pause", False]),
        ("pause", ["set_property", "pause", True]),
        ("stop", ["stop"]),
    ],
)
def test_basic_controls_send_expected_commands(
    monkeypatch,
    method_name: str,
    expected_command: list[object],
) -> None:
    stub = _StubSocket([b'{"error":"success"}\n'])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")
    getattr(backend, method_name)()

    sent_payload = json.loads(stub.sent[0].decode("utf-8").strip())
    assert sent_payload["command"] == expected_command


def test_set_volume_validates_range() -> None:
    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")

    with pytest.raises(ValueError):
        backend.set_volume(-1)

    with pytest.raises(ValueError):
        backend.set_volume(101)


def test_set_volume_sends_command(monkeypatch) -> None:
    stub = _StubSocket([b'{"error":"success"}\n'])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")
    backend.set_volume(65)

    sent_payload = json.loads(stub.sent[0].decode("utf-8").strip())
    assert sent_payload["command"] == ["set_property", "volume", 65]


def test_transport_error_is_mapped(monkeypatch) -> None:
    stub = _StubSocket(fail_connect=True)
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")

    with pytest.raises(PlaybackError) as exc_info:
        backend.play()

    assert "transport error" in str(exc_info.value)


def test_invalid_json_response_is_mapped(monkeypatch) -> None:
    stub = _StubSocket([b"not-json\n"])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")

    with pytest.raises(PlaybackError) as exc_info:
        backend.play()

    assert "invalid JSON" in str(exc_info.value)


@pytest.mark.parametrize(
    "raw_json",
    [
        b"[]\n",
        b'"text"\n',
        b"null\n",
    ],
)
def test_non_object_json_response_is_rejected(monkeypatch, raw_json: bytes) -> None:
    stub = _StubSocket([raw_json])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")

    with pytest.raises(PlaybackError) as exc_info:
        backend.stop()

    assert "JSON object" in str(exc_info.value)


def test_response_missing_error_field_is_rejected(monkeypatch) -> None:
    stub = _StubSocket([b'{"data":true}\n'])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")

    with pytest.raises(PlaybackError) as exc_info:
        backend.play()

    assert "missing required 'error' field" in str(exc_info.value)


def test_chunked_response_is_assembled(monkeypatch) -> None:
    stub = _StubSocket([b'{"error":', b'"success"}\n'])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")
    backend.pause()

    sent_payload = json.loads(stub.sent[0].decode("utf-8").strip())
    assert sent_payload["command"] == ["set_property", "pause", True]


def test_command_error_field_is_mapped(monkeypatch) -> None:
    stub = _StubSocket([b'{"error":"invalid parameter"}\n'])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")

    with pytest.raises(PlaybackError) as exc_info:
        backend.pause()

    assert "command failed" in str(exc_info.value)


def test_error_null_response_is_treated_as_success(monkeypatch) -> None:
    stub = _StubSocket([b'{"error":null}\n'])
    monkeypatch.setattr(
        "ccatv.playback.mpv_ipc.socket.socket",
        lambda *_args, **_kwargs: stub,
    )

    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")
    backend.stop()

    sent_payload = json.loads(stub.sent[0].decode("utf-8").strip())
    assert sent_payload["command"] == ["stop"]


def test_close_does_not_raise() -> None:
    backend = MpvIpcBackend(socket_path="/tmp/mpv.sock")
    backend.close()
