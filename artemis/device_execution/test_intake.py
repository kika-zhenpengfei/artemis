# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""WebUI APK and source-revision intake for Codex-planned mobile tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import BinaryIO, Any
from uuid import UUID, uuid4
import zipfile

from artemis.runtime import trace_store
from artemis.device_execution.apk_metadata import read_apk_package_name


_MAX_APK_BYTES = 500 * 1024 * 1024


def _head(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("repository_root must be a Git checkout with a HEAD commit") from exc


def _source_state(root: Path) -> tuple[bool, str]:
    """Fingerprint tracked changes and untracked files in the selected checkout."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root, capture_output=True, timeout=15, check=True,
        ).stdout
        tracked = subprocess.run(
            ["git", "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"],
            cwd=root, capture_output=True, timeout=15, check=True,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, timeout=15, check=True,
        ).stdout
        digest = hashlib.sha256()
        digest.update(status)
        digest.update(tracked)
        for relative in sorted(path for path in untracked.split(b"\0") if path):
            file = root / os.fsdecode(relative)
            digest.update(relative)
            if file.is_symlink():
                digest.update(os.fsencode(os.readlink(file)))
            else:
                with file.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        return bool(status), digest.hexdigest()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Could not verify the source checkout state") from exc


def browse_repository_directory(path: str | None = None, *, home: Path | None = None) -> dict[str, Any]:
    """List local directories for the WebUI's source-repository selector."""
    base = (home or Path.home()).resolve(strict=True)
    roots = [base]
    for configured in os.environ.get("ARTEMIS_SOURCE_ROOTS", "").split(os.pathsep):
        if not configured.strip():
            continue
        try:
            root = Path(configured).expanduser().resolve(strict=True)
        except OSError:
            continue
        if root.is_dir() and root not in roots:
            roots.append(root)
    try:
        current = Path(path).expanduser().resolve(strict=True) if path else base
    except OSError as exc:
        raise ValueError("Source directory does not exist") from exc
    if not any(current.is_relative_to(root) for root in roots) or not current.is_dir():
        raise ValueError("Source directory must be inside an allowed source root")

    directories = []
    try:
        children = sorted(current.iterdir(), key=lambda child: child.name.casefold())
    except OSError as exc:
        raise ValueError("Could not list the source directory") from exc
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            resolved = child.resolve(strict=True)
            if not any(resolved.is_relative_to(root) for root in roots) or not resolved.is_dir():
                continue
            directories.append(
                {
                    "name": child.name,
                    "path": str(resolved),
                    "is_repository": (resolved / ".git").exists(),
                }
            )
        except OSError:
            continue
    return {
        "current": str(current),
        "parent": str(current.parent)
        if any(current.parent.is_relative_to(root) for root in roots)
        else None,
        "roots": [str(root) for root in roots],
        "is_repository": (current / ".git").exists(),
        "directories": directories,
    }


class TestIntakeStore:
    @staticmethod
    def _directory(intake_id: str) -> Path:
        try:
            if str(UUID(intake_id)) != intake_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("intake_id must be a canonical UUID") from exc
        return Path(trace_store.get_trace_dir(intake_id))

    def read(self, intake_id: str) -> dict[str, Any]:
        path = self._directory(intake_id) / "test_intake.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("Test intake does not exist") from exc

    def list(self) -> list[dict[str, Any]]:
        root = Path(trace_store.TRACES_DIR)
        if not root.is_dir():
            return []
        items = []
        for path in root.glob("*/test_intake.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                items.append(item)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(items, key=lambda item: item.get("created_at", 0), reverse=True)

    def create(
        self,
        *,
        goal: str,
        repository_root: str,
        apk: BinaryIO,
    ) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("goal must not be empty")
        try:
            root = Path(repository_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("repository_root does not exist") from exc
        if not root.is_dir():
            raise ValueError("repository_root must be a directory")
        if not (root / ".git").exists():
            raise ValueError("repository_root must be the root of a Git checkout")
        head = _head(root)
        working_tree_dirty, source_state_sha256 = _source_state(root)
        intake_id = str(uuid4())
        directory = self._directory(intake_id)
        directory.mkdir(parents=True, exist_ok=True)
        apk_path = directory / "inputs" / "app.apk"
        apk_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = apk_path.with_suffix(".upload")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as output:
                while chunk := apk.read(1024 * 1024):
                    size += len(chunk)
                    if size > _MAX_APK_BYTES:
                        raise ValueError("APK exceeds the 500 MiB upload limit")
                    output.write(chunk)
                    digest.update(chunk)
            if not zipfile.is_zipfile(temporary):
                raise ValueError("Uploaded file is not an APK/ZIP archive")
            with zipfile.ZipFile(temporary) as archive:
                if "AndroidManifest.xml" not in archive.namelist():
                    raise ValueError("Uploaded archive has no AndroidManifest.xml")
            package_name = read_apk_package_name(temporary)
            os.replace(temporary, apk_path)
        finally:
            temporary.unlink(missing_ok=True)

        record = {
            "intake_id": intake_id,
            "goal": goal.strip(),
            "repository_root": str(root),
            "source_commit": head,
            "working_tree_dirty": working_tree_dirty,
            "source_state_sha256": source_state_sha256,
            "package_name": package_name,
            "apk_path": str(apk_path),
            "apk_sha256": digest.hexdigest(),
            "apk_size": size,
            "workflow_id": None,
            "status": "awaiting_codex_plan",
            "created_at": time.time(),
        }
        (directory / "test_intake.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return record

    def validate_link(self, intake_id: str, *, goal: str, repository_root: str) -> dict[str, Any]:
        intake = self.read(intake_id)
        if intake["status"] != "awaiting_codex_plan":
            raise ValueError("Test intake is already linked to a workflow")
        if intake["goal"] != goal.strip() or intake["repository_root"] != str(
            Path(repository_root).resolve()
        ):
            raise ValueError("goal and repository_root must match the submitted test intake")
        if _head(Path(intake["repository_root"])) != intake["source_commit"]:
            raise ValueError("Checkout HEAD changed since the WebUI test intake was submitted")
        dirty, fingerprint = _source_state(Path(intake["repository_root"]))
        if fingerprint != intake.get("source_state_sha256"):
            if intake.get("source_state_sha256") or dirty:
                raise ValueError("Source checkout changed since the WebUI test intake was submitted")
        apk_path = Path(intake["apk_path"])
        with apk_path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != intake["apk_sha256"]:
                raise ValueError("Submitted APK changed after upload")
        return intake

    def link(self, intake_id: str, workflow_id: str) -> None:
        intake = self.read(intake_id)
        if intake["status"] != "awaiting_codex_plan":
            raise ValueError("Test intake is already linked to a workflow")
        intake["workflow_id"] = workflow_id
        intake["status"] = "context_linked"
        self._save(intake)

    def mark_planned(self, intake_id: str) -> None:
        intake = self.read(intake_id)
        if intake["status"] == "planned":
            return
        if intake["status"] != "context_linked":
            raise ValueError("Test intake has no linked context")
        intake["status"] = "planned"
        self._save(intake)

    def set_status(self, intake_id: str, status: str) -> None:
        if status not in {"running", "finished"}:
            raise ValueError("unsupported intake status")
        intake = self.read(intake_id)
        intake["status"] = status
        self._save(intake)

    def _save(self, intake: dict[str, Any]) -> None:
        intake_id = intake["intake_id"]
        path = self._directory(intake_id) / "test_intake.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(intake, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
