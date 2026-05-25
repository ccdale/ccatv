# ccatv systemd operations

This document covers the current M5 operational model for `ccatv-service`.

## Current scope

The packaged systemd unit is a **user service**. It runs as your own login user without any `sudo` access.

That means:

- the long-running service continuously evaluates due recording jobs
- recordings are written under `~/.local/share/ccatv/recordings`
- the database defaults to `~/.local/share/ccatv/ccatv.sqlite3`
- local CLI commands such as `ccatv setup` and `ccatv epg-sync-sd` still use the in-process service client path introduced in M4
- the unit does not yet expose the M3 Unix socket transport during normal service startup

## Unit file

The unit ships at [systemd/ccatv.service](../systemd/ccatv.service).

The unit requires only:

- the `ccatv-service` console script at `/usr/bin/ccatv-service`
- your XDG config already populated with `ccatv setup`

### Manual installation (no package manager)

Copy the unit file to the standard user-service location and reload the daemon:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ccatv.service ~/.config/systemd/user/ccatv.service
systemctl --user daemon-reload
```

### Package-based installation

When installed via the Arch PKGBUILD or Debian package, the unit is placed in
`/usr/lib/systemd/user/ccatv.service`. This makes it available to every user on
the system without any manual copying — `systemctl --user daemon-reload` is
sufficient after installation.

## Lifecycle commands

After installation:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ccatv.service
systemctl --user status ccatv.service
```

Common lifecycle operations:

```bash
systemctl --user start ccatv.service
systemctl --user stop ccatv.service
systemctl --user restart ccatv.service
systemctl --user status ccatv.service
journalctl --user-unit ccatv.service -f
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
- `ProtectSystem=strict`

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

The current unit runs only the scheduler-loop mode. If you want to experiment with the M3 IPC transport, run `ccatv-service --socket-path ...` manually or create a separate experimental unit until the transport and worker loop are unified.