# ccatv

ccatv is a Linux TV recording and guide service with a Flask control web app.

Playback/library browsing is expected to be handled externally (for example, Kodi).

> [!NOTE]
> If you are in the UK you will need a TV Licence to use this software.

## What Exists Today

Current implemented components:

- Recorder and scheduler service daemon (`ccatv-service`)
- Flask web control app (`ccatv-web`) for guide, scheduling, channels, and recordings
- CLI tools (`ccatv`, `ccatv-setup`, `ccatv-status`) for setup and operations
- EPG ingestion from OTA and Schedules Direct
- Recording post-processing pipeline

No end-user GTK4 frontend is currently shipped.

## Quick Start

Install dependencies in a local checkout:

```bash
cd /home/chris/src/ccatv
uv sync
```

Optional smoke checks:

```bash
uv run pytest -q
uv run ruff check .
```

## Configuration

Run setup once on the recorder host:

```bash
uv run ccatv setup --host your-dvbstreamer-host --adapter-count 4 --username your-user
```

This writes:

- `~/.config/dvbstreamer/userconfig.json`
- `~/.config/ccatv/runtime.json`

Runtime file shape:

```json
{
  "dvb_adapter_count": 1,
  "dvbstreamer_host": "localhost",
  "sd_lineup_id": "YOUR_LINEUP_ID"
}
```

`sd_lineup_id` is optional. When present, SD sync commands can omit `--lineup-id`.

Runtime precedence for host/adapter count:

1. Environment (`CCATV_DVBSTREAMER_HOST`, `CCATV_DVB_ADAPTER_COUNT`)
2. Runtime config (`runtime.json`)
3. Defaults (`localhost`, `1`)

## Secrets Handling

Do not commit Schedules Direct secrets (`username`, `password`, `token`).
Keep credentials/auth only in local runtime/config files.

## Running the Service and Web App

Run service HTTP transport on the recorder host:

```bash
uv run ccatv-service \
  --http-bind-host 0.0.0.0 \
  --http-port 8787 \
  --http-auth-token YOUR_SERVICE_TOKEN
```

Run web app (same host or remote desktop):

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

- `CCATV_SERVICE_AUTH_TOKEN` is required by `ccatv-web`
- `CCATV_WEB_AUTH_TOKEN` is optional, recommended if LAN exposed
- For LAN access to web UI, use `--listen-host 0.0.0.0`

## Daily Metadata Sync

Recommended: let the service run daily metadata updates:

```bash
uv run ccatv-service \
  --enable-daily-metadata-sync \
  --daily-metadata-sync-time 03:00
```

This runs OTA sync then Schedules Direct daily sync in sequence.
`sd_lineup_id` is resolved from:

1. `--sd-lineup-id`
2. `CCATV_SD_LINEUP_ID`
3. runtime config `sd_lineup_id`

Manual commands:

```bash
uv run ccatv epg-sync-ota
uv run ccatv epg-sync-sd-daily --lineup-id YOUR_LINEUP_ID
uv run ccatv epg-sync-sd-full --lineup-id YOUR_LINEUP_ID
```

## CLI Commands

Primary commands:

- `uv run ccatv setup`
- `uv run ccatv status`
- `uv run ccatv epg-sync-ota`
- `uv run ccatv epg-sync-sd`
- `uv run ccatv epg-sync-sd-daily`
- `uv run ccatv epg-sync-sd-full`
- `uv run ccatv channel-map`
- `uv run ccatv recordings-backfill-metadata`

Dedicated entrypoints:

- `uv run ccatv-setup`
- `uv run ccatv-status`
- `uv run ccatv-service`
- `uv run ccatv-web`

## Web Routes

Pages:

- `/` guide/timeline
- `/channel-manager`
- `/recordings`

Auth routes:

- `GET /auth/session`
- `POST /auth/session`
- `DELETE /auth/session`

API routes:

- `GET /api/health`
- `GET /api/service/info`
- `GET /api/channels`
- `GET /api/dvbservices`
- `POST /api/channels/mapping`
- `POST /api/channels/favorite`
- `GET /api/guide`
- `GET /api/guide/search`
- `GET /api/schedules`
- `POST /api/schedules`
- `DELETE /api/schedules/<job_id>`
- `GET /api/recordings`
- `DELETE /api/recordings/<recording_id>`
- `POST /api/recordings/<recording_id>/stop`

## Integration Testing

Integration tests are opt-in:

```bash
uv run pytest -m integration
```

See `tests/integration/README.md` for environment setup.

## Legacy / Discarded Scope

Historical or currently out-of-scope topics are documented separately in:

- `docs/discarded-and-legacy-scope.md`
