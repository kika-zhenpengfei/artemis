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

"""History deletion updates durable batch views without replaying accepted work."""

import asyncio
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from apps.admin_console.core.state import ServerState
from apps.admin_console.database.repositories.session_repository import SessionRepository
from apps.admin_console.routers import sessions, tasks
from apps.admin_console.schemas.task_schema import TaskBatchRequest
from apps.admin_console.services import task_batch_service as batches
from artemis.data_engine.models import SessionMetadata
from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store
from artemis.runtime.device_lock import DeviceExecutionLock


@pytest.fixture
def history(tmp_path, monkeypatch):
    traces = tmp_path / "traces"
    db = tmp_path / "history.db"
    storage = StorageManager(db, traces)
    repository = SessionRepository(db)
    state = ServerState()
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: locks)
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(traces))
    monkeypatch.setattr(sessions, "DB_PATH", db)
    monkeypatch.setattr(sessions, "TRACES_PATH", traces)
    for module in (sessions, tasks, batches):
        monkeypatch.setattr(module, "state", state)
        monkeypatch.setattr(module, "session_repo", repository)
    service = batches.TaskBatchService()
    monkeypatch.setattr(tasks, "task_batch_service", service)
    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(tasks.router)
    with TestClient(app) as client:
        yield SimpleNamespace(
            storage=storage, service=service, state=state, client=client, traces=traces
        )


def create_batch(history, statuses=("completed", "running")):
    request = TaskBatchRequest(
        batch_id=uuid4(),
        expected_adb_endpoint_id="local-test-endpoint",
        items=[
            {"goal": f"Private goal {i}", "device_serial": f"phone-{i}"}
            for i in range(len(statuses))
        ],
    )

    async def run(item):
        index = int(item.device_serial.rsplit("-", 1)[1])
        status = statuses[index]
        history.storage.create_session(
            SessionMetadata(
                session_id=item.session_id,
                initial_goal=item.goal,
                status=status,
                pid=os.getpid() if status == "running" else None,
            )
        )
        trace_store.write_status(
            item.session_id,
            {
                "trace_id": item.session_id,
                "status": status,
                "result": "Private result",
                "pid": os.getpid() if status == "running" else None,
            },
        )
        (history.traces / item.session_id / "evidence.txt").write_text("Private evidence")
        if status == "running":
            history.state.active_runs[item.session_id] = {"session_id": item.session_id}
        return {"tasks": [{"session_id": item.session_id}]}

    response = asyncio.run(history.service.submit(request, run))
    return request, response


def test_delete_completed_item_preserves_other_running_item(history, monkeypatch):
    request, batch = create_batch(history)
    deleted, running = [item["session_id"] for item in batch["items"]]

    response = history.client.post(f"/api/sessions/{deleted}/delete")

    assert response.status_code == 200
    refreshed = history.client.get(f"/api/task_batches/{request.batch_id}").json()
    assert [item["session_id"] for item in refreshed["items"]] == [running]
    assert refreshed["summary"]["total"] == refreshed["summary"]["running"] == 1
    assert history.storage.get_session(deleted) is None
    assert history.storage.get_session(running).status == "running"
    assert not (history.traces / deleted).exists()
    assert (history.traces / running / "evidence.txt").read_text() == "Private evidence"

    async def forbidden_run(_request):
        pytest.fail("Retry must retain the pruned batch without replaying removed items")

    monkeypatch.setattr(tasks, "run_task", forbidden_run)
    retry = history.client.post("/api/task_batches", json=request.model_dump(mode="json"))
    assert retry.status_code == 200
    assert [item["session_id"] for item in retry.json()["items"]] == [running]


@pytest.mark.parametrize("clear_all", [False, True])
def test_deleted_batch_is_hidden_and_its_id_cannot_replay(history, monkeypatch, clear_all):
    request, batch = create_batch(history, ("completed", "completed"))
    if clear_all:
        assert history.client.post("/api/cleanup").status_code == 200
    else:
        for item in batch["items"]:
            assert (
                history.client.post(f"/api/sessions/{item['session_id']}/delete").status_code == 200
            )

    assert history.client.get("/api/task_batches").json() == []
    assert history.client.get(f"/api/task_batches/{request.batch_id}").status_code == 410

    async def forbidden_run(_request):
        pytest.fail("Deleted batch IDs must never dispatch again")

    monkeypatch.setattr(tasks, "run_task", forbidden_run)
    payload = request.model_dump(mode="json")
    assert history.client.post("/api/task_batches", json=payload).status_code == 410
    payload["items"][0]["goal"] = "Different task"
    assert history.client.post("/api/task_batches", json=payload).status_code == 409
    assert batches.TaskBatchService().list() == []
    for item in batch["items"]:
        assert history.storage.get_session(item["session_id"]) is None
        assert not (history.traces / item["session_id"]).exists()
    for path in (history.traces / "_task_batches").glob("*.json"):
        assert "Private goal" not in path.read_text()
        assert "Private result" not in path.read_text()


