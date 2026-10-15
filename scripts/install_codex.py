"""Install Codex dependencies inside this checkout without changing host toolchains."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile


NODE_VERSION = "22.23.2"


def runtime_environment(root: Path, tool_dirs: list[str]) -> dict[str, str]:
    """Keep tool lookup and package caches local to ARTEMIS child processes."""
    tools = root / ".artemis-tools"
    env = dict(os.environ)
    # npm treats environment variable names case-insensitively.
    for name in list(env):
        if name.lower() in {
            "npm_config_cache",
            "npm_config_prefix",
            "npm_config_global",
            "npm_config_logs_dir",
        }:
            del env[name]
    env.update(
        {
            "PATH": os.pathsep.join(
                dict.fromkeys([*tool_dirs, *env.get("PATH", "").split(os.pathsep)])
            ),
            "UV_PROJECT_ENVIRONMENT": str(root / ".venv"),
            "UV_PROJECT": str(root),
            "UV_WORKING_DIR": str(root),
            "UV_PYTHON_INSTALL_DIR": str(tools / "python"),
            "UV_PYTHON_BIN_DIR": str(tools / "bin"),
            "UV_CACHE_DIR": str(tools / "uv-cache"),
            "UV_NO_MODIFY_PATH": "1",
            "UV_PYTHON_INSTALL_BIN": "0",
            "UV_PYTHON_INSTALL_REGISTRY": "0",
            "UV_PYTHON_NO_REGISTRY": "1",
            "UV_PYTHON_PREFERENCE": "managed",
            "UV_NO_ENV_FILE": "1",
            "NPM_CONFIG_CACHE": str(tools / "npm-cache"),
            "NPM_CONFIG_LOGS_DIR": str(tools / "npm-cache/_logs"),
            "NPM_CONFIG_PREFIX": str(tools / "npm-prefix"),
            "NPM_CONFIG_GLOBAL": "false",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NG_CLI_ANALYTICS": "false",
        }
    )
    return env


def reject_linked_directory(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise RuntimeError(f"Refusing to write through a linked directory: {path}")


def node_compatible(version: str) -> bool:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        return False
    major, minor, patch = map(int, match.groups())
    return major >= 26 or major == 24 and minor >= 15 or major == 22 and (minor, patch) >= (22, 3)


def executable_works(path: Path, argument: str, env: dict[str, str]) -> str | None:
    try:
        result = subprocess.run(
            [str(path), argument], env=env, capture_output=True, text=True, timeout=15, check=True
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def download_archive(url: str, destination: Path) -> None:
    print(f"Downloading {url}", flush=True)
    with urlopen(url, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def extract_archive(archive: Path, destination: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise RuntimeError("Downloaded archive contains a path outside its directory")
            bundle.extractall(destination)
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")


def install_archive(url: str, target: Path, folder: str, executable: str) -> Path:
    """Publish a complete tool directory; never delete or replace an existing tool."""
    reject_linked_directory(target)
    binary = target / executable
    if target.exists():
        if binary.is_file():
            return binary
        raise RuntimeError(f"Incomplete local tool directory: {target}. Move it aside and retry.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=target.parent) as temporary:
        staging = Path(temporary)
        archive = staging / "archive"
        download_archive(url, archive)
        extracted = staging / "extracted"
        extract_archive(archive, extracted)
        content = extracted / folder
        candidate = content / executable
        if not candidate.is_file():
            raise RuntimeError(f"Downloaded archive is missing {executable}")
        if os.name != "nt":
            candidate.chmod(candidate.stat().st_mode | 0o111)
        if target.exists():
            raise RuntimeError(f"Tool directory appeared during installation: {target}; retry.")
        content.rename(target)
    return binary


def npm_cli_for(node: Path, env: dict[str, str]) -> Path | None:
    candidates = [
        node.parent / "node_modules/npm/bin/npm-cli.js",
        node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js",
    ]
    npm = shutil.which("npm", path=env.get("PATH"))
    if npm:
        resolved = Path(npm).resolve()
        if resolved.suffix == ".js":
            candidates.insert(0, resolved)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def ensure_node(tools: Path, env: dict[str, str]) -> tuple[Path, Path]:
    available = shutil.which("node", path=env.get("PATH"))
    if available:
        node = Path(available).resolve()
        version = executable_works(node, "--version", env)
        npm = npm_cli_for(node, env)
        if version and node_compatible(version) and npm:
            print(f"Using existing Node.js {version}: {node}", flush=True)
            return node, npm
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win"}.get(platform.system())
    arch = {
        "x86_64": "x64",
        "AMD64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "ARM64": "arm64",
    }.get(platform.machine())
    if not system or not arch:
        raise RuntimeError("No compatible Node.js found; install one for this platform and retry.")
    if system == "linux" and platform.libc_ver()[0] == "musl":
        raise RuntimeError(
            "On musl Linux, provide a compatible Node.js/npm in PATH before installing."
        )
    folder = f"node-v{NODE_VERSION}-{system}-{arch}"
    suffix = "zip" if system == "win" else "tar.gz"
    executable = "node.exe" if system == "win" else "bin/node"
    node = install_archive(
        f"https://nodejs.org/dist/v{NODE_VERSION}/{folder}.{suffix}",
        tools / folder,
        folder,
        executable,
    )
    version = executable_works(node, "--version", env)
    npm = npm_cli_for(node, {**env, "PATH": str(node.parent)})
    if not version or not node_compatible(version) or not npm:
        raise RuntimeError(f"Project-local Node.js cannot run on this computer: {node}")
    return node, npm


def ensure_adb(tools: Path, env: dict[str, str]) -> Path:
    name = "adb.exe" if os.name == "nt" else "adb"
    found = shutil.which("adb", path=env.get("PATH"))
    candidates = [Path(found)] if found else []
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if env.get(key):
            candidates.append(Path(env[key]) / "platform-tools" / name)
    candidates.extend(
        [
            Path.home() / "Library/Android/sdk/platform-tools" / name,
            Path.home() / "Android/Sdk/platform-tools" / name,
        ]
    )
    if env.get("LOCALAPPDATA"):
        candidates.append(Path(env["LOCALAPPDATA"]) / "Android/Sdk/platform-tools" / name)
    for candidate in candidates:
        if candidate.is_file() and executable_works(candidate, "version", env) is not None:
            print(f"Using existing ADB: {candidate}", flush=True)
            return candidate.resolve()
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if not system or system == "linux" and platform.machine() not in {"x86_64", "AMD64"}:
        raise RuntimeError(
            "No ADB found. Provide Android platform-tools for this platform in PATH."
        )
    adb = install_archive(
        f"https://dl.google.com/android/repository/platform-tools-latest-{system}.zip",
        tools / "platform-tools",
        "platform-tools",
        name,
    )
    if executable_works(adb, "version", env) is None:
        raise RuntimeError(f"Project-local ADB cannot run on this computer: {adb}")
    return adb


def save_runtime(root: Path, tool_dirs: list[str], adb: Path) -> None:
    target = root / ".artemis-tools/runtime.json"
    content = (
        json.dumps({"root": str(root), "tool_dirs": tool_dirs, "adb": str(adb)}, indent=2) + "\n"
    )
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return
    fd, temporary = tempfile.mkstemp(prefix=".runtime-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(root: Path, *, launch: bool = False) -> None:
    tools = root / ".artemis-tools"
    for path in (
        tools,
        root / ".venv",
        root / "apps/showcase_ui/node_modules",
        root / "apps/showcase_ui/dist",
        root / "apps/showcase_ui/.angular",
        *(
            tools / name
            for name in ("python", "bin", "uv-cache", "npm-cache", "npm-cache/_logs", "npm-prefix")
        ),
    ):
        reject_linked_directory(path)
    tools.mkdir(exist_ok=True)
    env = runtime_environment(root, [])
    uv = shutil.which("uv", path=env["PATH"])
    if not uv:
        raise RuntimeError("Run install_codex.sh or install_codex.bat to provision uv first.")
    node, npm = ensure_node(tools, env)
    adb = ensure_adb(tools, env)
    tool_dirs = list(dict.fromkeys([str(node.parent), str(adb.parent), str(Path(uv).parent)]))
    env = runtime_environment(root, tool_dirs)
    for optional in ("scrcpy", "ffmpeg"):
        if not shutil.which(optional, path=env["PATH"]):
            print(
                f"Optional tool {optional} is unavailable; leaving the host environment unchanged."
            )
    subprocess.run(
        [
            uv,
            "sync",
            "--frozen",
            "--python",
            "3.12",
            "--project",
            str(root),
            "--directory",
            str(root),
        ],
        cwd=root,
        env=env,
        check=True,
    )
    frontend = root / "apps/showcase_ui"
    for args in (["ci", "--no-audit", "--no-fund"], ["run", "build"]):
        subprocess.run(
            [str(node), str(npm), "--prefix", str(frontend), *args],
            cwd=frontend,
            env=env,
            check=True,
        )
    if not (frontend / "dist/frontend/browser/index.html").is_file():
        raise RuntimeError(
            "WebUI build did not produce index.html; MCP configuration was not changed."
        )
    python = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    subprocess.run(
        [str(python), str(root / "scripts/configure_codex.py")], cwd=root, env=env, check=True
    )
    save_runtime(root, tool_dirs, adb)
    print(
        "Codex executor ready. Restart Codex, then authorize the phone and confirm each workflow in WebUI."
    )
    print("Local launcher: bash artemis.sh ui --no-open (Windows: .\\artemis.bat ui --no-open)")
    if launch:
        subprocess.run(
            [str(python), "-m", "artemis", "ui", "--open"], cwd=root, env=env, check=True
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="Start WebUI after installation")
    args = parser.parse_args()
    try:
        install(Path(__file__).resolve().parents[1], launch=args.launch)
    except (
        OSError,
        RuntimeError,
        ValueError,
        tarfile.TarError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Codex installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
