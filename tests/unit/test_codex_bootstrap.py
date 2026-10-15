"""Validate bootstrap failure boundaries without installing tools or using real homes."""

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

from dotenv import dotenv_values
import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bootstrap():
    spec = importlib.util.spec_from_file_location(
        "configure_codex_under_test", ROOT / "scripts/configure_codex.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_index(root):
    index = root / "apps/showcase_ui/dist/frontend/browser/index.html"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("existing build", encoding="utf-8")
    return index


def test_new_env_uses_template_and_executor_settings(bootstrap, tmp_path):
    (tmp_path / ".env.example").write_text(
        "# Local UI defaults\nARTEMIS_UI_PORT=8123\n", encoding="utf-8"
    )

    assert bootstrap.configure_env(tmp_path) is None

    target = tmp_path / ".env"
    values = dotenv_values(target)
    assert values == {"ARTEMIS_UI_PORT": "8123", **bootstrap.EXECUTOR_ENV}
    assert "# Local UI defaults" in target.read_text(encoding="utf-8")
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_existing_env_changes_only_executor_fields_and_is_idempotent(bootstrap, tmp_path):
    original = (
        "# Keep local paths and ports\n"
        "ARTEMIS_UI_PORT=8123\n"
        "ARTEMIS_DAEMON_PORT=8123\n"
        "ARTEMIS_TRACES_DIR='/custom traces'\n"
        "LOCAL_VALUE='keep ${LITERAL}' # Keep this comment\n"
        "ARTEMIS_AGENT_CLIENT=zcode\n"
        "ARTEMIS_EXECUTION_MODE=autonomous\n"
        "ARTEMIS_EXECUTOR_ONLY=0\n"
    )
    target = tmp_path / ".env"
    target.write_text(original, encoding="utf-8")
    target.chmod(0o640)
    before = dotenv_values(target, interpolate=False)

    backup = bootstrap.configure_env(tmp_path)

    assert backup.read_text(encoding="utf-8") == original
    assert dotenv_values(target, interpolate=False) == {**before, **bootstrap.EXECUTOR_ENV}
    updated = target.read_bytes()
    assert b"# Keep local paths and ports" in updated
    assert b"LOCAL_VALUE='keep ${LITERAL}' # Keep this comment" in updated
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
        assert target.stat().st_mode & 0o777 == 0o640
    assert bootstrap.configure_env(tmp_path) is None
    assert target.read_bytes() == updated
    assert list(tmp_path.glob(".env.codex-backup-*")) == [backup]


@pytest.mark.parametrize("failure", ["set_key", "replace"])
def test_env_write_failure_preserves_original_and_removes_temporary_file(
    bootstrap, tmp_path, monkeypatch, failure
):
    target = tmp_path / ".env"
    original = b"# User configuration\nARTEMIS_UI_PORT=8123\n"
    target.write_bytes(original)

    def fail(*args, **kwargs):
        raise OSError("simulated write failure")

    owner = bootstrap if failure == "set_key" else bootstrap.os
    monkeypatch.setattr(owner, failure, fail)

    with pytest.raises(OSError, match="simulated write failure"):
        bootstrap.configure_env(tmp_path)

    assert target.read_bytes() == original
    leftovers = list(tmp_path.glob(".env.codex-*"))
    assert len(leftovers) == 1
    assert leftovers[0].name.startswith(".env.codex-backup-")
    assert leftovers[0].read_bytes() == original


@pytest.mark.parametrize("precondition", ["missing_build", "missing_adb", "broken_adb"])
def test_configure_checks_prerequisites_before_changing_user_configuration(
    bootstrap, tmp_path, monkeypatch, precondition
):
    target = tmp_path / ".env"
    target.write_text("LOCAL_SETTING=keep\n", encoding="utf-8")
    if precondition != "missing_build":
        _build_index(tmp_path)
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda name: None if precondition == "missing_adb" else "/stub/adb",
    )
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(17, command)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)

    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        bootstrap.configure(tmp_path)

    assert target.read_text(encoding="utf-8") == "LOCAL_SETTING=keep\n"
    assert calls == ([["adb", "version"]] if precondition == "broken_adb" else [])
    assert not list(tmp_path.glob(".env.codex-*"))


def test_configure_passes_executor_mode_and_propagates_mcp_failure(
    bootstrap, tmp_path, monkeypatch
):
    _build_index(tmp_path)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/stub/adb")
    monkeypatch.setenv("ARTEMIS_UI_PORT", "8123")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == sys.executable:
            raise subprocess.CalledProcessError(19, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)

    with pytest.raises(subprocess.CalledProcessError) as error:
        bootstrap.configure(tmp_path)

    assert error.value.returncode == 19
    command, options = calls[-1]
    assert command == [
        sys.executable,
        "-m",
        "artemis",
        "mcp",
        "--install",
        "codex",
        "--mode",
        "executor",
        "--skip-rules",
    ]
    assert options["cwd"] == tmp_path
    assert options["check"] is True
    assert options["env"]["ARTEMIS_UI_PORT"] == "8123"
    assert all(options["env"][key] == value for key, value in bootstrap.EXECUTOR_ENV.items())


