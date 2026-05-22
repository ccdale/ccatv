# Integration Test Configuration

The live integration smoke test is opt-in and reads JSON config from:

- `$CCATV_INTEGRATION_CONFIG` (if set), otherwise
- `$XDG_CONFIG_HOME/ccatv/integration.json` (fallback `~/.config/ccatv/integration.json`)

Example config for your remote host:

```json
{
  "enabled": true,
  "mode": "ssh",
  "remote_host": "druidmedia",
  "remote_user": "chris",
  "remote_port": 22,
  "remote_workdir": null,
  "dvbstreamer_host": "druidmedia",
  "dvb_adapter_count": 1,
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

Run integration-only tests:

`uv run pytest -m integration`

## What The Live Integration Test Verifies

The live smoke test exercises a full remote dvbstreamer lifecycle and recording flow:

1. Check remote SSH connectivity (when `mode` is `ssh`).
2. Check whether dvbstreamer is already running and stop it if found.
3. Start dvbstreamer on adapter `0` using the configured `start_command`.
4. Probe dvbctrl readiness with `readiness_command` (default: `lsmuxes`).
5. Run `select "BBC TWO HD"`.
6. Poll `festatus` until lock is reported.
7. Poll `stats` until activity increases.
8. Run `setmrl file:///tmp/...` to begin writing transport stream output.
9. Confirm the output file is created and grows during the capture window.
10. Run `setmrl null://` to stop file output.
11. Run `file /tmp/...` and check that output identifies an MPEG transport stream.
12. Cleanup in `finally`: reset `setmrl null://`, stop dvbstreamer, remove the temp file, and verify dvbstreamer is no longer running.

## Expected Runtime

This test is intentionally end-to-end and includes a recording-growth window, so expect roughly **1 minute** per run (often around 30-70 seconds, depending on signal and host performance).
