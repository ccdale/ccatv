from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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

    def __post_init__(self) -> None:
        self._dispatcher = ServiceCommandDispatcher(
            self.context,
            should_stop=self.should_stop,
            worker_cycle_lock=getattr(self.context, "worker_cycle_lock", None),
        )

    def execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        response = self._dispatcher.dispatch(
            {
                "apiVersion": API_VERSION,
                "command": command,
                "payload": payload,
            }
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
        close_app_context(self.context)


def create_local_service_client() -> LocalInProcessServiceClient:
    return LocalInProcessServiceClient(context=bootstrap_app())


__all__ = [
    "LocalInProcessServiceClient",
    "ServiceClient",
    "ServiceClientError",
    "create_local_service_client",
]
