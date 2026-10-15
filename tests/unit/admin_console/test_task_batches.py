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

"""Batch admission preserves target binding and never replays uncertain work."""

import asyncio
import importlib
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from apps.admin_console.routers import tasks
from apps.admin_console.schemas.task_schema import TaskBatchRequest
from apps.admin_console.services import task_batch_service as batch_module
from artemis.runtime import device_selection, trace_store
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock
from artemis.runtime.device_pool import DeviceStatus


@pytest.fixture
def batch_service(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(batch_module.state, "queue_items", [])
    monkeypatch.setattr(batch_module.state, "active_runs", {})
    monkeypatch.setattr(batch_module.state, "active_session_id", None)
    monkeypatch.setattr(batch_module.state, "active_connections", {})
    monkeypatch.setattr(batch_module.session_repo, "get_session_by_id", lambda _: None)
    monkeypatch.setattr(batch_module.session_repo, "process_is_alive", lambda _: False)
    monkeypatch.setattr(
        importlib.import_module("apps.admin_console.core.state"), "PAUSE_FILE", tmp_path / "pause"
    )
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: locks)
    monkeypatch.setenv("ARTEMIS_CONCURRENCY_MODE", "per_device")
    monkeypatch.setattr(device_selection, "current_adb_endpoint", AdbEndpoint.local)
    monkeypatch.setattr(
        type(tasks.task_queue_service), "ensure_worker_running", classmethod(lambda cls: None)
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("Tests must not spawn workers")),
    )
    monkeypatch.setattr(
        tasks.readiness_engine, "run_device_submission_probe", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        tasks.device_pool, "validate_explicit_serial_async", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        tasks.device_pool,
        "try_list_devices_async",
        AsyncMock(
            return_value=[DeviceStatus("phone-a", "device"), DeviceStatus("phone-b", "device")]
        ),
    )
    service = batch_module.TaskBatchService()
    monkeypatch.setattr(tasks, "task_batch_service", service)
    return service


def request_payload():
    return {
        "batch_id": str(uuid4()),
        "expected_adb_endpoint_id": AdbEndpoint.local().identity,
        "items": [
            {"goal": "Open details", "profile": "flash", "device_serial": "phone-a"},
            {"goal": "Check return", "profile": "pro", "device_serial": "phone-b"},
        ],
    }


def api_client():
    app = FastAPI()
    app.include_router(tasks.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        [{"goal": "  ", "device_serial": "phone-a"}],
        [{"goal": "A", "device_serial": " "}],
        [{"goal": "A"}],
        [{"goal": "A", "device_serial": "phone-a", "profile": "other"}],
        [{"goal": "A", "device_serial": "phone-a"}, {"goal": "B", "device_serial": " phone-a "}],
    ],
)
def test_invalid_items_reject_whole_batch_without_dispatch(batch_service, monkeypatch, invalid):
    run = AsyncMock()
    monkeypatch.setattr(tasks, "run_task", run)
    payload = {**request_payload(), "items": invalid}
    with api_client() as client:
        assert client.post("/api/task_batches", json=payload).status_code == 422
        assert client.get("/api/task_batches").json() == []
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_uses_independent_sessions_and_explicit_devices(batch_service):
    request = TaskBatchRequest(**request_payload())
    response = await tasks.create_task_batch(request)
    assert response["submission_status"] == "accepted"
    assert response["status"] == "queued"
    assert response["summary"]["queued"] == 2
    queued = {item["session_id"]: item for item in tasks.state.queue_items}
    assert len(queued) == 2
    for item in response["items"]:
        task = queued[item["session_id"]]
        assert task["device_serial"] == item["device_serial"]
        assert task["profile"] == item["profile"]
        assert task["adb_endpoint"]["identity"] == request.expected_adb_endpoint_id
        assert item["verification_status"] == "not_reported"
    assert await tasks.create_task_batch(request) == response
    assert len(tasks.state.queue_items) == 2


