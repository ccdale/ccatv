from __future__ import annotations

import json
import socket
from dataclasses import dataclass

from ccatv.playback.backend import PlaybackError


@dataclass(slots=True)
class MpvIpcBackend:
    """Minimal mpv JSON IPC backend over Unix socket."""

    socket_path: str
    timeout_seconds: float = 2.0

    def open(self, url: str) -> None:
        if not url.strip():
            raise ValueError("url must be a non-empty string")
        self._command(["loadfile", url.strip(), "replace"])

    def play(self) -> None:
        self._command(["set_property", "pause", False])

    def pause(self) -> None:
        self._command(["set_property", "pause", True])

    def stop(self) -> None:
        self._command(["stop"])

    def set_volume(self, percent: int) -> None:
        if percent < 0 or percent > 100:
            raise ValueError("percent must be in range 0..100")
        self._command(["set_property", "volume", percent])

    def close(self) -> None:
        # Stateless transport; no persistent socket is kept.
        return None

    def _command(self, args: list[object]) -> dict[str, object]:
        request = json.dumps({"command": args}, sort_keys=True).encode("utf-8") + b"\n"
        response = self._send(request)
        error_value = response.get("error")
        if error_value not in (None, "success"):
            raise PlaybackError(f"mpv IPC command failed: {error_value}")
        return response

    def _send(self, request: bytes) -> dict[str, object]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout_seconds)
            sock.connect(self.socket_path)
            sock.sendall(request)
            chunks: list[bytes] = []
            while True:
                block = sock.recv(4096)
                if not block:
                    break
                chunks.append(block)
                if b"\n" in block:
                    break
        except OSError as exc:
            raise PlaybackError(f"mpv IPC transport error: {exc}") from exc
        finally:
            sock.close()

        raw_response = b"".join(chunks).strip()
        if not raw_response:
            raise PlaybackError("mpv IPC returned empty response")

        try:
            payload = json.loads(raw_response.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PlaybackError(f"mpv IPC invalid JSON response: {exc}") from exc

        if not isinstance(payload, dict):
            raise PlaybackError("mpv IPC response must be a JSON object")
        return payload
