#!/usr/bin/env bash
# Run this checkout with its own Python environment and tools.
set -euo pipefail
ARTEMIS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTEMIS_PYTHON="${ARTEMIS_ROOT}/.venv/bin/python"
if [ ! -x "${ARTEMIS_PYTHON}" ]; then
    echo "ARTEMIS is not installed in this checkout. Run: bash install_codex.sh" >&2
    exit 1
fi
cd "${ARTEMIS_ROOT}"
exec "${ARTEMIS_PYTHON}" -m scripts.run_artemis "$@"
