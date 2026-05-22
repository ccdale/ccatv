# ccatv Proposed Architecture

Status: draft proposal  
Date: 2026-05-17

## Goals

- Build a Linux television application with a Python GTK4 user interface.
- Use dvbstreamer and dvbctrl as external TV recorder and tuner-control functions.
- Support sofa-friendly remote control operation.
- Use Schedules Direct as the primary TV guide metadata source.
- Integrate local/network playback and Jellyfin media browsing.
- License the project under GPLv3.

## Scope and Assumptions

- Kodi extraction is out of scope.
- The application is uv-managed and Python-first.
- dvbstreamer remains an external process, managed and controlled by ccatv.
- Playback backend should be swappable between libmpv and GStreamer.

## High-Level Architecture

ccatv should be a single desktop app process with clear internal service boundaries:

1. UI layer (GTK4)
2. Application/service layer (orchestration)
3. Integration adapters (dvbctrl, jellyfin, player backends)
4. Persistence layer (SQLite + SQLAlchemy)

### Data Flow

1. User input (remote or keyboard) enters UI action dispatcher.
2. Dispatcher triggers service methods (play channel, schedule recording, browse guide).
3. Services call integration adapters:
   - Player adapter (libmpv or GStreamer)
   - dvbctrl/dvbstreamer adapter for tuning/recording
  - Schedules Direct adapter for guide data and lineup metadata
   - Jellyfin API adapter for library metadata/playback URLs
4. State updates are persisted and projected back to UI view models.

## Component Breakdown

## 1) UI Module

Responsibilities:

- TV-oriented GTK4 interface with large controls and directional navigation.
- Primary screens:
  - Live TV
  - EPG/Guide
  - Recordings
  - Media library (Jellyfin)
  - Settings
- On-screen overlays for channel/program info, playback state, recording state.

Suggested package path:

- src/ccatv/ui

## 2) Playback Module

Define a backend interface and provide at least one concrete backend initially.

Interface responsibilities:

- open(url)
- play(), pause(), stop()
- seek(seconds)
- set_volume(level)
- set_subtitle_track(track_id)
- emit state events (playing, paused, buffering, failed)

Backends:

- MpvBackend (first implementation): control mpv via IPC (JSON socket).
- GstBackend (optional second implementation): GStreamer pipeline through GI bindings.

Recommendation:

- Start with mpv for fastest reliable baseline and simpler Python integration.
- Keep backend abstraction strict so GStreamer can be added later without UI/service rewrite.

Suggested package path:

- src/ccatv/playback

## 3) TV Recorder Module (dvbstreamer + dvbctrl)

Responsibilities:

- Start/stop and monitor dvbstreamer process.
- Run control commands through dvbctrl (initially subprocess-based).
- Maintain tuner and service state in app memory and DB.
- Provide recording operations for scheduler and manual recording.

Subcomponents:

- DvbStreamerManager
  - Starts dvbstreamer with configured adapter and output mode.
  - Monitors process health and restarts if needed.
- DvbCtrlClient
  - Sends commands such as select, current, stats, festatus, scan.
  - Parses responses and maps failures to typed app errors.
- ChannelService
  - Resolves channel identity and executes tune/select actions.

Protocol notes to model:

- Remote control protocol is line-oriented and returns response codes.
- Adapter-specific control port convention should be configuration-driven.
- Authentication is shared with dvbstreamer/dvbctrl via
  `$XDG_CONFIG_HOME/dvbstreamer/userconfig.json` (fallback
  `~/.config/dvbstreamer/userconfig.json`) using a flat JSON object with
  `username` and `password`.

Suggested package path:

- src/ccatv/tvrecorder

## 4) Scheduler Module

Responsibilities:

- One-shot and repeating recording timers.
- Pre-roll and post-roll handling.
- Conflict detection against tuner availability.
- Retry policy for transient failures.

Suggested package path:

- src/ccatv/scheduler

## 5) Metadata Module

Responsibilities:

- EPG ingestion and normalization (Schedules Direct first, optional DVB SI augmentation).
- Program metadata storage and lookup.
- Station and lineup mapping between broadcast channels and guide provider IDs.
- Jellyfin metadata and catalog integration.

Subcomponents:

- SchedulesDirectClient
  - Handles auth token lifecycle and API retries.
  - Pulls lineups, schedules, and program metadata in batches.
- GuideIngestionService
  - Converts provider payloads into normalized DB models.
  - Performs upserts and expiry pruning.
- ChannelGuideMapper
  - Maps local channel/service identifiers to Schedules Direct station IDs.
  - Stores explicit overrides for edge cases.

Sync strategy:

- Full lineup/program seed at setup time.
- Incremental refresh every N hours (for example, 6h).
- Short-interval "near now" refresh window for late schedule changes.

Suggested package path:

- src/ccatv/metadata

## 6) Input Module

Responsibilities:

- Read remote-control events from inputlirc.
- Map raw events to semantic app actions.
- Support configurable key mappings.

Suggested package path:

- src/ccatv/input

## 7) Storage Module

Responsibilities:

- Database schema and migrations.
- Access patterns for channels, EPG, timers, recordings, and app settings.

Suggested package path:

- src/ccatv/storage

## Proposed Repository Layout

```text
src/
  ccatv/
    app/
    ui/
    playback/
    tvrecorder/
    scheduler/
    metadata/
    input/
    storage/
tests/
  unit/
  integration/
docs/
  architecture-proposal.md
  error-handling.md
```

## Package Choices

## Runtime dependencies

- pygobject
- pycairo
- sqlalchemy
- apscheduler
- pydantic-settings
- psutil
- tenacity
- structlog
- httpx
- orjson
- jellyfin-apiclient-python
- python-dateutil
- tzdata

Optional runtime dependencies:

- python-mpv (recommended initial backend)
- GStreamer via GI bindings (optional backend)
- websockets (if using Jellyfin realtime event channels)

## Development dependencies

- pytest
- pytest-asyncio
- ruff
- mypy (recommended)

## Configuration Model

Use environment variables + settings file, loaded by pydantic-settings.

Core settings:

- adapter index
- dvbstreamer executable path
- dvbctrl executable path
- dvbstreamer bind address/host
- recording output root
- preferred playback backend (mpv or gst)
- schedules direct username
- schedules direct password or application password
- schedules direct country/postal code and lineup ids
- guide sync cadence and retention window
- jellyfin URL and credentials/token

## Process Management and Reliability

Rules:

- External process ownership belongs to DvbStreamerManager only.
- No module outside tvrecorder directly launches/kills dvbstreamer.
- Every external command and response is logged with correlation ids.

Recovery behaviors:

- Detect process exit and transition to recoverable error state.
- Attempt bounded restart with exponential backoff.
- Surface user-visible errors in UI with actionable text.

Exception handling policy:

- Catch at module boundaries (entrypoint, worker loops, external I/O).
- Avoid blanket try/except in internal pure logic functions.
- Use notify/raise/exit semantics consistently by boundary type.
- See docs/error-handling.md and src/ccatv/errors.py for the project standard.

## Recording Lifecycle

1. Scheduler dispatches recording job.
2. Job enters pre-roll and verifies tuner lock.
3. Job tunes/selects service through DvbCtrlClient.
4. Stream output is written to target recording path.
5. Job enters post-roll, finalizes output, stores metadata.
6. Job marks success or failure and emits notification.

Recording metadata to store:

- channel/service id
- start/end timestamps
- title/subtitle/description
- filepath
- tuner health snapshots (optional)

## Security and Licensing Notes

- Project is GPLv3-compatible by intent.
- If embedding third-party libraries, verify license compatibility per dependency.
- Do not store plaintext secrets in repo; keep credentials in local config/env.

## Testing Strategy

## Unit tests

- command parsing and error mapping for DvbCtrlClient
- scheduler conflict logic
- input event mapping
- playback backend state transitions (mocked)
- schedules direct payload parsing, normalization, and channel mapping

## Integration tests

- process manager behavior with controlled dvbstreamer/dummy process
- end-to-end timer to recording job state transition
- persistence tests for timers and recordings
- scheduled guide sync jobs and incremental upsert behavior

## Milestones

1. Foundation
   - Create project package layout.
   - Implement settings, logging, and storage base.
2. TV core
   - Implement DvbStreamerManager and DvbCtrlClient.
   - Implement channel tuning service.
3. Playback
   - Implement MpvBackend and Live TV screen integration.
4. Recording
   - Implement scheduler and recording pipeline.
5. Metadata (Schedules Direct) and Jellyfin
  - Integrate guide metadata pipeline and Jellyfin browsing.
6. Hardening
   - Add recovery paths, diagnostics, and broad integration tests.

## Initial Recommended Build Order

1. tvrecorder process and command adapters
2. mpv playback adapter
3. minimal Live TV UI path
4. scheduler and recorder lifecycle
5. Schedules Direct EPG and Jellyfin integration
6. optional GStreamer backend

## Open Design Decisions

- Single tuner first vs multi-tuner from day one.
- Long-term preference between mpv and GStreamer as default backend.
- Whether to keep guide metadata strictly from Schedules Direct or blend in DVB SI as a fallback.
- Recording container/format and file naming policy.

## Legacy Reference and Migration

Prior art exists in an older Schedules Direct API class:

- https://github.com/ccdale/tvrecord/blob/main/tvrecord/tvrecordsd/sdapi.py

Migration guidance:

- Rebuild the client using typed models, token refresh handling, explicit retry/backoff, and structured logging.
- Keep adapter interface narrow so provider internals can evolve without touching UI/scheduler modules.
- Add contract tests from captured API responses to prevent regressions over time.
