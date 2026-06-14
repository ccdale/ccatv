#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/bin"

mkdir -p "${TARGET_DIR}"
install -m 755 "${REPO_DIR}/scripts/ccatv-start" "${TARGET_DIR}/ccatv-start"
install -m 755 "${REPO_DIR}/scripts/ccatv-stop" "${TARGET_DIR}/ccatv-stop"
install -m 755 "${REPO_DIR}/scripts/ccatv-restart" "${TARGET_DIR}/ccatv-restart"
install -m 755 "${REPO_DIR}/scripts/ccatv-bounce-flask" "${TARGET_DIR}/ccatv-bounce-flask"
install -m 755 "${REPO_DIR}/scripts/ccatv-epg-daily" "${TARGET_DIR}/ccatv-epg-daily"
install -m 755 "${REPO_DIR}/scripts/ccatv-status" "${TARGET_DIR}/ccatv-status"

echo "Installed ccatv-start, ccatv-stop, ccatv-restart, ccatv-bounce-flask, ccatv-epg-daily, and ccatv-status to ${TARGET_DIR}"
