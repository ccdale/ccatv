from __future__ import annotations

from ccatv.app.bootstrap import bootstrap_app
from ccatv.settings import AppSettings


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
    assert context.write_preflight.transient_retry_count == 2
    assert context.write_preflight.transient_retry_delay_seconds == 0.2
