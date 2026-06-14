# Discarded and Legacy Scope

This document tracks areas that are no longer active priorities, paused, or kept only as historical reference.

## Current Primary Scope

Active development focus is:

- recorder/scheduler service (`ccatv-service`)
- Flask control web app (`ccatv-web`)
- CLI operations and metadata ingestion workflows

Playback and library UX are expected to be handled by external tools (for example, Kodi).

## GTK4 Frontend

GTK4 was previously explored, but there is currently no shipped end-user GTK4 frontend entrypoint.

Code under GTK-facing or playback abstraction modules may remain in the repository as groundwork, but it is not the primary product direction at this time.

## OS Packaging and Service-Manager Docs

The repository still contains packaging and service-manager artifacts/documents. Treat these as reference material unless explicitly revived:

- `docs/packaging.md`
- `docs/systemd-operations.md`
- `archlinux/PKGBUILD`
- `systemd/*.service`
- helper scripts under `scripts/`

These are not the canonical definition of the current product direction.

## How To Use This Document

When README and older docs appear to disagree:

1. README describes the currently supported behavior and entrypoints.
2. This document captures discarded/paused/historical areas.
3. Older design/proposal docs should be treated as historical unless updated.
