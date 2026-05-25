from __future__ import annotations

from http import client as _http_client
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


def _extract_payload_or_raise(
    response: dict[str, object],
    *,
    malformed_prefix: str,
) -> dict[str, object]:
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
            message=f"{malformed_prefix}: {response}",
        )

    payload_obj = response.get("payload")
    if not isinstance(payload_obj, dict):
        raise ServiceClientError(
            code="INTERNAL_ERROR",
            message="service returned malformed payload",
        )
    return payload_obj


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
        return _extract_payload_or_raise(
            response,
            malformed_prefix="service returned malformed response",
        )

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

        return _extract_payload_or_raise(
            response,
            malformed_prefix="service returned malformed response",
        )

    def close(self) -> None:
        pass  # stateless; no persistent connection to close


@dataclass(frozen=True, slots=True)
class HttpServiceClient:
    host: str
    port: int
    auth_token: str
    timeout_seconds: float = 10.0

    def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        request_bytes = json.dumps(
            {"apiVersion": API_VERSION, "command": command, "payload": payload},
            sort_keys=True,
        ).encode("utf-8")

        connection = _http_client.HTTPConnection(
            host=self.host,
            port=self.port,
            timeout=self.timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                "/api/v1/command",
                body=request_bytes,
                headers={
                    "Authorization": f"Bearer {self.auth_token}",
                    "Content-Type": "application/json",
                },
            )
            response_obj = connection.getresponse()
            status_code = response_obj.status
            response_bytes = response_obj.read()
        except OSError as exc:
            raise ServiceClientError(
                code="TRANSPORT_ERROR",
                message=f"HTTP transport error: {exc}",
                retryable=True,
            ) from exc
        finally:
            connection.close()

        try:
            response = json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            retryable = status_code >= 500 or status_code in (408, 429)
            raise ServiceClientError(
                code="TRANSPORT_ERROR",
                message=(
                    f"HTTP {status_code} response not valid JSON: {exc}"
                ),
                retryable=retryable,
            ) from exc

        if not isinstance(response, dict):
            raise ServiceClientError(
                code="TRANSPORT_ERROR",
                message=f"HTTP {status_code} response was not a JSON object",
                retryable=False,
            )

        if status_code == 401 and response.get("ok") is not True:
            error = response.get("error")
            if not isinstance(error, dict):
                raise ServiceClientError(
                    code="AUTHENTICATION_REQUIRED",
                    message="missing or invalid bearer token",
                    retryable=False,
                )

        return _extract_payload_or_raise(
            response,
            malformed_prefix="service returned malformed HTTP response",
        )

    def close(self) -> None:
        pass  # stateless; no persistent connection to close


def create_local_service_client() -> LocalInProcessServiceClient:
    return LocalInProcessServiceClient(context=bootstrap_app())


def create_service_client(
    *,
    socket_path: str | None = None,
    http_host: str | None = None,
    http_port: int = 8787,
    http_auth_token: str | None = None,
) -> ServiceClient:
    """Return a :class:`UnixSocketServiceClient` when *socket_path* is given,
    otherwise fall back to a :class:`LocalInProcessServiceClient`."""
    if socket_path and http_host:
        raise ValueError("socket_path and http_host cannot both be set")
    if http_host:
        if not http_auth_token:
            raise ValueError("http_auth_token is required when http_host is set")
        return HttpServiceClient(
            host=http_host,
            port=http_port,
            auth_token=http_auth_token,
        )
    if socket_path:
        return UnixSocketServiceClient(socket_path=socket_path)
    return create_local_service_client()


__all__ = [
    "LocalInProcessServiceClient",
    "HttpServiceClient",
    "ServiceClient",
    "ServiceClientError",
    "UnixSocketServiceClient",
    "create_local_service_client",
    "create_service_client",
]
