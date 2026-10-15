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

"""Closed-loop tests for external-agent device execution."""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
import pytest
from PIL import Image

from artemis.context import ExecutionSetup
from artemis.device_execution import DeviceExecutionManager, DeviceRuntime
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.device_execution.workflow_start import WorkflowStartStore
from artemis.device_execution.workflow_runner import PreparedWorkflowRunner
from artemis.mcp.actuators.mock import MockActuator
from artemis.runtime import trace_store


@pytest.fixture(autouse=True)
def isolate_runtime_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr("artemis.data_engine.engine.PAUSE_FILE", tmp_path / "paused")
    monkeypatch.setattr(
        "artemis.data_engine.engine.DataEngine._ipc_port_candidates", staticmethod(lambda: []),
    )
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda name: tmp_path / name)


class _MockRuntimeAdapter:
    def __init__(self, traces_path: Path):
        self.actuator = MockActuator(width=1000, height=2000)
        self.actuator.ctx.execution_setup = ExecutionSetup(traces_path=traces_path)
        self.closed = False

        original_get_screen_data = self.actuator.driver.get_screen_data

        async def get_screen_data():
            data = await original_get_screen_data()
            data.ui_elements = [
                {
                    "text": "Debug",
                    "class": "android.widget.Button",
                    "resource_id": "debug_button",
                    "resource-id": "debug_button",
                    "clickable": "true",
                    "bounds": "[100,200][300,400]",
                }
            ]
            return data

        self.actuator.driver.get_screen_data = get_screen_data

    async def open(self, **_kwargs) -> DeviceRuntime:
        return DeviceRuntime(
            ctx=self.actuator.ctx,
            actuator=self.actuator,
            device_serial="mock-device-001",
        )

    async def close(self, runtime: DeviceRuntime) -> None:
        self.closed = True
        await runtime.actuator.driver.disconnect()


