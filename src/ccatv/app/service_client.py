from __future__ import annotations

import json
import socket as _socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ccatv.app.bootstrap import AppContext, bootstrap_app, close_app_context
from ccatv.app.service_dispatcher import API_VERSION, ServiceCommandDispatcher


@dataclass(frozen=True, slots=True)
class ServiceClientError(Exception):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, object] | None = None


@runtime_checkable
class ServiceClient(Protocol):
    def execute(
        self, command: str, payload: dict[str, object]
    ) -> dict[str, object]: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class LocalInProcessServiceClient(ServiceClient):
    context: AppContext
    should_stop: Callable[[], bool] = lambda: False
    _dispatcher: ServiceCommandDispatcher = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._dispatcher = ServiceCommandDispatcher(
            self.context,
            should_stop=self.should_stop,
            worker_cycle_lock=getattr(self.context, "worker_cycle_lock", None),
        )

    def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        response = self._dispatcher.dispatch({
            "apiVersion": API_VERSION,
            "command": command,
            "payload": payload,
        })
        if response.get("ok") is not True:
            error = response.get("error")
            if isinstance(error, dict):
                raise ServiceClientError(
                    code=str(error.get("code", "INTERNAL_ERROR")),
                    message=str(error.get("message", "unknown service error")),
                    retryable=bool(error.get("retryable", False)),
                    details=(
                        error.get("details")
                        if isinstance(error.get("details"), dict)
                        else {}
                    ),
                )
            raise ServiceClientError(
                code="INTERNAL_ERROR",
                message=f"service returned malformed response: {response}",
            )

        payload_obj = response.get("payload")
        if not isinstance(payload_obj, dict):
            raise ServiceClientError(
                code="INTERNAL_ERROR",
                message="service returned malformed payload",
            )
        return payload_obj

    def close(self) -> None:
        close_app_context(self.context)


@dataclass(frozen=True, slots=True)
class UnixSocketServiceClient:
    """Service client that communicates with a running ccatv-service over a Unix socket.

    Each call to ``execute`` opens a fresh connection, sends the request, waits
    for the response and then closes the connection.  ``close`` is a no-op
    because no persistent state is held between calls.
    """

    socket_path: str
    timeout_seconds: float = 10.0

    def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        request_bytes = json.dumps(
            {"apiVersion": API_VERSION, "command": command, "payload": payload},
            sort_keys=True,
        ).encode("utf-8")

        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout_seconds)
            sock.connect(self.socket_path)
            sock.sendall(request_bytes)
            sock.shutdown(_socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                block = sock.recv(4096)
                if not block:
                    break
                chunks.append(block)
        except OSError as exc:
            raise ServiceClientError(
                code="TRANSPORT_ERROR",
                message=f"IPC socket error: {exc}",
                retryable=True,
            ) from exc
        finally:
            sock.close()

        response_bytes = b"".join(chunks).strip()
        try:
            response = json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ServiceClientError(
                code="TRANSPORT_ERROR",
                message=f"IPC response not valid JSON: {exc}",
                retryable=True,
            ) from exc

        if not isinstance(response, dict):
            raise ServiceClientError(
                code="TRANSPORT_ERROR",
                message="IPC response was not a JSON object",
                retryable=False,
            )

        if response.get("ok") is not True:
            error = response.get("error")
            if isinstance(error, dict):
                raise ServiceClientError(
                    code=str(error.get("code", "INTERNAL_ERROR")),
                    message=str(error.get("message", "unknown service error")),
                    retryable=bool(error.get("retryable", False)),
                    details=(
                        error.get("details")
                        if isinstance(error.get("details"), dict)
                        else {}
                    ),
                )
            raise ServiceClientError(
                code="INTERNAL_ERROR",
                message=f"service returned malformed response: {response}",
            )

        payload_obj = response.get("payload")
        if not isinstance(payload_obj, dict):
            raise ServiceClientError(
                code="INTERNAL_ERROR",
                message="service returned malformed payload",
            )
        return payload_obj

    def close(self) -> None:
        pass  # stateless; no persistent connection to close


def create_local_service_client() -> LocalInProcessServiceClient:
    return LocalInProcessServiceClient(context=bootstrap_app())


def create_service_client(*, socket_path: str | None = None) -> ServiceClient:
    """Return a :class:`UnixSocketServiceClient` when *socket_path* is given,
    otherwise fall back to a :class:`LocalInProcessServiceClient`."""
    if socket_path:
        return UnixSocketServiceClient(socket_path=socket_path)
    return create_local_service_client()


__all__ = [
    "LocalInProcessServiceClient",
    "ServiceClient",
    "ServiceClientError",
    "UnixSocketServiceClient",
    "create_local_service_client",
    "create_service_client",
]
