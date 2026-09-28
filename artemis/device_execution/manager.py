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

"""Deep device-execution module used by external reasoning agents.

The public interface is deliberately session-oriented: open, observe, act,
verify, close. Device selection, FIFO locking, indexed-element resolution,
act-then-observe, trace persistence and WebUI IPC events stay behind it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol
import uuid

from adbutils import AdbClient

from artemis.clients.screen_client_factory import create_screen_client
from artemis.config import settings
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.engine import DataEngine
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.mcp.action_executor import McpActionExecutor
from artemis.agents.scenario_rules import scenario_guidance
from artemis.mcp.action_session import get_action_session
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators.base import Actuator
from artemis.mcp.actuators.adb import AdbActuator
from artemis.platform import platform
from artemis.runtime import DeviceExecutionLock, trace_store
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def _normalize_agent_client(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", (value or "").strip().lower()).strip("-_")
    return normalized or "external-agent"


def _agent_display_name(agent_client: str) -> str:
    if agent_client == "codex":
        return "Codex"
    if agent_client == "zcode":
        return "ZCode"
    return agent_client.replace("_", "-").title()


_SCREEN_TARGET_ACTIONS = frozenset(
    {
        "click",
        "click_sequence",
        "focus_and_clear_text",
        "input_text",
        "long_press",
        "swipe",
    }
)


@dataclass(slots=True)
class DeviceRuntime:
    """Resources owned by one device-execution session."""

    ctx: ArtemisContext
    actuator: Actuator
    device_serial: str
    lock: DeviceExecutionLock | None = None


class DeviceRuntimeAdapter(Protocol):
    """Internal seam for production ADB and in-memory test runtimes."""

    async def open(
        self,
        *,
        session_id: str,
        goal: str,
        device_serial: str | None,
        agent_client: str,
        lock_timeout_seconds: float,
    ) -> DeviceRuntime: ...

    async def close(self, runtime: DeviceRuntime) -> None: ...


class DeviceExecution(Protocol):
    """Stable execution port consumed by MCP and other external orchestrators."""

    async def open(
        self,
        *,
        goal: str,
        device_serial: str | None = None,
        session_id: str | None = None,
        lock_timeout_seconds: float = 30.0,
        idle_timeout_seconds: float = 900.0,
        workflow_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def status(self, session_id: str | None = None) -> dict[str, Any]: ...

    async def observe(
        self,
        session_id: str,
        *,
        settle_ms: int = 400,
    ) -> tuple[dict[str, Any], bytes | None]: ...

    async def act(
        self,
        session_id: str,
        *,
        action: str,
        arguments: dict[str, Any] | None = None,
        observation_id: str | None = None,
        case_id: str | None = None,
    ) -> tuple[dict[str, Any], bytes | None]: ...

    async def verify(
        self,
        session_id: str,
        *,
        assertion: str,
        status: str,
        evidence: str,
        observation_id: str | None = None,
        case_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def close(
        self,
        session_id: str,
        *,
        status: str = "completed",
        summary: str | None = None,
    ) -> dict[str, Any]: ...


class AdbDeviceRuntimeAdapter:
    """Production adapter backed by ADB and the existing unified actuator."""

    @staticmethod
    def _adb_client() -> AdbClient:
        host = settings.ADB_HOST or os.environ.get("ADB_HOST") or "localhost"
        port_value = settings.ADB_PORT or os.environ.get("ADB_PORT") or 5037
        port = int(port_value)
        return AdbClient(host=host, port=port)

    async def open(
        self,
        *,
        session_id: str,
        goal: str,
        device_serial: str | None,
        agent_client: str,
        lock_timeout_seconds: float,
    ) -> DeviceRuntime:
        adb = self._adb_client()
        devices = await asyncio.to_thread(adb.device_list)
        ready_serials: list[str] = []
        for device in devices:
            try:
                state = await asyncio.to_thread(device.get_state)
            except Exception:
                logger.debug("Could not read ADB state for %s", device.serial, exc_info=True)
                continue
            if state == "device":
                ready_serials.append(device.serial)
        if device_serial:
            if device_serial not in ready_serials:
                available = ", ".join(ready_serials) or "none"
                raise RuntimeError(
                    f"Target Android device '{device_serial}' is not ready. Ready devices: {available}."
                )
            resolved_serial = device_serial
        elif len(ready_serials) == 1:
            resolved_serial = ready_serials[0]
        elif not ready_serials:
            raise RuntimeError("No authorized Android device is ready.")
        else:
            raise RuntimeError(
                "Several Android devices are ready; choose one explicitly with device_serial: "
                + ", ".join(ready_serials)
            )

        device_lock = DeviceExecutionLock(
            resolved_serial,
            description=f"{_agent_display_name(agent_client)} device session: {goal[:120]}",
            session_id=session_id,
            ingress=agent_client,
        )
        try:
            await asyncio.to_thread(device_lock.acquire, timeout=lock_timeout_seconds)
        except Exception:
            device_lock.release()
            raise

        try:
            ui_client = create_screen_client(resolved_serial)
            ctx = ArtemisContext(
                device=DeviceContext(
                    host_platform=platform.os_type.name,
                    mobile_platform=DevicePlatform.ANDROID,
                    device_id=resolved_serial,
                    device_width=1080,
                    device_height=2400,
                ),
                adb_client=adb,
                ui_adb_client=ui_client,
            )
            actuator = AdbActuator(ctx, UnifiedMobileController(ctx))
            return DeviceRuntime(
                ctx=ctx,
                actuator=actuator,
                device_serial=resolved_serial,
                lock=device_lock,
            )
        except Exception:
            device_lock.release()
            raise

    async def close(self, runtime: DeviceRuntime) -> None:
        try:
            controller = getattr(runtime.actuator, "controller", None)
            driver = getattr(controller, "driver", None)
            if driver is not None and callable(getattr(driver, "disconnect", None)):
                await driver.disconnect()
        except Exception as exc:
            logger.debug("Device driver disconnect failed: %s", exc, exc_info=True)
        finally:
            if runtime.lock is not None:
                runtime.lock.release()


@dataclass(slots=True)
class _ExecutionState:
    indexed_elements: list[dict[str, Any]] = field(default_factory=list)
    indexed_points: list[Any] = field(default_factory=list)
    latest_screenshot: str | None = None
    latest_ui_hierarchy: str | None = None


@dataclass(slots=True)
class _Session:
    session_id: str
    goal: str
    runtime: DeviceRuntime
    executor: McpActionExecutor
    agent_client: str
    workflow_id: str | None = None
    state: _ExecutionState = field(default_factory=_ExecutionState)
    last_observation_id: str | None = None
    last_screenshot_bytes: bytes | None = None
    last_activity: float = field(default_factory=time.monotonic)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle_timeout_seconds: float = 900.0
    watchdog: asyncio.Task[None] | None = None


class DeviceExecutionManager:
    """Owns external-agent device sessions and records their observable outcomes."""

    def __init__(
        self,
        runtime_adapter: DeviceRuntimeAdapter | None = None,
        data_engine_factory: Callable[[ArtemisContext], DataEngine] = DataEngine,
        agent_client: str | None = None,
    ):
        self._runtime_adapter = runtime_adapter or AdbDeviceRuntimeAdapter()
        self._data_engine_factory = data_engine_factory
        self._agent_client = _normalize_agent_client(
            agent_client or os.environ.get("ARTEMIS_AGENT_CLIENT")
        )
        self._test_workflows = TestWorkflowStore()
        self._sessions: dict[str, _Session] = {}
        self._opening_sessions: set[str] = set()
        self._registry_lock = asyncio.Lock()

    async def open(
        self,
        *,
        goal: str,
        device_serial: str | None = None,
        session_id: str | None = None,
        lock_timeout_seconds: float = 30.0,
        idle_timeout_seconds: float = 900.0,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        planned_workflow = self._test_workflows.planned(workflow_id) if workflow_id else None
        if planned_workflow and goal.strip() and goal.strip() != planned_workflow["goal"]:
            raise ValueError("goal must match the planned test workflow")
        clean_goal = planned_workflow["goal"] if planned_workflow else goal.strip()
        if not clean_goal:
            raise ValueError("goal must not be empty")
        if workflow_id and session_id and session_id != workflow_id:
            raise ValueError("session_id must match workflow_id")
        resolved_session_id = workflow_id or session_id or str(uuid.uuid4())
        async with self._registry_lock:
            if (
                resolved_session_id in self._sessions
                or resolved_session_id in self._opening_sessions
            ):
                raise ValueError(f"Device session '{resolved_session_id}' is already open.")
            self._opening_sessions.add(resolved_session_id)

        try:
            runtime = await self._runtime_adapter.open(
                session_id=resolved_session_id,
                goal=clean_goal,
                device_serial=device_serial,
                agent_client=self._agent_client,
                lock_timeout_seconds=max(0.1, lock_timeout_seconds),
            )
        except Exception:
            async with self._registry_lock:
                self._opening_sessions.discard(resolved_session_id)
            raise
        data_engine: DataEngine | None = None
        try:
            data_engine = self._data_engine_factory(runtime.ctx)
            runtime.ctx.data_engine = data_engine
            data_engine.start_session(
                goal=clean_goal,
                device_info={
                    **runtime.ctx.device.model_dump(),
                    "profile": self._agent_client,
                    "execution_mode": "external_agent",
                },
                session_id=resolved_session_id,
            )
            trace_store.init_trace(
                resolved_session_id,
                clean_goal,
                _agent_display_name(self._agent_client),
                device_serial=runtime.device_serial,
            )
            trace_store.update_trace_pid(resolved_session_id, os.getpid())
            executor = McpActionExecutor(
                runtime.ctx,
                actuator=runtime.actuator,
                agent_name=self._agent_client,
            )
            session = _Session(
                session_id=resolved_session_id,
                goal=clean_goal,
                runtime=runtime,
                executor=executor,
                agent_client=self._agent_client,
                workflow_id=workflow_id,
                idle_timeout_seconds=max(60.0, idle_timeout_seconds),
            )
            if workflow_id:
                self._test_workflows.bind(
                    workflow_id,
                    session_id=resolved_session_id,
                    device_serial=runtime.device_serial,
                )
            async with self._registry_lock:
                self._sessions[resolved_session_id] = session
                self._opening_sessions.discard(resolved_session_id)
            session.watchdog = asyncio.create_task(
                self._watch_idle(resolved_session_id),
                name=f"artemis-device-session-{resolved_session_id}",
            )
        except Exception:
            if data_engine is not None:
                data_engine.end_session(status="failed")
                await data_engine.shutdown()
            await self._runtime_adapter.close(runtime)
            async with self._registry_lock:
                self._sessions.pop(resolved_session_id, None)
                self._opening_sessions.discard(resolved_session_id)
            raise

        return {
            "session_id": resolved_session_id,
            "status": "running",
            "goal": clean_goal,
            "device_serial": runtime.device_serial,
            "agent_client": self._agent_client,
            "workflow_id": workflow_id,
            "profile": planned_workflow["profile"] if planned_workflow else None,
            "coordinate_space": "normalized 0-1000",
            "capabilities": sorted(executor.action_tool_names),
            "next": "Call mobile_observe before any screen-targeted action.",
        }

    async def status(self, session_id: str | None = None) -> dict[str, Any]:
        async with self._registry_lock:
            if session_id:
                session = self._sessions.get(session_id)
                if session is None:
                    return {
                        "session_id": session_id,
                        "status": "closed",
                        "persisted_status": trace_store.read_status(session_id),
                    }
                return self._session_status(session)
            return {"sessions": [self._session_status(item) for item in self._sessions.values()]}

    async def observe(
        self,
        session_id: str,
        *,
        settle_ms: int = 400,
    ) -> tuple[dict[str, Any], bytes | None]:
        session = await self._require_session(session_id)
        async with session.operation_lock:
            session.last_activity = time.monotonic()
            engine = session.runtime.ctx.data_engine
            step_id = engine.allocate_step_id()
            action_session = await get_action_session(
                session.runtime.ctx,
                actuator=session.runtime.actuator,
            )
            observed = await action_session.observe(settle_ms=max(0, settle_ms))
            image_bytes = self._read_image(observed.screenshot_path) if observed.ok else None
            if observed.hierarchy_ok:
                session.state.indexed_elements = observed.elements
                session.state.indexed_points = [item.get("center") for item in observed.elements]
                session.state.latest_ui_hierarchy = observed.elements_text
            if observed.width:
                session.runtime.ctx.device.device_width = observed.width
            if observed.height:
                session.runtime.ctx.device.device_height = observed.height
            session.state.latest_screenshot = observed.screenshot_path
            observation_id = str(uuid.uuid4()) if observed.ok else None
            session.last_observation_id = observation_id
            session.last_screenshot_bytes = image_bytes
            foreground_app = await self._foreground_app(session)
            guidance = scenario_guidance(session.goal, observed.elements_text)
            if session.workflow_id:
                self._test_workflows.record_scenario_rules(
                    session.workflow_id, guidance["rule_ids"]
                )

            engine.record_trace(
                type="tool",
                name="observe_screen",
                payload={"settle_ms": settle_ms, "observation_id": observation_id},
                step_id=step_id,
                status="success" if observed.ok else "failed",
            )
            engine.record_step(
                pre_screenshot_bytes=image_bytes,
                foreground_app=foreground_app,
                action_taken={
                    "action": "observe_screen",
                    "args": {"settle_ms": settle_ms},
                    "status": "success" if observed.ok else "failed",
                },
                summary=observed.message,
                last_execution_result=observed.model_dump(mode="json"),
                extra_metadata={
                    "executor": session.agent_client,
                    "event_kind": "observation",
                    "observation_id": observation_id,
                    "scenario_rule_ids": guidance["rule_ids"],
                },
            )
            return self._observation_payload(session, observed, observation_id), image_bytes

    async def act(
        self,
        session_id: str,
        *,
        action: str,
        arguments: dict[str, Any] | None = None,
        observation_id: str | None = None,
        case_id: str | None = None,
    ) -> tuple[dict[str, Any], bytes | None]:
        session = await self._require_session(session_id)
        clean_action = action.strip().split(":")[-1]
        args = dict(arguments or {})
        async with session.operation_lock:
            session.last_activity = time.monotonic()
            if session.workflow_id:
                self._test_workflows.validate_case(session.workflow_id, case_id)
            elif case_id:
                raise ValueError("case_id requires a session opened with workflow_id")
            if clean_action not in session.executor.action_tool_names:
                return self._action_error(
                    session,
                    clean_action,
                    f"Action '{clean_action}' is not available in this device session.",
                    code=ActionCode.UNSUPPORTED,
                )
            if clean_action in _SCREEN_TARGET_ACTIONS:
                if not session.last_observation_id:
                    return self._action_error(
                        session,
                        clean_action,
                        "Observe the screen before performing a screen-targeted action.",
                    )
                if observation_id != session.last_observation_id:
                    return self._action_error(
                        session,
                        clean_action,
                        "The observation_id is missing or stale; call mobile_observe again.",
                    )

            engine = session.runtime.ctx.data_engine
            step_id = engine.allocate_step_id()
            pre_image = session.last_screenshot_bytes
            result = await session.executor.execute(
                clean_action,
                args,
                tool_call_id=str(uuid.uuid4()),
                state=session.state,
                index_elements=list(session.state.indexed_elements),
            )
            image_bytes = result.screenshot_bytes
            if image_bytes is None and result.screenshot_path:
                image_bytes = self._read_image(result.screenshot_path)
            if image_bytes is not None:
                session.last_screenshot_bytes = image_bytes
            if result.ui_elements_text is not None:
                session.state.latest_ui_hierarchy = result.ui_elements_text
            if result.metadata.get("hierarchy_ok") is False:
                session.state.indexed_elements = []
                session.state.indexed_points = []
            new_observation_id = str(uuid.uuid4()) if image_bytes is not None else None
            if new_observation_id:
                session.last_observation_id = new_observation_id
            foreground_app = await self._foreground_app(session)
            guidance = scenario_guidance(session.goal, session.state.latest_ui_hierarchy or "")
            if session.workflow_id:
                self._test_workflows.record_scenario_rules(
                    session.workflow_id, guidance["rule_ids"]
                )
            action_record = {
                "action": clean_action,
                "args": args,
                "status": result.status,
                "case_id": case_id,
                **(result.metadata.get("target_semantics") or {}),
            }
            target_coordinates = result.metadata.get("target_coordinates")
            if target_coordinates:
                action_record["coordinates"] = target_coordinates
            execution_result = {
                "status": result.status,
                "message": result.text_summary,
                **result.metadata,
            }
            engine.record_step(
                pre_screenshot_bytes=pre_image,
                post_screenshot_bytes=image_bytes,
                foreground_app=foreground_app,
                action_taken=action_record,
                summary=result.text_summary,
                last_execution_result=execution_result,
                extra_metadata={
                    "executor": session.agent_client,
                    "event_kind": "action",
                    "observation_id": new_observation_id,
                    "case_id": case_id,
                    "scenario_rule_ids": guidance["rule_ids"],
                },
            )
            if session.workflow_id and case_id:
                self._test_workflows.record_action(
                    session.workflow_id,
                    case_id=case_id,
                    action=clean_action,
                    status=result.status,
                    step_id=str(step_id),
                )
            payload = {
                "session_id": session_id,
                "step_id": str(step_id),
                "action": clean_action,
                "case_id": case_id,
                "ok": result.status == "success",
                "code": result.metadata.get("code"),
                "message": result.text_summary,
                "observation": self._state_observation_payload(session, new_observation_id),
            }
            return payload, image_bytes

    async def verify(
        self,
        session_id: str,
        *,
        assertion: str,
        status: str,
        evidence: str,
        observation_id: str | None = None,
        case_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self._require_session(session_id)
        normalized_status = status.strip().lower()
        if normalized_status not in {"passed", "failed", "inconclusive"}:
            raise ValueError("status must be 'passed', 'failed', or 'inconclusive'")
        clean_assertion = assertion.strip()
        clean_evidence = evidence.strip()
        if not clean_assertion:
            raise ValueError("assertion must not be empty")
        if not clean_evidence:
            raise ValueError("evidence must not be empty")
        async with session.operation_lock:
            session.last_activity = time.monotonic()
            if observation_id and observation_id != session.last_observation_id:
                raise ValueError(
                    "observation_id is stale; observe the screen again before verifying"
                )
            if session.workflow_id:
                if not observation_id:
                    raise ValueError("observation_id is required for planned case verification")
                self._test_workflows.validate_verification(
                    session.workflow_id,
                    case_id=case_id,
                    checkpoint_id=checkpoint_id,
                    assertion=clean_assertion,
                )
            elif case_id or checkpoint_id:
                raise ValueError("case_id requires a session opened with workflow_id")
            engine = session.runtime.ctx.data_engine
            step_id = engine.allocate_step_id()
            payload = {
                "assertion": clean_assertion,
                "status": normalized_status,
                "evidence": clean_evidence,
                "observation_id": session.last_observation_id,
                "case_id": case_id,
                "checkpoint_id": checkpoint_id,
            }
            engine.record_trace(
                type="tool",
                name="verify",
                payload={"args": payload},
                step_id=step_id,
                status="success" if normalized_status == "passed" else "failed",
            )
            engine.record_step(
                pre_screenshot_bytes=session.last_screenshot_bytes,
                foreground_app=await self._foreground_app(session),
                action_taken={"action": "verify", "args": payload, "status": normalized_status},
                summary=f"Verification {normalized_status}: {clean_assertion}",
                last_execution_result=payload,
                extra_metadata={
                    "executor": session.agent_client,
                    "event_kind": "verification",
                    "observation_id": session.last_observation_id,
                    "case_id": case_id,
                    "checkpoint_id": checkpoint_id,
                },
            )
            case_result = (
                self._test_workflows.record_verification(
                    session.workflow_id,
                    case_id=case_id,
                    checkpoint_id=checkpoint_id,
                    status=normalized_status,
                    assertion=clean_assertion,
                    evidence=clean_evidence,
                    step_id=str(step_id),
                    observation_id=session.last_observation_id,
                )
                if session.workflow_id and case_id
                else {}
            )
            engine._publish(
                "verification_recorded",
                {"session_id": session_id, "step_id": str(step_id), **payload},
            )
            return {"session_id": session_id, "step_id": str(step_id), **payload, **case_result}

    async def close(
        self,
        session_id: str,
        *,
        status: str = "completed",
        summary: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = status.strip().lower()
        if normalized_status not in {"completed", "failed", "cancelled"}:
            raise ValueError("status must be 'completed', 'failed', or 'cancelled'")
        async with self._registry_lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            persisted = trace_store.read_status(session_id)
            return {
                "session_id": session_id,
                "status": persisted.get("status") if persisted else "closed",
                "already_closed": True,
            }

        current_task = asyncio.current_task()
        if session.watchdog is not None and session.watchdog is not current_task:
            session.watchdog.cancel()
        async with session.operation_lock:
            if session.workflow_id and normalized_status == "completed":
                counts = self._test_workflows.snapshot(session.workflow_id)["counts"]
                if counts["failed"] or counts["inconclusive"] or counts["not_run"]:
                    normalized_status = "failed"
            test_report = None
            try:
                try:
                    if summary:
                        session.runtime.ctx.data_engine.record_trace(
                            type="tool",
                            name=f"{session.agent_client}_summary",
                            payload={"summary": summary},
                            status="success",
                        )
                    session.runtime.ctx.data_engine.end_session(status=normalized_status)
                    trace_store.update_trace_status(
                        session_id,
                        normalized_status,
                        error=summary if normalized_status == "failed" else None,
                        result=summary if normalized_status == "completed" else None,
                        device_serial=session.runtime.device_serial,
                    )
                    if session.workflow_id:
                        test_report = self._test_workflows.finish(
                            session.workflow_id, session_status=normalized_status
                        )
                finally:
                    await session.runtime.ctx.__aexit__(None, None, None)
            finally:
                await self._runtime_adapter.close(session.runtime)
        return {
            "session_id": session_id,
            "status": normalized_status,
            "device_serial": session.runtime.device_serial,
            "summary": summary,
            "test_report": test_report,
        }

    async def close_all(self, status: str = "cancelled") -> None:
        async with self._registry_lock:
            session_ids = list(self._sessions)
        for session_id in session_ids:
            try:
                await self.close(session_id, status=status, summary="Device executor stopped.")
            except Exception:
                logger.exception("Failed to close device session %s", session_id)

    async def _require_session(self, session_id: str) -> _Session:
        async with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(
                f"Device session '{session_id}' is not open. Call mobile_session(operation='open') first."
            )
        return session

    async def _watch_idle(self, session_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                session = await self._require_session(session_id)
                if time.monotonic() - session.last_activity >= session.idle_timeout_seconds:
                    await self.close(
                        session_id,
                        status="cancelled",
                        summary=(
                            f"Closed after the external "
                            f"{_agent_display_name(session.agent_client)} session became idle."
                        ),
                    )
                    return
        except (asyncio.CancelledError, ValueError):
            return

    @staticmethod
    async def _foreground_app(session: _Session) -> str | None:
        try:
            controller = getattr(session.runtime.actuator, "controller", None)
            driver = getattr(controller, "driver", None)
            if driver is not None and callable(getattr(driver, "get_current_package", None)):
                return await driver.get_current_package()
        except Exception:
            logger.debug("Foreground package lookup failed", exc_info=True)
        return None

    @staticmethod
    def _read_image(path: str | None) -> bytes | None:
        if not path:
            return None
        try:
            return Path(path).read_bytes()
        except OSError:
            return None

    @staticmethod
    def _session_status(session: _Session) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "status": "running",
            "goal": session.goal,
            "device_serial": session.runtime.device_serial,
            "agent_client": session.agent_client,
            "workflow_id": session.workflow_id,
            "last_observation_id": session.last_observation_id,
            "idle_seconds": round(time.monotonic() - session.last_activity, 1),
        }

    @staticmethod
    def _observation_payload(session: _Session, observed: Any, observation_id: str | None) -> dict:
        guidance = scenario_guidance(session.goal, observed.elements_text)
        return {
            "session_id": session.session_id,
            "observation_id": observation_id,
            "ok": observed.ok,
            "code": observed.code.value,
            "message": observed.message,
            "image_name": observed.image_name,
            "width": observed.width,
            "height": observed.height,
            "hierarchy_ok": observed.hierarchy_ok,
            "elements_text": observed.elements_text,
            "element_count": len(observed.elements),
            "scenario_guidance": guidance,
        }

    @staticmethod
    def _state_observation_payload(session: _Session, observation_id: str | None) -> dict | None:
        if observation_id is None:
            return None
        guidance = scenario_guidance(session.goal, session.state.latest_ui_hierarchy or "")
        return {
            "observation_id": observation_id,
            "image_name": Path(session.state.latest_screenshot).stem
            if session.state.latest_screenshot
            else None,
            "elements_text": session.state.latest_ui_hierarchy,
            "element_count": len(session.state.indexed_elements),
            "width": session.runtime.ctx.device.device_width,
            "height": session.runtime.ctx.device.device_height,
            "scenario_guidance": guidance,
        }

    @staticmethod
    def _action_error(
        session: _Session,
        action: str,
        message: str,
        *,
        code: ActionCode = ActionCode.INVALID_ARGS,
    ) -> tuple[dict[str, Any], None]:
        return (
            {
                "session_id": session.session_id,
                "action": action,
                "ok": False,
                "code": code.value,
                "message": message,
                "observation": None,
            },
            None,
        )