def test_case_preparation_blocks_execution_then_records_ready_state(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
        source = tmp_path / "source"
        source.mkdir()
        (source / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
        store = TestWorkflowStore()
        workflow_id = store.create(
            goal="Check screen",
            repository_root=str(source),
            requirements=[{"id": "R1", "description": "Screen", "code_refs": [
                {"path": "Screen.kt", "line": 1, "reason": "UI source"}
            ]}],
        )["workflow_id"]
        store.set_cases(workflow_id, profile="flash", cases=[{
            "id": "C1", "title": "Open", "requirement_ids": ["R1"],
            "steps": ["Tap Debug"], "expected_result": "Debug visible",
            "environment": {"package_name": "com.example.app", "reset": "clear_data"},
        }])

        class FakeEnvironment:
            def __init__(self):
                self.ready = False
                self.restores = []

            async def prepare(self, _serial, _spec):
                if not self.ready:
                    return {"status": "blocked", "reason": "state unavailable", "evidence": []}
                return {
                    "status": "ready", "reason": None, "evidence": ["App data cleared"],
                    "restore": [{"namespace": "global", "key": "auto_time", "value": "1"}],
                }

            async def restore(self, serial, originals):
                self.restores.append((serial, originals))
                return {"status": "restored", "evidence": ["Restored auto_time"], "errors": []}

        environment = FakeEnvironment()
        manager = DeviceExecutionManager(
            runtime_adapter=_MockRuntimeAdapter(tmp_path / "data"),
            environment=environment,
        )
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        observed, _ = await manager.observe(workflow_id, settle_ms=0)
        with pytest.raises(ValueError, match="mobile_prepare_case"):
            await manager.act(workflow_id, action="click", arguments={"target": 1},
                              observation_id=observed["observation_id"], case_id="C1")
        blocked = await manager.prepare_case(workflow_id, case_id="C1")
        assert blocked["status"] == "blocked"
        assert store.snapshot(workflow_id)["counts"]["blocked"] == 1
        environment.ready = True
        ready = await manager.prepare_case(workflow_id, case_id="C1")
        assert ready["status"] == "ready"
        assert store.snapshot(workflow_id)["counts"]["blocked"] == 0
        with pytest.raises(ValueError, match="observation_id is required"):
            await manager.verify(workflow_id, case_id="C1", assertion="Debug visible",
                                 status="passed", evidence="screen visible")
        current, _ = await manager.observe(workflow_id, settle_ms=0)
        await manager.verify(
            workflow_id, case_id="C1", assertion="Debug visible", status="passed",
            evidence="Debug control appears", observation_id=current["observation_id"],
        )
        assert store.snapshot(workflow_id)["counts"]["passed"] == 1
        await manager.prepare_case(workflow_id, case_id="C1")
        assert store.snapshot(workflow_id)["counts"]["not_run"] == 1
        await manager.close(workflow_id)
        assert len(environment.restores) == 2
        assert store.snapshot(workflow_id)["cases"][0]["restoration"]["status"] == "restored"

    asyncio.run(scenario())


def test_current_codex_session_executes_and_persists_closed_loop(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "legacy-traces"))
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter, agent_client="codex")

        opened = await manager.open(goal="Open the app's debug page", idle_timeout_seconds=60)
        session_id = opened["session_id"]
        assert opened["agent_client"] == "codex"
        assert "click" in opened["capabilities"]
        assert "observe_screen" not in opened["capabilities"]
        engine = adapter.actuator.ctx.data_engine
        events: list[tuple[str, dict]] = []
        engine.subscribe(lambda event_type, data: events.append((event_type, data)))

        unsupported, _ = await manager.act(
            session_id,
            action="ask_explorer",
            arguments={"question": "Where is Debug?"},
        )
        assert unsupported["code"] == "UNSUPPORTED"

        observed, image = await manager.observe(session_id, settle_ms=0)
        assert observed["ok"] is True
        assert observed["element_count"] == 1
        assert observed["timings_ms"]["device_capture"] >= 0
        assert "Debug" in observed["elements_text"]
        assert image is not None and image.startswith(b"\x89PNG")

        rejected, _ = await manager.act(
            session_id,
            action="click",
            arguments={"target": 1},
            observation_id="stale-observation",
        )
        assert rejected["ok"] is False
        assert adapter.actuator.action_history == []

        acted, post_image = await manager.act(
            session_id,
            action="click",
            arguments={"target": 1},
            observation_id=observed["observation_id"],
        )
        assert acted["ok"] is True
        assert acted["timings_ms"]["post_action_observe"] >= 0
        assert acted["observation"]["observation_id"] != observed["observation_id"]
        assert post_image is not None
        assert adapter.actuator.action_history[-1] == {
            "action": "tap",
            "x": 200,
            "y": 300,
            "times": 1,
            "duration_ms": 100,
        }

        verified = await manager.verify(
            session_id,
            assertion="Debug page is visible",
            status="passed",
            evidence="The current screen contains the Debug control.",
            observation_id=acted["observation"]["observation_id"],
        )
        assert verified["status"] == "passed"

        closed = await manager.close(session_id, summary="Debug page opened and verified.")
        assert closed["status"] == "completed"
        assert adapter.closed is True

        stored_session = engine.storage.get_session(session_id)
        assert stored_session is not None
        assert stored_session.status == "completed"
        stored_steps = engine.storage.get_steps(session_id)
        assert [step.extra_metadata["event_kind"] for step in stored_steps] == [
            "observation",
            "action",
            "verification",
        ]
        assert all(step.extra_metadata["executor"] == "codex" for step in stored_steps)
        event_types = [event_type for event_type, _ in events]
        assert event_types.count("step_recorded") == 3
        assert "verification_recorded" in event_types
        assert "session_ended" in event_types

    asyncio.run(scenario())


