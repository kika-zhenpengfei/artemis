"""Finish the Codex bootstrap after installing dependencies and building WebUI."""

from datetime import UTC, datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from dotenv import dotenv_values, set_key


EXECUTOR_ENV = {
    "ARTEMIS_AGENT_CLIENT": "codex",
    "ARTEMIS_EXECUTION_MODE": "executor",
    "ARTEMIS_EXECUTOR_ONLY": "1",
}


def configure_env(root: Path) -> Path | None:
    """Change only executor selection, retaining local paths, ports and comments."""
    target = root / ".env"
    existing = target.exists()
    if existing:
        values = dotenv_values(target, interpolate=False)
        if all(values.get(key) == value for key, value in EXECUTOR_ENV.items()):
            return None

    backup = None
    if existing:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = target.with_name(f".env.codex-backup-{stamp}")
        shutil.copy2(target, backup)
        backup.chmod(0o600)

    fd, temporary = tempfile.mkstemp(prefix=".env.codex-", dir=root)
    os.close(fd)
    pending = Path(temporary)
    try:
        source = target if existing else root / ".env.example"
        pending.write_bytes(source.read_bytes() if source.exists() else b"")
        for key, value in EXECUTOR_ENV.items():
            set_key(pending, key, value, quote_mode="never")
        pending.chmod(target.stat().st_mode & 0o777 if existing else 0o600)
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)
    return backup


def configure(root: Path) -> None:
    index = root / "apps/showcase_ui/dist/frontend/browser/index.html"
    if not index.is_file():
        raise RuntimeError("Current WebUI build is missing; rerun the Codex installer.")
    if not shutil.which("adb"):
        raise RuntimeError("ADB is missing from PATH; install Android SDK Platform Tools.")
    # Check the executable without requiring a connected/authorized phone yet.
    subprocess.run(["adb", "version"], check=True)
    backup = configure_env(root)
    if backup:
        print(f"Existing .env backed up to {backup}", flush=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "artemis",
            "mcp",
            "--install",
            "codex",
            "--mode",
            "executor",
            "--skip-rules",
        ],
        cwd=root,
        env={**os.environ, **EXECUTOR_ENV},
        check=True,
    )
    print("Codex executor configured. Restart Codex to load the MCP tools.")


def main() -> int:
    try:
        configure(Path(__file__).resolve().parents[1])
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Codex setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