@pytest.mark.parametrize("failure", ["busy", "race", "endpoint"])
@pytest.mark.asyncio
async def test_one_rejected_item_does_not_block_other_device(batch_service, monkeypatch, failure):
    if failure == "busy":
        monkeypatch.setattr(
            tasks.device_pool,
            "try_list_devices_async",
            AsyncMock(
                return_value=[
                    DeviceStatus("phone-a", "device"),
                    DeviceStatus("phone-b", "device", is_busy=True),
                ]
            ),
        )
    elif failure == "race":
        original = DeviceExecutionLock.acquire

        def acquire(lock, **kwargs):
            if lock.device_id == "phone-b":
                raise DeviceBusyError("phone-b became busy during admission")
            return original(lock, **kwargs)

        monkeypatch.setattr(DeviceExecutionLock, "acquire", acquire)
    else:
        original = tasks.run_task

        async def run(request):
            if request.device_serial == "phone-b":
                raise HTTPException(409, "The ADB server changed")
            return await original(request)

        monkeypatch.setattr(tasks, "run_task", run)
    result = await tasks.create_task_batch(TaskBatchRequest(**request_payload()))
    assert result["submission_status"] == "partial"
    assert result["summary"]["accepted"] == result["summary"]["rejected"] == 1
    assert [item["device_serial"] for item in tasks.state.queue_items] == ["phone-a"]
    assert result["items"][1]["status"] == "rejected"
    assert result["items"][1]["error"]


@pytest.mark.asyncio
async def test_same_id_same_payload_is_idempotent_across_instances(batch_service):
    request = TaskBatchRequest(**request_payload())
    await batch_service.submit(request, tasks.run_task)
    recreated = batch_module.TaskBatchService()
    run = AsyncMock(side_effect=AssertionError("A persisted batch must not dispatch again"))
    result = await recreated.submit(request, run)
    assert result["summary"]["accepted"] == 2
    assert len(tasks.state.queue_items) == 2
    changed = request.model_copy(update={"items": list(reversed(request.items))})
    with pytest.raises(batch_module.TaskBatchError) as rejected:
        await recreated.submit(changed, run)
    assert rejected.value.status_code == 409
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_duplicate_submissions_only_dispatch_each_item_once(batch_service):
    request = TaskBatchRequest(**request_payload())
    run = AsyncMock(wraps=tasks.run_task)
    results = await asyncio.gather(
        batch_service.submit(request, run),
        batch_module.TaskBatchService().submit(request, run),
    )
    assert run.await_count == 2
    assert len(tasks.state.queue_items) == 2
    assert {result["batch_id"] for result in results} == {str(request.batch_id)}


@pytest.mark.asyncio
async def test_different_returned_session_is_unknown_and_not_retried(batch_service):
    request = TaskBatchRequest(**request_payload())
    run = AsyncMock(return_value={"tasks": [{"session_id": "unrelated-session"}]})
    result = await batch_service.submit(request, run)
    assert result["submission_status"] == result["status"] == "unknown"
    assert result["summary"]["unknown"] == 2
    assert all(item["session_id"] != "unrelated-session" for item in result["items"])
    await batch_service.submit(request, run)
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_claimed_batch(batch_service):
    request = TaskBatchRequest(**request_payload())
    entered, release = asyncio.Event(), asyncio.Event()
    original = tasks.run_task

    async def run(item):
        entered.set()
        await release.wait()
        return await original(item)

    submission = asyncio.create_task(batch_service.submit(request, run))
    await entered.wait()
    submission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submission
    release.set()
    await asyncio.gather(*batch_service._submissions)
    assert batch_service.get(str(request.batch_id))["summary"]["accepted"] == 2


def test_restart_reconciles_pending_by_session_id_and_keeps_missing_evidence_unknown(
    batch_service, monkeypatch
):
    request = TaskBatchRequest(**request_payload())
    manifest, _ = batch_service._claim(request)
    manifest["owner_pid"] = 0
    batch_service._save(manifest)
    sid = manifest["items"][0]["session_id"]
    monkeypatch.setattr(
        batch_module.session_repo,
        "get_session_by_id",
        lambda current: {"status": "completed"} if current == sid else None,
    )
    trace_store.init_trace(sid, "Open details", "Flash")
    # Persisted DB terminal state wins over this stale running trace.
    result = batch_module.TaskBatchService().get(str(request.batch_id))
    assert result["items"][0]["status"] == "completed"
    assert result["items"][0]["admission_status"] == "accepted"
    assert result["items"][0]["verification_status"] == "not_reported"
    assert result["items"][1]["status"] == "unknown"
    assert result["status"] == result["submission_status"] == "unknown"


@pytest.mark.asyncio
async def test_accepted_but_lost_queue_is_unknown_after_restart(batch_service, monkeypatch):
    request = TaskBatchRequest(**request_payload())
    await batch_service.submit(request, tasks.run_task)
    monkeypatch.setattr(tasks.state, "queue_items", [])
    monkeypatch.setattr(DeviceExecutionLock, "get_queued_tasks", lambda: [])
    result = batch_module.TaskBatchService().get(str(request.batch_id))
    assert result["submission_status"] == "accepted"
    assert result["status"] == "unknown"
    assert result["summary"]["unknown"] == 2


