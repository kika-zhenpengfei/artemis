"""Exercise Windows bootstrap boundaries without installing tools or changing PATH."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def test_windows_codex_entry_bypasses_system_dependency_installer():
    batch = (ROOT / "install_codex.bat").read_text(encoding="ascii")
    assert 'scripts\\install_codex.ps1" %*' in batch
    assert "install_deps.ps1" not in batch
    legacy = (ROOT / "scripts/install_deps.ps1").read_text(encoding="ascii")
    redirect = legacy.index("if ($Codex) {")
    environment_changes = legacy.index('$ErrorActionPreference = "Continue"')
    assert redirect < environment_changes
    assert "exit $LASTEXITCODE" in legacy[redirect:environment_changes]


def test_windows_daily_entry_uses_project_environment():
    batch = (ROOT / "artemis.bat").read_text(encoding="ascii")
    assert "setlocal" in batch
    assert 'cd /d "%~dp0"' in batch
    assert '"%~dp0.venv\\Scripts\\python.exe" -m scripts.run_artemis %*' in batch
    assert "install_codex.bat" in batch
    assert "exit /b %ERRORLEVEL%" in batch
    assert "uv run" not in batch


def test_isolated_windows_entry_has_no_system_install_or_persistent_path_commands():
    source = (ROOT / "scripts/install_codex.ps1").read_text(encoding="ascii")
    for forbidden in (
        "winget",
        "choco",
        "scoop",
        "nvm",
        "start-server",
        "install.ps1",
        '"User"',
        '"Machine"',
        "Set-ItemProperty",
        "setx",
    ):
        assert forbidden not in source


def test_windows_bootstrap_rejects_linked_managed_directories_before_running_uv():
    source = (ROOT / "scripts/install_codex.ps1").read_text(encoding="ascii")
    checks = source[source.index("$ManagedDirectories = @(") : source.index("    $Uv = Join-Path")]
    for entry in (
        "$ToolsDir",
        "$UvDir",
        '(Join-Path $ToolsDir "python")',
        '(Join-Path $ToolsDir "uv-cache")',
        '(Join-Path $ToolsDir "bin")',
        '(Join-Path $RootDir ".venv")',
    ):
        assert entry in checks
    assert "foreach ($Directory in $ManagedDirectories)" in checks
    assert "[IO.FileAttributes]::ReparsePoint" in checks
    assert "throw" in checks


@pytest.fixture
def run_windows_bootstrap(tmp_path):
    if not POWERSHELL:
        pytest.skip("PowerShell is not installed on this host")
    root = tmp_path / "checkout with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for filename in ("install_codex.ps1", "install_deps.ps1"):
        shutil.copy2(ROOT / "scripts" / filename, scripts / filename)
    log = tmp_path / "uv-call.json"
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        r"""
$ErrorActionPreference = "Stop"
$env:PROCESSOR_ARCHITECTURE = "AMD64"
Remove-Item Env:\PROCESSOR_ARCHITEW6432 -ErrorAction SilentlyContinue
function Get-Command {
    param($Name, $CommandType, $ErrorAction)
    if ($Name -eq "uv.exe") {
        return [PSCustomObject]@{ Source = "Invoke-FakeUv" }
    }
    throw "Unexpected command lookup: $Name"
}
function Invoke-FakeUv {
    $entry = @{
        arguments = @($args)
        cwd = (Get-Location).Path
        python = $env:UV_PYTHON_INSTALL_DIR
        cache = $env:UV_CACHE_DIR
        bin = $env:UV_PYTHON_BIN_DIR
        no_modify_path = $env:UV_NO_MODIFY_PATH
        preference = $env:UV_PYTHON_PREFERENCE
        venv = $env:UV_PROJECT_ENVIRONMENT
        install_registry = $env:UV_PYTHON_INSTALL_REGISTRY
        no_registry = $env:UV_PYTHON_NO_REGISTRY
        install_bin = $env:UV_PYTHON_INSTALL_BIN
        no_env_file = $env:UV_NO_ENV_FILE
        working_dir = $env:UV_WORKING_DIR
        project = $env:UV_PROJECT
    }
    $entry | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $env:BOOTSTRAP_LOG -Encoding UTF8
    $global:LASTEXITCODE = [int]$env:BOOTSTRAP_EXIT_CODE
}
function Invoke-WebRequest { throw "Unexpected download" }
function Start-Process { throw "Unexpected process launch" }
function winget { throw "Unexpected system install" }
function choco { throw "Unexpected system install" }
function scoop { throw "Unexpected system install" }
function nvm { throw "Unexpected global Node change" }
function adb { throw "Unexpected device command" }
$parameters = @{}
if ($env:BOOTSTRAP_LEGACY -eq "1") { $parameters["Codex"] = $true }
if ($env:BOOTSTRAP_LAUNCH -eq "1") { $parameters["Launch"] = $true }
if ($env:BOOTSTRAP_HELP -eq "1") { $parameters["Help"] = $true }
& $env:BOOTSTRAP_ENTRY @parameters
exit $LASTEXITCODE
""",
        encoding="ascii",
    )

    def run(*, launch=False, legacy=False, help_only=False, exit_code=0, linked_directory=None):
        if linked_directory:
            target = tmp_path / "external-tools"
            target.mkdir()
            linked = root / linked_directory
            linked.parent.mkdir(parents=True, exist_ok=True)
            try:
                linked.symlink_to(target, target_is_directory=True)
            except OSError as error:
                pytest.skip(f"Creating directory symlinks is unavailable: {error}")
        environment = {
            **os.environ,
            "BOOTSTRAP_LOG": str(log),
            "BOOTSTRAP_ENTRY": str(
                scripts / ("install_deps.ps1" if legacy else "install_codex.ps1")
            ),
            "BOOTSTRAP_EXIT_CODE": str(exit_code),
            "BOOTSTRAP_LEGACY": str(int(legacy)),
            "BOOTSTRAP_LAUNCH": str(int(launch)),
            "BOOTSTRAP_HELP": str(int(help_only)),
            "UV_PYTHON_INSTALL_DIR": str(tmp_path / "unrelated-python"),
            "UV_CACHE_DIR": str(tmp_path / "unrelated-cache"),
            "UV_PROJECT_ENVIRONMENT": str(tmp_path / "unrelated-venv"),
            "UV_PYTHON_INSTALL_REGISTRY": "1",
            "UV_PYTHON_NO_REGISTRY": "0",
            "UV_PYTHON_INSTALL_BIN": "1",
            "UV_NO_ENV_FILE": "0",
            "UV_ENV_FILE": str(tmp_path / "unrelated.env"),
            "UV_WORKING_DIR": str(tmp_path / "unrelated-workdir"),
            "UV_PROJECT": str(tmp_path / "unrelated-project"),
        }
        completed = subprocess.run(
            [POWERSHELL, "-NoProfile", "-File", str(harness)],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(log.read_text(encoding="utf-8-sig")) if log.exists() else None
        return completed, payload, root

    return run


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("launch", [False, True])
def test_windows_bootstrap_bounds_uv_paths_to_project_and_forwards_launch(
    run_windows_bootstrap, legacy, launch
):
    completed, payload, root = run_windows_bootstrap(legacy=legacy, launch=launch)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert payload["arguments"] == [
        "run",
        "--directory",
        str(root),
        "--no-project",
        "--python",
        "3.12",
        "python",
        str(root / "scripts/install_codex.py"),
        *(["--launch"] if launch else []),
    ]
    assert Path(payload["cwd"]) == root
    for key, directory in (("python", "python"), ("cache", "uv-cache"), ("bin", "bin")):
        assert Path(payload[key]) == root / ".artemis-tools" / directory
    assert Path(payload["venv"]) == root / ".venv"
    assert payload["no_modify_path"] == "1"
    assert payload["preference"] == "managed"
    assert payload["install_registry"] == "0"
    assert payload["no_registry"] == "1"
    assert payload["install_bin"] == "0"
    assert payload["no_env_file"] == "1"
    assert Path(payload["working_dir"]) == root
    assert Path(payload["project"]) == root


def test_windows_bootstrap_propagates_shared_installer_failure(run_windows_bootstrap):
    completed, payload, _ = run_windows_bootstrap(exit_code=23)
    assert payload is not None
    assert completed.returncode == 23


@pytest.mark.parametrize(
    "directory",
    [
        ".artemis-tools",
        ".artemis-tools/uv",
        ".artemis-tools/python",
        ".artemis-tools/uv-cache",
        ".artemis-tools/bin",
        ".venv",
    ],
)
def test_windows_bootstrap_rejects_existing_tool_directory_links(run_windows_bootstrap, directory):
    completed, payload, _ = run_windows_bootstrap(linked_directory=directory)
    assert completed.returncode != 0
    assert payload is None
    assert "linked directory" in completed.stdout


def test_windows_bootstrap_help_does_not_initialize_environment(run_windows_bootstrap):
    completed, payload, root = run_windows_bootstrap(help_only=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert payload is None
    assert not (root / ".artemis-tools").exists()