@pytest.mark.parametrize("status", ["running", "queued"])
@pytest.mark.parametrize("clear_all", [False, True])
def test_active_task_history_cannot_be_deleted(history, status, clear_all):
    request, batch = create_batch(history, ("running" if status == "running" else "completed",))
    sid = batch["items"][0]["session_id"]
    if status == "queued":
        history.state.queue_items.append({"session_id": sid, "status": "pending"})
    url = "/api/cleanup" if clear_all else f"/api/sessions/{sid}/delete"

    response = history.client.post(url)

    assert response.status_code == 409
    assert "stop" in response.json()["detail"].lower()
    assert history.storage.get_session(sid) is not None
    assert (history.traces / sid / "evidence.txt").exists()
    assert history.client.get(f"/api/task_batches/{request.batch_id}").status_code == 200


@pytest.mark.asyncio
async def test_inflight_admission_blocks_deletion_until_finished(history):
    request = TaskBatchRequest(
        batch_id=uuid4(),
        expected_adb_endpoint_id="local-test-endpoint",
        items=[{"goal": "Pending admission", "device_serial": "phone-0"}],
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    seen = []

    async def run(item):
        seen.append(item.session_id)
        entered.set()
        await finish.wait()
        return {"tasks": [{"session_id": item.session_id}]}

    submission = asyncio.create_task(history.service.submit(request, run))
    await entered.wait()
    try:
        for url in ("/api/cleanup", f"/api/sessions/{seen[0]}/delete"):
            response = await asyncio.to_thread(history.client.post, url)
            assert response.status_code == 409
            assert "submission" in response.json()["detail"].lower()
    finally:
        finish.set()
        await submission
    assert len(seen) == 1
    assert history.client.post("/api/cleanup").status_code == 200
    assert history.client.get("/api/task_batches").json() == []


def test_direct_storage_deletion_updates_batches_and_preserves_other_rows(history):
    request, batch = create_batch(history, ("completed", "completed"))
    first, second = [item["session_id"] for item in batch["items"]]
    history.storage.delete_session(UUID(first))
    assert [
        item["session_id"]
        for item in batches.TaskBatchService().get(str(request.batch_id))["items"]
    ] == [second]
    history.storage.clear_all_data()
    assert batches.TaskBatchService().list() == []
    assert history.storage.get_session(second) is None


def test_live_worker_in_database_blocks_cleanup_without_daemon_state(history):
    request, batch = create_batch(history, ("running",))
    history.state.active_runs.clear()
    sid = batch["items"][0]["session_id"]
    assert history.client.post("/api/cleanup").status_code == 409
    assert history.client.post(f"/api/sessions/{sid}/delete").status_code == 409
    assert history.storage.get_session(sid).status == "running"
    assert history.client.get(f"/api/task_batches/{request.batch_id}").status_code == 200


@pytest.mark.parametrize("lease_kind", ["owner", "queued"])
@pytest.mark.parametrize("clear_all", [False, True])
def test_external_lease_blocks_deletion_without_database_or_daemon_state(
    history, lease_kind, clear_all
):
    sid = str(uuid4())
    trace_store.write_status(sid, {"trace_id": sid, "status": "running"})
    owner = DeviceExecutionLock("external-phone", session_id=sid)
    ticket = None
    if lease_kind == "owner":
        owner.acquire(blocking=False)
    else:
        ticket = DeviceExecutionLock.reserve(device_id="external-phone", session_id=sid)
    try:
        url = "/api/cleanup" if clear_all else f"/api/sessions/{sid}/delete"
        assert history.client.post(url).status_code == 409
        assert trace_store.read_status(sid)["status"] == "running"
        assert history.storage.get_session(sid) is None
    finally:
        owner.release()
        if ticket:
            DeviceExecutionLock.cancel_reservation(ticket)
