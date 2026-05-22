from __future__ import annotations

from pathlib import Path

import pytest

from ccatv.runtime_config import RuntimeConfig, RuntimeConfigError, RuntimeConfigStore


def test_runtime_store_loads_defaults_when_missing(tmp_path: Path) -> None:
    store = RuntimeConfigStore(config_dir=tmp_path)

    loaded = store.load()

    assert loaded == RuntimeConfig()


def test_runtime_store_round_trip(tmp_path: Path) -> None:
    store = RuntimeConfigStore(config_dir=tmp_path)
    expected = RuntimeConfig(dvb_adapter_count=4, dvbstreamer_host="druidmedia")

    path = store.save(expected)
    loaded = store.load()

    assert path == tmp_path / "runtime.json"
    assert loaded == expected
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(tmp_path.stat().st_mode & 0o777) == "0o700"


def test_runtime_store_rejects_invalid_shape(tmp_path: Path) -> None:
    store = RuntimeConfigStore(config_dir=tmp_path)
    store.path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="invalid runtime config shape"):
        store.load()


def test_runtime_store_rejects_invalid_adapter_count(tmp_path: Path) -> None:
    store = RuntimeConfigStore(config_dir=tmp_path)
    store.path.write_text(
        '{"dvb_adapter_count": 0, "dvbstreamer_host": "druidmedia"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="invalid dvb_adapter_count"):
        store.load()
