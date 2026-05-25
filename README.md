# ccatv

ccatv is a Linux television application.

> [!NOTE]
> If you are in the UK you will need a TV Licence to use this software

Current project direction:

- Python GTK4 user interface designed for across-the-room TV use
- Playback backend abstraction with:
	- libmpv as the first implementation
	- GStreamer as an optional/alternate backend
- TV recorder and tuner control via external dvbstreamer + dvbctrl
	- reference repository: https://github.com/ccdale/dvbstreamer
- TV guide and schedule metadata from Schedules Direct
- Remote control support via a Windows Media Center remote and inputlirc
- Jellyfin integration for media library access on the local network
- Licensed under GNU GPLv3 (or later)

## Secrets Handling

Schedules Direct credentials and authentication state are sensitive.

- Schedules Direct `username`, `password`, and API `token` must never be committed.
- These values should only be read from local runtime config under `$XDG_CONFIG_HOME` and/or local runtime caches.
- Do not add secrets to repository files, tests, fixtures, docs examples, or CI configuration.

## TvRecorder Configuration

TvRecorder persists local dvbctrl credentials under
`$XDG_CONFIG_HOME/dvbstreamer/userconfig.json`.

If `XDG_CONFIG_HOME` is unset, this resolves to
`$HOME/.config/dvbstreamer/userconfig.json`.

Current file shape:

```json
{
	"password": "your-password",
	"username": "your-username"
}
```

Both `dvbstreamer` and `dvbctrl` read auth directly from this file.

To create or update the local file interactively, run `uv run ccatv-setup`.

You can also use the shared CLI entrypoint with `uv run ccatv setup`.

ccatv runtime connection settings are stored separately in:
`$XDG_CONFIG_HOME/ccatv/runtime.json` (fallback `$HOME/.config/ccatv/runtime.json`).

Current runtime file shape:

```json
{
	"dvb_adapter_count": 1,
	"dvbstreamer_host": "localhost"
}
```

You can override these with CLI setup flags:
- `uv run ccatv setup --host druidmedia --adapter-count 4 --username your-user`

Runtime precedence for host and adapter count is:
1. Environment variables (`CCATV_DVBSTREAMER_HOST`, `CCATV_DVB_ADAPTER_COUNT`)
2. Local runtime config (`runtime.json`)
3. Built-in defaults (`localhost`, `1`)

## Integration Testing

The integration smoke test is opt-in and supports local or SSH-managed
dvbstreamer lifecycle.

Configuration lives in:
- `$CCATV_INTEGRATION_CONFIG` (if set), otherwise
- `$XDG_CONFIG_HOME/ccatv/integration.json` (fallback `~/.config/ccatv/integration.json`)

Run only integration tests with:
- `uv run pytest -m integration`

See `tests/integration/README.md` for config details and an SSH example for
`druidmedia`/`chris`.

See docs/architecture-proposal.md for the proposed architecture and phased implementation plan.

## Service-First Pivot (In Progress)

ccatv is pivoting toward a service-first design:
- one long-running service process (`ccatv-service`) for recorder/metadata workflows
- multiple front ends (CLI, GTK4, Flask/FastAPI) as clients of a stable service API

M1 contract draft:
- `docs/service-api-contract.md`

Current daemon skeleton command:
- `uv run ccatv-service --run-once`

Systemd and packaging docs:
- [docs/systemd-operations.md](/home/chris/src/ccatv/docs/systemd-operations.md)
- [docs/packaging.md](/home/chris/src/ccatv/docs/packaging.md)