def test_observe_waits_for_screen_change_in_one_recorded_step(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        opened = await manager.open(goal="Wait for a dynamic page")
        session_id = opened["session_id"]
        first, _ = await manager.observe(session_id, settle_ms=0)
        original_get_screen_data = adapter.actuator.driver.get_screen_data
        captures = 0

        async def changing_screen():
            nonlocal captures
            captures += 1
            data = await original_get_screen_data()
            if captures >= 2:
                data.ui_elements[0]["text"] = "Ready"
            return data

        adapter.actuator.driver.get_screen_data = changing_screen
        result, _ = await manager.observe(
            session_id,
            settle_ms=0,
            wait_for_change=True,
            timeout_ms=2000,
            poll_interval_ms=100,
        )
        assert result["wait_status"] == "changed"
        assert result["poll_count"] == 2
        assert result["observation_id"] != first["observation_id"]
        assert "Ready" in result["elements_text"]
        await manager.close(session_id)
        steps = adapter.actuator.ctx.data_engine.storage.get_steps(session_id)
        assert len(steps) == 2
        assert all(step.action_taken["action"] == "observe_screen" for step in steps)

    asyncio.run(scenario())


def test_observe_screen_change_wait_times_out_with_a_fresh_observation(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        opened = await manager.open(goal="Wait for a dynamic page")
        session_id = opened["session_id"]
        first, _ = await manager.observe(session_id, settle_ms=0)
        result, _ = await manager.observe(
            session_id,
            settle_ms=0,
            wait_for_change=True,
            timeout_ms=100,
            poll_interval_ms=100,
        )
        assert result["wait_status"] == "timeout"
        assert result["poll_count"] >= 1
        assert result["observation_id"] != first["observation_id"]
        await manager.close(session_id)

    asyncio.run(scenario())


def test_observe_detects_image_only_screen_change(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        original_get_screen_data = adapter.actuator.driver.get_screen_data
        captures = 0

        def encoded_gradient(reverse: bool) -> str:
            image = Image.new("L", (90, 80))
            image.putdata([89 - x if reverse else x for _y in range(80) for x in range(90)])
            output = io.BytesIO()
            image.save(output, format="PNG")
            return base64.b64encode(output.getvalue()).decode("ascii")

        async def image_only_change():
            nonlocal captures
            captures += 1
            data = await original_get_screen_data()
            data.screenshot_base64 = encoded_gradient(reverse=captures >= 3)
            data.screenshot_bytes = base64.b64decode(data.screenshot_base64)
            return data

        adapter.actuator.driver.get_screen_data = image_only_change
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        opened = await manager.open(goal="Wait for image-only content")
        session_id = opened["session_id"]
        await manager.observe(session_id, settle_ms=0)
        result, _ = await manager.observe(
            session_id,
            settle_ms=0,
            wait_for_change=True,
            timeout_ms=2000,
            poll_interval_ms=100,
        )
        assert result["wait_status"] == "changed"
        assert result["poll_count"] == 2
        await manager.close(session_id)

    asyncio.run(scenario())


def test_zcode_session_identity_is_persisted(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "legacy-traces"))
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter, agent_client="zcode")

        opened = await manager.open(goal="Open settings")
        session_id = opened["session_id"]
        assert opened["agent_client"] == "zcode"
        status = await manager.status(session_id)
        assert status["agent_client"] == "zcode"

        await manager.observe(session_id, settle_ms=0)
        await manager.close(session_id, status="cancelled")
        steps = adapter.actuator.ctx.data_engine.storage.get_steps(session_id)
        assert steps[0].extra_metadata["executor"] == "zcode"

    asyncio.run(scenario())


def test_verify_requires_observable_evidence(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "legacy-traces"))
        adapter = _MockRuntimeAdapter(tmp_path)
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        opened = await manager.open(goal="Verify a screen")
        try:
            try:
                await manager.verify(
                    opened["session_id"],
                    assertion="Screen is correct",
                    status="passed",
                    evidence=" ",
                )
            except ValueError as exc:
                assert str(exc) == "evidence must not be empty"
            else:
                raise AssertionError("empty evidence must be rejected")
        finally:
            await manager.close(opened["session_id"], status="cancelled")

    asyncio.run(scenario())


