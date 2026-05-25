# ccatv systemd operations

This document covers the current M5 operational model for `ccatv-service`.

## Current scope

The packaged systemd unit runs `ccatv-service` in scheduler-loop mode.

That means:

- the long-running service continuously evaluates due recording jobs
- recordings are written under `/var/lib/ccatv/recordings`
- local CLI commands such as `ccatv setup` and `ccatv epg-sync-sd` still use the in-process service client path introduced in M4
- the unit does not yet expose the M3 Unix socket transport during normal service startup

## Unit file

Install [systemd/ccatv.service](/home/chris/src/ccatv/systemd/ccatv.service) to `/usr/lib/systemd/system/ccatv.service` on Arch Linux or `/lib/systemd/system/ccatv.service` on Debian-family systems.

The unit assumes:

- a dedicated `ccatv` user and group
- writable state under `/var/lib/ccatv`
- runtime files under `/run/ccatv`
- the `ccatv-service` console script is available at `/usr/bin/ccatv-service`

## Lifecycle commands

After installation:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ccatv.service
sudo systemctl status ccatv.service
```

Common lifecycle operations:

```bash
sudo systemctl start ccatv.service
sudo systemctl stop ccatv.service
sudo systemctl restart ccatv.service
sudo systemctl status ccatv.service
sudo journalctl -u ccatv.service -f
```

## Readiness recommendations

Current recommendation is to use `Type=simple`.

Why:

- `ccatv-service` does not yet emit `sd_notify` readiness signals
- scheduler-loop mode is considered ready once the process has started successfully
- deeper application health can be checked through logs and future service transport enhancements

Operational guidance:

- treat `systemctl is-active ccatv.service` as the process-level readiness signal
- treat the journal as the source of truth for startup failures and scheduler-cycle errors
- keep `Restart=on-failure` enabled so transient failures recover automatically

## Logging recommendations

The service currently logs to stdout/stderr and therefore to journald under systemd.

Recommended operational pattern:

- use `journalctl -u ccatv.service` for history
- use `journalctl -u ccatv.service -f` for live troubleshooting
- keep the default journald integration rather than redirecting logs to files

If you need more verbosity for debugging, set:

```bash
sudo systemctl edit ccatv.service
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
- `ProtectHome=yes`
- `UMask=0077`

Recommended adjustments only if operational evidence requires them:

- shorten `RestartSec` if the host needs faster recovery
- widen `ReadWritePaths` only if you move the database or recording directory
- add explicit environment overrides instead of editing the unit directly when possible

## Configuration placement

The unit expects configuration under `/var/lib/ccatv/.config` through `XDG_CONFIG_HOME`.

That means the following files should exist for the service account:

- `/var/lib/ccatv/.config/ccatv/runtime.json`
- `/var/lib/ccatv/.config/dvbstreamer/userconfig.json`

Bootstrap them with the `ccatv` service user, for example:

```bash
sudo -u ccatv XDG_CONFIG_HOME=/var/lib/ccatv/.config ccatv setup --host druidmedia --adapter-count 4 --username your-user
```

## Known limitation

The current unit runs only the scheduler-loop mode. If you want to experiment with the M3 IPC transport, run `ccatv-service --socket-path ...` manually or create a separate experimental unit until the transport and worker loop are unified.