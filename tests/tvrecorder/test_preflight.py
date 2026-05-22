from __future__ import annotations

import socket
from inspect import signature

import pytest

from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlCommandError
from ccatv.tvrecorder.preflight import WritePreflightChecker, WritePreflightError


class _StubClient:
    def __init__(self, should_succeed: bool, error: Exception | None = None) -> None:
        self.should_succeed = should_succeed
        self.error = error

    def run_command(self, command: str):
        if not self.should_succeed:
            if self.error is None:
                raise RuntimeError("Stub client failed without a configured error")
            raise self.error
        return object()


def _dvbctrl_init_default(param_name: str):
    return signature(DvbCtrlClient.__init__).parameters[param_name].default


def test_check_raises_for_invalid_adapter_count() -> None:
    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=0,
        preferred_adapter_index=0,
    )

    with pytest.raises(
        WritePreflightError, match="Adapter count must be greater than 0"
    ):
        checker.check()


def test_check_raises_when_host_cannot_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    checker = WritePreflightChecker(
        host="bad-host",
        adapter_count=1,
        preferred_adapter_index=0,
    )

    def _bad_getaddrinfo(*args, **kwargs):
        raise socket.gaierror("host lookup failed")

    monkeypatch.setattr(socket, "getaddrinfo", _bad_getaddrinfo)

    with pytest.raises(
        WritePreflightError,
        match="Host 'bad-host' cannot be resolved",
    ):
        checker.check()


def test_check_raises_for_preferred_adapter_out_of_bounds() -> None:
    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=2,
        preferred_adapter_index=3,
    )

    with pytest.raises(WritePreflightError, match="out of range"):
        checker.check()


def test_check_raises_for_negative_preferred_adapter_index() -> None:
    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=2,
        preferred_adapter_index=-1,
    )

    with pytest.raises(WritePreflightError, match="out of range"):
        checker.check()


def test_check_selects_preferred_online_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    def _factory(adapter_index: int):
        if adapter_index in {2, 3}:
            return _StubClient(should_succeed=True)
        return _StubClient(
            should_succeed=False,
            error=DvbCtrlCommandError("offline"),
        )

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=4,
        preferred_adapter_index=2,
        client_factory=_factory,
    )

    result = checker.check()

    assert result.online_adapters == (2, 3)
    assert result.selected_adapter == 2


def test_check_prefers_adapter_zero_when_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    def _factory(adapter_index: int):
        if adapter_index == 0:
            return _StubClient(should_succeed=True)
        return _StubClient(
            should_succeed=False,
            error=DvbCtrlCommandError("offline"),
        )

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=2,
        preferred_adapter_index=0,
        client_factory=_factory,
    )

    result = checker.check()

    assert result.online_adapters == (0,)
    assert result.selected_adapter == 0


def test_check_falls_back_to_first_online_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    def _factory(adapter_index: int):
        if adapter_index == 1:
            return _StubClient(should_succeed=True)
        return _StubClient(
            should_succeed=False,
            error=DvbCtrlCommandError("offline"),
        )

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=3,
        preferred_adapter_index=2,
        client_factory=_factory,
    )

    result = checker.check()

    assert result.online_adapters == (1,)
    assert result.selected_adapter == 1


def test_check_falls_back_when_preferred_zero_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    def _factory(adapter_index: int):
        if adapter_index == 1:
            return _StubClient(should_succeed=True)
        return _StubClient(
            should_succeed=False,
            error=DvbCtrlCommandError("offline"),
        )

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=3,
        preferred_adapter_index=0,
        client_factory=_factory,
    )

    result = checker.check()

    assert result.online_adapters == (1,)
    assert result.selected_adapter == 1


def test_check_raises_when_no_adapters_online(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    def _factory(adapter_index: int):
        return _StubClient(
            should_succeed=False,
            error=DvbCtrlCommandError(f"adapter {adapter_index} offline"),
        )

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=2,
        preferred_adapter_index=0,
        client_factory=_factory,
    )

    with pytest.raises(
        WritePreflightError, match="No writable tuner path is available"
    ):
        checker.check()


def test_check_handles_client_factory_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    def _factory(adapter_index: int):
        raise RuntimeError(f"factory exploded for {adapter_index}")

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=2,
        preferred_adapter_index=0,
        client_factory=_factory,
    )

    with pytest.raises(
        WritePreflightError, match="No writable tuner path is available"
    ):
        checker.check()


def test_check_uses_default_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    captured: list[tuple[str, str, int, float, int, float]] = []

    class _DefaultClientStub:
        def __init__(
            self,
            executable_path: str,
            host: str,
            adapter_index: int,
            timeout_seconds: float,
            transient_retry_count: int = _dvbctrl_init_default("transient_retry_count"),
            transient_retry_delay_seconds: float = _dvbctrl_init_default(
                "transient_retry_delay_seconds"
            ),
            **_: object,
        ) -> None:
            captured.append(
                (
                    executable_path,
                    host,
                    adapter_index,
                    timeout_seconds,
                    transient_retry_count,
                    transient_retry_delay_seconds,
                )
            )

        def run_command(self, command: str):
            return object()

    import ccatv.tvrecorder.preflight as preflight_module

    monkeypatch.setattr(preflight_module, "DvbCtrlClient", _DefaultClientStub)

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=2,
        preferred_adapter_index=1,
        executable_path="/opt/bin/dvbctrl",
        timeout_seconds=3.5,
    )

    result = checker.check()

    assert result.online_adapters == (0, 1)
    assert result.selected_adapter == 1
    assert captured == [
        (
            "/opt/bin/dvbctrl",
            "druidmedia",
            0,
            3.5,
            _dvbctrl_init_default("transient_retry_count"),
            _dvbctrl_init_default("transient_retry_delay_seconds"),
        ),
        (
            "/opt/bin/dvbctrl",
            "druidmedia",
            1,
            3.5,
            _dvbctrl_init_default("transient_retry_count"),
            _dvbctrl_init_default("transient_retry_delay_seconds"),
        ),
    ]


def test_check_uses_default_client_factory_with_preferred_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [object()])

    captured: list[tuple[str, str, int, float, int, float]] = []

    class _DefaultClientStub:
        def __init__(
            self,
            executable_path: str,
            host: str,
            adapter_index: int,
            timeout_seconds: float,
            transient_retry_count: int = _dvbctrl_init_default("transient_retry_count"),
            transient_retry_delay_seconds: float = _dvbctrl_init_default(
                "transient_retry_delay_seconds"
            ),
            **_: object,
        ) -> None:
            captured.append(
                (
                    executable_path,
                    host,
                    adapter_index,
                    timeout_seconds,
                    transient_retry_count,
                    transient_retry_delay_seconds,
                )
            )

        def run_command(self, command: str):
            return object()

    import ccatv.tvrecorder.preflight as preflight_module

    monkeypatch.setattr(preflight_module, "DvbCtrlClient", _DefaultClientStub)

    checker = WritePreflightChecker(
        host="druidmedia",
        adapter_count=2,
        preferred_adapter_index=0,
    )

    result = checker.check()

    assert result.online_adapters == (0, 1)
    assert result.selected_adapter == 0
    assert captured == [
        (
            "dvbctrl",
            "druidmedia",
            0,
            10.0,
            _dvbctrl_init_default("transient_retry_count"),
            _dvbctrl_init_default("transient_retry_delay_seconds"),
        ),
        (
            "dvbctrl",
            "druidmedia",
            1,
            10.0,
            _dvbctrl_init_default("transient_retry_count"),
            _dvbctrl_init_default("transient_retry_delay_seconds"),
        ),
    ]
