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

"""Shared selection never silently executes on a different connected device."""

import asyncio
import importlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from apps.admin_console.routers import tasks
from artemis.device_execution import manager as manager_module
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock
from artemis.runtime.device_pool import DevicePool, DeviceStatus
from artemis.runtime import device_selection as selection


@pytest.fixture(autouse=True)
def isolated_locks(tmp_path, monkeypatch):
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _: locks)
    monkeypatch.setenv("ARTEMIS_CONCURRENCY_MODE", "per_device")


def test_choice_is_shared_by_instances_and_isolated_by_endpoint():
    local = AdbEndpoint.create("localhost", 5037)
    remote = AdbEndpoint.create("192.0.2.10", 5037)
    selection.save_selected_serial("phone-a", local)
    selection.save_selected_serial("phone-b", remote)
    assert selection.get_selected_serial(AdbEndpoint.create("localhost", 5037)) == "phone-a"
    assert selection.get_selected_serial(remote) == "phone-b"
    selection.save_selected_serial(None, local)
    assert selection.get_selected_serial(local) is None
    assert selection.get_selected_serial(remote) == "phone-b"


def test_another_process_reads_the_saved_choice():
    endpoint = AdbEndpoint.create("localhost", 5037)
    selection.save_selected_serial("phone-a", endpoint)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys; "
                "from artemis.runtime import device_selection as s; "
                "from artemis.runtime.adb_endpoint import AdbEndpoint; "
                "s.get_temp_dir = lambda _: Path(sys.argv[1]); "
                "print(s.get_selected_serial(AdbEndpoint.create('localhost', 5037)))"
            ),
            str(selection._selection_path(endpoint).parent),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().endswith("phone-a")


@pytest.mark.parametrize(
    "devices,code",
    [
        ([DeviceStatus("phone-b", "device")], 409),
        ([DeviceStatus("phone-a", "offline"), DeviceStatus("phone-b", "device")], 409),
        ([DeviceStatus("phone-a", "device", is_busy=True)], 409),
        ([DeviceStatus("phone-a", "device", queued_count=1)], 409),
        (None, 503),
    ],
)
def test_saved_choice_never_falls_back_when_unavailable(monkeypatch, devices, code):
    selection.save_selected_serial("phone-a")
    monkeypatch.setattr(
        selection.device_pool, "try_list_devices_async", AsyncMock(return_value=devices)
    )
    with pytest.raises(selection.DeviceSelectionError) as error:
        asyncio.run(selection.resolve_device_selection())
    assert error.value.status_code == code
    assert selection.get_selected_serial() == "phone-a"


def test_explicit_serial_wins_and_preserves_queue_semantics(monkeypatch):
    selection.save_selected_serial("phone-a")
    listing = AsyncMock(
        side_effect=AssertionError("Explicit queued execution must not require idle")
    )
    monkeypatch.setattr(selection.device_pool, "try_list_devices_async", listing)
    assert asyncio.run(selection.resolve_device_selection("phone-b")) == ("phone-b", False)
    listing.assert_not_awaited()


def test_corrupt_preference_is_an_error_not_automatic_selection():
    endpoint = AdbEndpoint.local()
    selection.save_selected_serial("phone-a", endpoint)
    selection._selection_path(endpoint).write_text("{broken", encoding="utf-8")
    with pytest.raises(selection.DeviceSelectionError) as error:
        selection.get_selected_serial(endpoint)
    assert error.value.status_code == 503


def test_device_api_saves_clears_and_rejects_busy_without_changing_choice(monkeypatch):
    listing = AsyncMock(
        return_value=[
            DeviceStatus("phone-a", "device"),
            DeviceStatus("phone-b", "device", queued_count=1),
        ]
    )
    monkeypatch.setattr(selection.device_pool, "try_list_devices_async", listing)
    app = FastAPI()
    app.include_router(tasks.router)
    with TestClient(app) as client:
        assert client.post("/api/devices/selection", json={"serial": "phone-a"}).json() == {
            "selected_serial": "phone-a"
        }
        snapshot = client.get("/api/devices").json()
        assert snapshot["selected_serial"] == "phone-a"
        assert snapshot["devices"][1]["queued_count"] == 1
        assert client.post("/api/devices/selection", json={"serial": "phone-b"}).status_code == 409
        assert selection.get_selected_serial() == "phone-a"
        listing.return_value = None
        assert client.get("/api/devices").status_code == 503
        assert client.post("/api/devices/selection", json={"serial": None}).json() == {
            "selected_serial": None
        }


