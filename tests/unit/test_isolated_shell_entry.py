"""Exercise the Unix installer boundary without network, real tools, or real homes."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _executable(path, source):
    path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
    path.chmod(0o755)


UV_STUB = """import json, os, sys
from pathlib import Path
with Path(os.environ['ENTRY_TEST_LOG']).open('a') as stream:
    stream.write(json.dumps({'args': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}) + '\\n')
if sys.argv[1:] == ['--version']:
    print('uv 0.11.18 test stub')
elif sys.argv[1:2] == ['run']:
    sys.exit(int(os.environ.get('ENTRY_TEST_UV_EXIT', '0')))
else:
    sys.exit(97)
"""


@pytest.fixture
def shell_entry(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("Bash is unavailable")
    root = tmp_path / "checkout with spaces"
    (root / "scripts").mkdir(parents=True)
    for name in (
        "install_codex.sh",
        "artemis.sh",
        "scripts/install_codex.sh",
        "scripts/install_deps.sh",
    ):
        shutil.copy2(ROOT / name, root / name)
    (root / "scripts/install_codex.py").write_text("# Stubbed by uv\n", encoding="utf-8")
    private_home = tmp_path / "existing user home"
    private_home.mkdir()
    (private_home / ".zshrc").write_text("# Keep shell settings\n", encoding="utf-8")
    (private_home / ".local/share/node").mkdir(parents=True)
    (private_home / ".local/share/node/existing").write_text("keep", encoding="utf-8")
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("bash", "dirname", "mkdir", "mktemp", "mv", "rm", "tar"):
        path = shutil.which(name)
        if not path:
            pytest.skip(f"Required test utility is missing: {name}")
        (binaries / name).symlink_to(path)
    _executable(
        binaries / "uname",
        "import os, sys\nprint(os.environ.get('ENTRY_TEST_OS', 'Darwin') "
        "if sys.argv[1] == '-s' else os.environ.get('ENTRY_TEST_ARCH', 'arm64'))\n",
    )
    _executable(binaries / "uv", UV_STUB)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": str(binaries),
        "HOME": str(private_home),
        "CODEX_HOME": str(private_home / "custom-codex"),
        "ENTRY_TEST_LOG": str(log),
    }

    def run(*args, script="install_codex.sh", extra_env=None):
        result = subprocess.run(
            [bash, str(root / script), *args],
            cwd=tmp_path,
            env={**environment, **(extra_env or {})},
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert (private_home / ".zshrc").read_text() == "# Keep shell settings\n"
        assert (private_home / ".local/share/node/existing").read_text() == "keep"
        assert sorted(str(path.relative_to(private_home)) for path in private_home.rglob("*")) == [
            ".local",
            ".local/share",
            ".local/share/node",
            ".local/share/node/existing",
            ".zshrc",
        ]
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return root, binaries, environment, run


@pytest.mark.parametrize(
    "script,args",
    [
        ("install_codex.sh", ("--help",)),
        ("scripts/install_deps.sh", ("--codex", "--help")),
        ("install_codex.sh", ("--unknown",)),
        ("scripts/install_deps.sh", ("--codex", "--unknown")),
    ],
)
def test_help_and_invalid_arguments_do_not_create_environment(shell_entry, script, args):
    root, _, _, run = shell_entry

    result, calls = run(*args, script=script)

    assert result.returncode == (0 if args[-1] == "--help" else 2)
    assert not calls
    assert not (root / ".artemis-tools").exists()


@pytest.mark.parametrize(
    "script,args,launch",
    [
        ("install_codex.sh", (), False),
        ("install_codex.sh", ("--open",), True),
        ("install_codex.sh", ("-l",), True),
        ("scripts/install_deps.sh", ("--codex",), False),
        ("scripts/install_deps.sh", ("--launch", "--codex"), True),
    ],
)
def test_existing_uv_is_reused_with_only_project_local_environment(
    shell_entry, script, args, launch
):
    root, binaries, environment, run = shell_entry

    result, calls = run(*args, script=script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    call = calls[-1]
    assert call["args"] == [
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
    for key, relative in {
        "UV_PYTHON_INSTALL_DIR": "python",
        "UV_CACHE_DIR": "uv-cache",
        "UV_PYTHON_BIN_DIR": "bin",
    }.items():
        assert call["env"][key] == str(root / ".artemis-tools" / relative)
    assert call["env"]["UV_PROJECT_ENVIRONMENT"] == str(root / ".venv")
    assert call["env"]["UV_NO_MODIFY_PATH"] == "1"
    assert call["env"]["UV_NO_ENV_FILE"] == "1"
    assert call["env"]["UV_PYTHON_INSTALL_BIN"] == "0"
    assert call["env"]["UV_PYTHON_INSTALL_REGISTRY"] == "0"
    assert call["env"]["UV_PYTHON_NO_REGISTRY"] == "1"
    assert call["env"]["UV_PYTHON_PREFERENCE"] == "managed"
    assert call["env"]["HOME"] == environment["HOME"]
    assert call["env"]["CODEX_HOME"] == environment["CODEX_HOME"]
    assert call["env"]["PATH"] == os.pathsep.join(
        [
            str(root / ".artemis-tools/uv"),
            str(root / ".artemis-tools/bin"),
            str(binaries),
        ]
    )
    assert call["cwd"] == str(root)


def test_shared_installer_failure_propagates(shell_entry):
    _, _, _, run = shell_entry

    result, calls = run(extra_env={"ENTRY_TEST_UV_EXIT": "19"})

    assert result.returncode == 19
    assert len(calls) == 2


@pytest.mark.parametrize(
    "linked", [".venv", ".artemis-tools/python", ".artemis-tools/uv-cache", ".artemis-tools/bin"]
)
def test_linked_python_and_cache_directories_are_rejected_before_uv(shell_entry, tmp_path, linked):
    root, _, _, run = shell_entry
    outside = tmp_path / "existing shared environment"
    outside.mkdir()
    (outside / "keep").write_text("unchanged", encoding="utf-8")
    target = root / linked
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)

    result, calls = run()

    assert result.returncode != 0
    assert "linked directory" in result.stderr
    assert not calls
    assert list(outside.iterdir()) == [outside / "keep"]
    assert (outside / "keep").read_text() == "unchanged"


def _mock_download(shell_entry, *, valid_binary=True, curl_exit=0):
    root, binaries, _, _ = shell_entry
    (binaries / "uv").unlink()
    archive = binaries / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name in ("uv", "uvx"):
            source = (f"#!{sys.executable}\n" + UV_STUB).encode()
            member = tarfile.TarInfo(f"uv-aarch64-apple-darwin/{name}")
            member.size = len(source)
            member.mode = 0o755 if valid_binary else 0o644
            output.addfile(member, io.BytesIO(source))
    _executable(
        binaries / "curl",
        "import shutil, sys\nfrom pathlib import Path\n"
        f"sys.exit({curl_exit}) if {curl_exit} else None\n"
        "assert sys.argv[-1] == 'https://github.com/astral-sh/uv/releases/download/0.11.18/uv-aarch64-apple-darwin.tar.gz'\n"
        "assert '=https' in sys.argv\n"
        f"shutil.copyfile({str(archive)!r}, sys.argv[sys.argv.index('--output') + 1])\n",
    )
    return root


def test_missing_uv_download_is_atomic_and_stays_within_checkout(shell_entry):
    root = _mock_download(shell_entry)
    _, _, _, run = shell_entry

    result, calls = run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / ".artemis-tools/uv/uv").is_file()
    assert not list((root / ".artemis-tools").glob(".uv-download.*"))
    assert len(calls) == 3
    # A repeated install reuses the validated local binary.
    result, calls = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 5


@pytest.mark.parametrize("valid_binary,curl_exit", [(False, 0), (True, 22)])
def test_failed_download_does_not_publish_partial_uv(shell_entry, valid_binary, curl_exit):
    root = _mock_download(shell_entry, valid_binary=valid_binary, curl_exit=curl_exit)
    _, _, _, run = shell_entry

    result, calls = run()

    assert result.returncode != 0
    assert not calls
    assert not (root / ".artemis-tools/uv").exists()
    assert not list((root / ".artemis-tools").glob(".uv-download.*"))


def test_broken_existing_private_uv_is_preserved(shell_entry):
    root, binaries, _, run = shell_entry
    (binaries / "uv").unlink()
    private_uv = root / ".artemis-tools/uv"
    private_uv.mkdir(parents=True)
    (private_uv / "existing").write_text("inspect me", encoding="utf-8")

    result, calls = run()

    assert result.returncode != 0
    assert "Invalid project-local uv" in result.stderr
    assert not calls
    assert (private_uv / "existing").read_text() == "inspect me"


def test_musl_linux_is_reported_before_changes(shell_entry):
    root, binaries, _, run = shell_entry
    _executable(binaries / "ldd", "print('musl libc (aarch64)')\n")

    result, calls = run(extra_env={"ENTRY_TEST_OS": "Linux"})

    assert result.returncode != 0
    assert "musl Linux is not supported" in result.stderr
    assert not calls
    assert not (root / ".artemis-tools").exists()


def test_artemis_runtime_entry_requires_installed_project_python(shell_entry):
    root, _, _, run = shell_entry

    result, calls = run("status", script="artemis.sh")

    assert result.returncode != 0
    assert "bash install_codex.sh" in result.stderr
    assert not calls
    assert not (root / ".artemis-tools").exists()


def test_artemis_runtime_entry_uses_project_python_and_preserves_arguments(shell_entry):
    root, _, _, run = shell_entry
    (root / ".venv/bin").mkdir(parents=True)
    _executable(root / ".venv/bin/python", UV_STUB.replace("sys.exit(97)", "sys.exit(0)"))

    result, calls = run("--adb", "devices", "-l", script="artemis.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[-1]["args"] == ["-m", "scripts.run_artemis", "--adb", "devices", "-l"]
    assert calls[-1]["cwd"] == str(root)
