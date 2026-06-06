# Service Filter Usage

dvbstreamer has the ability to stream more than one service at the same time. This is achieved by using the service filter options. Basically, the full multiplex contains a number of services, denoted by 'program identifiers' - PIDs. There are seperate PIDs for the video, audio and subtitle streams (as well as additional audio and control streams).

The service filter's job is to filter out from the full multiplex stream only the required PIDs for a specific service.

Additionally, you can tell dvbstreamer to only filter the main Audio, Video and Subtitle streams, ignoring everything else.

It is possible to stream more than one service from the same dvbstreamer adapter, so long as all the services share the same multiplex.

Therefore, it should be a simple matter to decide which dvbstreamer to use to make a new recording:

1. find the multiplex id for the required service
    * `serviceinfo` command
2.
    * audit the running dvbstreamers to find the one already tuned to that multiplex
        * `lssfs` command lists current service filters, if none, the whole adapter is free to use
        * `getsf` command shows the service currently being streamed on a service filter
        * use a combination of these commands plus `serviceinfo` to assess which multiplex the dvbstreamer is currently tuned to.
    * OR start a new dvbstreamer on the next adapter and tune it to the required multiplex
3. Add a new service filter, possibly named after the channel, though no spaces in the name, setting it to output to the null:// endpoint.
4. set the service to be streamed for the new service filter
5. set the output / endpoint for the service filter to be the output file
6. when the programme being recorded is finished set the output to the null:// endpoint again
7. delete the service filter

by using service filters more programmes can be recorded so long as the services share a mux. This will remove the commands `select`, `getmrl` and `setmrl` as the `<Primary>` service will no longer be used.

## Migration Plan: Move Away From `<Primary>` (Current Implementation Only)

This plan is intentionally scoped to the current recording flow only (start capture, run capture, stop capture, cleanup). It does not change future scheduling policy or cross-mux planning yet.

### Current Behavior To Replace

Today, `DvbCtrlCaptureController` does this for each recording:

1. resolve channel name to dvbstreamer service name
2. `select <service>` on `<Primary>`
3. `setmrl file://...` on `<Primary>`
4. on stop, `setmrl null://`

This means one active recording path per adapter slot, with capture coupled to `<Primary>` state.

### Target Behavior

For each recording, use an explicit service filter:

1. `addsf <filter_name> null://`
2. `setsf <filter_name> <service_name>`
3. optional: `setsfavsonly <filter_name> on`
4. `setsfmrl <filter_name> file://...`
5. on stop: `setsfmrl <filter_name> null://`
6. always cleanup: `rmsf <filter_name>`

`<Primary>` is no longer part of the recording path.

### Implementation Steps

1. Add typed service methods in `TvRecorderService` for service-filter lifecycle.
2. Add a new capture controller (for example `ServiceFilterCaptureController`) that:
     - creates a deterministic filter name per recording/job
     - starts output via `setsfmrl`
     - stops output via `setsfmrl ... null://`
     - removes the filter in a `finally` cleanup path
3. Switch orchestrator wiring in bootstrap from `DvbCtrlCaptureController` to the new controller.
4. Keep adapter allocation unchanged in this phase: one active job per adapter slot is still fine.
5. Add structured logs that include filter name, service name, adapter index, and output path.
6. Keep cleanup best-effort and idempotent (ignore missing filter on repeated cleanup).

### Effect On Current Code Paths

- `src/ccatv/tvrecorder/orchestrator.py`:
    - no scheduler-state model changes required
    - `start_capture` and `stop_capture` behavior changes under the controller
    - exception path should still attempt capture cleanup
- `src/ccatv/tvrecorder/service.py`:
    - gains service-filter helper methods wrapping typed commands
- `src/ccatv/app/bootstrap.py`:
    - controller wiring changes for base service and adapter slots
- `src/ccatv/tvrecorder/commands.py`:
    - service-filter command builders already exist; no new command primitives needed for this phase

### Test Impact (Current Scope)

Add or update tests for:

1. service methods invoking `addsf`, `setsf`, `setsfmrl`, `rmsf`
2. capture controller start/stop/cleanup sequences
3. cleanup behavior when stop or remove commands fail
4. orchestrator behavior remains unchanged from a state-transition perspective

### Deferred (Out Of Scope For This Plan)

1. choosing adapters based on mux affinity for future jobs
2. allowing multiple concurrent recordings per adapter slot
3. long-lived reusable filters and pooling policies
4. migration of historical/operational tooling around `<Primary>`