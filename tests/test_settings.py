from __future__ import annotations

from ccatv.settings import AppSettings


def test_from_env_loads_core_defaults(monkeypatch) -> None:
    monkeypatch.delenv("CCATV_LOG_LEVEL", raising=False)
    monkeypatch.delenv("CCATV_DVBCTRL_PATH", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_HOST", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_INDEX", raising=False)
    monkeypatch.delenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", raising=False)

    settings = AppSettings.from_env()

    assert settings.log_level == "INFO"
    assert settings.dvbctrl_path == "dvbctrl"
    assert settings.dvbstreamer_host == "localhost"
    assert settings.dvb_adapter_index == 0
    assert settings.dvbctrl_timeout_seconds == 10.0


def test_from_env_parses_numeric_and_string_overrides(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_LOG_LEVEL", "debug")
    monkeypatch.setenv("CCATV_DVBCTRL_PATH", "/opt/bin/dvbctrl")
    monkeypatch.setenv("CCATV_DVBSTREAMER_HOST", "10.0.0.5")
    monkeypatch.setenv("CCATV_DVB_ADAPTER_INDEX", "2")
    monkeypatch.setenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "2.75")

    settings = AppSettings.from_env()

    assert settings.log_level == "DEBUG"
    assert settings.dvbctrl_path == "/opt/bin/dvbctrl"
    assert settings.dvbstreamer_host == "10.0.0.5"
    assert settings.dvb_adapter_index == 2
    assert settings.dvbctrl_timeout_seconds == 2.75


def test_from_env_falls_back_for_invalid_numeric_values(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DVB_ADAPTER_INDEX", "not-an-int")
    monkeypatch.setenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "not-a-float")

    settings = AppSettings.from_env()

    assert settings.dvb_adapter_index == 0
    assert settings.dvbctrl_timeout_seconds == 10.0
