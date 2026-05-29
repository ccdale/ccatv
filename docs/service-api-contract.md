# ccatv Service API Contract (M1 Draft)

## Purpose

This document defines the first stable contract between the long-running ccatv
service process and external front ends (CLI, GTK4, Flask/FastAPI).

M1 scope is contract definition only. Transport can be local Unix socket JSON
or HTTP in future milestones. Payloads and behaviors defined here are transport
agnostic.

## Versioning

- Contract version: `v1alpha1`
- Every request MUST include `apiVersion` and `command`.
- Unknown commands return `error.code = "UNSUPPORTED_COMMAND"`.

Request envelope:

```json
{
  "apiVersion": "v1alpha1",
  "command": "recording.schedule.create",
  "requestId": "optional-client-correlation-id",
  "payload": {}
}
```

Response envelope:

```json
{
  "apiVersion": "v1alpha1",
  "requestId": "echoed-if-provided",
  "ok": true,
  "payload": {}
}
```

Error envelope:

```json
{
  "apiVersion": "v1alpha1",
  "requestId": "echoed-if-provided",
  "ok": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "human-readable summary",
    "retryable": false,
    "details": {}
  }
}
```

## Error Codes

Common:
- `VALIDATION_ERROR`
- `NOT_FOUND`
- `CONFLICT`
- `UNSUPPORTED_COMMAND`
- `AUTH_REQUIRED`
- `PERMISSION_DENIED`
- `INTERNAL_ERROR`

Recorder domain:
- `RECORDING_NOT_RUNNABLE`
- `SCHEDULER_JOB_NOT_DUE`
- `PRECHECK_FAILED`

Metadata domain:
- `SD_AUTH_FAILED`
- `SD_RATE_LIMITED`
- `SD_UPSTREAM_ERROR`

## Command Set (M1)

### Health and Service Info

#### `service.health.get`
- Purpose: Liveness and key subsystem readiness.
- Payload: `{}`

Response payload:

```json
{
  "status": "ok",
  "timeUtc": "2026-05-23T16:00:00Z",
  "database": {
    "path": "...",
    "reachable": true,
    "readable": true,
    "writable": true,
    "error": null,
    "failedAt": null
  },
  "recorder": {
    "workerEnabled": true
  }
}
```

#### `service.info.get`
- Purpose: Runtime metadata and feature flags.
- Payload: `{}`

Response payload:

```json
{
  "appName": "ccatv",
  "appVersion": "0.1.122",
  "apiVersion": "v1alpha1",
  "capabilities": [
    "service.health",
    "service.info",
    "recording.schedule",
    "recording.worker.cycle",
    "metadata.guide",
    "metadata.sd.sync",
    "runtime.setup"
  ],
  "commands": [
    "service.health.get",
    "service.info.get",
    "recording.schedule.create",
    "recording.schedule.list",
    "recording.worker.cycle.run",
    "metadata.guide.list",
    "metadata.sd.sync.run",
    "metadata.sd.sync.status.get",
    "runtime.setup.save"
  ]
}
```

Capability naming rules:
- `capabilities` are stable namespace prefixes.
- `commands` is the exact dispatchable command list for the running service.
- Clients MUST invoke commands from `commands` rather than inferring full command names.
- Every command in `commands` MUST begin with one of the prefixes in `capabilities`.
- `service.info.get` is the source of truth for currently supported commands in this runtime.
- Commands marked as deferred in this document are contract targets and are intentionally not discoverable until implemented: they MUST NOT appear in `service.info.get` capability/command lists and MUST return `UNSUPPORTED_COMMAND` when invoked.

## M1 Capability Matrix and CLI Migration Mapping

This maps existing CLI/runtime flows to the M1 service command surface.

| Existing front-end flow | M1 command(s) | Status | Notes |
| --- | --- | --- | --- |
| `ccatv-service --run-once` recorder cycle | `recording.worker.cycle.run` | Implemented | Single-cycle execution now available through dispatcher command path. |
| `ccatv epg-sync-sd --lineup-id ...` one-shot sync | `metadata.sd.sync.run` | Implemented | Lineup/window/seed path supported; timeout and shutdown cancellation hardened. |
| service liveness/status | `service.health.get` | Implemented | Includes read/write DB readiness details and probe failure step diagnostics. |
| service metadata + features | `service.info.get` | Implemented | Returns app metadata, API version, and concrete capability list. |
| `ccatv setup` runtime credential/config mutation | `runtime.setup.save` | Implemented | CLI now routes setup persistence through service command dispatch path. |
| scheduler create/list APIs | `recording.schedule.create`, `recording.schedule.list` | Implemented | Dispatcher now supports scheduling and listing jobs through service command handlers. |
| one-channel guide listing API | `metadata.guide.list` | Implemented | Returns channel-filtered guide broadcasts in a target UTC window for Flask channel/programme selection workflows. |
| metadata checkpoint/status read | `metadata.sd.sync.status.get` | Implemented | Dispatcher now returns latest ingest run and checkpoint for Schedules Direct. |

