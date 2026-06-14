from __future__ import annotations

from ccatv.tvrecorder.commands import (
    addsf_command,
    current_command,
    festatus_command,
    getsfavsonly_command,
    getsfmrl_command,
    lssfs_command,
    rmsf_command,
    select_command,
    setsfavsonly_command,
    setsf_command,
    setsfmrl_command,
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


def test_addsf_command_sets_null_output_by_default() -> None:
    assert addsf_command("sports filter").render() == "addsf 'sports filter' null://"


def test_rmsf_command_quotes_filter_name() -> None:
    assert rmsf_command("sports filter").render() == "rmsf 'sports filter'"


def test_lssfs_command_renders_expected_text() -> None:
    assert lssfs_command().render() == "lssfs"


def test_setsf_command_quotes_filter_and_service_names() -> None:
    assert (
        setsf_command("sports filter", "BBC ONE HD").render()
        == "setsf 'sports filter' 'BBC ONE HD'"
    )


def test_setsfmrl_command_quotes_filter_and_mrl() -> None:
    assert (
        setsfmrl_command("sports filter", "udp://239.1.1.1:1234").render()
        == "setsfmrl 'sports filter' udp://239.1.1.1:1234"
    )


def test_getsfmrl_command_quotes_filter_name() -> None:
    assert getsfmrl_command("sports filter").render() == "getsfmrl 'sports filter'"


def test_setsfavsonly_command_defaults_to_off_status() -> None:
    assert setsfavsonly_command("sports filter").render() == "setsfavsonly 'sports filter' off"


def test_setsfavsonly_command_supports_explicit_status() -> None:
    assert (
        setsfavsonly_command("sports filter", "off").render()
        == "setsfavsonly 'sports filter' off"
    )


def test_getsfavsonly_command_quotes_filter_name() -> None:
    assert getsfavsonly_command("sports filter").render() == "getsfavsonly 'sports filter'"
