from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlError

from .runtime import IntegrationTestConfig, build_executor

pytestmark = pytest.mark.integration


def _assert_command_ok(
    result: subprocess.CompletedProcess[str], *, operation: str
) -> None:
    if result.returncode == 0:
        return
    details = (
        f"{operation} failed with exit code {result.returncode}.\n"
        f"stdout:\n{result.stdout.strip()}\n"
        f"stderr:\n{result.stderr.strip()}"
    )
    raise AssertionError(details)


def test_live_dvbstreamer_lifecycle_smoke() -> None:
    config = IntegrationTestConfig.load()
    if not config.enabled:
        pytest.skip("Integration disabled in config (set enabled=true).")

    if config.mode == "ssh" and shutil.which("ssh") is None:
        pytest.skip("ssh executable is required for ssh integration mode")

    executor = build_executor(config)
    stop_timeout_seconds = 10.0

    if config.mode == "ssh":
        connectivity = executor.run("true", stop_timeout_seconds)
        _assert_command_ok(connectivity, operation="ssh connectivity check")

    stop_result = executor.run(config.render_stop_command(), stop_timeout_seconds)
    _assert_command_ok(stop_result, operation="pre-test stop")

    try:
        start_result = executor.run(
            config.render_start_command(),
            config.start_timeout_seconds,
        )
        _assert_command_ok(start_result, operation="start")

        client = DvbCtrlClient(
            executable_path=config.dvbctrl_path,
            host=config.dvbstreamer_host,
            adapter_index=config.dvb_adapter_index,
            timeout_seconds=config.dvbctrl_timeout_seconds,
        )

        last_error = "no readiness attempt executed"
        for _ in range(config.readiness_attempts):
            try:
                probe = client.run_command(config.readiness_command)
            except DvbCtrlError as exc:
                last_error = str(exc)
                time.sleep(config.readiness_delay_seconds)
                continue
            if probe.returncode != 0:
                last_error = (
                    f"returncode={probe.returncode}, stderr={probe.stderr.strip()}"
                )
                time.sleep(config.readiness_delay_seconds)
                continue
            break
        else:
            raise AssertionError(
                "dvbstreamer never became ready for dvbctrl probe: "
                f"{config.readiness_command}; last_error={last_error}"
            )
    finally:
        stop_result = executor.run(config.render_stop_command(), stop_timeout_seconds)
        _assert_command_ok(stop_result, operation="stop")
