# ccatv Status and TODO

This file is a human-readable snapshot of what is implemented, what is scaffolded, and what is next.

## Current Status

The project is in early foundation stage.

Implemented and validated:
- Python package bootstrap with app context, settings, and logging wiring.
- App bootstrap now constructs both DvbCtrlClient and DvbStreamerManager from settings.
- Project metadata access from pyproject.toml via package helpers.
- Error handling policy docs and reusable error helpers.
- dvbctrl subprocess adapter with typed result and typed exceptions.
- Shared dvbstreamer/dvbctrl auth config via
	`$XDG_CONFIG_HOME/dvbstreamer/userconfig.json` with flat `username/password`.
- Local ccatv runtime config via
	`$XDG_CONFIG_HOME/ccatv/runtime.json` for `dvbstreamer_host` and `dvb_adapter_count`.
- ccatv no longer passes dvbctrl credentials with `-u/-p`; auth is read by
	dvbctrl directly from userconfig.
- Typed dvbctrl command catalog for current, stats, festatus, and select.
- TvRecorderService parser layer for current/stats/festatus outputs.
- Fixture-based unit tests for parser behavior.
- DvbStreamerManager lifecycle scaffold (start/stop/health/status) with bootstrap wiring.

Quality baseline:
- Ruff linting configured and used in workflow.
- Unit tests currently passing.

## Scaffolded but Not Yet Integrated

These pieces exist but are not yet wired to a live runtime flow:
- TvRecorderService command path is test-covered but not yet validated against a live dvbstreamer instance.
- DvbStreamerManager lifecycle is unit-tested but not yet exercised in a real process integration path.

## Not Started Yet

- Live GTK4 UI flow.
- Full schedules direct ingestion implementation.
- Jellyfin integration implementation.
- inputlirc remote mapping implementation.
- Recording scheduler and conflict policy implementation.

## External Environment Prerequisites

The next development step needs a working local dvbstreamer setup.

Required before integration work:
- dvbstreamer executable available.
- dvbctrl executable available.
- test adapter/channel configuration available.

## Next Milestones

1. Harden process lifecycle and command reliability for runtime use:
	- handle force-kill timeout path in manager stop() consistently
	- add manager health-check edge-case tests
	- add retry/backoff policy for transient command failures
2. Add integration tests or scripted checks against a live dvbstreamer process.
3. Validate end-to-end select/current/stats/festatus flow against real command output.
4. Start persistence foundation (schema + migration scaffold) for recording/scheduler state.
5. Start scheduler skeleton and recording lifecycle state model.
6. Begin Schedules Direct client implementation behind existing contracts.

## Later Milestones (After Recorder + Persistence)

- Playback backend abstraction (mpv first).
- GTK4 live TV / guide shell UI.
- inputlirc remote mapping.
- Jellyfin integration.

## Contributor Notes

- Keep commits small and incremental.
- Run Ruff and tests before committing.
- Treat post-Ruff file changes as formatter side-effects unless intentionally authored.
- Keep this file updated as milestones move from scaffolded to integrated.