def test_source_linked_pro_cases_execute_and_appear_in_web_ui(tmp_path, monkeypatch):
    async def scenario():
        traces = tmp_path / "traces"
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(traces))
        source_root = tmp_path / "app"
        source_root.mkdir()
        (source_root / "DebugScreen.kt").write_text(
            "class DebugScreen {\n    fun open() = Unit\n}\n", encoding="utf-8"
        )
        store = TestWorkflowStore()
        context = store.create(
            goal="Verify the debug screen",
            repository_root=str(source_root),
            requirements=[
                {
                    "id": "R1",
                    "description": "The Debug button opens the debug screen",
                    "code_refs": [
                        {
                            "path": "DebugScreen.kt",
                            "line": 2,
                            "reason": "Defines the opening action",
                        }
                    ],
                }
            ],
        )
        workflow_id = context["workflow_id"]
        with pytest.raises(ValueError, match="Generate cases"):
            store.planned(workflow_id)
        plan = store.set_cases(
            workflow_id,
            profile="pro",
            cases=[
                {
                    "id": "C1",
                    "title": "Open debug screen",
                    "requirement_ids": ["R1"],
                    "preconditions": ["App is open"],
                    "steps": ["Tap Debug"],
                    "expected_result": "Debug screen is visible",
                    "checkpoints": [{"id": "P1", "assertion": "Debug control is visible"}],
                }
            ],
        )
        assert Path(plan["plan_path"]).is_file()
        assert "DebugScreen.kt:2" in Path(plan["plan_path"]).read_text(encoding="utf-8")
        from apps.admin_console.routers.sessions import list_prepared_test_workflows

        prepared = await list_prepared_test_workflows()
        assert prepared[0]["workflow_id"] == workflow_id

        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter, agent_client="codex")
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        opened = await manager.open(goal="", workflow_id=workflow_id)
        assert opened["profile"] == "pro"
        assert opened["session_id"] == workflow_id
        assert await list_prepared_test_workflows() == []
        observed, _ = await manager.observe(workflow_id, settle_ms=0)
        with pytest.raises(ValueError, match="case_id"):
            await manager.act(workflow_id, action="click", arguments={"target": 1})
        action, _ = await manager.act(
            workflow_id,
            action="click",
            arguments={"target": 1},
            observation_id=observed["observation_id"],
            case_id="C1",
        )
        assert action["ok"] is True
        observation_id = action["observation"]["observation_id"]
        with pytest.raises(ValueError, match="observation_id is required"):
            await manager.verify(
                workflow_id,
                assertion="Debug control is visible",
                status="passed",
                evidence="Debug appears in the hierarchy",
                case_id="C1",
                checkpoint_id="P1",
            )
        with pytest.raises(ValueError, match="must match the planned"):
            await manager.verify(
                workflow_id,
                assertion="A different assertion",
                status="passed",
                evidence="Debug appears in the hierarchy",
                observation_id=observation_id,
                case_id="C1",
                checkpoint_id="P1",
            )
        checkpoint = await manager.verify(
            workflow_id,
            assertion="Debug control is visible",
            status="passed",
            evidence="Debug appears in the hierarchy",
            observation_id=observation_id,
            case_id="C1",
            checkpoint_id="P1",
        )
        assert checkpoint["case_status"] == "not_run"
        final = await manager.verify(
            workflow_id,
            assertion="Debug screen is visible",
            status="passed",
            evidence="Debug remains visible after the tap",
            observation_id=observation_id,
            case_id="C1",
        )
        assert final["case_status"] == "passed"

        from apps.admin_console.routers.sessions import get_session_test_workflow
        from apps.admin_console.services import media_service as media_service_module
        from apps.admin_console.services.media_service import MediaService

        web_result = await get_session_test_workflow(workflow_id)
        assert web_result["counts"]["passed"] == 1
        assert web_result["cases"][0]["actions"][0]["action"] == "click"
        monkeypatch.setattr(media_service_module, "TRACES_PATH", traces)
        notes = MediaService.get_session_notes_content(workflow_id)
        assert "test_plan.md" in notes
        assert "test_report.md" in notes
        assert "Debug remains visible after the tap" in notes["test_report.md"]

        closed = await manager.close(workflow_id)
        assert closed["test_report"]["counts"]["passed"] == 1
        assert store.snapshot(workflow_id)["phase"] == "finished"
        assert adapter.closed is True

    asyncio.run(scenario())


