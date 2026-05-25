from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ccatv.app.service_client import ServiceClient, create_service_client


@dataclass(slots=True)
class GtkServiceGateway:
    """Socket-first service gateway intended for GTK4 front-end use.

    This layer gives the UI typed methods while keeping command-envelope
    details contained in one place.
    """

    socket_path: str
    _client_factory: Callable[[], ServiceClient] = field(repr=False)

    def get_service_health(self) -> dict[str, object]:
        return self._execute("service.health.get", {})

    def get_service_info(self) -> dict[str, object]:
        return self._execute("service.info.get", {})

    def list_schedules(self, *, state: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {}
        if state is not None:
            state_value = state.strip()
            if not state_value:
                raise ValueError("state must be a non-empty string when provided")
            payload["state"] = state_value
        return self._execute("recording.schedule.list", payload)

    def create_schedule(
        self,
        *,
        channel_name: str,
        start_at_utc: str,
        duration_seconds: int,
    ) -> dict[str, object]:
        if not channel_name.strip():
            raise ValueError("channel_name must be a non-empty string")
        if not start_at_utc.strip():
            raise ValueError("start_at_utc must be a non-empty UTC timestamp string")
        if duration_seconds < 1:
            raise ValueError("duration_seconds must be greater than 0")

        return self._execute(
            "recording.schedule.create",
            {
                "channelName": channel_name.strip(),
                "startAtUtc": start_at_utc.strip(),
                "durationSeconds": duration_seconds,
            },
        )

    def _execute(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        client = self._client_factory()
        try:
            return client.execute(command, payload)
        finally:
            client.close()


def create_gtk_service_gateway(*, socket_path: str) -> GtkServiceGateway:
    return GtkServiceGateway(
        socket_path=socket_path,
        _client_factory=lambda: create_service_client(socket_path=socket_path),
    )


__all__ = [
    "GtkServiceGateway",
    "create_gtk_service_gateway",
]
