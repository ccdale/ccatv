from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
import re
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET


@dataclass(frozen=True, slots=True)
class PostProcessingRequest:
    recording_id: int
    channel_name: str
    output_path: str
    program_title: str | None = None
    program_description: str | None = None
    program_start_at_utc: str | None = None
    program_stop_at_utc: str | None = None


@dataclass(frozen=True, slots=True)
class PostProcessingResult:
    success: bool
    message: str | None = None


class PostProcessingRunner(Protocol):
    def run(self, request: PostProcessingRequest) -> PostProcessingResult: ...


@dataclass(slots=True)
class NoOpPostProcessingRunner:
    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        return PostProcessingResult(
            success=True, message="no post-processing configured"
        )


@dataclass(slots=True)
class SerializedPostProcessingRunner:
    """Wraps another runner so that only one post-processing job runs at a time."""

    runner: PostProcessingRunner
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        with self._lock:
            return self.runner.run(request)


@dataclass(slots=True)
class ChainedPostProcessingRunner:
    runners: tuple[PostProcessingRunner, ...]

    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        messages: list[str] = []
        for runner in self.runners:
            result = runner.run(request)
            if result.message:
                messages.append(result.message)
            if not result.success:
                return PostProcessingResult(
                    success=False,
                    message="; ".join(messages) or result.message,
                )
        return PostProcessingResult(success=True, message="; ".join(messages) or None)


@dataclass(slots=True)
class NfoSidecarPostProcessingRunner:
    overwrite_existing: bool = False
    run_comskip: bool = False
    comskip_command: tuple[str, ...] = (
        "/usr/bin/comskip",
        "--ini=/home/chris/.config/comskip/comskip.ini",
    )
    process_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None

    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        nfo_path = Path(request.output_path).with_suffix(".nfo")
        messages: list[str] = []
        if nfo_path.exists() and not self.overwrite_existing:
            messages.append(f"nfo sidecar already exists: {nfo_path}")
        else:
            root = ET.Element("episodedetails")
            ET.SubElement(root, "title").text = (
                request.program_title
                or Path(request.output_path).stem
                or f"recording-{request.recording_id}"
            )
            ET.SubElement(root, "showtitle").text = request.channel_name

            if request.program_description:
                ET.SubElement(root, "plot").text = request.program_description
            if request.program_start_at_utc:
                ET.SubElement(root, "aired").text = _iso_date(request.program_start_at_utc)
                ET.SubElement(root, "premiered").text = _iso_date(
                    request.program_start_at_utc
                )
            if request.program_start_at_utc:
                ET.SubElement(root, "dateadded").text = request.program_start_at_utc
            if request.program_stop_at_utc:
                ET.SubElement(root, "endtime").text = request.program_stop_at_utc
            ET.SubElement(root, "studio").text = request.channel_name

            tree = ET.ElementTree(root)
            ET.indent(tree, space="  ")
            nfo_path.parent.mkdir(parents=True, exist_ok=True)
            tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
            messages.append(f"wrote nfo: {nfo_path}")

        if self.run_comskip:
            comskip_message = self._run_comskip(request.output_path)
            if comskip_message:
                messages.append(comskip_message)

        return PostProcessingResult(success=True, message="; ".join(messages) or None)

    def _run_comskip(self, output_path: str) -> str:
        command = [*self.comskip_command, output_path]
        expanded_command = [self._expand_command_part(part) for part in command]
        runner = self.process_runner or _run_process
        try:
            result = runner(expanded_command)
        except FileNotFoundError:
            return f"comskip executable not found: {expanded_command[0]}"

        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else ""
            if stderr:
                return f"comskip failed ({result.returncode}): {stderr}"
            return f"comskip failed ({result.returncode})"
        return "comskip complete"

    def _expand_command_part(self, part: str) -> str:
        if part.startswith("~"):
            return str(Path(part).expanduser())
        if part.startswith("--ini=~"):
            ini_value = part.split("=", 1)[1]
            return f"--ini={Path(ini_value).expanduser()}"
        return part