def test_source_links_and_pro_checkpoints_are_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    source_root = tmp_path / "app"
    source_root.mkdir()
    (source_root / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
    (tmp_path / "Outside.kt").write_text("class Outside\n", encoding="utf-8")
    store = TestWorkflowStore()
    requirement = {
        "id": "R1",
        "description": "Screen opens",
        "code_refs": [{"path": "Screen.kt", "line": 1, "reason": "Target screen"}],
    }
    with pytest.raises(ValueError, match="under"):
        store.create(
            goal="Open screen",
            repository_root=str(source_root),
            requirements=[
                {
                    **requirement,
                    "code_refs": [
                        {"path": "../Outside.kt", "line": 1, "reason": "Outside repository"}
                    ],
                }
            ],
        )
    with pytest.raises(ValueError, match="outside"):
        store.create(
            goal="Open screen",
            repository_root=str(source_root),
            requirements=[
                {
                    **requirement,
                    "code_refs": [{"path": "Screen.kt", "line": 5, "reason": "Wrong line"}],
                }
            ],
        )
    workflow_id = store.create(
        goal="Open screen", repository_root=str(source_root), requirements=[requirement]
    )["workflow_id"]
    with pytest.raises(ValueError, match="Pro cases must declare"):
        store.set_cases(
            workflow_id,
            profile="pro",
            cases=[
                {
                    "id": "C1",
                    "title": "Open screen",
                    "requirement_ids": ["R1"],
                    "steps": ["Tap Open"],
                    "expected_result": "Screen is visible",
                }
            ],
        )
    store.set_cases(
        workflow_id,
        profile="flash",
        cases=[
            {
                "id": "C1",
                "title": "Open screen",
                "requirement_ids": ["R1"],
                "steps": ["Tap Open"],
                "expected_result": "Screen is visible",
            }
        ],
    )
    assert store.list_prepared()[0]["workflow_id"] == workflow_id
    store.bind(workflow_id, session_id=workflow_id, device_serial="mock-device")
    assert store.list_prepared() == []
    result = store.record_verification(
        workflow_id,
        case_id="C1",
        checkpoint_id=None,
        status="passed",
        assertion="Screen is visible",
        evidence="Screen title appears",
        step_id="step-1",
        observation_id="observation-1",
    )
    assert result["case_status"] == "passed"
    report = store.finish(workflow_id, session_status="completed")
    assert report["counts"]["passed"] == 1


def test_unrun_planned_case_fails_completed_session(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
        source_root = tmp_path / "app"
        source_root.mkdir()
        (source_root / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
        store = TestWorkflowStore()
        workflow_id = store.create(
            goal="Verify screen",
            repository_root=str(source_root),
            requirements=[
                {
                    "id": "R1",
                    "description": "Screen appears",
                    "code_refs": [
                        {"path": "Screen.kt", "line": 1, "reason": "Screen implementation"}
                    ],
                }
            ],
        )["workflow_id"]
        store.set_cases(
            workflow_id,
            profile="flash",
            cases=[
                {
                    "id": "C1",
                    "title": "Open screen",
                    "requirement_ids": ["R1"],
                    "steps": ["Open app"],
                    "expected_result": "Screen appears",
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        original_open = adapter.open

        async def slow_open(**kwargs):
            await asyncio.sleep(0.02)
            return await original_open(**kwargs)

        adapter.open = slow_open
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        opening = asyncio.create_task(manager.open(goal="", workflow_id=workflow_id))
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="already open"):
            await manager.open(goal="", workflow_id=workflow_id)
        await opening
        closed = await manager.close(workflow_id)
        assert closed["status"] == "failed"
        assert closed["test_report"]["counts"]["not_run"] == 1
        assert trace_store.read_status(workflow_id)["status"] == "failed"

    asyncio.run(scenario())


def _hybrid_workflow(tmp_path: Path, monkeypatch, *, profile: str = "flash") -> str:
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    root = tmp_path / "app"
    root.mkdir()
    (root / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
    store = TestWorkflowStore()
    workflow_id = store.create(
        goal="Verify Debug control",
        repository_root=str(root),
        requirements=[
            {
                "id": "R1",
                "description": "Debug is reachable",
                "code_refs": [{"path": "Screen.kt", "line": 1, "reason": "Screen"}],
            }
        ],
    )["workflow_id"]
    store.set_cases(
        workflow_id,
        profile=profile,
        cases=[
            {
                "id": "C1",
                "title": "Debug",
                "requirement_ids": ["R1"],
                "steps": ["Tap Debug"],
                "expected_result": "Debug is visible",
                "checkpoints": [{"id": "P1", "assertion": "Debug control is visible"}]
                if profile == "pro"
                else [],
            }
        ],
    )
    return workflow_id


def _visible_debug() -> dict:
    return {"state": "visible", "locator": {"resource_id": "debug_button"}}


def test_hybrid_plan_rejects_unbounded_or_unverified_actions(tmp_path, monkeypatch):
    workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
    store = TestWorkflowStore()
    base = {
        "case_id": "C1",
        "final_assertion": _visible_debug(),
        "steps": [
            {
                "kind": "auto",
                "action": "click",
                "locator": {"resource_id": "debug_button"},
                "expect": _visible_debug(),
            }
        ],
    }
    with pytest.raises(ValueError, match="without target"):
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    **base,
                    "steps": [{**base["steps"][0], "arguments": {"target": 1}}],
                }
            ],
        )
    with pytest.raises(ValueError, match="expect"):
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    **base,
                    "steps": [
                        {key: value for key, value in base["steps"][0].items() if key != "expect"}
                    ],
                }
            ],
        )
    with pytest.raises(ValueError, match="between 0 and 10000"):
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    **base,
                    "steps": [
                        {
                            "kind": "auto",
                            "action": "wait_for_delay",
                            "arguments": {"time_in_ms": 60000},
                            "expect": _visible_debug(),
                        }
                    ],
                }
            ],
        )
    with pytest.raises(ValueError, match="differ from the trigger"):
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    **base,
                    "steps": [
                        {
                            "kind": "model",
                            "goal": "Dismiss overlay",
                            "if_visible": {"resource_id": "debug_button"},
                            "resume_when": _visible_debug(),
                        }
                    ],
                }
            ],
        )


