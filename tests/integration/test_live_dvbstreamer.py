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
MULTI_ADAPTER_RECORDING_WINDOW_SECONDS = 20.0
MULTI_ADAPTER_POLL_INTERVAL_SECONDS = 2.0
MULTI_ADAPTER_OUTPUT_PATH_PREFIX = "/tmp/ccatv-multi-adapter"


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


def _render_command_for_adapter(config: IntegrationTestConfig, template: str, adapter: int) -> str:
    values = {
        "adapter_count": str(config.dvb_adapter_count),
        "adapter_index": str(adapter),
        "host": shlex.quote(config.dvbstreamer_host),
    }
    return template.format(**values)


def _extract_services_by_mux(serviceinfo_output: str) -> dict[str, list[str]]:
    services_by_mux: dict[str, list[str]] = {}
    current_name: str | None = None
    current_mux: str | None = None

    for raw_line in serviceinfo_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        name_match = re.search(r"(?i)\b(?:service|name)\b\s*[:=]\s*(.+)", line)
        mux_match = re.search(r"(?i)\bmux(?:\s+id)?\b\s*[:=]\s*(.+)", line)

        if name_match:
            current_name = name_match.group(1).strip().strip('"')
        if mux_match:
            current_mux = mux_match.group(1).strip()

        if current_name and current_mux:
            services_by_mux.setdefault(current_mux, [])
            if current_name not in services_by_mux[current_mux]:
                services_by_mux[current_mux].append(current_name)
            current_name = None
            current_mux = None

    return services_by_mux


def _discover_channels_on_distinct_muxes(client: DvbCtrlClient) -> list[str]:
    try:
        serviceinfo = client.run_command("serviceinfo")
    except DvbCtrlError:
        return []

    services_by_mux = _extract_services_by_mux(serviceinfo.stdout)
    channels: list[str] = []
    for mux in sorted(services_by_mux.keys()):
        if not services_by_mux[mux]:
            continue
        channels.append(services_by_mux[mux][0])
        if len(channels) >= 4:
            break
    return channels


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


