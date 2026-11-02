"""Workflow claims permit automatic allocation and serialize startup."""

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.runtime import trace_store


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr("artemis.data_engine.engine.PAUSE_FILE", tmp_path / "paused")
    monkeypatch.setattr(
        "artemis.data_engine.engine.DataEngine._ipc_port_candidates", staticmethod(lambda: []),
    )
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda name: tmp_path / name)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
    store = TestWorkflowStore()
    workflow_id = store.create(
        goal="Check screen", repository_root=str(source),
        requirements=[{"id": "R1", "description": "Screen", "code_refs": [
            {"path": "Screen.kt", "line": 1, "reason": "UI source"},
        ]}],
    )["workflow_id"]
    cases = [{
        "id": "C1", "title": "Open", "requirement_ids": ["R1"],
        "steps": ["Tap Debug"], "expected_result": "Debug visible",
    }]
    store.set_cases(workflow_id, profile="flash", cases=cases)
    return store, workflow_id, cases


def test_published_plan_waits_without_device_and_confirmation_is_shared(workflow):
    from artemis.device_execution.workflow_start import WorkflowStartStore

    _store, workflow_id, _cases = workflow
    starts = WorkflowStartStore()
    pending = starts.list_pending()
    assert pending[0]["workflow_id"] == workflow_id
    assert pending[0]["status"] == "awaiting_device"
    assert pending[0]["case_count"] == 1
    assert pending[0]["profile"] == "flash"
    assert pending[0]["device_serial"] is None
    starts.confirm(workflow_id, "phone-a", "endpoint-a")
    saved = WorkflowStartStore().status(workflow_id)
    assert saved["status"] == "confirmed"
    assert saved["device_serial"] == "phone-a"
    assert saved["adb_endpoint_id"] == "endpoint-a"
    assert not (Path(trace_store.TRACES_DIR) / workflow_id / "status.json").exists()


def test_start_claim_needs_no_confirmation_and_failure_allows_retry(workflow):
    from artemis.device_execution.workflow_start import WorkflowStartError, WorkflowStartStore

    _store, workflow_id, _cases = workflow
    starts = WorkflowStartStore()
    claimed = starts.claim(workflow_id)
    assert claimed["status"] == "starting"
    assert claimed["device_serial"] is None
    with pytest.raises(WorkflowStartError, match="already starting"):
        starts.claim(workflow_id)
    with pytest.raises(WorkflowStartError, match="already starting"):
        starts.confirm(workflow_id, "phone-b", "endpoint-a")
    starts.failed(workflow_id, claimed["claim_token"], "Device busy")
    assert starts.status(workflow_id)["error"] == "Device busy"
    assert starts.status(workflow_id)["status"] == "awaiting_device"
    assert starts.claim(workflow_id)["status"] == "starting"


def test_plan_edit_invalidates_confirmation_and_cannot_race_startup(workflow):
    from artemis.device_execution.workflow_start import WorkflowStartError, WorkflowStartStore

    store, workflow_id, cases = workflow
    starts = WorkflowStartStore()
    starts.confirm(workflow_id, "phone-a", "endpoint-a")
    store.set_cases(workflow_id, profile="flash", cases=[{**cases[0], "steps": ["Open Settings"]}])
    assert starts.status(workflow_id)["status"] == "awaiting_device"
    starts.confirm(workflow_id, "phone-a", "endpoint-a")
    starts.claim(workflow_id)
    with pytest.raises(WorkflowStartError, match="already starting"):
        store.set_cases(workflow_id, profile="flash", cases=cases)


def test_unconfirmed_open_attempts_automatic_allocation_without_creating_a_session_when_busy(workflow):
    from artemis.device_execution.manager import DeviceExecutionManager
    from artemis.runtime.device_lock import DeviceBusyError

    _store, workflow_id, _cases = workflow
    adapter = Mock(open=AsyncMock(side_effect=DeviceBusyError("No idle device")), close=AsyncMock())
    factory = Mock()
    manager = DeviceExecutionManager(runtime_adapter=adapter, data_engine_factory=factory)
    result = asyncio.run(manager.open(goal="", workflow_id=workflow_id))
    assert result["status"] == "awaiting_device"
    assert result["workflow_id"] == workflow_id
    assert "automatic" in result["next"]
    adapter.open.assert_awaited_once()
    assert adapter.open.call_args.kwargs["device_serial"] is None
    factory.assert_not_called()
    assert not (Path(trace_store.TRACES_DIR) / workflow_id / "status.json").exists()


