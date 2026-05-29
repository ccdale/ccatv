from __future__ import annotations

import subprocess
from email.message import Message

from ccatv import _app_info_from_installed_metadata, _git_root


def test_git_root_suppresses_stderr(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _check_output_stub(*args, **kwargs):
        captured["kwargs"] = kwargs
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    monkeypatch.setattr("ccatv.subprocess.check_output", _check_output_stub)

    result = _git_root()

    assert result is None
    assert captured["kwargs"]["stderr"] is subprocess.DEVNULL


def test_app_info_from_installed_metadata_returns_none_when_missing(monkeypatch) -> None:
    def _metadata_missing(_name: str):
        raise Exception("not installed")

    monkeypatch.setattr("ccatv.importlib_metadata.metadata", _metadata_missing)

    assert _app_info_from_installed_metadata() is None


def test_app_info_from_installed_metadata_reads_version(monkeypatch) -> None:
    metadata = Message()
    metadata["Name"] = "ccatv"
    metadata["Summary"] = "TV app"

    monkeypatch.setattr("ccatv.importlib_metadata.metadata", lambda _name: metadata)
    monkeypatch.setattr("ccatv.importlib_metadata.version", lambda _name: "9.9.9")

    info = _app_info_from_installed_metadata()

    assert info is not None
    assert info.name == "ccatv"
    assert info.version == "9.9.9"
    assert info.description == "TV app"