def test_live_multi_adapter_parallel_recording_distinct_muxes() -> None:
    config = IntegrationTestConfig.load()
    if not config.enabled:
        pytest.skip("Integration disabled in config (set enabled=true).")

    if config.dvb_adapter_count < 4:
        pytest.skip(
            "Multi-adapter integration requires at least 4 adapters "
            f"(configured={config.dvb_adapter_count})."
        )

    if config.mode == "ssh" and shutil.which("ssh") is None:
        pytest.skip("ssh executable is required for ssh integration mode")

    executor = build_executor(config)
    stop_timeout_seconds = 10.0
    adapters = [0, 1, 2, 3]
    clients: list[DvbCtrlClient] = []
    output_paths = {
        adapter: f"{MULTI_ADAPTER_OUTPUT_PATH_PREFIX}-a{adapter}-{uuid4().hex}.ts"
        for adapter in adapters
    }

    def _stop_adapter(adapter: int) -> None:
        stop_command = _render_command_for_adapter(config, config.stop_command, adapter)
        stop_result = executor.run(stop_command, stop_timeout_seconds)
        _assert_command_ok(stop_result, operation=f"stop adapter {adapter}")

    for adapter in adapters:
        _stop_adapter(adapter)

    try:
        for adapter in adapters:
            start_command = _render_command_for_adapter(
                config,
                config.start_command,
                adapter,
            )
            start_result = executor.run(start_command, config.start_timeout_seconds)
            _assert_command_ok(start_result, operation=f"start adapter {adapter}")

            client = DvbCtrlClient(
                executable_path=config.dvbctrl_path,
                host=config.dvbstreamer_host,
                adapter_index=adapter,
                timeout_seconds=config.dvbctrl_timeout_seconds,
            )
            _wait_for_dvbctrl_success(
                client,
                config.readiness_command,
                timeout_seconds=(
                    config.readiness_attempts * config.readiness_delay_seconds
                ),
                poll_interval_seconds=config.readiness_delay_seconds,
                operation=f"dvbctrl readiness probe (adapter {adapter})",
            )
            clients.append(client)

        channels = _discover_channels_on_distinct_muxes(clients[0])
        if len(channels) < 4:
            pytest.skip(
                "Could not discover 4 distinct-mux channels via `serviceinfo`; "
                "update broadcast data or provide channel discovery support."
            )

        baseline_sizes: dict[int, int] = {}
        saw_file: dict[int, bool] = {adapter: False for adapter in adapters}
        max_sizes: dict[int, int] = {adapter: 0 for adapter in adapters}

        for adapter, client in zip(adapters, clients, strict=True):
            channel_name = channels[adapter]
            client.run_command(f"select {shlex.quote(channel_name)}")
            _wait_for_lock(client)
            _wait_for_stats_activity(client)
            client.run_command(f"setmrl file://{output_paths[adapter]}")

            size = _file_size_bytes(
                executor,
                shlex.quote(output_paths[adapter]),
                stop_timeout_seconds,
            )
            baseline_sizes[adapter] = size or 0
            if size is not None:
                saw_file[adapter] = True
                max_sizes[adapter] = size

        growth_deadline = time.monotonic() + MULTI_ADAPTER_RECORDING_WINDOW_SECONDS
        while time.monotonic() < growth_deadline:
            time.sleep(MULTI_ADAPTER_POLL_INTERVAL_SECONDS)
            for adapter in adapters:
                current_size = _file_size_bytes(
                    executor,
                    shlex.quote(output_paths[adapter]),
                    stop_timeout_seconds,
                )
                if current_size is None:
                    continue
                saw_file[adapter] = True
                max_sizes[adapter] = max(max_sizes[adapter], current_size)

        for client in clients:
            client.run_command("setmrl null://")

        for adapter in adapters:
            if not saw_file[adapter]:
                raise AssertionError(
                    f"recording output file was never created for adapter {adapter}"
                )
            if max_sizes[adapter] <= baseline_sizes[adapter]:
                raise AssertionError(
                    "recording output file did not grow for adapter "
                    f"{adapter}. baseline_size={baseline_sizes[adapter]}, "
                    f"max_size={max_sizes[adapter]}"
                )

            file_type_result = executor.run(
                f"file {shlex.quote(output_paths[adapter])}",
                stop_timeout_seconds,
            )
            _assert_command_ok(
                file_type_result,
                operation=f"file type check (adapter {adapter})",
            )
            file_description = file_type_result.stdout.lower()
            if "mpeg" not in file_description or "transport" not in file_description:
                raise AssertionError(
                    "recording file was not identified as MPEG transport stream "
                    f"for adapter {adapter}. file output:\n"
                    f"{file_type_result.stdout.strip()}"
                )
    finally:
        cleanup_errors: list[str] = []
        active_exception = sys.exc_info()[0]

        for client in clients:
            try:
                client.run_command("setmrl null://")
            except DvbCtrlError as exc:
                cleanup_errors.append(f"cleanup setmrl null failed: {exc}")

        for adapter in adapters:
            rm_result = executor.run(
                f"rm -f {shlex.quote(output_paths[adapter])}",
                stop_timeout_seconds,
            )
            if rm_result.returncode != 0:
                cleanup_errors.append(
                    "cleanup remove output file failed "
                    f"for adapter {adapter} (exit={rm_result.returncode}): "
                    f"{rm_result.stderr.strip()}"
                )

            try:
                _stop_adapter(adapter)
            except Exception as exc:
                cleanup_errors.append(f"cleanup stop failed for adapter {adapter}: {exc}")

            status_command = _render_command_for_adapter(config, config.status_command, adapter)
            stopped_status = executor.run(status_command, stop_timeout_seconds)
            if stopped_status.returncode == 0:
                cleanup_errors.append(
                    f"dvbstreamer adapter {adapter} still running after stop.\n"
                    f"stdout:\n{stopped_status.stdout.strip()}\n"
                    f"stderr:\n{stopped_status.stderr.strip()}"
                )

        if cleanup_errors and active_exception is None:
            raise AssertionError("\n".join(cleanup_errors))
