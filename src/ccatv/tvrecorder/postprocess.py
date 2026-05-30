from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol
import subprocess
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
class NfoSidecarPostProcessingRunner:
    overwrite_existing: bool = False
    run_comskip: bool = False
    comskip_command: tuple[str, ...] = (
        "/usr/bin/comskip",
        "--ini=~/.config/comskip/comskip.ini",
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


__all__ = [
    "NfoSidecarPostProcessingRunner",
    "NoOpPostProcessingRunner",
    "PostProcessingRequest",
    "PostProcessingResult",
    "PostProcessingRunner",
]