def test_workflow_opens_and_records_allocated_device_without_confirmation(workflow, tmp_path):
    from artemis.context import ExecutionSetup
    from artemis.device_execution.manager import DeviceExecutionManager, DeviceRuntime
    from artemis.device_execution.workflow_start import WorkflowStartStore
    from artemis.mcp.actuators.mock import MockActuator

    async def scenario():
        store, workflow_id, _cases = workflow
        actuator = MockActuator(width=1000, height=2000)
        actuator.ctx.execution_setup = ExecutionSetup(traces_path=tmp_path / "data")
        runtime = DeviceRuntime(actuator.ctx, actuator, "allocated-phone", Mock())
        adapter = Mock(open=AsyncMock(return_value=runtime), close=AsyncMock())
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        result = await manager.open(goal="", workflow_id=workflow_id)
        try:
            assert result["status"] == "running"
            assert result["device_serial"] == "allocated-phone"
            assert adapter.open.call_args.kwargs["device_serial"] is None
            assert store.snapshot(workflow_id)["device_serial"] == "allocated-phone"
            assert WorkflowStartStore().status(workflow_id)["device_serial"] == "allocated-phone"
        finally:
            await manager.close(workflow_id)
        adapter.close.assert_awaited_once_with(runtime)

    asyncio.run(scenario())


def test_startup_failure_and_cancellation_release_claim_for_reselection(workflow):
    from artemis.device_execution.manager import DeviceExecutionManager
    from artemis.device_execution.workflow_start import WorkflowStartStore
    from artemis.runtime.device_lock import DeviceBusyError

    async def scenario():
        _store, workflow_id, _cases = workflow
        starts = WorkflowStartStore()
        adapter = Mock(open=AsyncMock(side_effect=DeviceBusyError("Phone is busy")), close=AsyncMock())
        manager = DeviceExecutionManager(runtime_adapter=adapter, data_engine_factory=Mock())
        starts.confirm(workflow_id, "phone-a", "endpoint-a")
        result = await manager.open(goal="", workflow_id=workflow_id)
        assert result["status"] == "awaiting_device"
        assert "busy" in result["error"]
        kwargs = adapter.open.call_args.kwargs
        assert kwargs["device_serial"] is None
        assert "expected_adb_endpoint_id" not in kwargs
        assert kwargs["lock_timeout_seconds"] == 0

        started = asyncio.Event()

        async def wait_forever(**_kwargs):
            started.set()
            await asyncio.Event().wait()

        starts.confirm(workflow_id, "phone-b", "endpoint-b")
        adapter.open.side_effect = wait_forever
        task = asyncio.create_task(manager.open(goal="", workflow_id=workflow_id))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert starts.status(workflow_id)["status"] == "awaiting_device"
        assert starts.confirm(workflow_id, "phone-c", "endpoint-c")["status"] == "confirmed"

    asyncio.run(scenario())


def test_session_initialization_failure_closes_runtime_before_allowing_retry(workflow):
    from artemis.device_execution.manager import DeviceExecutionManager
    from artemis.device_execution.workflow_start import WorkflowStartStore

    async def scenario():
        _store, workflow_id, _cases = workflow
        starts = WorkflowStartStore()
        starts.confirm(workflow_id, "phone-a", "endpoint-a")
        runtime = SimpleNamespace(ctx=SimpleNamespace())

        async def closed(_runtime):
            assert starts.status(workflow_id)["status"] == "starting"

        adapter = Mock(open=AsyncMock(return_value=runtime), close=AsyncMock(side_effect=closed))
        manager = DeviceExecutionManager(
            runtime_adapter=adapter, data_engine_factory=Mock(side_effect=OSError("Storage failed")),
        )
        with pytest.raises(OSError, match="Storage failed"):
            await manager.open(goal="", workflow_id=workflow_id)
        adapter.close.assert_awaited_once_with(runtime)
        assert starts.status(workflow_id)["status"] == "awaiting_device"

    asyncio.run(scenario())


def test_prepared_runner_does_not_advance_when_no_device_is_idle(workflow):
    from artemis.device_execution.manager import DeviceExecutionManager
    from artemis.device_execution.workflow_runner import PreparedWorkflowRunner

    async def scenario():
        store, workflow_id, _cases = workflow
        visible = {"state": "visible", "locator": {"resource_id": "debug_button"}}
        store.set_execution_plans(workflow_id, plans=[{
            "case_id": "C1", "final_assertion": visible,
            "steps": [{"kind": "auto", "action": "click", "locator": {"resource_id": "debug_button"}, "expect": visible}],
        }])
        from artemis.runtime.device_lock import DeviceBusyError

        adapter = Mock(open=AsyncMock(side_effect=DeviceBusyError("No idle device")), close=AsyncMock())
        manager = DeviceExecutionManager(runtime_adapter=adapter, data_engine_factory=Mock())
        manager.prepare_case = AsyncMock()
        manager.run_plan = AsyncMock()
        result = await PreparedWorkflowRunner(manager=manager, store=store).run(workflow_id)
        assert result["status"] == "awaiting_device"
        manager.prepare_case.assert_not_called()
        manager.run_plan.assert_not_called()
        adapter.open.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("same_device", [False, True])