def test_main_reports_install_failure_with_nonzero_status(bootstrap, monkeypatch, capsys):
    def fail(root):
        raise subprocess.CalledProcessError(19, ["mcp", "--install"])

    monkeypatch.setattr(bootstrap, "configure", fail)

    assert bootstrap.main() == 1
    assert "Codex setup failed:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("version", "compatible"),
    [
        ("22.22.2", False),
        ("22.22.3", True),
        ("22.23.0", True),
        ("23.22.3", False),
        ("24.14.9", False),
        ("24.15.0", True),
        ("25.15.0", False),
        ("26.0.0", True),
    ],
)
def test_shell_node_range_matches_angular_requirements(version, compatible):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash is unavailable")
    script = (ROOT / "scripts/install_deps.sh").read_text(encoding="utf-8")
    function = re.search(r"^is_node_compatible\(\) \{.*?^\}", script, re.MULTILINE | re.DOTALL)
    assert function is not None
    result = subprocess.run(
        [
            bash,
            "-c",
            '\nhas_cmd() { return 0; }\nnode() { echo "$TEST_NODE_VERSION"; }\n'
            + function.group()
            + "\nis_node_compatible\n",
        ],
        env={"PATH": os.defpath, "TEST_NODE_VERSION": "v" + version},
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert (result.returncode == 0) == compatible, result.stderr


@pytest.mark.parametrize("system_python", [True, False], ids=["python3", "uv-python"])
def test_portable_adb_zip_fallback_restores_executable_permission(tmp_path, system_python):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash is unavailable")
    script = (ROOT / "scripts/install_deps.sh").read_text(encoding="utf-8")
    function = re.search(r"^install_system_packages\(\) \{.*?^\}", script, re.MULTILINE | re.DOTALL)
    assert function is not None
    archive = tmp_path / "platform-tools.zip"
    entry = zipfile.ZipInfo("platform-tools/adb")
    entry.external_attr = 0o100644 << 16
    with zipfile.ZipFile(archive, "w") as fixture_zip:
        fixture_zip.writestr(
            entry,
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$BOOTSTRAP_TEST_ADB_LOG"\n',
        )
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    calls = tmp_path / "commands.log"
    adb_calls = tmp_path / "adb.log"
    stubs = r"""
set -euo pipefail
BOLD='' GREEN='' BLUE='' YELLOW='' RED='' CYAN='' DIM='' NC=''
OS_TYPE=Linux
ARCH_TYPE=x86_64
record() { printf '%s\n' "$*" >> "$BOOTSTRAP_TEST_LOG"; }
has_cmd() {
    case "$1" in
        adb) [ -x "$HOME/.local/share/platform-tools/adb" ] ;;
        ffmpeg|scrcpy|uv) return 0 ;;
        python3) [ "$BOOTSTRAP_TEST_SYSTEM_PYTHON" = true ] ;;
        *) return 1 ;;
    esac
}
request_sudo() { return 1; }
curl() {
    record "fixture download"
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -o ]; then
            cp "$BOOTSTRAP_TEST_ZIP" "$2"
            return 0
        fi
        shift
    done
    return 97
}
python3() {
    record "python3 $*"
    "$BOOTSTRAP_TEST_PYTHON" "$@"
}
uv() {
    record "uv $*"
    if [ "$1 $2 $3 $4 $5" != 'run --no-project --python 3.12 python' ]; then
        return 97
    fi
    shift 5
    "$BOOTSTRAP_TEST_PYTHON" "$@"
}
# Only the extracted fixture may run, including the installer's start-server call.
adb() { "$HOME/.local/share/platform-tools/adb" "$@"; }
"""
    result = subprocess.run(
        [bash, "-c", stubs + function.group() + "\ninstall_system_packages\n"],
        env={
            "PATH": os.defpath,
            "HOME": str(isolated_home),
            "TMPDIR": str(tmp_path),
            "BOOTSTRAP_TEST_SYSTEM_PYTHON": str(system_python).lower(),
            "BOOTSTRAP_TEST_ZIP": str(archive),
            "BOOTSTRAP_TEST_PYTHON": sys.executable,
            "BOOTSTRAP_TEST_LOG": str(calls),
            "BOOTSTRAP_TEST_ADB_LOG": str(adb_calls),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    installed_adb = isolated_home / ".local/share/platform-tools/adb"
    assert os.access(installed_adb, os.X_OK)
    assert (isolated_home / ".local/bin/adb").resolve() == installed_adb
    assert adb_calls.read_text(encoding="utf-8").splitlines() == ["start-server"]
    commands = calls.read_text(encoding="utf-8").splitlines()
    assert commands[0] == "fixture download"
    expected = (
        "python3 -m zipfile -e "
        if system_python
        else ("uv run --no-project --python 3.12 python -m zipfile -e ")
    )
    assert len(commands) == 2
    assert commands[1].startswith(expected)
