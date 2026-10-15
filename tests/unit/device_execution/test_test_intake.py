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

"""WebUI APK and commit submissions stay linked to source-checked cases."""

import io
import subprocess
import zipfile

import pytest

from artemis.device_execution import test_intake as intake_module
from artemis.device_execution.apk_metadata import parse_badging_package
from artemis.device_execution.test_intake import TestIntakeStore, browse_repository_directory
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.runtime import trace_store


def _apk_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
    return output.getvalue()


def _source_checkout(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "Screen.kt"],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root


def test_intake_upload_links_to_real_code_and_core_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(intake_module, "read_apk_package_name", lambda _: "com.example.app")
    root = _source_checkout(tmp_path)
    store = TestIntakeStore()
    intake = store.create(
        goal="Check screen",
        repository_root=str(root),
        apk=io.BytesIO(_apk_bytes()),
    )
    assert intake["status"] == "awaiting_codex_plan"
    assert intake["package_name"] == "com.example.app"
    assert intake["source_commit"] == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert store.read(intake["intake_id"])["apk_sha256"] == intake["apk_sha256"]
    workflow = TestWorkflowStore().create(
        goal="Check screen",
        repository_root=str(root),
        intake_id=intake["intake_id"],
        requirements=[
            {
                "id": "R1",
                "description": "Screen exists",
                "code_refs": [{"path": "Screen.kt", "line": 1, "reason": "Target UI"}],
            }
        ],
    )
    assert store.read(intake["intake_id"])["status"] == "context_linked"
    TestWorkflowStore().set_cases(
        workflow["workflow_id"],
        profile="flash",
        cases=[
            {
                "id": "C1",
                "title": "Open screen",
                "requirement_ids": ["R1"],
                "steps": ["Open the screen"],
                "expected_result": "Screen visible",
            }
        ],
    )
    assert store.read(intake["intake_id"])["status"] == "planned"
    report = TestWorkflowStore().report(workflow["workflow_id"])
    assert report["submitted_apk"]["sha256"] == intake["apk_sha256"]
    assert report["source_revision"]["commit"] == intake["source_commit"]


def test_intake_accepts_dirty_checkout_and_rejects_later_source_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(intake_module, "read_apk_package_name", lambda _: "com.example.app")
    root = _source_checkout(tmp_path)
    store = TestIntakeStore()
    with pytest.raises(ValueError, match="APK/ZIP"):
        store.create(
            goal="Check screen",
            repository_root=str(root),
            apk=io.BytesIO(b"invalid"),
        )
    (root / "Screen.kt").write_text("class ChangedScreen\n", encoding="utf-8")
    (root / "Draft.kt").write_text("class Draft\n", encoding="utf-8")
    intake = store.create(
        goal="Check screen",
        repository_root=str(root),
        apk=io.BytesIO(_apk_bytes()),
    )
    assert intake["working_tree_dirty"] is True
    assert intake["source_state_sha256"]
    store.validate_link(intake["intake_id"], goal="Check screen", repository_root=str(root))
    (root / "Draft.kt").write_text("class RevisedDraft\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since"):
        store.validate_link(intake["intake_id"], goal="Check screen", repository_root=str(root))
    (root / "Draft.kt").write_text("class Draft\n", encoding="utf-8")
    (root / "Screen.kt").write_text("class ChangedAgain\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since"):
        store.validate_link(intake["intake_id"], goal="Check screen", repository_root=str(root))


def test_repository_browser_stays_within_allowed_roots_and_marks_git_roots(tmp_path, monkeypatch):
    monkeypatch.delenv("ARTEMIS_SOURCE_ROOTS", raising=False)
    home = tmp_path / "home"
    repo = home / "WorkSpace" / "app"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "outside-link").symlink_to(outside, target_is_directory=True)

    root = browse_repository_directory(home=home)
    assert root["parent"] is None
    assert [item["name"] for item in root["directories"]] == ["WorkSpace"]
    selected = browse_repository_directory(str(repo), home=home)
    assert selected["is_repository"] is True
    with pytest.raises(ValueError, match="allowed source root"):
        browse_repository_directory(str(outside), home=home)

    monkeypatch.setenv("ARTEMIS_SOURCE_ROOTS", str(outside))
    configured = browse_repository_directory(str(outside), home=home)
    assert configured["roots"] == [str(home), str(outside)]
    assert configured["parent"] is None


def test_badging_package_rejects_missing_or_invalid_identity():
    assert parse_badging_package("package: name='com.example.app' versionCode='1'") == "com.example.app"
    with pytest.raises(ValueError, match="package name"):
        parse_badging_package("application-label:'Example'")
    with pytest.raises(ValueError, match="package name"):
        parse_badging_package("package: name='not a package'")