def test_workflows_use_independent_device_locks(workflow, tmp_path, same_device):
    from artemis.context import ExecutionSetup
    from artemis.device_execution.manager import DeviceExecutionManager, DeviceRuntime
    from artemis.device_execution.workflow_start import WorkflowStartStore
    from artemis.mcp.actuators.mock import MockActuator
    from artemis.runtime.device_lock import DeviceExecutionLock

    async def scenario():
        store, first, cases = workflow
        second = store.create(
            goal="Another test", repository_root=str(tmp_path / "source"),
            requirements=[{"id": "R1", "description": "Screen", "code_refs": [
                {"path": "Screen.kt", "line": 1, "reason": "UI source"},
            ]}],
        )["workflow_id"]
        store.set_cases(second, profile="flash", cases=cases)
        starts = WorkflowStartStore()
        entered = 0
        both_entered = asyncio.Event()

        class RuntimeAdapter:
            async def open(self, *, session_id, device_serial, expected_adb_endpoint_id, **_kwargs):
                nonlocal entered
                entered += 1
                if entered == 2:
                    both_entered.set()
                await both_entered.wait()
                lock = DeviceExecutionLock(
                    device_serial, session_id=session_id, lock_scope=expected_adb_endpoint_id,
                    max_concurrency=0, concurrency_mode="per_device",
                )
                lock.acquire(blocking=False)
                actuator = MockActuator(width=1000, height=2000)
                actuator.ctx.execution_setup = ExecutionSetup(traces_path=tmp_path / session_id)
                return DeviceRuntime(actuator.ctx, actuator, device_serial, lock)

            async def close(self, runtime):
                runtime.lock.release()
                await runtime.actuator.driver.disconnect()

        manager = DeviceExecutionManager(runtime_adapter=RuntimeAdapter())
        try:
            results = await asyncio.gather(
                manager.open(goal="", workflow_id=first, device_serial="phone-a", expected_adb_endpoint_id="mock-endpoint"),
                manager.open(goal="", workflow_id=second, device_serial="phone-a" if same_device else "phone-b", expected_adb_endpoint_id="mock-endpoint"),
            )
            assert [item["status"] for item in results].count("running") == (1 if same_device else 2)
            assert [item["status"] for item in results].count("awaiting_device") == (1 if same_device else 0)
            owners = DeviceExecutionLock.get_active_owners()
            assert {owner.device_id for owner in owners.values()} == ({"phone-a"} if same_device else {"phone-a", "phone-b"})
            if same_device:
                waiting = next(item for item in results if item["status"] == "awaiting_device")
                assert "already controlled" in waiting["error"]
        finally:
            await manager.close_all()
        assert DeviceExecutionLock.get_active_owners() == {}

    asyncio.run(scenario())


def test_cancelled_runtime_waits_for_lock_thread_and_releases_late_acquisition(monkeypatch):
    from artemis.device_execution import manager as manager_module

    async def scenario():
        started = threading.Event()
        released = []

        class Lock:
            def __init__(self, *_args, **_kwargs):
                self.acquired = False

            def acquire(self, *, cancel_event, **_kwargs):
                started.set()
                assert cancel_event.wait(2)
                self.acquired = True

            def release(self):
                released.append(self.acquired)
                self.acquired = False

        monkeypatch.setattr(manager_module, "DeviceExecutionLock", Lock)
        monkeypatch.setattr(
            manager_module, "resolve_device_selection", AsyncMock(return_value=("phone-a", True)),
        )
        adb = SimpleNamespace(device_list=lambda: [
            SimpleNamespace(serial="phone-a", get_state=lambda: "device"),
        ])
        monkeypatch.setattr(manager_module.AdbDeviceRuntimeAdapter, "_adb_client", staticmethod(lambda: adb))
        task = asyncio.create_task(manager_module.AdbDeviceRuntimeAdapter().open(
            session_id="cancel-test", goal="Check screen", device_serial="phone-a",
            agent_client="codex", lock_timeout_seconds=0,
        ))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released == [True]

    asyncio.run(scenario())
