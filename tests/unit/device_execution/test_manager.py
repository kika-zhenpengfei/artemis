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

from artemis.context import ExecutionSetup
from artemis.device_execution import DeviceExecutionManager, DeviceRuntime
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
