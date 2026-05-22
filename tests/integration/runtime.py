from __future__ import annotations

import json
import os
import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CONFIG_ENV_VAR = "CCATV_INTEGRATION_CONFIG"


@dataclass(frozen=True, slots=True)
class IntegrationTestConfig:
    enabled: bool = False
    mode: Literal["local", "ssh"] = "ssh"
    remote_host: str = "druidmedia"
    remote_user: str = "chris"
    remote_port: int = 22
    remote_workdir: str | None = "~"
    dvbstreamer_host: str = "druidmedia"
    dvb_adapter_count: int = 1
    dvb_adapter_index: int = 0
    dvbctrl_path: str = "dvbctrl"
    dvbctrl_timeout_seconds: float = 10.0
    readiness_command: str = "lsmuxes"
    readiness_attempts: int = 10
    readiness_delay_seconds: float = 1.0
    start_timeout_seconds: float = 20.0
    start_command: str = (
        "nohup dvbstreamer -a {adapter_index} -i 0.0.0.0 -o null:// "
        ">/tmp/ccatv-dvbstreamer.log 2>&1 &"
    )
    stop_command: str = "pkill -f 'dvbstreamer -a {adapter_index}' || true"
    status_command: str = "pgrep -f 'dvbstreamer -a {adapter_index}'"

    @classmethod
    def load(cls, path: Path | None = None) -> IntegrationTestConfig:
        config_path = path or _default_config_path()
        if not config_path.exists():
            return cls()

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Integration config must be an object: {config_path}")

        allowed_keys = {field.name for field in cls.__dataclass_fields__.values()}
        values = {key: raw[key] for key in raw if key in allowed_keys}
        if values.get("remote_workdir", None) == "":
            values["remote_workdir"] = None
        return cls(**values)

    def render_start_command(self) -> str:
        return self._render_command(self.start_command)

    def render_stop_command(self) -> str:
        return self._render_command(self.stop_command)

    def render_status_command(self) -> str:
        return self._render_command(self.status_command)

    def _render_command(self, template: str) -> str:
        try:
            return template.format(
                adapter_count=self.dvb_adapter_count,
                adapter_index=self.dvb_adapter_index,
                host=self.dvbstreamer_host,
            )
        except KeyError as exc:
            missing_key = exc.args[0]
            raise ValueError(
                "Unsupported placeholder in integration command template: "
                f"{missing_key}. Supported placeholders are "
                "adapter_count, adapter_index, host."
            ) from exc


class CommandExecutor(ABC):
    @abstractmethod
    def run(
        self, command: str, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and return the completed process result."""


@dataclass(frozen=True, slots=True)
class LocalCommandExecutor(CommandExecutor):
    def run(
        self, command: str, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class SshCommandExecutor(CommandExecutor):
    host: str
    user: str
    port: int
    workdir: str | None = None

    def run(
        self, command: str, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        remote_command = command
        if self.workdir:
            remote_command = f"cd {shlex.quote(self.workdir)} && {command}"

        target = f"{self.user}@{self.host}"
        return subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-p",
                str(self.port),
                target,
                f"bash -lc {shlex.quote(remote_command)}",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )


def build_executor(config: IntegrationTestConfig) -> CommandExecutor:
    if config.mode == "local":
        return LocalCommandExecutor()
    return SshCommandExecutor(
        host=config.remote_host,
        user=config.remote_user,
        port=config.remote_port,
        workdir=config.remote_workdir,
    )


def _default_config_path() -> Path:
    override = os.getenv(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()

    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        base = Path(xdg_config_home)
    else:
        base = Path.home() / ".config"
    return base / "ccatv" / "integration.json"