def test_pool_reports_reservations_and_ignores_other_endpoint(monkeypatch):
    endpoint = AdbEndpoint.create("localhost", 5037)
    monkeypatch.setenv(DeviceExecutionLock.LOCK_SCOPE_ENV, endpoint.identity)
    pool = DevicePool()
    monkeypatch.setattr(
        pool, "_query_adb_devices_sync", lambda: [("phone-a", "device", None, None)]
    )
    other = DeviceExecutionLock.reserve(device_id="phone-a", lock_scope="tcp:192.0.2.10:5037")
    assert pool.list_devices()[0].queued_count == 0
    own = DeviceExecutionLock.reserve(device_id="phone-a", lock_scope=endpoint.identity)
    assert pool.list_devices()[0].queued_count == 1
    DeviceExecutionLock.cancel_reservation(own)
    DeviceExecutionLock.cancel_reservation(other)


def test_runtime_rejects_lock_race_instead_of_waiting_or_switching(monkeypatch):
    selection.save_selected_serial("phone-a")
    monkeypatch.setattr(
        selection.device_pool,
        "try_list_devices_async",
        AsyncMock(return_value=[DeviceStatus("phone-a", "device")]),
    )
    endpoint = selection.current_adb_endpoint()
    lock = DeviceExecutionLock("phone-a", lock_scope=endpoint.identity)
    lock.acquire()
    adb = SimpleNamespace(
        device_list=lambda: [
            SimpleNamespace(serial="phone-a", get_state=lambda: "device"),
            SimpleNamespace(serial="phone-b", get_state=lambda: "device"),
        ]
    )
    monkeypatch.setattr(
        manager_module.AdbDeviceRuntimeAdapter, "_adb_client", staticmethod(lambda: adb)
    )
    try:
        with pytest.raises(DeviceBusyError):
            asyncio.run(
                manager_module.AdbDeviceRuntimeAdapter().open(
                    session_id="new-test",
                    goal="Check display",
                    device_serial=None,
                    agent_client="codex",
                    lock_timeout_seconds=30,
                )
            )
        assert DeviceExecutionLock.get_queued_tasks() == []
        assert DeviceExecutionLock.get_active_owner("phone-b", endpoint.identity) is None
    finally:
        lock.release()


@pytest.mark.parametrize("competing_claim", ["owner", "reservation"])
def test_strict_queue_admission_rejects_a_competing_claim(monkeypatch, competing_claim):
    queue = importlib.import_module("apps.admin_console.services.task_queue_service")
    service = queue.TaskQueueService
    monkeypatch.setattr(service, "ensure_worker_running", lambda: None)
    monkeypatch.setattr(service, "_find_duplicate_submission", lambda *args: None)
    monkeypatch.setattr(service, "_reject_unavailable_device", AsyncMock(return_value=None))
    monkeypatch.setattr(
        queue, "resolve_device_selection", AsyncMock(return_value=("phone-a", True))
    )
    fake_state = SimpleNamespace(
        queue_items=[],
        queue_tasks=[],
        is_running=False,
        wake_event=SimpleNamespace(set=lambda: None),
    )
    monkeypatch.setattr(queue, "state", fake_state)
    endpoint = queue.current_adb_endpoint()
    lock = DeviceExecutionLock("phone-a", lock_scope=endpoint.identity)
    ticket = None
    if competing_claim == "owner":
        lock.acquire()
    else:
        ticket = DeviceExecutionLock.reserve(device_id="phone-a", lock_scope=endpoint.identity)
    try:
        with pytest.raises(selection.DeviceSelectionError):
            asyncio.run(
                service.enqueue_tasks(["Check display"], device_serial="phone-a", require_idle=True)
            )
        assert fake_state.queue_items == []
        assert DeviceExecutionLock.get_active_owner("phone-b", endpoint.identity) is None
    finally:
        lock.release()
        if ticket:
            DeviceExecutionLock.cancel_reservation(ticket)


