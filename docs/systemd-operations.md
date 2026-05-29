# ccatv systemd operations

This document covers the current operational model for `ccatv-service`,
`ccatv-api`, and `ccatv-web` user services.

## Current scope

The packaged systemd units are **user services**. They run as your own login
user without any `sudo` access.

That means:

- the long-running recorder service continuously evaluates due recording jobs
- the local API transport service exposes authenticated HTTP command endpoints on `127.0.0.1:8787`
- the Flask web service can expose remote schedule/guide API routes
- recordings are written under `~/.local/share/ccatv/recordings`
- the database defaults to `~/.local/share/ccatv/ccatv.sqlite3`
- local CLI commands such as `ccatv setup` and `ccatv epg-sync-sd` still use the in-process service client path introduced in M4
- the unit does not yet expose the M3 Unix socket transport during normal service startup

## Unit files

The units ship at:

- [systemd/ccatv.service](../systemd/ccatv.service)
- [systemd/ccatv-api.service](../systemd/ccatv-api.service)
- [systemd/ccatv-web.service](../systemd/ccatv-web.service)

`ccatv.service` requires:

- the `ccatv-service` console script at `/usr/local/bin/ccatv-service`
- your XDG config already populated with `ccatv setup`

`ccatv-web.service` requires:

- the `ccatv-web` console script at `/usr/local/bin/ccatv-web`
- the shared environment file at `~/.config/ccatv/web.env`

`ccatv-api.service` requires:

- the `ccatv-service` console script at `/usr/local/bin/ccatv-service`
- the shared environment file at `~/.config/ccatv/web.env`

### Manual installation (no package manager)

Copy the unit file to the standard user-service location and reload the daemon:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ccatv.service ~/.config/systemd/user/ccatv.service
cp systemd/ccatv-api.service ~/.config/systemd/user/ccatv-api.service
cp systemd/ccatv-web.service ~/.config/systemd/user/ccatv-web.service
systemctl --user daemon-reload
```

Create the token environment file used by `ccatv-web.service`:

```bash
mkdir -p ~/.config/ccatv
cat > ~/.config/ccatv/web.env <<'EOF'
CCATV_SERVICE_AUTH_TOKEN=replace-with-service-token
CCATV_WEB_AUTH_TOKEN=replace-with-web-token

# Optional topology overrides (defaults shown)
CCATV_API_BIND_HOST=127.0.0.1
CCATV_API_PORT=8787
CCATV_WEB_LISTEN_HOST=0.0.0.0
CCATV_WEB_LISTEN_PORT=5000
CCATV_WEB_SERVICE_HOST=127.0.0.1
CCATV_WEB_SERVICE_PORT=8787
EOF
chmod 600 ~/.config/ccatv/web.env
```

All three user services share this same `web.env` file.

### Package-based installation

When installed via the Arch PKGBUILD or Debian package, the unit is placed in
`/usr/lib/systemd/user/ccatv.service`, `/usr/lib/systemd/user/ccatv-api.service`, and `/usr/lib/systemd/user/ccatv-web.service`.
This makes them available to every user on the system without any manual
copying — `systemctl --user daemon-reload` is sufficient after installation.

## Lifecycle commands

After installation:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ccatv.service
systemctl --user enable --now ccatv-api.service
systemctl --user enable --now ccatv-web.service
systemctl --user status ccatv.service
systemctl --user status ccatv-api.service
systemctl --user status ccatv-web.service
```

Common lifecycle operations:

```bash
systemctl --user start ccatv.service
systemctl --user stop ccatv.service
systemctl --user restart ccatv.service
systemctl --user status ccatv.service
journalctl --user-unit ccatv.service -f

systemctl --user start ccatv-api.service
systemctl --user stop ccatv-api.service
systemctl --user restart ccatv-api.service
systemctl --user status ccatv-api.service
journalctl --user-unit ccatv-api.service -f

systemctl --user start ccatv-web.service
systemctl --user stop ccatv-web.service
systemctl --user restart ccatv-web.service
systemctl --user status ccatv-web.service
journalctl --user-unit ccatv-web.service -f
```

## Running without an active login session (linger)

By default systemd user services stop when you log out. To keep `ccatv-service` running even when no session is active:

```bash
loginctl enable-linger $USER
```

This is the recommended setting on a home media server where you want recordings to happen unattended.

## Readiness recommendations

Current recommendation is to use `Type=simple`.

Why:

- `ccatv-service` does not yet emit `sd_notify` readiness signals
- scheduler-loop mode is considered ready once the process has started successfully

Operational guidance:

- treat `systemctl --user is-active ccatv.service` as the process-level readiness signal
- treat the journal as the source of truth for startup failures and scheduler-cycle errors
- keep `Restart=on-failure` enabled so transient failures recover automatically

## Logging recommendations

The service logs to stdout/stderr, which journald captures automatically.

Recommended operational pattern:

- use `journalctl --user-unit ccatv.service` for history
- use `journalctl --user-unit ccatv.service -f` for live troubleshooting

To increase verbosity for debugging:

```bash
systemctl --user edit ccatv.service
```

and add:

```ini
[Service]
Environment=CCATV_LOG_LEVEL=DEBUG
```

## Failure policy recommendations

The supplied unit uses conservative hardening and restart defaults:

- `Restart=on-failure`
- `RestartSec=5`
- `NoNewPrivileges=yes`
- `PrivateTmp=yes`
- `ProtectSystem=full`
- `ReadWritePaths=%h/.local/share/ccatv`
- `ReadWritePaths=%h/.config/ccatv`

Recommended adjustments only if operational evidence requires them:

- shorten `RestartSec` if the host needs faster recovery
- add explicit environment overrides with `systemctl --user edit` instead of modifying the unit file directly

## Configuration placement

As a user service, the unit inherits your normal XDG directories. Configuration should already exist at:

- `~/.config/ccatv/runtime.json`
- `~/.config/dvbstreamer/userconfig.json`

Bootstrap them with:

```bash
ccatv setup --host druidmedia --adapter-count 4 --username your-user
```

## Known limitation

Scheduler-loop (`ccatv.service`) and HTTP API transport (`ccatv-api.service`) currently run as separate processes. This is intentional for now, but they are not yet unified into a single daemon mode.

## Remote host pattern (for example `druidmedia`)

When recorder and Flask run on the same host:

- run `ccatv-api.service` so `ccatv-service` HTTP transport is always available on `127.0.0.1:8787`
- run `ccatv-web` on `0.0.0.0:5000` if LAN clients need access
- keep `CCATV_WEB_AUTH_TOKEN` set so `/api/*` routes require bearer auth

When recorder API and Flask are on different hosts:

- set `CCATV_WEB_SERVICE_HOST` in `web.env` to the recorder host/IP
- set `CCATV_WEB_SERVICE_PORT` in `web.env` to the recorder API port
- keep `CCATV_SERVICE_AUTH_TOKEN` the same token expected by the recorder API