@dataclass(slots=True)
class MoveToNasPostProcessingRunner:
    destination_root: str = "/mnt/nas/ccatv"
    title_fallback: str = "untitled"

    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        source_path = Path(request.output_path)
        channel_dir = _sanitize_path_component(request.channel_name, fallback="channel")
        title_value = request.program_title or source_path.stem or self.title_fallback
        title_dir = _sanitize_path_component(title_value, fallback=self.title_fallback)
        destination_dir = Path(self.destination_root) / title_dir / channel_dir
        destination_dir.mkdir(parents=True, exist_ok=True)

        files_to_move = _collect_related_output_files(source_path)
        if not files_to_move:
            return PostProcessingResult(
                success=False,
                message=f"no recording output files found to move for {source_path}",
            )

        moved_paths: list[Path] = []
        for file_path in files_to_move:
            target_path = _next_available_destination(destination_dir / file_path.name)
            shutil.move(str(file_path), str(target_path))
            moved_paths.append(target_path)

        return PostProcessingResult(
            success=True,
            message=(
                f"moved {len(moved_paths)} recording file(s) to {destination_dir}"
            ),
        )


@dataclass(slots=True)
class FfmpegTranscodePostProcessingRunner:
    """Remux a .ts recording into a .mkv container using stream copy.

    After a successful transcode, the durations of the source and output are
    compared via ffprobe.  If they differ by more than *duration_tolerance_seconds*
    the result is a failure and the source is preserved.
    """

    ffmpeg_command: tuple[str, ...] = ("ffmpeg",)
    ffprobe_command: tuple[str, ...] = ("ffprobe",)
    delete_source: bool = False
    duration_tolerance_seconds: float = 2.0
    process_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None

    def run(self, request: PostProcessingRequest) -> PostProcessingResult:
        source_path = Path(request.output_path)
        output_path = source_path.with_suffix(".mkv")
        command = [
            *self.ffmpeg_command,
            "-y",
            "-i", str(source_path),
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "copy",
            str(output_path),
        ]
        runner = self.process_runner or _run_process
        try:
            result = runner(command)
        except FileNotFoundError:
            return PostProcessingResult(
                success=False,
                message=f"ffmpeg executable not found: {command[0]}",
            )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            msg = f"ffmpeg failed ({result.returncode})"
            if stderr:
                msg = f"{msg}: {stderr[-500:]}"
            return PostProcessingResult(success=False, message=msg)

        duration_check = self._check_durations(source_path, output_path, runner)
        if duration_check is not None:
            return duration_check

        if self.delete_source and source_path.exists():
            source_path.unlink()
        return PostProcessingResult(
            success=True,
            message=f"transcoded to mkv: {output_path}",
        )

    def _probe_duration(
        self,
        path: Path,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
    ) -> float | None:
        """Return duration in seconds from ffprobe, or None on any failure."""
        command = [
            *self.ffprobe_command,
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        try:
            result = runner(command)
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        try:
            return float((result.stdout or "").strip())
        except ValueError:
            return None

    def _check_durations(
        self,
        source_path: Path,
        output_path: Path,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]],
    ) -> PostProcessingResult | None:
        """Return a failure result if duration mismatch detected, else None."""
        source_duration = self._probe_duration(source_path, runner)
        output_duration = self._probe_duration(output_path, runner)

        if source_duration is None or output_duration is None:
            return PostProcessingResult(
                success=False,
                message=(
                    f"duration probe failed: source={source_duration} output={output_duration}"
                ),
            )

        delta = abs(source_duration - output_duration)
        if delta > self.duration_tolerance_seconds:
            return PostProcessingResult(
                success=False,
                message=(
                    f"duration mismatch: source={source_duration:.3f}s "
                    f"output={output_duration:.3f}s delta={delta:.3f}s "
                    f"(tolerance={self.duration_tolerance_seconds}s)"
                ),
            )
        return None


def _run_process(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
    )


def _iso_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d")


def _collect_related_output_files(source_path: Path) -> list[Path]:
    if not source_path.parent.exists():
        return []
    related = [
        candidate
        for candidate in source_path.parent.iterdir()
        if candidate.is_file() and candidate.stem == source_path.stem
    ]
    related.sort()
    return related


def _sanitize_path_component(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._ -]+", " ", value).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip(" .")
    if not normalized:
        return fallback
    return normalized


def _next_available_destination(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


__all__ = [
    "ChainedPostProcessingRunner",
    "FfmpegTranscodePostProcessingRunner",
    "MoveToNasPostProcessingRunner",
    "NfoSidecarPostProcessingRunner",
    "NoOpPostProcessingRunner",
    "PostProcessingRequest",
    "PostProcessingResult",
    "PostProcessingRunner",
    "SerializedPostProcessingRunner",
]
