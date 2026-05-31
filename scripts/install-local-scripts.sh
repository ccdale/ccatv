#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/bin"

mkdir -p "${TARGET_DIR}"
install -m 755 "${REPO_DIR}/scripts/ccatv-start" "${TARGET_DIR}/ccatv-start"
install -m 755 "${REPO_DIR}/scripts/ccatv-stop" "${TARGET_DIR}/ccatv-stop"
install -m 755 "${REPO_DIR}/scripts/ccatv-bounce-flask" "${TARGET_DIR}/ccatv-bounce-flask"
install -m 755 "${REPO_DIR}/scripts/ccatv-epg-daily" "${TARGET_DIR}/ccatv-epg-daily"

echo "Installed ccatv-start, ccatv-stop, ccatv-bounce-flask, and ccatv-epg-daily to ${TARGET_DIR}"