def test_strict_queue_admission_hands_device_to_its_reservation(monkeypatch):
    queue = importlib.import_module("apps.admin_console.services.task_queue_service")
    service = queue.TaskQueueService
    monkeypatch.setattr(service, "ensure_worker_running", lambda: None)
    monkeypatch.setattr(service, "_find_duplicate_submission", lambda *args: None)
    monkeypatch.setattr(service, "_reject_unavailable_device", AsyncMock(return_value=None))
    monkeypatch.setattr(
        queue, "resolve_device_selection", AsyncMock(return_value=("phone-a", True))
    )
    monkeypatch.setattr(service, "_broadcast_startup_progress", lambda *args: None)
    fake_state = SimpleNamespace(
        queue_items=[],
        queue_tasks=[],
        is_running=False,
        wake_event=SimpleNamespace(set=lambda: None),
    )
    monkeypatch.setattr(queue, "state", fake_state)
    result = asyncio.run(
        service.enqueue_tasks(["Check display"], device_serial="phone-a", require_idle=True)
    )
    item = result["tasks"][0]
    assert item["device_serial"] == "phone-a"
    assert DeviceExecutionLock.get_active_owners() == {}
    queued = DeviceExecutionLock.get_queued_tasks()
    assert len(queued) == 1
    assert queued[0]["session_id"] == item["session_id"]
    with pytest.raises(selection.DeviceSelectionError):
        asyncio.run(
            service.enqueue_tasks(["Another test"], device_serial="phone-a", require_idle=True)
        )
    DeviceExecutionLock.cancel_reservation(item["queue_ticket"])


def test_webui_run_inherits_selection_and_binds_probe_and_queue(monkeypatch):
    selection.save_selected_serial("phone-a")
    monkeypatch.setattr(
        selection.device_pool,
        "try_list_devices_async",
        AsyncMock(return_value=[DeviceStatus("phone-a", "device")]),
    )
    monkeypatch.setattr(
        tasks.device_pool, "validate_explicit_serial_async", AsyncMock(return_value=None)
    )
    probe = AsyncMock(return_value=None)
    enqueue = AsyncMock(return_value={"status": "started"})
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", probe)
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue)
    asyncio.run(tasks.run_task(tasks.RunRequest(goal="Check display")))
    probe.assert_awaited_once_with(target_serial="phone-a")
    assert enqueue.call_args.kwargs["device_serial"] == "phone-a"
    assert enqueue.call_args.kwargs["require_idle"] is True


def test_legacy_mcp_inherits_selection_and_rejects_stale_choice(monkeypatch, tmp_path):
    from artemis.runtime import trace_store

    runner = importlib.import_module("mcp_server.tools.task_runner")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    selection.save_selected_serial("phone-a")
    monkeypatch.setattr(
        selection.device_pool, "try_list_devices", lambda: [DeviceStatus("phone-b", "device")]
    )
    daemon = AsyncMock(side_effect=AssertionError("Stale choice must not dispatch"))
    monkeypatch.setattr(runner, "ensure_daemon_running", daemon)
    result = runner.mobile_run_task("Check display")
    assert result["status"] == "failed"
    assert "phone-a" in result["error"]
    daemon.assert_not_called()


def test_stale_endpoint_rejects_select_and_run_without_saving_or_dispatching(monkeypatch):
    old = AdbEndpoint.create("192.0.2.10", 5037)
    current = AdbEndpoint.create("192.0.2.11", 5037)
    monkeypatch.setattr(selection, "current_adb_endpoint", lambda: current)
    listing = AsyncMock(side_effect=AssertionError("Stale endpoint must not enumerate"))
    monkeypatch.setattr(selection.device_pool, "try_list_devices_async", listing)
    enqueue = AsyncMock()
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue)
    app = FastAPI()
    app.include_router(tasks.router)
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/devices/selection",
                json={
                    "serial": "same-serial",
                    "expected_adb_endpoint_id": old.identity,
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/run",
                json={
                    "goal": "Check display",
                    "device_serial": "same-serial",
                    "require_idle": True,
                    "expected_adb_endpoint_id": old.identity,
                },
            ).status_code
            == 409
        )
    assert selection.get_selected_serial(current) is None
    enqueue.assert_not_awaited()
    listing.assert_not_awaited()


def test_endpoint_switch_during_enumeration_rejects_selection(monkeypatch):
    old = AdbEndpoint.create("192.0.2.10", 5037)
    current = [old]
    monkeypatch.setattr(selection, "current_adb_endpoint", lambda: current[0])

    async def switch_server():
        current[0] = AdbEndpoint.create("192.0.2.11", 5037)
        return [DeviceStatus("same-serial", "device")]

    monkeypatch.setattr(selection.device_pool, "try_list_devices_async", switch_server)
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            tasks.select_execution_device(
                tasks.DeviceSelectionRequest(
                    serial="same-serial",
                    expected_adb_endpoint_id=old.identity,
                )
            )
        )
    assert rejected.value.status_code == 409
    assert "ADB server changed" in rejected.value.detail
    assert selection.get_selected_serial(old) is None
    assert selection.get_selected_serial(current[0]) is None
