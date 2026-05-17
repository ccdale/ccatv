from __future__ import annotations

from ccatv.tvrecorder.commands import (
    current_command,
    festatus_command,
    select_command,
    stats_command,
)


def test_current_command_renders_expected_text() -> None:
    assert current_command().render() == "current"


def test_stats_command_renders_expected_text() -> None:
    assert stats_command().render() == "stats"


def test_festatus_command_renders_expected_text() -> None:
    assert festatus_command().render() == "festatus"


def test_select_command_quotes_service_name() -> None:
    assert select_command("BBC ONE HD").render() == "select 'BBC ONE HD'"
