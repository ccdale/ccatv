from __future__ import annotations

from pathlib import Path
import subprocess

from ccatv.tvrecorder.postprocess import (
    ChainedPostProcessingRunner,
    MoveToNasPostProcessingRunner,
    NfoSidecarPostProcessingRunner,
    PostProcessingRequest,
)


def test_nfo_sidecar_postprocessor_writes_metadata(tmp_path: Path) -> None:
    output_path = tmp_path / "recordings" / "talking-pictures-tv-16-20260529T202441Z.ts"
    request = PostProcessingRequest(
        recording_id=16,
        channel_name="Talking Pictures TV",
        output_path=str(output_path),
        program_title="The Saint",
        program_description="Simon Templar investigates a mystery.",
        program_start_at_utc="2026-05-29T20:30:00Z",
        program_stop_at_utc="2026-05-29T21:30:00Z",
    )

    result = NfoSidecarPostProcessingRunner().run(request)

    assert result.success is True
    nfo_path = output_path.with_suffix(".nfo")
    assert nfo_path.exists()
    body = nfo_path.read_text(encoding="utf-8")
    assert "<title>The Saint</title>" in body
    assert "<showtitle>Talking Pictures TV</showtitle>" in body
    assert "<plot>Simon Templar investigates a mystery.</plot>" in body
    assert "<aired>2026-05-29</aired>" in body
    assert "<endtime>2026-05-29T21:30:00Z</endtime>" in body


def test_nfo_sidecar_postprocessor_does_not_overwrite_by_default(tmp_path: Path) -> None:
    output_path = tmp_path / "recordings" / "sample.ts"
    nfo_path = output_path.with_suffix(".nfo")
    nfo_path.parent.mkdir(parents=True, exist_ok=True)
    nfo_path.write_text("existing", encoding="utf-8")

    request = PostProcessingRequest(
        recording_id=1,
        channel_name="BBC TWO HD",
        output_path=str(output_path),
        program_title="Newsnight",
    )

    result = NfoSidecarPostProcessingRunner().run(request)

    assert result.success is True
    assert nfo_path.read_text(encoding="utf-8") == "existing"


def test_nfo_sidecar_postprocessor_can_overwrite_existing(tmp_path: Path) -> None:
    output_path = tmp_path / "recordings" / "sample.ts"
    nfo_path = output_path.with_suffix(".nfo")
    nfo_path.parent.mkdir(parents=True, exist_ok=True)
    nfo_path.write_text("existing", encoding="utf-8")

    request = PostProcessingRequest(
        recording_id=1,
        channel_name="BBC TWO HD",
        output_path=str(output_path),
        program_title="Newsnight",
    )

    result = NfoSidecarPostProcessingRunner(overwrite_existing=True).run(request)

    assert result.success is True
    assert "<title>Newsnight</title>" in nfo_path.read_text(encoding="utf-8")


def test_nfo_sidecar_postprocessor_runs_comskip_with_requested_command(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "recordings" / "sample.ts"
    captured: list[list[str]] = []

    def _runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    request = PostProcessingRequest(
        recording_id=1,
        channel_name="BBC TWO HD",
        output_path=str(output_path),
        program_title="Newsnight",
    )

    result = NfoSidecarPostProcessingRunner(
        run_comskip=True,
        comskip_command=(
            "/usr/bin/comskip",
            "--ini=~/.config/comskip/comskip.ini",
        ),
        process_runner=_runner,
    ).run(request)

    assert result.success is True
    assert captured == [
        [
            "/usr/bin/comskip",
            f"--ini={Path.home()}/.config/comskip/comskip.ini",
            str(output_path),
        ]
    ]


def test_nfo_sidecar_postprocessor_handles_missing_comskip_executable(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "recordings" / "sample.ts"

    def _runner(_command: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError()

    request = PostProcessingRequest(
        recording_id=1,
        channel_name="BBC TWO HD",
        output_path=str(output_path),
        program_title="Newsnight",
    )

    result = NfoSidecarPostProcessingRunner(
        run_comskip=True,
        process_runner=_runner,
    ).run(request)

    assert result.success is True
    assert result.message is not None
    assert "comskip executable not found" in result.message


def test_move_to_nas_postprocessor_moves_related_files_with_sanitized_path(
    tmp_path: Path,
) -> None:
    recordings_dir = tmp_path / "recordings"
    output_path = recordings_dir / "my-show.ts"
    nfo_path = output_path.with_suffix(".nfo")
    edl_path = output_path.with_suffix(".edl")
    recordings_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text("video", encoding="utf-8")
    nfo_path.write_text("<episodedetails />", encoding="utf-8")
    edl_path.write_text("0\t0\t0", encoding="utf-8")

    request = PostProcessingRequest(
        recording_id=7,
        channel_name="BBC ONE / HD",
        output_path=str(output_path),
        program_title="Newsnight: Episode 1?",
    )
    destination_root = tmp_path / "nas" / "ccatv"

    result = MoveToNasPostProcessingRunner(destination_root=str(destination_root)).run(
        request
    )

    assert result.success is True
    destination_dir = destination_root / "BBC ONE HD" / "Newsnight Episode 1"
    assert (destination_dir / "my-show.ts").exists()
    assert (destination_dir / "my-show.nfo").exists()
    assert (destination_dir / "my-show.edl").exists()
    assert not output_path.exists()
    assert not nfo_path.exists()
    assert not edl_path.exists()


def test_chained_postprocessor_writes_nfo_then_moves_to_nas(tmp_path: Path) -> None:
    output_path = tmp_path / "recordings" / "sample.ts"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("video", encoding="utf-8")

    request = PostProcessingRequest(
        recording_id=8,
        channel_name="Talking Pictures TV",
        output_path=str(output_path),
        program_title="The Saint",
        program_description="Simon Templar investigates a mystery.",
        program_start_at_utc="2026-05-29T20:30:00Z",
        program_stop_at_utc="2026-05-29T21:30:00Z",
    )

    destination_root = tmp_path / "nas" / "ccatv"
    runner = ChainedPostProcessingRunner(
        runners=(
            NfoSidecarPostProcessingRunner(),
            MoveToNasPostProcessingRunner(destination_root=str(destination_root)),
        )
    )

    result = runner.run(request)

    assert result.success is True
    final_dir = destination_root / "Talking Pictures TV" / "The Saint"
    assert (final_dir / "sample.ts").exists()
    final_nfo = final_dir / "sample.nfo"
    assert final_nfo.exists()
    nfo_body = final_nfo.read_text(encoding="utf-8")
    assert "<title>The Saint</title>" in nfo_body
    assert "<showtitle>Talking Pictures TV</showtitle>" in nfo_body
