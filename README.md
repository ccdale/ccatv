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

Remote HTTP transport for desktop clients:
- `uv run ccatv-service --http-bind-host 0.0.0.0 --http-port 8787 --http-auth-token YOUR_TOKEN`

Flask desktop frontend (first M6 integration):
- `CCATV_SERVICE_AUTH_TOKEN=YOUR_TOKEN uv run ccatv-web --service-host recorder-host --service-port 8787 --listen-host 127.0.0.1 --listen-port 5000`
- For LAN access from other machines, set `--listen-host 0.0.0.0`.
- Optional inbound web auth (recommended when LAN-exposed): set `CCATV_WEB_AUTH_TOKEN` (or `--web-auth-token`) and send `Authorization: Bearer ...` to Flask `/api/*` routes.
- API routes currently exposed by the web app:
	- `GET /api/health`
	- `GET /api/service/info`
	- `GET /api/schedules?state=...`
	- `GET /api/guide?channel=...&startAtUtc=...&windowHours=...`
	- `POST /api/schedules`

Packaging docs:
- [docs/packaging.md](/home/chris/src/ccatv/docs/packaging.md)

## Installation and Runbook

This section is the current end-to-end setup path for:

- `ccatv-service` (recorder/scheduler daemon)
- Flask backend (`ccatv-web`) for remote scheduling/recording dashboard APIs
- GTK4 app status and developer setup notes

### 1. Install ccatv

From source in a development checkout:

```bash
cd /home/chris/src/ccatv
uv sync
```

Optional smoke checks:

```bash
uv run pytest -q
uv run ruff check .
```

### 2. Configure runtime and dvbctrl credentials

Run setup once on the recorder host:

```bash
uv run ccatv setup --host your-dvbstreamer-host --adapter-count 4 --username your-user
```

This writes:

- `~/.config/dvbstreamer/userconfig.json`
- `~/.config/ccatv/runtime.json`

### 3. Run ccatv-service HTTP transport manually

If you want a remote desktop Flask UI to talk to the recorder host, run service HTTP transport:

```bash
uv run ccatv-service --http-bind-host 0.0.0.0 --http-port 8787 --http-auth-token YOUR_SERVICE_TOKEN
```

Security guidance:

- Use a strong random token for `--http-auth-token`.
- Prefer LAN-only exposure plus firewall rules.
- If possible, terminate TLS at a reverse proxy on the recorder host.

### 4. Install and run Flask backend (remote desktop)

On your desktop machine:

```bash
cd /home/chris/src/ccatv
uv sync
```

Run Flask backend pointing to the recorder host:

```bash
CCATV_SERVICE_AUTH_TOKEN=YOUR_SERVICE_TOKEN \
CCATV_WEB_AUTH_TOKEN=YOUR_WEB_TOKEN \
uv run ccatv-web \
	--service-host recorder-host-or-ip \
	--service-port 8787 \
	--listen-host 127.0.0.1 \
	--listen-port 5000
```

Notes:

- `CCATV_SERVICE_AUTH_TOKEN` is required (matches service `--http-auth-token`).
- `CCATV_WEB_AUTH_TOKEN` is optional but recommended, especially if you use `--listen-host 0.0.0.0`.
- Current API routes:
	- `GET /api/health`
	- `GET /api/service/info`
	- `GET /api/schedules?state=...`
	- `GET /api/guide?channel=...&startAtUtc=...&windowHours=...`
	- `POST /api/schedules`

### 4.1 EPG refresh commands

OTA grab + ingest (manual):

```bash
uv run ccatv epg-sync-ota
```

Schedules Direct daily rolling update (14-day window):

```bash
uv run ccatv epg-sync-sd-daily --lineup-id YOUR_LINEUP_ID
```

Schedules Direct manual full refresh (14-day window, clears existing SD window rows first):

```bash
uv run ccatv epg-sync-sd-full --lineup-id YOUR_LINEUP_ID
```

Daily sequential runner (OTA first, then SD daily update), with success/failure lines in log:

```bash
~/.local/bin/ccatv-epg-daily YOUR_LINEUP_ID
```

Built-in service scheduler option (recommended):

```bash
uv run ccatv-service \
	--output-directory ~/.local/share/ccatv/recordings \
	--enable-daily-metadata-sync \
	--daily-metadata-sync-time 03:00 \
	--sd-lineup-id YOUR_LINEUP_ID
```

This runs daily metadata updates in the daemon scheduler loop at local 03:00,
sequentially as OTA first then Schedules Direct daily update.

Runner log file:

- `~/.local/state/ccatv/logs/ccatv-epg-sync.log`

Install local helper scripts (including `ccatv-epg-daily`):

```bash
./scripts/install-local-scripts.sh
```

Optional `cron` fallback for daily sequential execution around 03:00:

```cron
0 3 * * * CCATV_SD_LINEUP_ID=YOUR_LINEUP_ID ~/.local/bin/ccatv-epg-daily
```

### 5. GTK4 app installation status

GTK4 live UI shell is not fully implemented yet, so there is currently no end-user `ccatv-gtk` entrypoint to install/run.

Current completed groundwork:

- playback abstraction and mpv IPC backend under `src/ccatv/playback/`
- GTK-facing service gateway under `src/ccatv/ui/service_gateway.py`

When the GTK4 executable entrypoint lands, this section should be updated with full package/dependency and launch instructions.