@pytest.mark.asyncio
async def test_terminal_results_persist_and_do_not_imply_validation_pass(
    batch_service, monkeypatch
):
    request = TaskBatchRequest(**request_payload())
    batch = await batch_service.submit(request, tasks.run_task)
    statuses = {
        batch["items"][0]["session_id"]: "completed",
        batch["items"][1]["session_id"]: "failed",
    }
    for sid, status in statuses.items():
        trace_store.init_trace(sid, "Goal", "Flash")
        trace_store.update_trace_status(
            sid,
            status,
            result={"summary": "Recorded result"},
            error="Failure" if status == "failed" else None,
        )
    result = batch_module.TaskBatchService().get(str(request.batch_id))
    assert result["status"] == "finished"
    assert result["summary"]["completed"] == result["summary"]["failed"] == 1
    assert all(item["verification_status"] == "not_reported" for item in result["items"])
    assert result["items"][1]["error"] == "Failure"
    assert result["items"][0]["result"] == {"summary": "Recorded result"}
    with api_client() as client:
        assert client.get(f"/api/task_batches/{request.batch_id}").json() == result
        assert client.get("/api/task_batches").json() == [result]
        assert client.get(f"/api/task_batches/{uuid4()}").status_code == 404
        assert client.get("/api/task_batches/not-a-uuid").status_code == 422


@pytest.mark.asyncio
async def test_pause_remains_visible_in_batch_summary(batch_service, monkeypatch):
    request = TaskBatchRequest(**request_payload())
    await batch_service.submit(request, tasks.run_task)
    state_module = importlib.import_module("apps.admin_console.core.state")
    state_module.PAUSE_FILE.write_text("Model overloaded", encoding="utf-8")
    result = batch_service.get(str(request.batch_id))
    assert result["status"] == "paused"
    assert result["summary"]["paused"] == 2


def test_corrupt_manifest_is_not_replaced_by_a_retry(batch_service):
    request = TaskBatchRequest(**request_payload())
    manifest, _ = batch_service._claim(request)
    path = batch_service._path(manifest["batch_id"])
    path.write_text("{corrupt", encoding="utf-8")
    with api_client() as client:
        assert (
            client.post("/api/task_batches", json=request.model_dump(mode="json")).status_code
            == 503
        )
    assert path.read_text(encoding="utf-8") == "{corrupt"


@pytest.mark.asyncio
async def test_stale_endpoint_rejects_all_items_without_enqueue(batch_service):
    payload = request_payload()
    payload["expected_adb_endpoint_id"] = AdbEndpoint.create("192.0.2.10", 5037).identity
    result = await tasks.create_task_batch(TaskBatchRequest(**payload))
    assert result["submission_status"] == "rejected"
    assert result["summary"]["rejected"] == 2
    assert tasks.state.queue_items == []


@pytest.mark.asyncio
async def test_save_failure_does_not_leave_live_process_permanently_submitting(
    batch_service, monkeypatch
):
    request = TaskBatchRequest(**request_payload())
    original_save = batch_service._save

    def fail_after_claim(manifest):
        if any(item["admission_status"] != "pending" for item in manifest["items"]):
            raise batch_module.TaskBatchError("Disk write failed", 503)
        original_save(manifest)

    async def admitted(item):
        return {"tasks": [{"session_id": item.session_id}]}

    run = AsyncMock(side_effect=admitted)
    monkeypatch.setattr(batch_service, "_save", fail_after_claim)
    with pytest.raises(batch_module.TaskBatchError, match="Disk write failed"):
        await batch_service.submit(request, run)
    result = batch_service.get(str(request.batch_id))
    assert result["submission_status"] == result["status"] == "unknown"
    await batch_service.submit(request, run)
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_unexpected_dispatch_error_is_logged_and_remains_unknown(batch_service, caplog):
    run = AsyncMock(side_effect=RuntimeError("Unexpected dispatch failure"))
    result = await batch_service.submit(TaskBatchRequest(**request_payload()), run)
    assert result["submission_status"] == "unknown"
    assert result["items"][0]["error"] == "Unexpected dispatch failure"
    assert "Could not confirm batch task admission" in caplog.text
