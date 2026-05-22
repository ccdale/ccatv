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

    assert path == tmp_path / "tvrecorder.json"
    assert loaded == expected
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"


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
