from __future__ import annotations

from pathlib import Path

import pytest

from ccatv.tvrecorder.config import (
    DvbCtrlCredentials,
    TvRecorderConfig,
    TvRecorderConfigError,
    TvRecorderConfigStore,
)


def test_load_returns_default_when_file_missing(tmp_path: Path) -> None:
    store = TvRecorderConfigStore(config_dir=tmp_path)

    config = store.load()

    assert config == TvRecorderConfig()


def test_save_and_load_round_trip_credentials(tmp_path: Path) -> None:
    store = TvRecorderConfigStore(config_dir=tmp_path)
    expected = TvRecorderConfig(
        dvbctrl_credentials=DvbCtrlCredentials(
            password="secret",
            username="alice",
        )
    )

    path = store.save(expected)
    loaded = store.load()

    assert path == tmp_path / "userconfig.json"
    assert loaded == expected
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"

    raw_text = path.read_text(encoding="utf-8")
    assert '"username": "alice"' in raw_text
    assert '"password": "secret"' in raw_text
    assert '"dvbctrl"' not in raw_text


def test_load_raises_for_invalid_json(tmp_path: Path) -> None:
    store = TvRecorderConfigStore(config_dir=tmp_path)
    store.path.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(TvRecorderConfigError, match="invalid tvrecorder config"):
        store.load()


def test_load_raises_for_invalid_top_level_shape(tmp_path: Path) -> None:
    store = TvRecorderConfigStore(config_dir=tmp_path)
    store.path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(TvRecorderConfigError, match="invalid tvrecorder config shape"):
        store.load()


def test_store_defaults_to_xdg_dvbstreamer_userconfig(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-home")

    store = TvRecorderConfigStore()

    assert store.path == Path("/tmp/xdg-home/dvbstreamer/userconfig.json")
