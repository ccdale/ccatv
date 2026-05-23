from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from ccatv.storage import PersistenceStore, initialize_database
from ccatv.tvrecorder.dvbctrl import DvbCtrlClient, DvbCtrlError
from ccatv.tvrecorder.orchestrator import (
    DvbCtrlCaptureController,
    PeriodicCheckPolicy,
    RecorderOrchestrator,
)
from ccatv.tvrecorder.service import (
    RecordingHealthCheckPolicy,
    RecordingPaddingPolicy,
    TvRecorderService,
)

from .runtime import IntegrationTestConfig, build_executor

pytestmark = pytest.mark.integration

LOCK_TIMEOUT_SECONDS = 30.0
LOCK_POLL_INTERVAL_SECONDS = 1.0
RECORDING_WINDOW_SECONDS = 30.0
RECORDING_POLL_INTERVAL_SECONDS = 2.0
SAMPLE_OUTPUT_PATH_PREFIX = "/tmp/bbctwohd"
ORCHESTRATOR_OUTPUT_PATH_PREFIX = "/tmp/ccatv-orchestrator"


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


def _wait_for_dvbctrl_success(
    client: DvbCtrlClient,
    command: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    operation: str,
) -> subprocess.CompletedProcess[str] | None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no attempt executed"
    while time.monotonic() < deadline:
        try:
            result = client.run_command(command)
        except DvbCtrlError as exc:
            last_error = str(exc)
            time.sleep(poll_interval_seconds)
            continue
        return subprocess.CompletedProcess(
            args=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    raise AssertionError(
        f"{operation} did not succeed before timeout ({timeout_seconds}s): {last_error}"
    )


def _wait_for_lock(client: DvbCtrlClient) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    last_output = "no festatus output received"
    while time.monotonic() < deadline:
        try:
            festatus = client.run_command("festatus")
        except DvbCtrlError as exc:
            last_output = str(exc)
            time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            continue

        combined_output = f"{festatus.stdout}\n{festatus.stderr}".strip()
        last_output = combined_output or "(empty output)"
        normalized = combined_output.upper()
        if "FE_HAS_LOCK" in normalized:
            return
        tuner_status_lines = [
            line.strip().upper()
            for line in combined_output.splitlines()
            if line.strip().upper().startswith("TUNER STATUS")
        ]
        if any("LOCK" in line and "NO LOCK" not in line for line in tuner_status_lines):
            return
        time.sleep(LOCK_POLL_INTERVAL_SECONDS)

    raise AssertionError(
        "festatus never reported FE_HAS_LOCK before timeout. "
        f"Last output:\n{last_output}"
    )


def _stats_signal_total(stats_output: str) -> int:
    values = [int(token) for token in re.findall(r"\b\d+\b", stats_output)]
    return sum(values)


def _wait_for_stats_activity(client: DvbCtrlClient) -> None:
    baseline = client.run_command("stats")
    baseline_total = _stats_signal_total(baseline.stdout)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    last_output = baseline.stdout
    while time.monotonic() < deadline:
        time.sleep(LOCK_POLL_INTERVAL_SECONDS)
        sample = client.run_command("stats")
        sample_total = _stats_signal_total(sample.stdout)
        last_output = sample.stdout
        if sample_total > baseline_total:
            return
    raise AssertionError(
        "stats output never showed increasing activity before timeout. "
        f"Baseline total={baseline_total}. Last output:\n{last_output}"
    )


def _file_size_bytes(executor, path: str, timeout_seconds: float) -> int | None:
    result = executor.run(f"stat -c %s {path}", timeout_seconds)
    if result.returncode != 0:
        return None
    size_text = result.stdout.strip()
    if not size_text.isdigit():
        return None
    return int(size_text)


def _utc_iso_now_minus(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_live_dvbstreamer_lifecycle_smoke() -> None:
    config = IntegrationTestConfig.load()
    if not config.enabled:
        pytest.skip("Integration disabled in config (set enabled=true).")

    if config.mode == "ssh" and shutil.which("ssh") is None:
        pytest.skip("ssh executable is required for ssh integration mode")

    executor = build_executor(config)
    stop_timeout_seconds = 10.0
    connectivity_timeout_seconds = 5.0
    output_path = f"{SAMPLE_OUTPUT_PATH_PREFIX}-{uuid4().hex}.ts"
    output_mrl = f"file://{output_path}"
    quoted_output_path = shlex.quote(output_path)
    client: DvbCtrlClient | None = None

    if config.mode == "ssh":
        connectivity = executor.run("true", connectivity_timeout_seconds)
        _assert_command_ok(connectivity, operation="ssh connectivity check")

    stop_result = executor.run(config.render_stop_command(), stop_timeout_seconds)
    _assert_command_ok(stop_result, operation="pre-test stop")
    pre_start_status = executor.run(
        config.render_status_command(), stop_timeout_seconds
    )
    if pre_start_status.returncode == 0:
        raise AssertionError(
            "dvbstreamer still running after pre-test stop.\n"
            f"stdout:\n{pre_start_status.stdout.strip()}\n"
            f"stderr:\n{pre_start_status.stderr.strip()}"
        )

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

        _wait_for_dvbctrl_success(
            client,
            config.readiness_command,
            timeout_seconds=config.readiness_attempts * config.readiness_delay_seconds,
            poll_interval_seconds=config.readiness_delay_seconds,
            operation="dvbctrl readiness probe",
        )

        client.run_command('select "BBC TWO HD"')
        _wait_for_lock(client)
        _wait_for_stats_activity(client)

        client.run_command(f"setmrl {output_mrl}")
        baseline_size = _file_size_bytes(
            executor, quoted_output_path, stop_timeout_seconds
        )
        max_size = baseline_size or 0
        saw_file = baseline_size is not None
        growth_deadline = time.monotonic() + RECORDING_WINDOW_SECONDS
        while time.monotonic() < growth_deadline:
            time.sleep(RECORDING_POLL_INTERVAL_SECONDS)
            current_size = _file_size_bytes(
                executor,
                quoted_output_path,
                stop_timeout_seconds,
            )
            if current_size is None:
                continue
            saw_file = True
            max_size = max(max_size, current_size)

        if not saw_file:
            raise AssertionError(
                "recording output file was never created after setmrl file://"
            )
        if baseline_size is None:
            baseline_size = 0
        if max_size <= baseline_size:
            raise AssertionError(
                "recording output file did not grow during capture window. "
                f"baseline_size={baseline_size}, max_size={max_size}"
            )

        client.run_command("setmrl null://")
        file_type_result = executor.run(
            f"file {quoted_output_path}", stop_timeout_seconds
        )
        _assert_command_ok(file_type_result, operation="file type check")
        file_description = file_type_result.stdout.lower()
        if "mpeg" not in file_description or "transport" not in file_description:
            raise AssertionError(
                "recording file was not identified as MPEG transport stream. "
                f"file output:\n{file_type_result.stdout.strip()}"
            )
    finally:
        cleanup_errors: list[str] = []
        active_exception = sys.exc_info()[0]

        if client is not None:
            try:
                client.run_command("setmrl null://")
            except DvbCtrlError as exc:
                cleanup_errors.append(f"cleanup setmrl null failed: {exc}")

        rm_result = executor.run(f"rm -f {quoted_output_path}", stop_timeout_seconds)
        if rm_result.returncode != 0:
            cleanup_errors.append(
                "cleanup remove output file failed "
                f"(exit={rm_result.returncode}): {rm_result.stderr.strip()}"
            )

        stop_result = executor.run(config.render_stop_command(), stop_timeout_seconds)
        if stop_result.returncode != 0:
            cleanup_errors.append(
                "cleanup stop failed "
                f"(exit={stop_result.returncode}): {stop_result.stderr.strip()}"
            )

        stopped_status = executor.run(
            config.render_status_command(), stop_timeout_seconds
        )
        if stopped_status.returncode == 0:
            cleanup_errors.append(
                "dvbstreamer still running after stop.\n"
                f"stdout:\n{stopped_status.stdout.strip()}\n"
                f"stderr:\n{stopped_status.stderr.strip()}"
            )

        if cleanup_errors and active_exception is None:
            raise AssertionError("\n".join(cleanup_errors))


def test_live_orchestrator_runs_due_scheduler_job(tmp_path) -> None:
    config = IntegrationTestConfig.load()
    if not config.enabled:
        pytest.skip("Integration disabled in config (set enabled=true).")

    if config.mode == "ssh" and shutil.which("ssh") is None:
        pytest.skip("ssh executable is required for ssh integration mode")

    executor = build_executor(config)
    stop_timeout_seconds = 10.0
    connectivity_timeout_seconds = 5.0
    output_path = f"{ORCHESTRATOR_OUTPUT_PATH_PREFIX}-{uuid4().hex}.ts"
    quoted_output_path = shlex.quote(output_path)
    connection = None
    client: DvbCtrlClient | None = None

    if config.mode == "ssh":
        connectivity = executor.run("true", connectivity_timeout_seconds)
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
        _wait_for_dvbctrl_success(
            client,
            config.readiness_command,
            timeout_seconds=config.readiness_attempts * config.readiness_delay_seconds,
            poll_interval_seconds=config.readiness_delay_seconds,
            operation="dvbctrl readiness probe",
        )

        connection = initialize_database(tmp_path / "orchestrator-integration.sqlite3")
        persistence = PersistenceStore(connection=connection)
        service = TvRecorderService(
            client,
            persistence=persistence,
            padding_policy=RecordingPaddingPolicy(
                post_finish_seconds=0,
                pre_start_seconds=0,
            ),
            health_policy=RecordingHealthCheckPolicy(
                early_growth_checks=2,
                early_growth_interval_seconds=1.5,
                final_stability_checks=1,
                final_stability_interval_seconds=1.0,
                growth_min_bytes=1,
                periodic_growth_checks=1,
                periodic_growth_interval_seconds=1.0,
            ),
            file_size_reader=lambda path: _file_size_bytes(
                executor,
                shlex.quote(path),
                stop_timeout_seconds,
            ),
            sleep_fn=time.sleep,
        )
        orchestrator = RecorderOrchestrator(
            service=service,
            persistence=persistence,
            capture_controller=DvbCtrlCaptureController(service=service),
            periodic_policy=PeriodicCheckPolicy(
                growth_min_bytes=1,
                interval_seconds=4.0,
            ),
        )

        job = service.schedule_recording(
            channel_name="BBC TWO HD",
            start_at_utc=_utc_iso_now_minus(2.0),
            duration_seconds=14,
        )
        results = orchestrator.run_due_jobs(
            output_path_builder=lambda _job: output_path,
            max_jobs=1,
        )

        assert [result.job_id for result in results] == [job.id]
        assert results[0].scheduler_state == "completed"
        assert results[0].recording_state == "ready"
        assert results[0].error is None
        assert persistence.get_scheduler_job(job.id, required=True).state == "completed"
        assert results[0].recording_id is not None
        recording = persistence.get_recording(results[0].recording_id, required=True)
        assert recording.state == "ready"

        file_type_result = executor.run(
            f"file {quoted_output_path}",
            stop_timeout_seconds,
        )
        _assert_command_ok(file_type_result, operation="file type check")
        file_description = file_type_result.stdout.lower()
        if "mpeg" not in file_description or "transport" not in file_description:
            raise AssertionError(
                "orchestrator output file was not identified as MPEG transport stream. "
                f"file output:\n{file_type_result.stdout.strip()}"
            )
    finally:
        cleanup_errors: list[str] = []
        active_exception = sys.exc_info()[0]

        if client is not None:
            try:
                client.run_command("setmrl null://")
            except DvbCtrlError as exc:
                cleanup_errors.append(f"cleanup setmrl null failed: {exc}")

        if connection is not None:
            connection.close()

        rm_result = executor.run(f"rm -f {quoted_output_path}", stop_timeout_seconds)
        if rm_result.returncode != 0:
            cleanup_errors.append(
                "cleanup remove output file failed "
                f"(exit={rm_result.returncode}): {rm_result.stderr.strip()}"
            )

        stop_result = executor.run(config.render_stop_command(), stop_timeout_seconds)
        if stop_result.returncode != 0:
            cleanup_errors.append(
                "cleanup stop failed "
                f"(exit={stop_result.returncode}): {stop_result.stderr.strip()}"
            )

        stopped_status = executor.run(
            config.render_status_command(),
            stop_timeout_seconds,
        )
        if stopped_status.returncode == 0:
            cleanup_errors.append(
                "dvbstreamer still running after stop.\n"
                f"stdout:\n{stopped_status.stdout.strip()}\n"
                f"stderr:\n{stopped_status.stderr.strip()}"
            )

        if cleanup_errors and active_exception is None:
            raise AssertionError("\n".join(cleanup_errors))