### Recorder Scheduling

#### `recording.schedule.create`
- Purpose: Create a scheduled recording job.

Payload:

```json
{
  "channelName": "BBC TWO HD",
  "startAtUtc": "2026-05-24T19:00:00Z",
  "durationSeconds": 3600
}
```

Response payload:

```json
{
  "job": {
    "id": 123,
    "state": "scheduled"
  }
}
```

### Guide Listing

#### `metadata.guide.list`
- Purpose: List guide programmes for one channel inside a UTC time window.

Payload:

```json
{
  "channel": "BBC TWO HD",
  "startAtUtc": "2026-05-24T19:00:00Z",
  "windowHours": 4
}
```

Notes:
- `channel` is required.
- `startAtUtc` is optional; when omitted, current UTC time is used.
- `windowHours` is optional, defaults to `6`, and must be greater than 0.
- In each `programs` entry, `callsign`, `logicalChannelNumber`, `stopAtUtc`, and `description` may be `null` when upstream source data does not provide values.

Response payload:

```json
{
  "channel": "BBC TWO HD",
  "window": {
    "startAtUtc": "2026-05-24T19:00:00Z",
    "endAtUtc": "2026-05-24T23:00:00Z"
  },
  "programs": [
    {
      "source": "schedules_direct",
      "sourceChannelId": "100",
      "channelName": "BBC TWO HD",
      "callsign": "BBCTWO",
      "logicalChannelNumber": "2",
      "startAtUtc": "2026-05-24T19:00:00Z",
      "stopAtUtc": "2026-05-24T20:00:00Z",
      "durationSeconds": 3600,
      "title": "Newsnight",
      "description": "Late-night news and analysis"
    }
  ]
}
```

#### `recording.schedule.list`
- Purpose: List scheduler jobs.
- Payload `state` filter is optional. When omitted, service returns all jobs.

Payload:

```json
{
  "state": "scheduled"
}
```

Response payload:

```json
{
  "jobs": [
    {
      "id": 123,
      "channelName": "BBC TWO HD",
      "startAtUtc": "2026-05-24T19:00:00Z",
      "durationSeconds": 3600,
      "state": "scheduled"
    }
  ]
}
```

#### `recording.worker.cycle.run`
- Purpose: Execute one due-job cycle immediately.

Payload:

```json
{
  "maxJobsPerCycle": 3,
  "outputDirectory": "/tmp"
}
```

Response payload:

```json
{
  "results": [
    {
      "jobId": 123,
      "schedulerState": "completed",
      "recordingId": 456,
      "recordingState": "ready",
      "error": null
    }
  ]
}
```

### Metadata Sync (Schedules Direct)

#### `metadata.sd.sync.run`
- Purpose: Execute one SD sync pass for a lineup.

Payload:

```json
{
  "lineupId": "USA-OTA-X",
  "seed": false,
  "windowHours": 24
}
```

Response payload:

```json
{
  "stats": {
    "channelsUpserted": 50,
    "programsUpserted": 1800,
    "schedulesUpserted": 2200,
    "staleSchedulesPruned": 130,
    "ingestRunId": 44
  }
}
```

#### `metadata.sd.sync.status.get`
- Purpose: Return latest ingest run/checkpoint status.

Payload:

```json
{
  "source": "schedules_direct"
}
```

Response payload:

```json
{
  "lastRun": {
    "id": 44,
    "status": "ok",
    "finishedAtUtc": "2026-05-23T16:20:00Z"
  },
  "checkpoint": {
    "lastSuccessfulIngestUtc": "2026-05-23T16:20:00Z"
  }
}
```

## Idempotency and Retries

- Commands that mutate data SHOULD accept optional `idempotencyKey`.
- The service SHOULD treat duplicate keys within a replay window as safe retries.
- Front ends SHOULD retry only when `retryable = true`.

## Security and Secrets

- Secrets are runtime only.
- Service MUST NOT include SD username/password/token in responses, logs, or
  error details.
- Local transport should rely on filesystem permissions initially.

## Guide Source Priority Policy

When the same logical programme slot is available from multiple guide sources,
source precedence is:

1. `dvbstreamer_ota` (over-the-air broadcaster EPG)
2. `schedules_direct`

The service should prefer OTA guide data and only fall back to Schedules Direct
for slots not covered by OTA data.

## Milestone Mapping

M1 (this draft):
- Define command names, payloads, envelope shape, and error model.

M2:
- Bind these commands to in-process use-cases.

M3:
- Expose the contract over daemon transport.

## Local IPC Transport (M3)

The daemon now supports local Unix socket transport for request/response
envelopes.

- Transport: `AF_UNIX` stream socket.
- Request body: one JSON command envelope per connection.
- Response body: one JSON response envelope per connection.
- Encoding: UTF-8 JSON.
- Recommended socket permissions: owner read/write only.

The same command contract and response envelope definitions in this document
apply unchanged over the Unix socket transport.
