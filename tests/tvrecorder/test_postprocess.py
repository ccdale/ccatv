from __future__ import annotations

from pathlib import Path

from ccatv.tvrecorder.postprocess import (
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
