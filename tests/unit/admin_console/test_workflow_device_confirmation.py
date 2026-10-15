"""Per-workflow HTTP confirmation persists a choice without starting a runner."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.admin_console.routers import sessions
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.device_execution.workflow_start import WorkflowStartStore
from artemis.runtime import trace_store
from artemis.runtime.device_selection import DeviceSelectionError


@pytest.fixture
def workflow_api(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
    workflow_ids = []
    store = TestWorkflowStore()
    for goal in ("Check first screen", "Check second screen"):
        workflow_id = store.create(goal=goal, repository_root=str(source), requirements=[{
            "id": "R1", "description": goal,
            "code_refs": [{"path": "Screen.kt", "line": 1, "reason": "Screen source"}],
        }])["workflow_id"]
        store.set_cases(workflow_id, profile="flash", cases=[{
            "id": "C1", "title": goal, "requirement_ids": ["R1"],
            "steps": ["Open screen"], "expected_result": "Screen is visible",
        }])
        workflow_ids.append(workflow_id)
    app = FastAPI()
    app.include_router(sessions.router)
    return TestClient(app), workflow_ids


def test_confirmations_are_independent_and_never_launch_runner(workflow_api, monkeypatch):
    client, ids = workflow_api
    endpoint = sessions.check_adb_endpoint().identity
    resolve = AsyncMock(side_effect=lambda serial, **kwargs: (serial, True))
    runner = Mock(side_effect=AssertionError("Confirmation must not launch a prepared runner"))
    monkeypatch.setattr(sessions, "resolve_device_selection", resolve)
    monkeypatch.setattr(sessions, "PreparedWorkflowRunner", runner)
    before = client.get("/api/test_workflows/waiting").json()["workflows"]
    assert {row["workflow_id"] for row in before} == set(ids)
    assert all(row["status"] == "awaiting_device" for row in before)
    for workflow_id, serial in zip(ids, ("phone-A", "phone-B")):
        result = client.post(f"/api/test_workflows/{workflow_id}/confirm_device", json={
            "device_serial": serial, "expected_adb_endpoint_id": endpoint,
        })
        assert result.status_code == 200
        assert result.json()["status"] == "confirmed"
        assert result.json()["device_serial"] == serial
        assert trace_store.read_status(workflow_id) is None
    assert [WorkflowStartStore().status(key)["device_serial"] for key in ids] == ["phone-A", "phone-B"]
    for call in resolve.await_args_list:
        assert call.kwargs == {"require_idle": True, "expected_adb_endpoint_id": endpoint}
    runner.assert_not_called()


def test_busy_or_changed_endpoint_cannot_confirm(workflow_api, monkeypatch):
    client, ids = workflow_api
    resolve = AsyncMock(side_effect=DeviceSelectionError("Device is busy"))
    monkeypatch.setattr(sessions, "resolve_device_selection", resolve)
    url = f"/api/test_workflows/{ids[0]}/confirm_device"
    response = client.post(url, json={
        "device_serial": "phone-A", "expected_adb_endpoint_id": sessions.check_adb_endpoint().identity,
    })
    assert response.status_code == 409
    assert WorkflowStartStore().status(ids[0])["status"] == "awaiting_device"
    response = client.post(url, json={"device_serial": "phone-A", "expected_adb_endpoint_id": "stale-endpoint"})
    assert response.status_code == 409
    assert "ADB server changed" in response.json()["detail"]
    assert resolve.await_count == 1
    assert client.post(url, json={"device_serial": "phone-A"}).status_code == 422
    assert client.post("/api/test_workflows/not-a-uuid/confirm_device", json={
        "device_serial": "phone-A", "expected_adb_endpoint_id": "endpoint",
    }).status_code == 422


def test_prepared_run_waits_without_device_confirmation(workflow_api, monkeypatch):
    client, ids = workflow_api
    run = AsyncMock()
    monkeypatch.setattr(sessions, "PreparedWorkflowRunner", lambda: SimpleNamespace(validate=lambda _: None, run=run))
    monkeypatch.setattr(sessions, "_prepared_runs", {})
    monkeypatch.setattr(sessions, "_prepared_runners", {})
    response = client.post(f"/api/test_workflows/{ids[0]}/run", json={"device_serial": "phone-A"})
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_device"
    assert sessions._prepared_runs == {}
    run.assert_not_awaited()
