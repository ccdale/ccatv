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
