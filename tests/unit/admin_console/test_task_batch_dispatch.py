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

"""Exercise batch admission through the real queue dispatcher without devices."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from apps.admin_console.core.state import ServerState
from apps.admin_console.routers import tasks
from apps.admin_console.schemas.task_schema import TaskBatchRequest
from apps.admin_console.services import task_batch_service as batch_module
from artemis.runtime import device_selection, trace_store
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.device_pool import DeviceStatus

queue_module = importlib.import_module("apps.admin_console.services.task_queue_service")
state_module = importlib.import_module("apps.admin_console.core.state")
TaskQueueService = queue_module.TaskQueueService


@pytest.fixture
def batch_dispatch(tmp_path, monkeypatch):
    state = ServerState()
    for module in (state_module, tasks, queue_module, batch_module):
        monkeypatch.setattr(module, "state", state)
    monkeypatch.setattr(state_module, "PAUSE_FILE", tmp_path / "pause")
    monkeypatch.setattr(queue_module, "PAUSE_FILE", tmp_path / "pause")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(TaskQueueService, "_run_tasks", set())
    monkeypatch.setattr(TaskQueueService, "_forced_stop_tasks", set())
    # Admission is real; dispatch is triggered explicitly once both items exist.
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", lambda: None)
    monkeypatch.setenv("ARTEMIS_CONCURRENCY_MODE", "per_device")
    monkeypatch.setenv("ARTEMIS_CANCEL_GRACE_SECONDS", "0")
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: locks)
    monkeypatch.setattr("artemis.runtime.cancel_requests.get_temp_dir", lambda _: locks)
    monkeypatch.setattr(device_selection, "current_adb_endpoint", AdbEndpoint.local)
    monkeypatch.setattr(queue_module, "current_adb_endpoint", AdbEndpoint.local)
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

    sessions = {}
    repository = MagicMock()
    repository.get_session_by_id.side_effect = sessions.get
    repository.get_session_status.side_effect = lambda sid: sessions.get(sid, {}).get("status")
    repository.get_video_recording_for_session.return_value = {"status": "ready"}

    def update_session(sid, status, end_time):
        if sid not in sessions:
            return False
        sessions[sid].update(status=status, end_time=end_time)
        return True

    repository.update_session_status.side_effect = update_session
    for module in (tasks, queue_module, batch_module):
        monkeypatch.setattr(module, "session_repo", repository)

    processes = {}
    started = asyncio.Event()

    class FakeProcess:
        def __init__(self, sid, serial, ticket):
            self.pid = 900_000 + len(processes)
            self.session_id = sid
            self.device_serial = serial
            self.ticket = ticket
            self.stdout = None
            self.returncode = None
            self.finished = asyncio.Event()

        async def wait(self):
            await self.finished.wait()
            return self.returncode

        def finish(self, code):
            if self.returncode is None:
                self.returncode = code
                DeviceExecutionLock.cancel_reservation(self.ticket)
                self.finished.set()

        def kill(self):
            self.finish(-9)

    async def spawn(*command, **kwargs):
        env = kwargs["env"]
        sid = env["ARTEMIS_SESSION_ID"]
        serial = command[command.index("--device-serial") + 1]
        assert serial == env["ADB_DEVICE_SERIAL"]
        assert env[DeviceExecutionLock.LOCK_SCOPE_ENV] == AdbEndpoint.local().identity
        proc = FakeProcess(sid, serial, env[DeviceExecutionLock.QUEUE_TICKET_ENV])
        processes[sid] = proc
        sessions[sid] = {"session_id": sid, "status": "running", "pid": proc.pid}
        trace_store.init_trace(sid, command[3], "flash", device_serial=serial)
        if len(processes) == 2:
            started.set()
        return proc

    def alive(pid, *_args):
        return any(proc.pid == pid and proc.returncode is None for proc in processes.values())

    def terminate(pid, *_args):
        proc = next(proc for proc in processes.values() if proc.pid == pid)
        proc.kill()
        return True

    spawn_mock = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
    monkeypatch.setattr(state_module, "pid_is_alive", alive)
    monkeypatch.setattr(queue_module, "pid_is_alive", alive)
    repository.process_is_alive.side_effect = alive
    monkeypatch.setattr(queue_module.process_supervisor, "terminate_tree", terminate)
    monkeypatch.setattr(queue_module.process_supervisor, "terminate_tree_verified", terminate)
    monkeypatch.setattr(TaskQueueService, "_wait_for_worker_process", lambda proc: proc.wait())
    service = batch_module.TaskBatchService()
    monkeypatch.setattr(tasks, "task_batch_service", service)
    return SimpleNamespace(
        state=state, service=service, processes=processes, started=started, spawn=spawn_mock
    )


@pytest.mark.parametrize("stop_first", [False, True], ids=["both-complete", "stop-one"])
@pytest.mark.asyncio
async def test_batch_dispatches_distinct_devices_and_preserves_independent_results(
    batch_dispatch, stop_first
):
    harness = batch_dispatch
    request = TaskBatchRequest(
        batch_id=uuid4(),
        expected_adb_endpoint_id=AdbEndpoint.local().identity,
        items=[
            {"goal": "Open details", "profile": "flash", "device_serial": "phone-a"},
            {"goal": "Check return", "profile": "pro", "device_serial": "phone-b"},
        ],
    )
    try:
        accepted = await tasks.create_task_batch(request)
        assert accepted["summary"]["queued"] == 2
        first, second = [item["session_id"] for item in accepted["items"]]

        TaskQueueService._dispatch_pending_tasks()
        await asyncio.wait_for(harness.started.wait(), timeout=2)
        # Neither process can finish until the test releases it: observing both
        # active here proves overlap rather than two fast sequential executions.
        assert set(harness.state.active_runs) == {first, second}
        assert {proc.device_serial for proc in harness.processes.values()} == {"phone-a", "phone-b"}
        assert all(proc.returncode is None for proc in harness.processes.values())
        assert harness.service.get(str(request.batch_id))["summary"]["running"] == 2

        # Response-loss retries while both devices are occupied must not spawn again.
        retried = await tasks.create_task_batch(request)
        assert [item["session_id"] for item in retried["items"]] == [first, second]
        assert harness.spawn.await_count == 2
        if stop_first:
            assert TaskQueueService.stop_tasks(session_id=first)
            assert harness.processes[first].returncode == -9
            assert harness.processes[second].returncode is None
            assert second in harness.state.active_runs
            assert second not in harness.state.cancelled_session_ids
        else:
            harness.processes[first].finish(0)
        harness.processes[second].finish(0)
        await asyncio.wait_for(asyncio.gather(*TaskQueueService._run_tasks), timeout=2)

        # A fresh service instance reads the persisted manifest and final traces.
        final = batch_module.TaskBatchService().get(str(request.batch_id))
        assert final["status"] == "finished"
        assert final["summary"]["completed"] == (1 if stop_first else 2)
        assert final["summary"]["cancelled"] == (1 if stop_first else 0)
        assert final["summary"]["unknown"] == 0
        assert all(item["verification_status"] == "not_reported" for item in final["items"])
        assert not harness.state.active_runs
        assert not harness.state.queue_items
    finally:
        for proc in harness.processes.values():
            proc.finish(-9)
        await asyncio.gather(*TaskQueueService._run_tasks, return_exceptions=True)


@pytest.mark.parametrize("stop_all", [False, True], ids=["stop-one-pending", "stop-all-pending"])
@pytest.mark.asyncio
async def test_pending_batch_cancellation_survives_queue_removal(batch_dispatch, stop_all):
    harness = batch_dispatch
    request = TaskBatchRequest(
        batch_id=uuid4(),
        expected_adb_endpoint_id=AdbEndpoint.local().identity,
        items=[
            {"goal": "Open details", "device_serial": "phone-a"},
            {"goal": "Check return", "device_serial": "phone-b"},
        ],
    )
    accepted = await tasks.create_task_batch(request)
    first, second = [item["session_id"] for item in accepted["items"]]
    assert all(trace_store.read_status(sid) is None for sid in (first, second))
    assert TaskQueueService.stop_tasks(clear_all=stop_all, session_id=None if stop_all else first)

    final = batch_module.TaskBatchService().get(str(request.batch_id))
    assert final["summary"]["cancelled"] == (2 if stop_all else 1)
    assert final["summary"]["queued"] == (0 if stop_all else 1)
    assert final["summary"]["unknown"] == 0
    harness.spawn.assert_not_awaited()
