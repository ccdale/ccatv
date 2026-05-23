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
- DvbCtrlClient now retries transient timeout/network command failures with backoff.
- TvRecorderService parser layer for current/stats/festatus outputs.
- Fixture-based unit tests for parser behavior.
- DvbStreamerManager lifecycle scaffold (start/stop/health/status) with bootstrap wiring.
- Manager health-check edge-case tests now cover no-process, clean-exit, and failed-state refresh paths.
- Write preflight checker now validates host resolution and online adapters before write paths are used.
- Live integration test now validates remote/local dvbstreamer lifecycle with config-driven SSH support.
- Live integration test now exercises select, lock/status polling, stats activity, recording file growth, file-type validation, and cleanup paths.
- Initial persistence foundation scaffold is in place with SQLite migration tracking and base recording/scheduler tables.
- Persistence store adapter now supports recording and scheduler state create/update/list paths and is bootstrapped into app context.
- TvRecorderService now integrates persistence-backed scheduler/recording state transitions, including a post-processing phase after capture completion.
- Recording scheduling now supports configurable pre-start/post-finish padding and configurable early/periodic/final output-file health-check policies.

Quality baseline:
- Ruff linting configured and used in workflow.
- Unit tests currently passing.

## Scaffolded but Not Yet Integrated

These pieces exist but are not yet wired to application runtime workflows:
- TvRecorderService command path is validated in tests but not yet wired to a user-facing recording flow.

## Not Started Yet

- Live GTK4 UI flow.
- Full schedules direct ingestion implementation.
	- Prioritize this before final daemon-behavior design so richer guide data (including repeat/alternate airing windows) can drive conflict, retry, and recording-selection policies.
- Jellyfin integration implementation.
- inputlirc remote mapping implementation.
- Recording scheduler and conflict policy implementation.
- Future recorder efficiency enhancement: support multiple concurrent recordings on a single adapter when channels share the same mux by using dvbstreamer service filters (`setsfmrl`/`getsfmrl`).
	- Current target-machine strategy remains one adapter per recording (4 adapters => up to 4 simultaneous recordings).
	- Introduce after base recorder orchestration is stable, since this adds non-trivial control-flow/state complexity.
- Future recorder timing enhancement: investigate broadcaster-emitted programme start/stop events (where available) to improve recording efficiency and reduce unnecessary file size.
	- Keep configurable pre-start/post-finish padding as the baseline/safety behavior.
	- If event signals are available, write a per-recording marker file with observed event timestamps (or note that no events were seen) to support tuning and reliability analysis.

## External Environment Prerequisites

Integration validation requires a working dvbstreamer environment.

Required before live integration runs:
- dvbstreamer executable available.
- dvbctrl executable available.
- test adapter/channel configuration available.

## Next Milestones

1. Harden process lifecycle and command reliability for runtime use:
	- handle force-kill timeout path in manager stop() consistently
2. Completed: Add integration tests against a live dvbstreamer process.
	- config-driven host and adapter-count inputs support remote-host and local execution modes
3. Completed: Validate end-to-end select/current/stats/festatus flow against real command output.
4. Completed: persistence foundation for recording/scheduler state.
5. Start scheduler skeleton and recording lifecycle state model.
	- wire preflight checker into write operations (recording/scheduling) as write paths are introduced
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
