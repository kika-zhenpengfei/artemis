#!/usr/bin/env bash
# Bootstrap the Codex installer without changing shared tools or shell settings.
set -euo pipefail

LAUNCH=false
SHOW_HELP=false
for arg in "$@"; do
    case "${arg}" in
        --launch|-l|--open) LAUNCH=true ;;
        --help|-h) SHOW_HELP=true ;;
        *)
            echo "Unknown option: ${arg}" >&2
            exit 2
            ;;
    esac
done
if [ "${SHOW_HELP}" = true ]; then
    echo "Usage: bash install_codex.sh [--launch]"
    echo "Install Codex executor using project-local tools in .artemis-tools."
    echo "--launch, -l, --open: start WebUI after installation."
    echo "No system package manager, shared Node installation, or shell profile is modified."
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOOLS_DIR="${ROOT_DIR}/.artemis-tools"
UV_VERSION="0.11.18"
OS_TYPE="$(uname -s)"
ARCH_TYPE="$(uname -m)"

case "${ARCH_TYPE}" in
    arm64|aarch64) UV_ARCH=aarch64 ;;
    x86_64|amd64) UV_ARCH=x86_64 ;;
    *)
        echo "Unsupported architecture: ${ARCH_TYPE}. Expected arm64 or x86_64." >&2
        exit 1
        ;;
esac
case "${OS_TYPE}" in
    Darwin) UV_TARGET="${UV_ARCH}-apple-darwin" ;;
    Linux)
        LIBC_VERSION="$(ldd --version 2>&1 || true)"
        if [[ "${LIBC_VERSION}" = *musl* ]]; then
            echo "This installer supports glibc Linux; musl Linux is not supported." >&2
            exit 1
        fi
        if ! getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
            echo "Cannot identify glibc. Use a glibc Linux environment for this installer." >&2
            exit 1
        fi
        UV_TARGET="${UV_ARCH}-unknown-linux-gnu"
        ;;
    *)
        echo "Unsupported operating system: ${OS_TYPE}. Use install_codex.bat on Windows." >&2
        exit 1
        ;;
esac

for local_directory in "${TOOLS_DIR}" "${TOOLS_DIR}/uv" "${TOOLS_DIR}/python" \
    "${TOOLS_DIR}/uv-cache" "${TOOLS_DIR}/bin" "${ROOT_DIR}/.venv"; do
    if [ -L "${local_directory}" ]; then
        echo "Refusing to write through a linked directory: ${local_directory}" >&2
        exit 1
    fi
done
mkdir -p "${TOOLS_DIR}"
export UV_PYTHON_INSTALL_DIR="${TOOLS_DIR}/python"
export UV_CACHE_DIR="${TOOLS_DIR}/uv-cache"
export UV_PYTHON_BIN_DIR="${TOOLS_DIR}/bin"
export UV_NO_MODIFY_PATH=1
export UV_PYTHON_INSTALL_BIN=0
export UV_PYTHON_INSTALL_REGISTRY=0
export UV_PYTHON_NO_REGISTRY=1
export UV_NO_ENV_FILE=1
export UV_PYTHON_PREFERENCE=managed
export UV_PROJECT_ENVIRONMENT="${ROOT_DIR}/.venv"

UV_BIN="$(command -v uv || true)"
if [ -z "${UV_BIN}" ]; then
    UV_BIN="${TOOLS_DIR}/uv/uv"
    if [ -e "${TOOLS_DIR}/uv" ] || [ -L "${TOOLS_DIR}/uv" ]; then
        if [ -L "${TOOLS_DIR}/uv" ] || [ ! -x "${UV_BIN}" ]; then
            echo "Invalid project-local uv installation: ${TOOLS_DIR}/uv. Inspect it before retrying." >&2
            exit 1
        fi
    else
        for command_name in curl tar mktemp mv; do
            if ! command -v "${command_name}" >/dev/null 2>&1; then
                echo "Required bootstrap command is missing: ${command_name}" >&2
                exit 1
            fi
        done
        UV_STAGE="$(mktemp -d "${TOOLS_DIR}/.uv-download.XXXXXX")"
        cleanup_uv_stage() {
            if [ -n "${UV_STAGE:-}" ] && [ -d "${UV_STAGE}" ]; then
                rm -rf -- "${UV_STAGE}"
            fi
        }
        trap cleanup_uv_stage EXIT
        UV_ARCHIVE="uv-${UV_TARGET}"
        UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}.tar.gz"
        echo "Downloading uv ${UV_VERSION} into ${TOOLS_DIR}/uv..."
        curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
            --output "${UV_STAGE}/uv.tar.gz" "${UV_URL}"
        tar -xzf "${UV_STAGE}/uv.tar.gz" -C "${UV_STAGE}" \
            "${UV_ARCHIVE}/uv" "${UV_ARCHIVE}/uvx"
        if [ ! -x "${UV_STAGE}/${UV_ARCHIVE}/uv" ]; then
            echo "Downloaded archive does not contain an executable uv." >&2
            exit 1
        fi
        "${UV_STAGE}/${UV_ARCHIVE}/uv" --version
        if [ -e "${TOOLS_DIR}/uv" ] || [ -L "${TOOLS_DIR}/uv" ]; then
            echo "Project-local uv appeared during installation; retry after the other installer finishes." >&2
            exit 1
        fi
        mv "${UV_STAGE}/${UV_ARCHIVE}" "${TOOLS_DIR}/uv"
        cleanup_uv_stage
        trap - EXIT
    fi
fi

"${UV_BIN}" --version
export PATH="${TOOLS_DIR}/uv:${TOOLS_DIR}/bin:${PATH}"
INSTALL_ARGS=()
if [ "${LAUNCH}" = true ]; then
    INSTALL_ARGS+=(--launch)
fi
cd "${ROOT_DIR}"
"${UV_BIN}" run --directory "${ROOT_DIR}" --no-project --python 3.12 python "${SCRIPT_DIR}/install_codex.py" ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}
