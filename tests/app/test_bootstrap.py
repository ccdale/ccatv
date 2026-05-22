from __future__ import annotations

from inspect import signature

import pytest

import ccatv.app.bootstrap as bootstrap_module
from ccatv.app.bootstrap import bootstrap_app
from ccatv.settings import AppSettings
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient


def _dvbctrl_init_default(param_name: str):
    return signature(DvbCtrlClient.__init__).parameters[param_name].default


def test_bootstrap_uses_dvbctrl_without_inline_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        AppSettings,
        "from_env",
        classmethod(
            lambda cls: AppSettings(
                dvb_adapter_count=4,
                dvb_adapter_index=2,
                dvbctrl_path="dvbctrl",
                dvbctrl_timeout_seconds=4.25,
                dvbstreamer_bind_address="0.0.0.0",
                dvbstreamer_host="10.0.0.5",
                dvbstreamer_output_mrl="udp://239.10.10.10:1234",
                dvbstreamer_path="/opt/bin/dvbstreamer",
                dvbstreamer_stop_timeout_seconds=7.5,
            )
        ),
    )

    context = bootstrap_app()

    assert context.dvbctrl.executable_path == "dvbctrl"
    assert context.dvbctrl.host == "10.0.0.5"
    assert context.dvbctrl.adapter_index == 2
    assert context.dvbctrl.timeout_seconds == 4.25
    assert context.dvbctrl.transient_retry_count == _dvbctrl_init_default(
        "transient_retry_count"
    )
    assert context.dvbctrl.transient_retry_delay_seconds == _dvbctrl_init_default(
        "transient_retry_delay_seconds"
    )
    assert context.dvbstreamer.config.adapter_index == 2
    assert context.dvbstreamer.config.bind_address == "0.0.0.0"
    assert context.dvbstreamer.config.output_mrl == "udp://239.10.10.10:1234"
    assert context.dvbstreamer.config.executable_path == "/opt/bin/dvbstreamer"
    assert context.dvbstreamer.stop_timeout_seconds == 7.5
    assert context.write_preflight.host == "10.0.0.5"
    assert context.write_preflight.adapter_count == 4
    assert context.write_preflight.preferred_adapter_index == 2
    assert context.write_preflight.executable_path == "dvbctrl"
    assert context.write_preflight.timeout_seconds == 4.25
    assert (
        context.write_preflight.transient_retry_count
        == context.dvbctrl.transient_retry_count
    )
    assert (
        context.write_preflight.transient_retry_delay_seconds
        == context.dvbctrl.transient_retry_delay_seconds
    )
    assert context.write_preflight.transient_retry_count == _dvbctrl_init_default(
        "transient_retry_count"
    )
    assert (
        context.write_preflight.transient_retry_delay_seconds
        == _dvbctrl_init_default("transient_retry_delay_seconds")
    )


def test_bootstrap_propagates_custom_dvbctrl_retry_settings(monkeypatch) -> None:
    # Non-default retry values verify bootstrap propagates custom client settings.
    class _CustomRetryDvbCtrlClient:
        def __init__(
            self,
            executable_path: str,
            host: str,
            adapter_index: int,
            timeout_seconds: float,
            transient_retry_count: int = 5,
            transient_retry_delay_seconds: float = 0.75,
        ) -> None:
            self.executable_path = executable_path
            self.host = host
            self.adapter_index = adapter_index
            self.timeout_seconds = timeout_seconds
            self.transient_retry_count = transient_retry_count
            self.transient_retry_delay_seconds = transient_retry_delay_seconds

    monkeypatch.setattr(bootstrap_module, "DvbCtrlClient", _CustomRetryDvbCtrlClient)
    monkeypatch.setattr(
        AppSettings,
        "from_env",
        classmethod(
            lambda cls: AppSettings(
                dvb_adapter_count=2,
                dvb_adapter_index=1,
                dvbctrl_path="dvbctrl",
                dvbctrl_timeout_seconds=6.0,
                dvbstreamer_bind_address="0.0.0.0",
                dvbstreamer_host="10.0.0.6",
                dvbstreamer_output_mrl="udp://239.10.10.11:1234",
                dvbstreamer_path="/opt/bin/dvbstreamer",
                dvbstreamer_stop_timeout_seconds=8.0,
            )
        ),
    )

    context = bootstrap_app()

    assert context.dvbctrl.transient_retry_count != _dvbctrl_init_default(
        "transient_retry_count"
    )
    assert context.dvbctrl.transient_retry_delay_seconds != _dvbctrl_init_default(
        "transient_retry_delay_seconds"
    )
    assert context.dvbctrl.transient_retry_count == 5
    assert context.dvbctrl.transient_retry_delay_seconds == 0.75
    assert (
        context.write_preflight.transient_retry_count
        == context.dvbctrl.transient_retry_count
    )
    assert (
        context.write_preflight.transient_retry_delay_seconds
        == context.dvbctrl.transient_retry_delay_seconds
    )
    assert context.write_preflight.transient_retry_count == 5
    assert context.write_preflight.transient_retry_delay_seconds == 0.75


def test_bootstrap_propagates_dvbctrl_init_failure(monkeypatch) -> None:
    class _FailingDvbCtrlClient:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("dvbctrl init failed")

    monkeypatch.setattr(bootstrap_module, "DvbCtrlClient", _FailingDvbCtrlClient)
    monkeypatch.setattr(
        AppSettings,
        "from_env",
        classmethod(
            lambda cls: AppSettings(
                dvb_adapter_count=1,
                dvb_adapter_index=0,
                dvbctrl_path="dvbctrl",
                dvbctrl_timeout_seconds=3.0,
                dvbstreamer_bind_address="0.0.0.0",
                dvbstreamer_host="10.0.0.7",
                dvbstreamer_output_mrl="udp://239.10.10.12:1234",
                dvbstreamer_path="/opt/bin/dvbstreamer",
                dvbstreamer_stop_timeout_seconds=8.0,
            )
        ),
    )

    with pytest.raises(RuntimeError, match="dvbctrl init failed"):
        bootstrap_app()
