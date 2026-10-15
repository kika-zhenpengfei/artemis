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

"""WebUI launches prepared workflows once and exposes the terminal result."""

import asyncio
import base64
import io
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.admin_console.routers import sessions
from artemis.device_execution import test_intake as intake_module
from artemis.runtime import trace_store


@pytest.fixture
def confirmed_device(monkeypatch):
    """These runner/control tests start after the separate confirmation boundary."""
    endpoint = sessions.check_adb_endpoint().identity
    monkeypatch.setattr(sessions.WorkflowStartStore, "status", lambda _self, workflow_id: {
        "workflow_id": workflow_id, "status": "confirmed",
        "device_serial": "device-1", "adb_endpoint_id": endpoint,
    })

    async def resolve(serial, **kwargs):
        assert serial == "device-1"
        assert kwargs["require_idle"] is True
        assert kwargs["expected_adb_endpoint_id"] == endpoint
        return serial, True

    monkeypatch.setattr(sessions, "resolve_device_selection", resolve)


def test_prepared_workflow_run_reports_completion(monkeypatch, confirmed_device):
    class FakeRunner:
        def validate(self, workflow_id):
            assert workflow_id == "workflow-1"

        async def run(self, workflow_id, *, device_serial, **kwargs):
            assert device_serial == "device-1"
            assert kwargs["require_idle"] is True
            await asyncio.sleep(0)
            return {"workflow_id": workflow_id, "status": "completed"}

    async def scenario():
        monkeypatch.setattr(sessions, "PreparedWorkflowRunner", FakeRunner)
        monkeypatch.setattr(sessions, "_prepared_runs", {})
        started = await sessions.run_prepared_test_workflow(
            "workflow-1", sessions.PreparedRunRequest(device_serial="device-1")
        )
        assert started["status"] == "starting"
        with pytest.raises(HTTPException) as duplicate:
            await sessions.run_prepared_test_workflow(
                "workflow-1", sessions.PreparedRunRequest(device_serial="device-1")
            )
        assert duplicate.value.status_code == 409
        await sessions._prepared_runs["workflow-1"]
        result = await sessions.prepared_test_workflow_run_status("workflow-1")
        assert result["status"] == "completed"

    asyncio.run(scenario())


def test_webui_handoff_uses_same_runner_for_codex_actions(monkeypatch, confirmed_device):
    class FakeManager:
        async def observe(self, workflow_id, **_kwargs):
            assert workflow_id == "workflow-2"
            return {"observation_id": "fresh"}, b"image"

        async def act(self, workflow_id, *, case_id, observation_id, **_kwargs):
            assert (workflow_id, case_id, observation_id) == ("workflow-2", "C1", "fresh")
            return {"ok": True, "observation": {"observation_id": "next"}}, b"after"

    class FakeRunner:
        def __init__(self):
            self.control_lock = asyncio.Lock()
            self.manager = FakeManager()
            self.active_case_id = "C1"

        def validate(self, _workflow_id):
            return {}

        async def run(self, _workflow_id, *, device_serial, **_kwargs):
            assert device_serial == "device-1"
            return {"status": "handoff", "handoff": {"resume_token": "token-1"}}

        async def resume(self, _workflow_id, token):
            assert token == "token-1"
            self.active_case_id = None
            return {"status": "completed"}

    async def scenario():
        monkeypatch.setattr(sessions, "PreparedWorkflowRunner", FakeRunner)
        monkeypatch.setattr(sessions, "_prepared_runs", {})
        monkeypatch.setattr(sessions, "_prepared_runners", {})
        await sessions.run_prepared_test_workflow("workflow-2", sessions.PreparedRunRequest())
        await sessions._prepared_runs["workflow-2"]
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        observed = await sessions.control_prepared_test_workflow(
            "workflow-2", sessions.PreparedControlRequest(operation="observe"), request
        )
        assert base64.b64decode(observed["image_base64"]) == b"image"
        acted = await sessions.control_prepared_test_workflow(
            "workflow-2",
            sessions.PreparedControlRequest(
                operation="act", action="press_key", case_id="C1", observation_id="fresh"
            ),
            request,
        )
        assert acted["ok"] is True
        started = await sessions.control_prepared_test_workflow(
            "workflow-2",
            sessions.PreparedControlRequest(operation="resume", resume_token="token-1"),
            request,
        )
        assert started["status"] == "starting"
        await sessions._prepared_runs["workflow-2"]
        assert (await sessions.prepared_test_workflow_run_status("workflow-2"))[
            "status"
        ] == "completed"

    asyncio.run(scenario())


def test_concurrent_device_checks_cannot_start_same_prepared_workflow_twice(monkeypatch, confirmed_device):
    async def scenario():
        both_resolving = asyncio.Event()
        finish_run = asyncio.Event()
        resolve_count = 0
        running = []

        async def resolve(*_args, **_kwargs):
            nonlocal resolve_count
            resolve_count += 1
            if resolve_count == 2:
                both_resolving.set()
            await both_resolving.wait()
            return "device-1", True

        class FakeRunner:
            def validate(self, _workflow_id):
                return {}

            async def run(self, workflow_id, **_kwargs):
                running.append(self)
                await finish_run.wait()
                return {"workflow_id": workflow_id, "status": "completed"}

        monkeypatch.setattr(sessions, "resolve_device_selection", resolve)
        monkeypatch.setattr(sessions, "PreparedWorkflowRunner", FakeRunner)
        monkeypatch.setattr(sessions, "_prepared_runs", {})
        monkeypatch.setattr(sessions, "_prepared_runners", {})
        results = await asyncio.gather(
            *(
                sessions.run_prepared_test_workflow(
                    "same-workflow", sessions.PreparedRunRequest(require_idle=True)
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        rejected = next(result for result in results if isinstance(result, HTTPException))
        assert rejected.status_code == 409
        assert len(running) == 1
        assert sessions._prepared_runners["same-workflow"] is running[0]
        finish_run.set()
        await sessions._prepared_runs["same-workflow"]

    asyncio.run(scenario())


def test_webui_accepts_apk_and_records_selected_checkout_head(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(intake_module, "read_apk_package_name", lambda _: "com.example.app")
    source = tmp_path / "source"
    source.mkdir()
    (source / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
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
        subprocess.run(command, cwd=source, check=True, capture_output=True)
    apk = io.BytesIO()
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
    app = FastAPI()
    app.include_router(sessions.router)
    client = TestClient(app)
    (source / "Screen.kt").write_text("class EditedScreen\n", encoding="utf-8")
    response = client.post(
        "/api/test_intakes",
        data={
            "goal": "Check screen",
            "repository_root": str(source),
        },
        files={"apk": ("app.apk", apk.getvalue(), "application/vnd.android.package-archive")},
    )
    assert response.status_code == 200
    assert response.json()["package_name"] == "com.example.app"
    assert response.json()["working_tree_dirty"] is True
    assert response.json()["source_commit"] == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    intake_id = response.json()["intake_id"]
    assert client.get(f"/api/test_intakes/{intake_id}").json()["status"] == "awaiting_codex_plan"


def test_source_repository_browser_is_local_only(monkeypatch):
    app = FastAPI()
    app.include_router(sessions.router)
    monkeypatch.setattr(
        sessions, "browse_repository_directory",
        lambda path: {"current": "/home/test", "parent": None, "is_repository": False, "directories": []},
    )
    local = TestClient(app, client=("127.0.0.1", 50000))
    assert local.get("/api/source_repositories/browse").status_code == 200
    remote = TestClient(app, client=("192.0.2.1", 50000))
    assert remote.get("/api/source_repositories/browse").status_code == 403
