#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/bin"

mkdir -p "${TARGET_DIR}"
install -m 755 "${REPO_DIR}/scripts/ccatv-start" "${TARGET_DIR}/ccatv-start"
install -m 755 "${REPO_DIR}/scripts/ccatv-stop" "${TARGET_DIR}/ccatv-stop"

echo "Installed ccatv-start and ccatv-stop to ${TARGET_DIR}"
