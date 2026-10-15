"""Run ARTEMIS with project-local tools, without updating the user's PATH."""

import json
from pathlib import Path
import subprocess
import sys

from scripts.install_codex import runtime_environment


def run(root: Path, args: list[str]) -> int:
    settings = root / ".artemis-tools/runtime.json"
    if not settings.is_file():
        raise RuntimeError(
            "Local runtime is missing. Run install_codex.sh / install_codex.bat first."
        )
    data = json.loads(settings.read_text(encoding="utf-8"))
    if data.get("root") != str(root):
        raise RuntimeError(
            "The checkout was moved. Rerun the Codex installer to update local paths."
        )
    env = runtime_environment(root, data["tool_dirs"])
    if args[:1] == ["--adb"]:
        command = [data["adb"], *args[1:]]
    else:
        command = [sys.executable, "-m", "artemis", *args]
    return subprocess.run(command, cwd=root, env=env).returncode


def main() -> int:
    try:
        return run(Path(__file__).resolve().parents[1], sys.argv[1:])
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"ARTEMIS launcher failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
