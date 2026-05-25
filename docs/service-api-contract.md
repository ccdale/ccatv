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
  "appVersion": "0.1.119",
  "apiVersion": "v1alpha1",
  "capabilities": [
    "service.health",
    "service.info",
    "recording.worker.cycle",
    "metadata.sd.sync"
  ],
  "commands": [
    "service.health.get",
    "service.info.get",
    "recording.worker.cycle.run",
    "metadata.sd.sync.run"
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
| `ccatv setup` runtime credential/config mutation | N/A in M1 | Deferred | Remains local CLI-side in M1; migration evaluated in M2/M4. |
| scheduler create/list APIs | `recording.schedule.create`, `recording.schedule.list` | Deferred | Contracted but not wired to dispatcher in current M1 code. |
| metadata checkpoint/status read | `metadata.sd.sync.status.get` | Deferred | Contracted but not wired to dispatcher in current M1 code. |

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

#### `recording.schedule.list`
- Purpose: List scheduler jobs.

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

## Milestone Mapping

M1 (this draft):
- Define command names, payloads, envelope shape, and error model.

M2:
- Bind these commands to in-process use-cases.

M3:
- Expose the contract over daemon transport.
