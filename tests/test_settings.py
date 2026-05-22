from __future__ import annotations

from ccatv.runtime_config import RuntimeConfig, RuntimeConfigStore
from ccatv.settings import AppSettings


def test_from_env_loads_core_defaults(monkeypatch) -> None:
    monkeypatch.delenv("CCATV_LOG_LEVEL", raising=False)
    monkeypatch.delenv("CCATV_DVBCTRL_PATH", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_BIND_ADDRESS", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_HOST", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_OUTPUT_MRL", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_PATH", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_INDEX", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_COUNT", raising=False)
    monkeypatch.delenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", raising=False)

    settings = AppSettings.from_env()

    assert settings.log_level == "INFO"
    assert settings.dvbctrl_path == "dvbctrl"
    assert settings.dvbstreamer_bind_address == "127.0.0.1"
    assert settings.dvbstreamer_host == "localhost"
    assert settings.dvbstreamer_output_mrl == "null://"
    assert settings.dvbstreamer_path == "dvbstreamer"
    assert settings.dvbstreamer_stop_timeout_seconds == 5.0
    assert settings.dvb_adapter_count == 1
    assert settings.dvb_adapter_index == 0
    assert settings.dvbctrl_timeout_seconds == 10.0


def test_from_env_parses_numeric_and_string_overrides(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_LOG_LEVEL", "debug")
    monkeypatch.setenv("CCATV_DVBCTRL_PATH", "/opt/bin/dvbctrl")
    monkeypatch.setenv("CCATV_DVBSTREAMER_BIND_ADDRESS", "0.0.0.0")
    monkeypatch.setenv("CCATV_DVBSTREAMER_HOST", "10.0.0.5")
    monkeypatch.setenv("CCATV_DVBSTREAMER_OUTPUT_MRL", "udp://239.10.10.10:1234")
    monkeypatch.setenv("CCATV_DVBSTREAMER_PATH", "/opt/bin/dvbstreamer")
    monkeypatch.setenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", "6.25")
    monkeypatch.setenv("CCATV_DVB_ADAPTER_COUNT", "3")
    monkeypatch.setenv("CCATV_DVB_ADAPTER_INDEX", "2")
    monkeypatch.setenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "2.75")

    settings = AppSettings.from_env()

    assert settings.log_level == "DEBUG"
    assert settings.dvbctrl_path == "/opt/bin/dvbctrl"
    assert settings.dvbstreamer_bind_address == "0.0.0.0"
    assert settings.dvbstreamer_host == "10.0.0.5"
    assert settings.dvbstreamer_output_mrl == "udp://239.10.10.10:1234"
    assert settings.dvbstreamer_path == "/opt/bin/dvbstreamer"
    assert settings.dvbstreamer_stop_timeout_seconds == 6.25
    assert settings.dvb_adapter_count == 3
    assert settings.dvb_adapter_index == 2
    assert settings.dvbctrl_timeout_seconds == 2.75


def test_from_env_falls_back_for_invalid_numeric_values(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DVB_ADAPTER_COUNT", "not-an-int")
    monkeypatch.setenv("CCATV_DVB_ADAPTER_INDEX", "not-an-int")
    monkeypatch.setenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "not-a-float")
    monkeypatch.setenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", "not-a-float")

    settings = AppSettings.from_env()

    assert settings.dvb_adapter_count == 1
    assert settings.dvb_adapter_index == 0
    assert settings.dvbctrl_timeout_seconds == 10.0
    assert settings.dvbstreamer_stop_timeout_seconds == 5.0


def test_from_env_loads_host_and_adapter_count_from_runtime_config(monkeypatch) -> None:
    monkeypatch.delenv("CCATV_DVBSTREAMER_HOST", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_COUNT", raising=False)
    monkeypatch.setattr(
        RuntimeConfigStore,
        "load",
        lambda self: RuntimeConfig(dvb_adapter_count=4, dvbstreamer_host="druidmedia"),
    )

    settings = AppSettings.from_env()

    assert settings.dvbstreamer_host == "druidmedia"
    assert settings.dvb_adapter_count == 4
