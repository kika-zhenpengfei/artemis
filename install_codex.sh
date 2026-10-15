#!/usr/bin/env bash
# Install this checkout for Codex executor without configuring model credentials.
set -euo pipefail
ARTEMIS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${ARTEMIS_ROOT}/scripts/install_codex.sh" "$@"
