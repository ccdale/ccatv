#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/bin"

mkdir -p "${TARGET_DIR}"
install -m 755 "${REPO_DIR}/scripts/ccatv-provision" "${TARGET_DIR}/ccatv-provision"
install -m 755 "${REPO_DIR}/scripts/ccatv-bounce-flask" "${TARGET_DIR}/ccatv-bounce-flask"
install -m 755 "${REPO_DIR}/scripts/ccatv-epg-daily" "${TARGET_DIR}/ccatv-epg-daily"
install -m 755 "${REPO_DIR}/scripts/ccatv-status" "${TARGET_DIR}/ccatv-status"
install -m 755 "${REPO_DIR}/scripts/ccatv-verify-systemd-user" "${TARGET_DIR}/ccatv-verify-systemd-user"

echo "Installed ccatv-provision, ccatv-bounce-flask, ccatv-epg-daily, ccatv-status, and ccatv-verify-systemd-user to ${TARGET_DIR}"