def test_hybrid_fixed_steps_run_without_intermediate_model_calls(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch, profile="pro")
        store = TestWorkflowStore()
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": _visible_debug(),
                    "steps": [
                        {
                            "kind": "auto",
                            "action": "click",
                            "locator": {"resource_id": "debug_button"},
                            "expect": _visible_debug(),
                            "checkpoint_id": "P1",
                        },
                        {
                            "kind": "auto",
                            "action": "click",
                            "locator": {"resource_id": "debug_button"},
                            "expect": _visible_debug(),
                        },
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        result, image = await manager.run_plan(workflow_id, case_id="C1")
        assert result["status"] == "completed"
        assert image is None
        assert len(adapter.actuator.action_history) == 2
        assert store.snapshot(workflow_id)["counts"]["passed"] == 1
        assert store.snapshot(workflow_id)["execution_state"]["C1"]["cursor"] == 2
        closed = await manager.close(workflow_id)
        assert closed["status"] == "completed"
        assert adapter.closed is True

    asyncio.run(scenario())


def test_prepared_workflow_runner_completes_fixed_case_and_closes_lease(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        store.set_execution_plans(workflow_id, plans=[{
            "case_id": "C1",
            "final_assertion": _visible_debug(),
            "steps": [{
                "kind": "auto", "action": "click",
                "locator": {"resource_id": "debug_button"},
                "expect": _visible_debug(),
            }],
        }])
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        runner = PreparedWorkflowRunner(
            manager=DeviceExecutionManager(runtime_adapter=adapter), store=store
        )
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        result = await runner.run(workflow_id)
        assert result["status"] == "completed"
        assert result["test_report"]["counts"]["passed"] == 1
        assert result["test_report"]["cases"][0]["preparation"]["status"] == "ready"
        assert adapter.closed is True

    asyncio.run(scenario())


def test_prepared_workflow_runner_pauses_for_codex_then_resumes(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        store.set_execution_plans(workflow_id, plans=[{
            "case_id": "C1", "final_assertion": _visible_debug(),
            "steps": [{
                "kind": "auto", "action": "click", "locator": {"resource_id": "debug_button"},
                "expect": _visible_debug(),
            }],
        }])
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        original_get_screen_data = adapter.actuator.driver.get_screen_data
        debug_visible = False

        async def other_screen():
            nonlocal debug_visible
            data = await original_get_screen_data()
            if not debug_visible:
                data.ui_elements[0]["resource_id"] = "other_button"
                data.ui_elements[0]["resource-id"] = "other_button"
            return data

        adapter.actuator.driver.get_screen_data = other_screen
        runner = PreparedWorkflowRunner(
            manager=DeviceExecutionManager(runtime_adapter=adapter), store=store
        )
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        result = await runner.run(workflow_id)
        assert result["status"] == "handoff"
        assert "Target missing" in result["handoff"]["reason"]
        assert store.snapshot(workflow_id)["counts"]["not_run"] == 1
        assert adapter.closed is False
        debug_visible = True
        acted, _ = await runner.manager.act(
            workflow_id, action="press_key", arguments={"key": "BACK"}, case_id="C1"
        )
        assert acted["ok"] is True
        completed = await runner.resume(workflow_id, result["handoff"]["resume_token"])
        assert completed["status"] == "completed"
        assert completed["test_report"]["counts"]["passed"] == 1
        assert adapter.closed is True

    asyncio.run(scenario())


def test_hybrid_model_handoff_resumes_at_the_planned_step(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": _visible_debug(),
                    "steps": [
                        {
                            "kind": "model",
                            "goal": "Close a dynamic overlay",
                            "resume_when": _visible_debug(),
                            "max_actions": 2,
                        },
                        {
                            "kind": "auto",
                            "action": "click",
                            "locator": {"resource_id": "debug_button"},
                            "expect": _visible_debug(),
                        },
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        handoff, image = await manager.run_plan(workflow_id, case_id="C1")
        assert handoff["status"] == "handoff"
        assert image is not None
        assert "Debug" in handoff["observation"]["elements_text"]
        with pytest.raises(ValueError, match="resume_token"):
            await manager.run_plan(workflow_id, case_id="C1", resume_token="wrong")
        await manager.act(workflow_id, action="press_key", arguments={"key": "BACK"}, case_id="C1")
        completed, _ = await manager.run_plan(
            workflow_id, case_id="C1", resume_token=handoff["resume_token"]
        )
        assert completed["status"] == "completed"
        assert store.snapshot(workflow_id)["counts"]["passed"] == 1
        await manager.close(workflow_id)

    asyncio.run(scenario())


def test_hybrid_optional_dynamic_screen_is_skipped_when_absent(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": _visible_debug(),
                    "steps": [
                        {
                            "kind": "model",
                            "goal": "Close an ad if shown",
                            "if_visible": {"text": "Advertisement"},
                            "resume_when": _visible_debug(),
                        },
                        {
                            "kind": "auto",
                            "action": "click",
                            "locator": {"resource_id": "debug_button"},
                            "expect": _visible_debug(),
                        },
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        result, _ = await manager.run_plan(workflow_id, case_id="C1")
        assert result["status"] == "completed"
        assert result["metrics"]["handoffs"] == 0
        assert len(adapter.actuator.action_history) == 1
        await manager.close(workflow_id)

    asyncio.run(scenario())


def test_hybrid_fullscreen_ad_without_accessible_marker_uses_model_step(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": _visible_debug(),
                    "steps": [
                        {
                            "kind": "model",
                            "goal": "处理遮挡目标页面的全屏广告",
                            "if_visible": {"text": "Advertisement"},
                            "resume_when": _visible_debug(),
                        },
                        {
                            "kind": "auto",
                            "action": "click",
                            "locator": {"resource_id": "debug_button"},
                            "expect": _visible_debug(),
                        },
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        original_get_screen_data = adapter.actuator.driver.get_screen_data

        async def fullscreen_ad():
            data = await original_get_screen_data()
            data.ui_elements = [
                {
                    "text": "",
                    "class": "android.widget.ImageView",
                    "resource_id": "ad_surface",
                    "resource-id": "ad_surface",
                    "clickable": "true",
                    "bounds": "[0,0][1000,2000]",
                }
            ]
            return data

        adapter.actuator.driver.get_screen_data = fullscreen_ad
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        handoff, _ = await manager.run_plan(workflow_id, case_id="C1")
        assert handoff["status"] == "handoff"
        assert handoff["cursor"] == 0
        assert handoff["reason"] == "处理遮挡目标页面的全屏广告"
        assert "插屏或激励视频广告" in handoff["observation"]["scenario_guidance"]["prompt"]
        assert adapter.actuator.action_history == []
        await manager.close(workflow_id, status="cancelled")

    asyncio.run(scenario())


def test_hybrid_unexpected_screen_pauses_without_repeating_action(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        absent = {"state": "absent", "locator": {"resource_id": "debug_button"}}
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": absent,
                    "steps": [
                        {
                            "kind": "auto",
                            "action": "click",
                            "locator": {"resource_id": "debug_button"},
                            "expect": absent,
                            "timeout_ms": 0,
                        }
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        handoff, _ = await manager.run_plan(workflow_id, case_id="C1")
        assert handoff["status"] == "handoff"
        assert len(adapter.actuator.action_history) == 1
        existing_get_screen_data = adapter.actuator.driver.get_screen_data

        async def no_debug():
            data = await existing_get_screen_data()
            data.ui_elements = []
            return data

        adapter.actuator.driver.get_screen_data = no_debug
        await manager.observe(workflow_id, settle_ms=0)
        completed, _ = await manager.run_plan(
            workflow_id, case_id="C1", resume_token=handoff["resume_token"]
        )
        assert completed["status"] == "completed"
        assert len(adapter.actuator.action_history) == 1
        assert store.snapshot(workflow_id)["counts"]["passed"] == 1
        await manager.close(workflow_id)

    asyncio.run(scenario())


def test_hybrid_model_checkpoint_needs_verification(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch, profile="pro")
        store = TestWorkflowStore()
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": _visible_debug(),
                    "steps": [
                        {
                            "kind": "model",
                            "goal": "Inspect a dynamic screen",
                            "resume_when": _visible_debug(),
                            "checkpoint_id": "P1",
                        }
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        handoff, _ = await manager.run_plan(workflow_id, case_id="C1")
        pending, _ = await manager.run_plan(
            workflow_id, case_id="C1", resume_token=handoff["resume_token"]
        )
        assert pending["status"] == "handoff"
        assert "Verify" in pending["reason"]
        await manager.verify(
            workflow_id,
            case_id="C1",
            checkpoint_id="P1",
            assertion="Debug control is visible",
            status="passed",
            evidence="Debug is visible in the hierarchy",
            observation_id=handoff["observation"]["observation_id"],
        )
        completed, _ = await manager.run_plan(
            workflow_id, case_id="C1", resume_token=handoff["resume_token"]
        )
        assert completed["status"] == "completed"
        assert store.snapshot(workflow_id)["counts"]["passed"] == 1
        await manager.close(workflow_id)

    asyncio.run(scenario())


def test_hybrid_model_action_budget_stops_repeated_probing(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        missing = {"state": "visible", "locator": {"text": "Missing"}}
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": missing,
                    "steps": [
                        {
                            "kind": "model",
                            "goal": "Find the missing screen",
                            "resume_when": missing,
                            "max_actions": 1,
                        }
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        handoff, _ = await manager.run_plan(workflow_id, case_id="C1")
        acted, _ = await manager.act(
            workflow_id, action="press_key", arguments={"key": "BACK"}, case_id="C1"
        )
        assert acted["ok"] is True
        rejected, _ = await manager.act(
            workflow_id, action="press_key", arguments={"key": "BACK"}, case_id="C1"
        )
        assert rejected["code"] == "NO_PROGRESS"
        finished, _ = await manager.run_plan(
            workflow_id, case_id="C1", resume_token=handoff["resume_token"]
        )
        assert finished["status"] == "inconclusive"
        assert store.snapshot(workflow_id)["counts"]["inconclusive"] == 1
        await manager.close(workflow_id)

    asyncio.run(scenario())


def test_hybrid_long_fixed_path_returns_a_continuation(tmp_path, monkeypatch):
    async def scenario():
        workflow_id = _hybrid_workflow(tmp_path, monkeypatch)
        store = TestWorkflowStore()
        store.set_execution_plans(
            workflow_id,
            plans=[
                {
                    "case_id": "C1",
                    "final_assertion": _visible_debug(),
                    "steps": [
                        {
                            "kind": "auto",
                            "action": "wait_for_delay",
                            "arguments": {"time_in_ms": 0},
                            "expect": _visible_debug(),
                        }
                        for _ in range(6)
                    ],
                }
            ],
        )
        adapter = _MockRuntimeAdapter(tmp_path / "data")
        manager = DeviceExecutionManager(runtime_adapter=adapter)
        WorkflowStartStore().confirm(workflow_id, "mock-device-001", "mock-endpoint")
        await manager.open(goal="", workflow_id=workflow_id)
        first, _ = await manager.run_plan(workflow_id, case_id="C1")
        assert first["status"] == "continued"
        assert first["cursor"] == 5
        second, _ = await manager.run_plan(workflow_id, case_id="C1")
        assert second["status"] == "completed"
        assert second["metrics"]["fixed_actions"] == 6
        assert store.snapshot(workflow_id)["counts"]["passed"] == 1
        await manager.close(workflow_id)

    asyncio.run(scenario())
