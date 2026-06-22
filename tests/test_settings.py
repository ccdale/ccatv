from __future__ import annotations

from ccatv.runtime_config import RuntimeConfig, RuntimeConfigError, RuntimeConfigStore
from ccatv.settings import AppSettings


def test_from_env_loads_core_defaults(monkeypatch) -> None:
    monkeypatch.delenv("CCATV_LOG_LEVEL", raising=False)
    monkeypatch.delenv("CCATV_DVBCTRL_PATH", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_BIND_ADDRESS", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_HOST", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_OUTPUT_MRL", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_PATH", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_MANAGE_PROCESS", raising=False)
    monkeypatch.delenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CCATV_OTA_EPG_CHANNEL_NAME", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_INDEX", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_COUNT", raising=False)
    monkeypatch.delenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CCATV_RECORDING_PRE_START_SECONDS", raising=False)
    monkeypatch.delenv("CCATV_RECORDING_POST_FINISH_SECONDS", raising=False)
    monkeypatch.delenv("CCATV_RECORDING_EARLY_GROWTH_CHECKS", raising=False)
    monkeypatch.delenv("CCATV_RECORDING_EARLY_GROWTH_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("CCATV_RECORDING_PERIODIC_GROWTH_CHECKS", raising=False)
    monkeypatch.delenv(
        "CCATV_RECORDING_PERIODIC_GROWTH_INTERVAL_SECONDS",
        raising=False,
    )
    monkeypatch.delenv("CCATV_RECORDING_GROWTH_MIN_BYTES", raising=False)
    monkeypatch.delenv("CCATV_RECORDING_FINAL_STABILITY_CHECKS", raising=False)
    monkeypatch.delenv(
        "CCATV_RECORDING_FINAL_STABILITY_INTERVAL_SECONDS",
        raising=False,
    )
    monkeypatch.delenv("CCATV_COMSKIP_INI_PATH", raising=False)

    settings = AppSettings.from_env()

    assert settings.log_level == "INFO"
    assert settings.dvbctrl_path == "dvbctrl"
    assert settings.dvbstreamer_bind_address == "127.0.0.1"
    assert settings.dvbstreamer_host == "localhost"
    assert settings.dvbstreamer_output_mrl == "null://"
    assert settings.dvbstreamer_path == "dvbstreamer"
    assert settings.dvbstreamer_manage_process is True
    assert settings.dvbstreamer_debug_output is False
    assert settings.dvbstreamer_stop_timeout_seconds == 5.0
    assert settings.ota_epg_channel_name == "BBC TWO HD"
    assert settings.dvb_adapter_count == 1
    assert settings.dvb_adapter_index == 0
    assert settings.dvbctrl_timeout_seconds == 10.0
    assert settings.recording_pre_start_seconds == 120
    assert settings.recording_post_finish_seconds == 900
    assert settings.recording_early_growth_checks == 3
    assert settings.recording_early_growth_interval_seconds == 2.0
    assert settings.recording_periodic_growth_checks == 1
    assert settings.recording_periodic_growth_interval_seconds == 30.0
    assert settings.recording_growth_min_bytes == 1
    assert settings.recording_final_stability_checks == 2
    assert settings.recording_final_stability_interval_seconds == 2.0
    assert settings.comskip_ini_path == "/home/chris/.config/comskip/comskip.ini"


def test_from_env_parses_numeric_and_string_overrides(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_LOG_LEVEL", "debug")
    monkeypatch.setenv("CCATV_DVBCTRL_PATH", "/opt/bin/dvbctrl")
    monkeypatch.setenv("CCATV_DVBSTREAMER_BIND_ADDRESS", "0.0.0.0")
    monkeypatch.setenv("CCATV_DVBSTREAMER_HOST", "10.0.0.5")
    monkeypatch.setenv("CCATV_DVBSTREAMER_OUTPUT_MRL", "udp://239.10.10.10:1234")
    monkeypatch.setenv("CCATV_DVBSTREAMER_PATH", "/opt/bin/dvbstreamer")
    monkeypatch.setenv("CCATV_DVBSTREAMER_MANAGE_PROCESS", "false")
    monkeypatch.setenv("CCATV_DVBSTREAMER_DEBUG_OUTPUT", "true")
    monkeypatch.setenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", "6.25")
    monkeypatch.setenv("CCATV_OTA_EPG_CHANNEL_NAME", "BBC ONE East")
    monkeypatch.setenv("CCATV_DVB_ADAPTER_COUNT", "3")
    monkeypatch.setenv("CCATV_DVB_ADAPTER_INDEX", "2")
    monkeypatch.setenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "2.75")
    monkeypatch.setenv("CCATV_RECORDING_PRE_START_SECONDS", "180")
    monkeypatch.setenv("CCATV_RECORDING_POST_FINISH_SECONDS", "1200")
    monkeypatch.setenv("CCATV_RECORDING_EARLY_GROWTH_CHECKS", "4")
    monkeypatch.setenv("CCATV_RECORDING_EARLY_GROWTH_INTERVAL_SECONDS", "1.5")
    monkeypatch.setenv("CCATV_RECORDING_PERIODIC_GROWTH_CHECKS", "2")
    monkeypatch.setenv("CCATV_RECORDING_PERIODIC_GROWTH_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("CCATV_RECORDING_GROWTH_MIN_BYTES", "2048")
    monkeypatch.setenv("CCATV_RECORDING_FINAL_STABILITY_CHECKS", "3")
    monkeypatch.setenv("CCATV_RECORDING_FINAL_STABILITY_INTERVAL_SECONDS", "4")
    monkeypatch.setenv("CCATV_COMSKIP_INI_PATH", "/tmp/comskip-test.ini")

    settings = AppSettings.from_env()

    assert settings.log_level == "DEBUG"
    assert settings.dvbctrl_path == "/opt/bin/dvbctrl"
    assert settings.dvbstreamer_bind_address == "0.0.0.0"
    assert settings.dvbstreamer_host == "10.0.0.5"
    assert settings.dvbstreamer_output_mrl == "udp://239.10.10.10:1234"
    assert settings.dvbstreamer_path == "/opt/bin/dvbstreamer"
    assert settings.dvbstreamer_manage_process is False
    assert settings.dvbstreamer_debug_output is True
    assert settings.dvbstreamer_stop_timeout_seconds == 6.25
    assert settings.ota_epg_channel_name == "BBC ONE East"
    assert settings.dvb_adapter_count == 3
    assert settings.dvb_adapter_index == 2
    assert settings.dvbctrl_timeout_seconds == 2.75
    assert settings.recording_pre_start_seconds == 180
    assert settings.recording_post_finish_seconds == 1200
    assert settings.recording_early_growth_checks == 4
    assert settings.recording_early_growth_interval_seconds == 1.5
    assert settings.recording_periodic_growth_checks == 2
    assert settings.recording_periodic_growth_interval_seconds == 45.0
    assert settings.recording_growth_min_bytes == 2048
    assert settings.recording_final_stability_checks == 3
    assert settings.recording_final_stability_interval_seconds == 4.0
    assert settings.comskip_ini_path == "/tmp/comskip-test.ini"


def test_from_env_falls_back_for_invalid_numeric_values(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DVB_ADAPTER_COUNT", "not-an-int")
    monkeypatch.setenv("CCATV_DVB_ADAPTER_INDEX", "not-an-int")
    monkeypatch.setenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "not-a-float")
    monkeypatch.setenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", "not-a-float")
    monkeypatch.setenv("CCATV_RECORDING_PRE_START_SECONDS", "not-an-int")
    monkeypatch.setenv("CCATV_RECORDING_POST_FINISH_SECONDS", "not-an-int")
    monkeypatch.setenv("CCATV_RECORDING_EARLY_GROWTH_CHECKS", "not-an-int")
    monkeypatch.setenv("CCATV_RECORDING_EARLY_GROWTH_INTERVAL_SECONDS", "not-a-float")
    monkeypatch.setenv("CCATV_RECORDING_PERIODIC_GROWTH_CHECKS", "not-an-int")
    monkeypatch.setenv(
        "CCATV_RECORDING_PERIODIC_GROWTH_INTERVAL_SECONDS",
        "not-a-float",
    )
    monkeypatch.setenv("CCATV_RECORDING_GROWTH_MIN_BYTES", "not-an-int")
    monkeypatch.setenv("CCATV_RECORDING_FINAL_STABILITY_CHECKS", "not-an-int")
    monkeypatch.setenv(
        "CCATV_RECORDING_FINAL_STABILITY_INTERVAL_SECONDS",
        "not-a-float",
    )

    settings = AppSettings.from_env()

    assert settings.dvb_adapter_count == 1
    assert settings.dvb_adapter_index == 0
    assert settings.dvbctrl_timeout_seconds == 10.0
    assert settings.dvbstreamer_stop_timeout_seconds == 5.0
    assert settings.recording_pre_start_seconds == 120
    assert settings.recording_post_finish_seconds == 900
    assert settings.recording_early_growth_checks == 3
    assert settings.recording_early_growth_interval_seconds == 2.0
    assert settings.recording_periodic_growth_checks == 1
    assert settings.recording_periodic_growth_interval_seconds == 30.0
    assert settings.recording_growth_min_bytes == 1
    assert settings.recording_final_stability_checks == 2
    assert settings.recording_final_stability_interval_seconds == 2.0


def test_from_env_falls_back_for_non_positive_timeout_values(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DVBCTRL_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("CCATV_DVBSTREAMER_STOP_TIMEOUT_SECONDS", "-1")

    settings = AppSettings.from_env()

    assert settings.dvbctrl_timeout_seconds == 10.0
    assert settings.dvbstreamer_stop_timeout_seconds == 5.0


def test_from_env_falls_back_for_negative_adapter_index(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DVB_ADAPTER_INDEX", "-3")

    settings = AppSettings.from_env()

    assert settings.dvb_adapter_index == 0


def test_from_env_falls_back_for_negative_recording_policy_values(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_RECORDING_PRE_START_SECONDS", "-1")
    monkeypatch.setenv("CCATV_RECORDING_POST_FINISH_SECONDS", "-1")
    monkeypatch.setenv("CCATV_RECORDING_EARLY_GROWTH_CHECKS", "-1")
    monkeypatch.setenv("CCATV_RECORDING_EARLY_GROWTH_INTERVAL_SECONDS", "-1")
    monkeypatch.setenv("CCATV_RECORDING_PERIODIC_GROWTH_CHECKS", "-1")
    monkeypatch.setenv("CCATV_RECORDING_PERIODIC_GROWTH_INTERVAL_SECONDS", "-1")
    monkeypatch.setenv("CCATV_RECORDING_GROWTH_MIN_BYTES", "-1")
    monkeypatch.setenv("CCATV_RECORDING_FINAL_STABILITY_CHECKS", "-1")
    monkeypatch.setenv("CCATV_RECORDING_FINAL_STABILITY_INTERVAL_SECONDS", "-1")

    settings = AppSettings.from_env()

    assert settings.recording_pre_start_seconds == 120
    assert settings.recording_post_finish_seconds == 900
    assert settings.recording_early_growth_checks == 3
    assert settings.recording_early_growth_interval_seconds == 2.0
    assert settings.recording_periodic_growth_checks == 1
    assert settings.recording_periodic_growth_interval_seconds == 30.0
    assert settings.recording_growth_min_bytes == 1
    assert settings.recording_final_stability_checks == 2
    assert settings.recording_final_stability_interval_seconds == 2.0


def test_from_env_loads_host_and_adapter_count_from_runtime_config(monkeypatch) -> None:
    monkeypatch.delenv("CCATV_DVBSTREAMER_HOST", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_COUNT", raising=False)
    monkeypatch.delenv("CCATV_OTA_EPG_CHANNEL_NAME", raising=False)
    monkeypatch.setattr(
        RuntimeConfigStore,
        "load",
        lambda self: RuntimeConfig(
            dvb_adapter_count=4,
            dvbstreamer_host="druidmedia",
            ota_epg_channel_name="BBC ONE East",
        ),
    )

    settings = AppSettings.from_env()

    assert settings.dvbstreamer_host == "druidmedia"
    assert settings.ota_epg_channel_name == "BBC ONE East"
    assert settings.dvb_adapter_count == 4


def test_from_env_falls_back_when_runtime_config_load_fails(monkeypatch) -> None:
    monkeypatch.delenv("CCATV_DVBSTREAMER_HOST", raising=False)
    monkeypatch.delenv("CCATV_DVB_ADAPTER_COUNT", raising=False)
    monkeypatch.delenv("CCATV_OTA_EPG_CHANNEL_NAME", raising=False)
    monkeypatch.setattr(
        RuntimeConfigStore,
        "load",
        lambda self: (_ for _ in ()).throw(RuntimeConfigError("broken config")),
    )

    settings = AppSettings.from_env()

    assert settings.dvbstreamer_host == "localhost"
    assert settings.ota_epg_channel_name == "BBC TWO HD"
    assert settings.dvb_adapter_count == 1


def test_from_env_accepts_database_path_override(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DATABASE_PATH", "/tmp/custom-ccatv.sqlite3")

    settings = AppSettings.from_env()

    assert settings.database_path == "/tmp/custom-ccatv.sqlite3"


def test_from_env_ignores_blank_host_override(monkeypatch) -> None:
    monkeypatch.setenv("CCATV_DVBSTREAMER_HOST", "   ")
    monkeypatch.setattr(
        RuntimeConfigStore,
        "load",
        lambda self: RuntimeConfig(dvb_adapter_count=4, dvbstreamer_host="druidmedia"),
    )

    settings = AppSettings.from_env()

    assert settings.dvbstreamer_host == "druidmedia"
