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
- Build a Flask/FastAPI-based scheduler service (remote API) so recording jobs can be created/managed off-box; prioritize this before the GTK4 app work.
- Full schedules direct ingestion implementation.
	- Prioritize this before final daemon-behavior design so richer guide data (including repeat/alternate airing windows) can drive conflict, retry, and recording-selection policies.
- Jellyfin integration implementation.
- inputlirc remote mapping implementation.
- Recording scheduler and conflict policy implementation.
- Recording metadata sidecar generation (`.nfo`) for completed captures.
	- Write an `.nfo` file with the same basename as the recording file (only extension changes to `.nfo`).
	- Populate as much metadata as possible from OTA EPG plus Schedules Direct where available.
	- Trigger `.nfo` write only after initial recording output-file health checks pass green.
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

1. Completed: Harden process lifecycle and command reliability for runtime use.
	- handle force-kill timeout path in manager stop() consistently
2. Completed: Add integration tests against a live dvbstreamer process.
	- config-driven host and adapter-count inputs support remote-host and local execution modes
3. Completed: Validate end-to-end select/current/stats/festatus flow against real command output.
4. Completed: persistence foundation for recording/scheduler state.
5. Start scheduler skeleton and recording lifecycle state model.
	- wire preflight checker into write operations (recording/scheduling) as write paths are introduced
6. Begin Schedules Direct client implementation behind existing contracts.

## Service-First Pivot Roadmap

This project is now pivoting from a monolithic app shape to a service-first
architecture: one long-running tvrecorder service process with multiple front
ends (CLI, GTK4, Flask/FastAPI) as clients.

M1. Service API surface and boundary definitions.
- [x] Create M1 service API contract draft with envelope/error model.
- [x] Add daemon entrypoint skeleton (`ccatv-service`) as process boundary seed.
- [x] Define command capability matrix and migration mapping from existing CLI flows.

M2. Extract use-case orchestration from front-end glue.
- [x] Ensure recording and scheduling workflows are callable through service command handlers only.
	- [x] Initial dispatcher support added for `recording.schedule.create` and `recording.schedule.list`.
	- [x] Front-end workflow execution now routes through service command dispatch path.
- [x] Ensure metadata sync workflows are callable through service command handlers only.
	- [x] Added `metadata.sd.sync.status.get` dispatcher command handler.
	- [x] Scaffolded explicit guide source precedence policy (prefer `dvbstreamer_ota` over `schedules_direct` where both provide a slot).
	- [x] Integrated source precedence policy into metadata workflow read paths via repository preferred-broadcast query.
	- [x] `ccatv epg-sync-sd` now uses the local service-client dispatch path (`metadata.sd.sync.run`) instead of direct ingestion wiring.

M3. Daemon transport implementation.
- [x] Add local IPC transport (Unix socket) for request/response handling.
- [x] Add structured health and info commands over transport.

M4. Thin CLI client conversion.
- [x] Convert CLI command paths to service-client calls.
- [x] Keep backward-compatible behavior during migration.

M5. systemd operationalization.
- [ ] Add service unit file and lifecycle docs (start/stop/restart/status).
- [ ] Add readiness, logging, and failure policy recommendations.
- [ ] Add packaging instructions for Debian and Arch Linux machines (`dpkg` and `PKGBUILD`).

M6. Multi-front-end enablement.
- [ ] Introduce a shared service client module used by CLI/GTK4/Flask.
- [ ] Add first GTK4 and Flask/FastAPI command-path integrations against service API.

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
- Schedules Direct secrets policy: username, password, and API token are runtime secrets only and must never be committed.
