# Integration Tests

Live integration tests are opt-in and read JSON config from one of:

- `CCATV_INTEGRATION_CONFIG` (if set)
- `XDG_CONFIG_HOME/ccatv/integration.json` (fallback `~/.config/ccatv/integration.json`)

## Example Config

```json
{
  "enabled": true,
  "mode": "ssh",
  "remote_host": "druidmedia",
  "remote_user": "chris",
  "remote_port": 22,
  "remote_workdir": null,
  "dvbstreamer_host": "druidmedia",
  "dvb_adapter_count": 4,
  "dvb_adapter_index": 0,
  "dvbctrl_path": "dvbctrl",
  "dvbctrl_timeout_seconds": 10.0,
  "readiness_command": "lsmuxes",
  "readiness_attempts": 10,
  "readiness_delay_seconds": 1.0,
  "start_timeout_seconds": 20.0,
  "start_command": "dvbstreamer -Dd -a {adapter_index}",
  "stop_command": "pgrep -f '[d]vbstreamer -Dd -a {adapter_index}' && pkill -f '[d]vbstreamer -Dd -a {adapter_index}' || true",
  "status_command": "pgrep -f '[d]vbstreamer -Dd -a {adapter_index}'"
}
```

## Run

- Integration-only: `uv run pytest -m integration -q`
- Include skip reasons: `uv run pytest -m integration -q -rs`

## Current Integration Coverage

The integration marker currently includes three live tests in `tests/integration/test_live_dvbstreamer.py`:

1. `test_live_dvbstreamer_lifecycle_smoke`
2. `test_live_orchestrator_runs_due_scheduler_job`
3. `test_live_multi_adapter_parallel_recording_distinct_muxes`

## Multi-Adapter Test Prerequisites

The distinct-mux test has additional gates:

1. `dvb_adapter_count` must be at least 4.
2. `lsservices` output must include enough channels for probing.
3. `serviceinfo <channel>` output must allow discovery of at least 4 channels on distinct muxes.

Service discovery first enumerates channel names via `lsservices`, then parses channel name and mux identity from per-channel `serviceinfo` output, including `Multiplex UID` lines.

## Command Template Placeholders

The following placeholders are supported in `start_command`, `stop_command`, and `status_command`:

- `{adapter_index}`
- `{adapter_count}`
- `{host}`

Using any other placeholder raises a ValueError when rendering commands.

## Expected Runtime

- The smoke and orchestrator tests are typically around 30 to 70 seconds each.
- The multi-adapter test is longer because it starts four adapters and verifies parallel file growth across all of them.
