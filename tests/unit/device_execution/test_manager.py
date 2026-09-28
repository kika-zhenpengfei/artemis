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
from pathlib import Path
import pytest

from artemis.context import ExecutionSetup
from artemis.device_execution import DeviceExecutionManager, DeviceRuntime
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.mcp.actuators.mock import MockActuator
from artemis.runtime import trace_store


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
