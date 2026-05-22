from __future__ import annotations

from ccatv.settings import AppSettings
from ccatv.tvrecorder.config import (
    DvbCtrlCredentials,
    TvRecorderConfig,
    TvRecorderConfigStore,
)


def test_from_env_loads_dvbctrl_credentials_from_config(monkeypatch) -> None:
    monkeypatch.delenv("CCATV_DVBCTRL_USERNAME", raising=False)
    monkeypatch.delenv("CCATV_DVBCTRL_PASSWORD", raising=False)
    monkeypatch.setattr(
        TvRecorderConfigStore,
        "load",
        lambda self: TvRecorderConfig(
            dvbctrl_credentials=DvbCtrlCredentials(
                password="config-pass",
                username="config-user",
            )
        ),
    )

    settings = AppSettings.from_env()

    assert settings.dvbctrl_username == "config-user"
    assert settings.dvbctrl_password == "config-pass"


def test_from_env_prefers_environment_over_config(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DVBCTRL_USERNAME", "env-user")
    monkeypatch.setenv("CCATV_DVBCTRL_PASSWORD", "env-pass")
    monkeypatch.setattr(
        TvRecorderConfigStore,
        "load",
        lambda self: TvRecorderConfig(
            dvbctrl_credentials=DvbCtrlCredentials(
                password="config-pass",
                username="config-user",
            )
        ),
    )

    settings = AppSettings.from_env()

    assert settings.dvbctrl_username == "env-user"
    assert settings.dvbctrl_password == "env-pass"
