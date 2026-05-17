from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ccatv.tvrecorder.commands import DvbCtrlCommand
from ccatv.tvrecorder.dvbctrl import DvbCtrlResult
from ccatv.tvrecorder.service import TvRecorderService


FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(slots=True)
class StubDvbCtrlClient:
    responses: dict[str, DvbCtrlResult] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)

    def run_command(self, command: str) -> DvbCtrlResult:
        self.commands.append(command)
        return self.responses[command]


def _result(command: str, stdout: str) -> DvbCtrlResult:
    return DvbCtrlResult(
        command=("dvbctrl", *command.split()),
        returncode=0,
        stdout=stdout,
        stderr="",
    )


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_typed_command_render_quotes_arguments() -> None:
    cmd = DvbCtrlCommand(name="select", args=("BBC ONE HD",))
    assert cmd.render() == "select 'BBC ONE HD'"


def test_current_status_prefers_kv_service_field() -> None:
    client = StubDvbCtrlClient(
        responses={
            "current": _result("current", _fixture("current_output.txt")),
        }
    )
    service = TvRecorderService(client)

    status = service.current_status()

    assert status.service_name == "BBC TWO HD"
    assert status.fields["service"] == "BBC TWO HD"


def test_stats_snapshot_coerces_numeric_values() -> None:
    client = StubDvbCtrlClient(
        responses={
            "stats": _result("stats", _fixture("stats_output.txt")),
        }
    )
    service = TvRecorderService(client)

    snapshot = service.stats_snapshot()

    assert snapshot.metrics["packets"] == 12345
    assert snapshot.metrics["dropped packets"] == 12
    assert snapshot.metrics["rate"] == 5.5
    assert snapshot.metrics["state"] == "good"


def test_frontend_status_extracts_lock_and_signal_fields() -> None:
    client = StubDvbCtrlClient(
        responses={
            "festatus": _result(
                "festatus",
                _fixture("festatus_output.txt"),
            )
        }
    )
    service = TvRecorderService(client)

    status = service.frontend_status()

    assert status.locked is True
    assert status.signal == 78
    assert status.snr == 34
    assert status.ber == 0


def test_select_service_uses_typed_command_path() -> None:
    client = StubDvbCtrlClient(
        responses={
            "select 'BBC ONE HD'": _result("select", "ok\n"),
        }
    )
    service = TvRecorderService(client)

    service.select_service("BBC ONE HD")

    assert client.commands == ["select 'BBC ONE HD'"]
