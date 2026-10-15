"""Exercise project-local installation without network or host toolchain changes."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

from scripts import install_codex as installer
from scripts import run_artemis as launcher


@pytest.fixture(autouse=True)
def forbid_real_installation(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("An installation test attempted a real download or external command")

    monkeypatch.setattr(installer, "download_archive", forbidden)
    monkeypatch.setattr(installer.subprocess, "run", forbidden)


def make_archive(path, kind, entries):
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as bundle:
            for name, content in entries.items():
                bundle.writestr(name, content)
    else:
        with tarfile.open(path, "w:gz") as bundle:
            for name, content in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_archive_install_is_local_and_reuses_complete_tool(tmp_path, monkeypatch, kind):
    archive = tmp_path / "download"
    make_archive(archive, kind, {"tool/bin/runner": b"private executable"})
    calls = []

    def download(url, destination):
        calls.append(url)
        shutil.copyfile(archive, destination)

    monkeypatch.setattr(installer, "download_archive", download)
    target = tmp_path / "checkout" / ".artemis-tools" / "tool"
    binary = installer.install_archive("https://example.invalid/tool", target, "tool", "bin/runner")
    assert binary == target / "bin/runner"
    assert binary.read_bytes() == b"private executable"
    if os.name != "nt":
        assert binary.stat().st_mode & 0o111 == 0o111
    binary.write_bytes(b"keep existing tool")
    assert installer.install_archive("unused", target, "tool", "bin/runner") == binary
    assert binary.read_bytes() == b"keep existing tool"
    assert calls == ["https://example.invalid/tool"]
    assert list(target.parent.iterdir()) == [target]


def test_incomplete_existing_tool_is_preserved_without_download(tmp_path):
    target = tmp_path / ".artemis-tools" / "tool"
    target.mkdir(parents=True)
    marker = target / "user-file"
    marker.write_bytes(b"keep this directory")
    with pytest.raises(RuntimeError, match="Incomplete local tool directory"):
        installer.install_archive("unused", target, "tool", "bin/runner")
    assert marker.read_bytes() == b"keep this directory"
    assert list(target.iterdir()) == [marker]


@pytest.mark.parametrize("failure", ["download", "missing_executable"])
def test_failed_archive_install_cleans_staging_and_preserves_other_tools(
    tmp_path, monkeypatch, failure
):
    tools = tmp_path / ".artemis-tools"
    tools.mkdir()
    existing = tools / "existing-tool"
    existing.write_bytes(b"leave existing tools alone")

    def download(_url, destination):
        if failure == "download":
            destination.write_bytes(b"partial download")
            raise OSError("connection lost")
        make_archive(destination, "zip", {"tool/readme": b"no executable"})

    monkeypatch.setattr(installer, "download_archive", download)
    with pytest.raises((OSError, RuntimeError)):
        installer.install_archive("unused", tools / "tool", "tool", "bin/runner")
    assert list(tools.iterdir()) == [existing]
    assert existing.read_bytes() == b"leave existing tools alone"


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_archive_extraction_rejects_path_escape(tmp_path, kind):
    archive = tmp_path / "download"
    make_archive(archive, kind, {"../outside": b"must not escape"})
    with pytest.raises((RuntimeError, tarfile.FilterError)):
        installer.extract_archive(archive, tmp_path / "extracted")
    assert not (tmp_path / "outside").exists()


def test_tar_extraction_rejects_escaping_symlink(tmp_path):
    archive = tmp_path / "download"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("tool/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        bundle.addfile(member)
    with pytest.raises(tarfile.FilterError):
        installer.extract_archive(archive, tmp_path / "extracted")
    assert not (tmp_path / "outside").exists()


def test_archive_install_refuses_linked_destination(tmp_path):
    existing = tmp_path / "shared-tool"
    existing.mkdir()
    (existing / "runner").write_bytes(b"shared executable")
    target = tmp_path / "private-tool"
    target.symlink_to(existing, target_is_directory=True)
    with pytest.raises(RuntimeError, match="linked directory"):
        installer.install_archive("unused", target, "tool", "runner")
    assert (existing / "runner").read_bytes() == b"shared executable"


@pytest.mark.parametrize(
    "version,compatible",
    [
        ("v22.22.2", False),
        ("v22.22.3", True),
        ("22.23.0", True),
        ("22.21.99", False),
        ("23.99.0", False),
        ("24.14.99", False),
        ("24.15.0", True),
        ("24.16.0", True),
        ("25.15.0", False),
        ("26.0.0", True),
        ("27.0.0", True),
        ("v26.0.0-rc.1", False),
        ("unknown", False),
    ],
)
def test_node_compatibility_boundaries(version, compatible):
    assert installer.node_compatible(version) is compatible


def create_node_distribution(root):
    node = root / "bin/node"
    npm = root / "lib/node_modules/npm/bin/npm-cli.js"
    node.parent.mkdir(parents=True)
    npm.parent.mkdir(parents=True)
    node.write_bytes(b"existing node")
    npm.write_bytes(b"existing npm")
    return node, npm


def test_compatible_host_node_and_npm_are_reused(tmp_path, monkeypatch):
    node, npm = create_node_distribution(tmp_path / "host")
    monkeypatch.setattr(
        installer.shutil, "which", lambda name, **_kwargs: str(node) if name == "node" else None
    )
    monkeypatch.setattr(installer, "executable_works", lambda *_args: "v22.22.3")
    assert installer.ensure_node(tmp_path / ".artemis-tools", {"PATH": "/host/bin"}) == (node, npm)
    assert not (tmp_path / ".artemis-tools").exists()
    assert node.read_bytes() == b"existing node"


def test_incompatible_node_downloads_private_copy_without_replacing_host(tmp_path, monkeypatch):
    host_node, host_npm = create_node_distribution(tmp_path / "host")
    tools = tmp_path / "checkout" / ".artemis-tools"
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name, **_kwargs: str(host_node) if name == "node" else None,
    )
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(installer.platform, "libc_ver", lambda: ("glibc", "2.31"))
    monkeypatch.setattr(
        installer,
        "executable_works",
        lambda path, *_args: "v20.0.0" if path == host_node else "v22.23.2",
    )
    calls = []

    def install(url, target, folder, executable):
        calls.append((url, target, folder, executable))
        private_node, _npm = create_node_distribution(target)
        return private_node

    monkeypatch.setattr(installer, "install_archive", install)
    node, npm = installer.ensure_node(tools, {"PATH": "/host/bin"})
    assert node.is_relative_to(tools)
    assert npm.is_relative_to(tools)
    assert len(calls) == 1
    assert calls[0][1] == tools / "node-v22.23.2-linux-x64"
    assert host_node.read_bytes() == b"existing node"
    assert host_npm.read_bytes() == b"existing npm"


def test_runtime_environment_overrides_external_caches_only_for_children(tmp_path, monkeypatch):
    for name in (
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_CACHE_DIR",
        "UV_PROJECT",
        "UV_WORKING_DIR",
        "NPM_CONFIG_CACHE",
        "npm_config_cache",
        "npm_config_prefix",
        "npm_config_global",
        "npm_config_logs_dir",
        "NPM_CONFIG_LOGS_DIR",
    ):
        monkeypatch.setenv(name, "/shared/external")
    monkeypatch.setenv("PATH", os.pathsep.join(["/host/bin", "/usr/bin"]))
    before = dict(os.environ)
    tool_dirs = [str(tmp_path / ".artemis-tools/bin"), "/host/bin"]
    env = installer.runtime_environment(tmp_path, tool_dirs)
    assert dict(os.environ) == before
    assert env["PATH"].split(os.pathsep) == [tool_dirs[0], "/host/bin", "/usr/bin"]
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / ".venv")
    assert env["UV_PROJECT"] == str(tmp_path)
    assert env["UV_WORKING_DIR"] == str(tmp_path)
    for name in (
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_CACHE_DIR",
        "NPM_CONFIG_CACHE",
        "NPM_CONFIG_PREFIX",
        "NPM_CONFIG_LOGS_DIR",
    ):
        assert Path(env[name]).is_relative_to(tmp_path / ".artemis-tools")
    assert env["NPM_CONFIG_GLOBAL"] == "false"
    assert "npm_config_cache" not in env
    assert "npm_config_prefix" not in env
    assert "npm_config_global" not in env
    assert "npm_config_logs_dir" not in env
    assert env["UV_NO_MODIFY_PATH"] == "1"
    assert env["UV_PYTHON_INSTALL_REGISTRY"] == "0"
    assert not (tmp_path / ".artemis-tools").exists()


def test_existing_adb_is_probed_without_starting_a_server(tmp_path, monkeypatch):
    adb = tmp_path / "host/platform-tools/adb"
    adb.parent.mkdir(parents=True)
    adb.write_bytes(b"host adb")
    monkeypatch.setattr(
        installer.shutil, "which", lambda name, **_kwargs: str(adb) if name == "adb" else None
    )
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(
            command, 0, stdout="Android Debug Bridge version 1.0.41\n"
        )

    monkeypatch.setattr(installer.subprocess, "run", run)
    env = {"PATH": str(adb.parent)}
    assert installer.ensure_adb(tmp_path / ".artemis-tools", env) == adb
    assert len(calls) == 1
    assert calls[0][0] == [str(adb), "version"]
    assert calls[0][1]["env"] == env
    assert adb.read_bytes() == b"host adb"


@pytest.fixture
def installation_flow(tmp_path, monkeypatch):
    root = tmp_path / "checkout with spaces"
    index = root / "apps/showcase_ui/dist/frontend/browser/index.html"
    index.parent.mkdir(parents=True)
    index.write_text("old build", encoding="utf-8")
    node = root / ".artemis-tools/node/bin/node"
    npm = root / ".artemis-tools/node/lib/node_modules/npm/bin/npm-cli.js"
    adb = root / ".artemis-tools/platform-tools/adb"
    uv = str(root / ".artemis-tools/bin/uv")
    monkeypatch.setattr(
        installer.shutil, "which", lambda name, **_kwargs: uv if name == "uv" else None
    )
    monkeypatch.setattr(installer, "ensure_node", lambda *_args: (node, npm))
    monkeypatch.setattr(installer, "ensure_adb", lambda *_args: adb)
    calls = []

    def configure_failure(failure=None):
        def run(command, **options):
            if command[0] == uv:
                phase = "sync"
            elif command[0] == str(node):
                phase = "ci" if "ci" in command else "build"
            elif str(root / "scripts/configure_codex.py") in command:
                phase = "configure"
            else:
                phase = "launch"
            calls.append((phase, command, options))
            if phase == failure:
                raise subprocess.CalledProcessError(17, command)
            if phase == "build":
                index.write_text("new build", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(installer.subprocess, "run", run)

    configure_failure()
    return root, index, adb, calls, configure_failure


def test_install_rebuilds_existing_ui_and_saves_only_local_runtime(installation_flow, capsys):
    root, index, adb, calls, _configure = installation_flow
    before = dict(os.environ)
    installer.install(root)
    assert [phase for phase, _command, _options in calls] == ["sync", "ci", "build", "configure"]
    assert "--frozen" in calls[0][1]
    assert calls[0][1][calls[0][1].index("--python") + 1] == "3.12"
    assert index.read_text(encoding="utf-8") == "new build"
    assert all(options["check"] is True for _phase, _command, options in calls)
    assert all(
        options["env"]["UV_PROJECT_ENVIRONMENT"] == str(root / ".venv")
        for _phase, _command, options in calls
    )
    assert dict(os.environ) == before
    assert all("start-server" not in command for _phase, command, _options in calls)
    metadata = json.loads((root / ".artemis-tools/runtime.json").read_text(encoding="utf-8"))
    assert metadata["root"] == str(root)
    assert metadata["adb"] == str(adb)
    assert str(adb.parent) in metadata["tool_dirs"]
    assert "Codex executor ready" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["sync", "ci", "build", "configure"])
def test_install_stops_before_later_steps_and_readiness_on_failure(
    installation_flow, capsys, failure
):
    root, _index, _adb, calls, configure_failure = installation_flow
    configure_failure(failure)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(root, launch=True)
    phases = ["sync", "ci", "build", "configure"]
    assert [phase for phase, _command, _options in calls] == phases[: phases.index(failure) + 1]
    assert not (root / ".artemis-tools/runtime.json").exists()
    assert "Codex executor ready" not in capsys.readouterr().out


def test_install_requires_generated_ui_before_configuring_mcp(
    installation_flow, monkeypatch, capsys
):
    root, index, _adb, calls, _configure = installation_flow
    run = installer.subprocess.run

    def remove_generated_index(command, **options):
        result = run(command, **options)
        if "build" in command:
            index.unlink()
        return result

    monkeypatch.setattr(installer.subprocess, "run", remove_generated_index)
    with pytest.raises(RuntimeError, match="did not produce index.html"):
        installer.install(root)
    assert [phase for phase, _command, _options in calls] == ["sync", "ci", "build"]
    assert not (root / ".artemis-tools/runtime.json").exists()
    assert "Codex executor ready" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "relative_path", [".artemis-tools", ".venv", "apps/showcase_ui/node_modules"]
)
def test_install_rejects_linked_project_environment_before_running_commands(
    tmp_path, relative_path
):
    root = tmp_path / "checkout"
    root.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    marker = shared / "preserve"
    marker.write_bytes(b"external environment")
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(shared, target_is_directory=True)
    with pytest.raises(RuntimeError, match="linked directory"):
        installer.install(root)
    assert marker.read_bytes() == b"external environment"
    assert list(shared.iterdir()) == [marker]


@pytest.mark.parametrize("arguments", [["ui", "--no-open"], ["--adb", "devices", "-l"]])
def test_launcher_loads_private_path_and_forwards_arguments(tmp_path, monkeypatch, arguments):
    tools = tmp_path / ".artemis-tools"
    tools.mkdir()
    adb = str(tools / "platform-tools/adb")
    tool_dirs = [str(tools / "bin"), str(tools / "platform-tools")]
    (tools / "runtime.json").write_text(
        json.dumps({"root": str(tmp_path), "tool_dirs": tool_dirs, "adb": adb}), encoding="utf-8"
    )
    before = dict(os.environ)
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 23)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.run(tmp_path, arguments) == 23
    command, options = calls[0]
    expected = (
        [adb, *arguments[1:]]
        if arguments[0] == "--adb"
        else [sys.executable, "-m", "artemis", *arguments]
    )
    assert command == expected
    assert options["cwd"] == tmp_path
    assert options["env"]["PATH"].split(os.pathsep)[:2] == tool_dirs
    assert dict(os.environ) == before


@pytest.mark.parametrize("state", ["missing", "moved"])
def test_launcher_requires_installation_for_current_checkout(tmp_path, state):
    if state == "moved":
        settings = tmp_path / ".artemis-tools/runtime.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"root": "/old/checkout"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing|moved"):
        launcher.run(tmp_path, ["ui"])
