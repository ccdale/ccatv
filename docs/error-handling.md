# ccatv Error Handling Policy

Status: draft
Date: 2026-05-17

## Goals

- Keep failure handling predictable and visible.
- Avoid silent failures and duplicated error noise.
- Preserve full traceback context for debugging.
- Use user-facing notifications only at the right boundaries.

## Core Rules

1. Do not wrap every function in try/except.
2. Catch exceptions at boundaries:
   - CLI/app entrypoint
   - background job loop boundaries
   - external I/O (network, subprocess, file system)
3. Let internal pure/domain functions raise naturally.
4. Convert low-level exceptions to domain errors only when adding context.
5. Exit the process only at application edges.

## Notify vs Raise vs Exit

Use these semantics consistently:

- `errorNotify(...)`: log/print contextual error details and continue or handle upstream.
- `errorRaise(...)`: notify and re-raise the current exception.
- `errorExit(...)`: notify and terminate process with non-zero exit code.

## Decorator Guidance

Decorators should only wrap call-time execution, not definition-time.

Correct pattern:

- wrapper calls the function
- wrapper returns the original function result
- wrapper catches exceptions from function execution
- wrapper delegates to `errorNotify`/`errorRaise`/`errorExit`

Use decorators for boundary handlers (jobs/commands/tasks), not for all internal helpers.

## ccatv Defaults

- In library/application modules: prefer `errorRaise` or typed re-raise.
- In long-running worker loops: `errorNotify` plus retry/backoff policy.
- In top-level process bootstrap: `errorExit` on unrecoverable startup failures.

## Reference Implementation

See [src/ccatv/errors.py](../src/ccatv/errors.py) for a reusable implementation used by ccatv.
