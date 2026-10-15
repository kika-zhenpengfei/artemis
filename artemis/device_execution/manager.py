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
import threading
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
from artemis.device_execution.workflow_start import WorkflowStartStore
from artemis.device_execution.environment import DeviceEnvironment
from artemis.device_execution.hybrid_plan import condition_met, matching_index
from artemis.mcp.action_executor import McpActionExecutor
from artemis.agents.scenario_rules import scenario_guidance
from artemis.mcp.action_session import get_action_session
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators.base import Actuator
from artemis.mcp.actuators.adb import AdbActuator
from artemis.platform import platform
from artemis.runtime import DeviceExecutionLock, trace_store
from artemis.runtime.device_selection import check_adb_endpoint, resolve_device_selection
from artemis.runtime.device_lock import DeviceBusyError
from artemis.runtime.device_selection import DeviceSelectionError
from artemis.utils.image_hash import dhash_hex, hamming_distance_hex
from artemis.utils.logger import get_logger

logger = get_logger(__name__)
_MEANINGFUL_SCREEN_CHANGE_BITS = 10


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
        expected_adb_endpoint_id: str | None = None,
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
        require_idle: bool = False,
        expected_adb_endpoint_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def status(self, session_id: str | None = None) -> dict[str, Any]: ...

    async def run_plan(
        self, session_id: str, *, case_id: str, resume_token: str | None = None
    ) -> tuple[dict[str, Any], bytes | None]: ...

    async def prepare_case(self, session_id: str, *, case_id: str) -> dict[str, Any]: ...

    async def observe(
        self,
        session_id: str,
        *,
        settle_ms: int = 400,
        wait_for_change: bool = False,
        timeout_ms: int = 15000,
        poll_interval_ms: int = 1000,
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
        expected_adb_endpoint_id: str | None = None,
    ) -> DeviceRuntime:
        endpoint = check_adb_endpoint(expected_adb_endpoint_id)
        device_serial, require_idle = await resolve_device_selection(
            device_serial, require_idle=lock_timeout_seconds == 0,
            expected_adb_endpoint_id=endpoint.identity,
        )
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

        check_adb_endpoint(endpoint.identity)
        device_lock = DeviceExecutionLock(
            resolved_serial,
            description=f"{_agent_display_name(agent_client)} device session: {goal[:120]}",
            session_id=session_id,
            ingress=agent_client,
            lock_scope=endpoint.identity,
        )
        cancelled = threading.Event()
        acquisition = asyncio.create_task(asyncio.to_thread(
            device_lock.acquire, blocking=not require_idle, timeout=lock_timeout_seconds,
            cancel_event=cancelled,
        ))
        try:
            await asyncio.shield(acquisition)
        except BaseException:
            cancelled.set()
            try:
                await acquisition
            except (DeviceBusyError, asyncio.CancelledError):
                pass
            finally:
                device_lock.release()
            raise

        try:
            check_adb_endpoint(endpoint.identity)
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
    hierarchy_ok: bool = False


@dataclass(slots=True)
class _Session:
    session_id: str
    goal: str
    runtime: DeviceRuntime
    executor: McpActionExecutor
    agent_client: str
    workflow_id: str | None = None
    environment_restore: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    hybrid_cases: frozenset[str] = frozenset()
    run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    auto_runner_task: asyncio.Task | None = None
    closing: bool = False
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
        environment: DeviceEnvironment | None = None,
    ):
        self._runtime_adapter = runtime_adapter or AdbDeviceRuntimeAdapter()
        self._data_engine_factory = data_engine_factory
        self._agent_client = _normalize_agent_client(
            agent_client or os.environ.get("ARTEMIS_AGENT_CLIENT")
        )
        self._test_workflows = TestWorkflowStore()
        self._workflow_starts = WorkflowStartStore()
        self._environment = environment or DeviceEnvironment()
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
        require_idle: bool = False,
        expected_adb_endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        planned_workflow = None
        clean_goal = goal.strip()
        if not workflow_id and not clean_goal:
            raise ValueError("goal must not be empty")
        if workflow_id and session_id and session_id != workflow_id:
            raise ValueError("session_id must match workflow_id")
        resolved_session_id = workflow_id or session_id or str(uuid.uuid4())
        start_claim = None
        async with self._registry_lock:
            if (
                resolved_session_id in self._sessions
                or resolved_session_id in self._opening_sessions
            ):
                raise ValueError(f"Device session '{resolved_session_id}' is already open.")
            if workflow_id:
                start_claim = self._workflow_starts.claim(
                    workflow_id, device_serial=device_serial,
                    adb_endpoint_id=expected_adb_endpoint_id,
                )
                if start_claim["status"] != "starting":
                    return self._awaiting_device(start_claim)
                device_serial = start_claim["device_serial"]
                expected_adb_endpoint_id = start_claim["adb_endpoint_id"]
                require_idle = True
            self._opening_sessions.add(resolved_session_id)

        try:
            if workflow_id:
                planned_workflow = self._test_workflows.planned(workflow_id)
                if clean_goal and clean_goal != planned_workflow["goal"]:
                    raise ValueError("goal must match the planned test workflow")
                clean_goal = planned_workflow["goal"]
            runtime = await self._runtime_adapter.open(
                session_id=resolved_session_id,
                goal=clean_goal,
                device_serial=device_serial,
                agent_client=self._agent_client,
                lock_timeout_seconds=0 if require_idle else max(0.1, lock_timeout_seconds),
                **({"expected_adb_endpoint_id": expected_adb_endpoint_id} if expected_adb_endpoint_id else {}),
            )
        except BaseException as exc:
            self._opening_sessions.discard(resolved_session_id)
            if workflow_id and start_claim:
                waiting = self._workflow_starts.failed(
                    workflow_id, start_claim["claim_token"], str(exc) or "Device startup was cancelled.",
                )
                if isinstance(exc, (DeviceBusyError, DeviceSelectionError, RuntimeError)):
                    logger.warning(f"Workflow {workflow_id} could not open its confirmed device: {exc}")
                    return self._awaiting_device(waiting)
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
                hybrid_cases=frozenset(planned_workflow.get("execution_plans", {}))
                if planned_workflow
                else frozenset(),
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
            if workflow_id and start_claim:
                self._workflow_starts.started(workflow_id, start_claim["claim_token"])
        except BaseException as exc:
            if "session" in locals() and session.watchdog is not None:
                session.watchdog.cancel()
            try:
                if data_engine is not None:
                    try:
                        data_engine.end_session(status="failed")
                    finally:
                        await data_engine.shutdown()
            finally:
                try:
                    await self._runtime_adapter.close(runtime)
                finally:
                    self._sessions.pop(resolved_session_id, None)
                    self._opening_sessions.discard(resolved_session_id)
                    if workflow_id and start_claim:
                        self._workflow_starts.failed(
                            workflow_id, start_claim["claim_token"],
                            str(exc) or "Device startup was cancelled.",
                        )
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
            "next": (
                "Call mobile_prepare_case for cases with an environment, then mobile_observe."
                if planned_workflow
                else "Call mobile_observe before any screen-targeted action."
            ),
        }

    @staticmethod
    def _awaiting_device(state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "next": "Wait for device confirmation in the WebUI, then call mobile_session open with this workflow_id again.",
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

    @staticmethod
    def _check_auto_runner(session: _Session) -> None:
        if (
            session.auto_runner_task is not None
            and session.auto_runner_task is not asyncio.current_task()
        ):
            raise ValueError(
                "fixed execution is running; wait for its result before manual actions"
            )

    @staticmethod
    def _condition_met(session: _Session, expected: dict[str, Any]) -> bool:
        return (
            bool(session.last_observation_id)
            and session.state.hierarchy_ok
            and condition_met(session.state.indexed_elements, expected)
        )

    @staticmethod
    def _publish_execution(session: _Session, case_id: str, state: dict[str, Any]) -> None:
        session.runtime.ctx.data_engine._publish(
            "test_execution_updated",
            {
                "session_id": session.session_id,
                "case_id": case_id,
                "cursor": state["cursor"],
                "handoff": state.get("handoff") is not None,
                "done": state.get("done", False),
            },
        )

    async def prepare_case(self, session_id: str, *, case_id: str) -> dict[str, Any]:
        session = await self._require_session(session_id)
        self._check_auto_runner(session)
        if not session.workflow_id:
            raise ValueError("prepare_case requires a planned test session")
        async with session.operation_lock:
            session.last_activity = time.monotonic()
            case = self._test_workflows.validate_case(session.workflow_id, case_id)
            spec = case.get("environment")
            restore_errors = await self._restore_environment(session)
            if restore_errors:
                result = {
                    "status": "blocked",
                    "reason": "Previous device settings could not be restored: " + "; ".join(restore_errors),
                    "evidence": [],
                }
            elif spec:
                result = await self._environment.prepare(session.runtime.device_serial, spec)
                originals = result.pop("restore", [])
                if originals:
                    session.environment_restore[case_id] = originals
            else:
                result = {"status": "ready", "reason": None, "evidence": ["No device preparation declared."]}
            self._test_workflows.record_preparation(session.workflow_id, case_id, result)
            session.last_observation_id = None
            session.last_screenshot_bytes = None
            session.state = _ExecutionState()
            engine = session.runtime.ctx.data_engine
            step_id = engine.allocate_step_id()
            engine.record_step(
                action_taken={"action": "prepare_case", "case_id": case_id, "status": result["status"]},
                summary=result.get("reason") or f"Case environment {result['status']}",
                last_execution_result=result,
                extra_metadata={
                    "executor": session.agent_client,
                    "event_kind": "preparation",
                    "case_id": case_id,
                },
            )
            return {"session_id": session_id, "case_id": case_id, "step_id": str(step_id), **result}

    async def _restore_environment(self, session: _Session) -> list[str]:
        errors = []
        for case_id, originals in list(session.environment_restore.items()):
            try:
                result = await self._environment.restore(session.runtime.device_serial, originals)
            except Exception as exc:
                result = {
                    "status": "failed",
                    "evidence": [],
                    "errors": [f"Setting restoration raised: {exc}"],
                }
            if session.workflow_id:
                self._test_workflows.record_restoration(session.workflow_id, case_id, result)
            if result["status"] == "restored":
                session.environment_restore.pop(case_id, None)
            else:
                errors.extend(result["errors"])
        return errors

    async def run_plan(
        self, session_id: str, *, case_id: str, resume_token: str | None = None
    ) -> tuple[dict[str, Any], bytes | None]:
        """Run fixed steps locally until a model decision or case result is needed."""
        session = await self._require_session(session_id)
        if not session.workflow_id:
            raise ValueError("run_plan requires a planned test session")
        async with session.run_lock:
            if session.closing:
                raise ValueError("device session is closing")
            session.auto_runner_task = asyncio.current_task()
            started = time.monotonic()
            try:
                return await self._run_plan_locked(session, case_id, resume_token, started)
            finally:
                session.auto_runner_task = None

    async def _run_plan_locked(
        self, session: _Session, case_id: str, resume_token: str | None, started: float
    ) -> tuple[dict[str, Any], bytes | None]:
        workflow_id = session.workflow_id
        assert workflow_id is not None
        self._test_workflows.validate_case(workflow_id, case_id)
        self._test_workflows.require_prepared(workflow_id, case_id)
        plan, state = self._test_workflows.execution(workflow_id, case_id)
        metrics = state.setdefault("metrics", {})
        if state.get("done"):
            return {
                "status": state.get("terminal_status", "completed"),
                "case_id": case_id,
                "already_completed": True,
            }, None
        if not session.last_observation_id:
            initial, _ = await self.observe(session.session_id, settle_ms=0)
            metrics["initial_capture_ms"] = initial.get("timings_ms", {}).get("device_capture", 0)
        handoff = state.get("handoff")
        if handoff:
            if resume_token != handoff["token"]:
                raise ValueError("resume_token must match the active model handoff")
            model_actions = (
                len(
                    [
                        action
                        for action in self._test_workflows.snapshot(workflow_id)["cases"]
                        if action["id"] == case_id
                    ][0]["actions"]
                )
                - handoff["start_actions"]
            )
            if model_actions > handoff["max_actions"] or (
                model_actions >= handoff["max_actions"]
                and not self._condition_met(session, handoff["resume_when"])
            ):
                observation_id = session.last_observation_id
                if observation_id:
                    case = self._test_workflows.validate_case(workflow_id, case_id)
                    await self.verify(
                        session.session_id,
                        assertion=case["expected_result"],
                        status="inconclusive",
                        case_id=case_id,
                        observation_id=observation_id,
                        evidence="Model intervention limit exceeded before the planned screen returned.",
                    )
                else:
                    case = self._test_workflows.validate_case(workflow_id, case_id)
                    self._test_workflows.record_verification(
                        workflow_id,
                        case_id=case_id,
                        checkpoint_id=None,
                        assertion=case["expected_result"],
                        status="inconclusive",
                        evidence="Model intervention limit exceeded; no valid device observation was available.",
                        step_id="none",
                        observation_id=None,
                    )
                state["done"] = True
                state["terminal_status"] = "inconclusive"
                state["handoff"] = None
                self._test_workflows.save_execution_state(workflow_id, case_id, state)
                self._publish_execution(session, case_id, state)
                return {
                    "status": "inconclusive",
                    "case_id": case_id,
                    "reason": "model action limit exceeded",
                }, session.last_screenshot_bytes
            if not self._condition_met(session, handoff["resume_when"]):
                return self._handoff_payload(session, case_id, state, started)
            if handoff.get("checkpoint_id"):
                report_case = next(
                    case
                    for case in self._test_workflows.snapshot(workflow_id)["cases"]
                    if case["id"] == case_id
                )
                verified = any(
                    item["checkpoint_id"] == handoff["checkpoint_id"] and item["status"] == "passed"
                    for item in report_case["verifications"]
                )
                if not verified and handoff["kind"] == "model":
                    return self._handoff_payload(
                        session,
                        case_id,
                        state,
                        started,
                        reason="Verify the Pro checkpoint before resuming.",
                    )
                if handoff["kind"] == "auto" and not verified:
                    await self._verify_checkpoint(
                        session, case_id, handoff["checkpoint_id"], handoff["resume_when"]
                    )
            metrics["handoff_wait_ms"] = metrics.get("handoff_wait_ms", 0) + round(
                (time.time() - handoff["started_at"]) * 1000
            )
            if handoff["advance"]:
                state["cursor"] += 1
            state["handoff"] = None
            self._test_workflows.save_execution_state(workflow_id, case_id, state)
            self._publish_execution(session, case_id, state)
        elif resume_token:
            raise ValueError("there is no active model handoff")

        steps_executed = 0
        while state["cursor"] < len(plan["steps"]):
            step = plan["steps"][state["cursor"]]
            if step["kind"] == "auto" and (
                steps_executed >= 5
                or (steps_executed > 0 and time.monotonic() - started >= 20)
            ):
                return {
                    "status": "continued",
                    "case_id": case_id,
                    "cursor": state["cursor"],
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "metrics": metrics,
                }, None
            if step["kind"] == "model":
                marker = step["if_visible"]
                if (
                    marker
                    and self._condition_met(session, {"state": "absent", "locator": marker})
                    and self._condition_met(session, step["resume_when"])
                ):
                    state["cursor"] += 1
                    self._test_workflows.save_execution_state(workflow_id, case_id, state)
                    self._publish_execution(session, case_id, state)
                    continue
                return self._pause_plan(
                    session,
                    case_id,
                    state,
                    started,
                    kind="model",
                    reason=step["goal"],
                    resume_when=step["resume_when"],
                    max_actions=step["max_actions"],
                    checkpoint_id=step["checkpoint_id"],
                    advance=True,
                )

            arguments = dict(step["arguments"])
            if step["locator"]:
                target = (
                    matching_index(session.state.indexed_elements, step["locator"])
                    if session.state.hierarchy_ok
                    else None
                )
                if target is None:
                    return self._pause_plan(
                        session,
                        case_id,
                        state,
                        started,
                        kind="auto",
                        reason=f"Target missing or ambiguous: {step['locator']}",
                        resume_when={"state": "visible", "locator": step["locator"]},
                        max_actions=4,
                        checkpoint_id=None,
                        advance=False,
                    )
                arguments["target"] = target
            observation_id = session.last_observation_id
            action_result, _ = await self.act(
                session.session_id,
                action=step["action"],
                arguments=arguments,
                observation_id=observation_id,
                case_id=case_id,
            )
            timings = action_result.get("timings_ms", {})
            metrics["fixed_actions"] = metrics.get("fixed_actions", 0) + 1
            metrics["device_action_ms"] = metrics.get("device_action_ms", 0) + timings.get(
                "device_action", 0
            )
            metrics["post_action_observe_ms"] = metrics.get(
                "post_action_observe_ms", 0
            ) + timings.get("post_action_observe", 0)
            if not action_result["ok"]:
                return self._pause_plan(
                    session,
                    case_id,
                    state,
                    started,
                    kind="auto",
                    reason=action_result["message"],
                    resume_when=step["expect"],
                    max_actions=4,
                    checkpoint_id=step["checkpoint_id"],
                    advance=True,
                )
            deadline = time.monotonic() + step["timeout_ms"] / 1000
            while not self._condition_met(session, step["expect"]):
                if time.monotonic() >= deadline:
                    return self._pause_plan(
                        session,
                        case_id,
                        state,
                        started,
                        kind="auto",
                        reason=f"Expected screen did not appear: {step['expect']}",
                        resume_when=step["expect"],
                        max_actions=4,
                        checkpoint_id=step["checkpoint_id"],
                        advance=True,
                    )
                await asyncio.sleep(min(0.3, max(0, deadline - time.monotonic())))
                observed, _ = await self.observe(session.session_id, settle_ms=0)
                metrics["poll_capture_ms"] = metrics.get("poll_capture_ms", 0) + observed.get(
                    "timings_ms", {}
                ).get("device_capture", 0)
            if step["checkpoint_id"]:
                await self._verify_checkpoint(
                    session, case_id, step["checkpoint_id"], step["expect"]
                )
            state["cursor"] += 1
            steps_executed += 1
            self._test_workflows.save_execution_state(workflow_id, case_id, state)
            self._publish_execution(session, case_id, state)

        final = plan["final_assertion"]
        if not self._condition_met(session, final):
            return self._pause_plan(
                session,
                case_id,
                state,
                started,
                kind="final",
                reason=f"Final assertion is not observable: {final}",
                resume_when=final,
                max_actions=4,
                checkpoint_id=None,
                advance=False,
            )
        case = self._test_workflows.validate_case(workflow_id, case_id)
        final_result = await self.verify(
            session.session_id,
            case_id=case_id,
            assertion=case["expected_result"],
            status="passed",
            observation_id=session.last_observation_id,
            evidence=f"Device hierarchy satisfies {final}; observation {session.last_observation_id}.",
        )
        state["done"] = True
        state["terminal_status"] = (
            "completed"
            if final_result.get("case_status") == "passed"
            else final_result.get("case_status")
        )
        self._test_workflows.save_execution_state(workflow_id, case_id, state)
        self._publish_execution(session, case_id, state)
        return {
            "status": state["terminal_status"],
            "case_status": final_result.get("case_status"),
            "case_id": case_id,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "metrics": metrics,
            "observation": self._state_observation_payload(session, session.last_observation_id),
        }, None

    async def _verify_checkpoint(
        self, session: _Session, case_id: str, checkpoint_id: str, expected: dict[str, Any]
    ) -> None:
        case = self._test_workflows.validate_case(session.workflow_id, case_id)
        checkpoint = next(item for item in case["checkpoints"] if item["id"] == checkpoint_id)
        await self.verify(
            session.session_id,
            case_id=case_id,
            checkpoint_id=checkpoint_id,
            assertion=checkpoint["assertion"],
            status="passed",
            observation_id=session.last_observation_id,
            evidence=f"Device hierarchy satisfies {expected}; observation {session.last_observation_id}.",
        )

    def _pause_plan(
        self,
        session: _Session,
        case_id: str,
        state: dict[str, Any],
        started: float,
        *,
        kind: str,
        reason: str,
        resume_when: dict[str, Any],
        max_actions: int,
        checkpoint_id: str | None,
        advance: bool,
    ) -> tuple[dict[str, Any], bytes | None]:
        report_case = next(
            case
            for case in self._test_workflows.snapshot(session.workflow_id)["cases"]
            if case["id"] == case_id
        )
        state["handoff"] = {
            "token": str(uuid.uuid4()),
            "kind": kind,
            "reason": reason,
            "resume_when": resume_when,
            "max_actions": max_actions,
            "start_actions": len(report_case["actions"]),
            "checkpoint_id": checkpoint_id,
            "advance": advance,
            "started_at": time.time(),
        }
        state.setdefault("metrics", {})["handoffs"] = state["metrics"].get("handoffs", 0) + 1
        self._test_workflows.save_execution_state(session.workflow_id, case_id, state)
        self._publish_execution(session, case_id, state)
        return self._handoff_payload(session, case_id, state, started)

    def _handoff_payload(
        self,
        session: _Session,
        case_id: str,
        state: dict[str, Any],
        started: float,
        *,
        reason: str | None = None,
    ) -> tuple[dict[str, Any], bytes | None]:
        handoff = state["handoff"]
        return {
            "status": "handoff",
            "case_id": case_id,
            "cursor": state["cursor"],
            "resume_token": handoff["token"],
            "reason": reason or handoff["reason"],
            "resume_when": handoff["resume_when"],
            "max_actions": handoff["max_actions"],
            "checkpoint_id": handoff["checkpoint_id"],
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "metrics": state.get("metrics", {}),
            "observation": self._state_observation_payload(session, session.last_observation_id),
        }, session.last_screenshot_bytes

    async def observe(
        self,
        session_id: str,
        *,
        settle_ms: int = 400,
        wait_for_change: bool = False,
        timeout_ms: int = 15000,
        poll_interval_ms: int = 1000,
    ) -> tuple[dict[str, Any], bytes | None]:
        session = await self._require_session(session_id)
        self._check_auto_runner(session)
        if wait_for_change:
            if not session.last_observation_id or not session.last_screenshot_bytes:
                raise ValueError("wait_for_change requires a prior observation")
            if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30000:
                raise ValueError("timeout_ms must be between 1 and 30000")
            if type(poll_interval_ms) is not int or not 100 <= poll_interval_ms <= 5000:
                raise ValueError("poll_interval_ms must be between 100 and 5000")
        async with session.operation_lock:
            session.last_activity = time.monotonic()
            engine = session.runtime.ctx.data_engine
            step_id = engine.allocate_step_id()
            action_session = await get_action_session(
                session.runtime.ctx,
                actuator=session.runtime.actuator,
            )
            wait_started = time.monotonic()
            deadline = wait_started + timeout_ms / 1000
            baseline_text = session.state.latest_ui_hierarchy
            baseline_hash = dhash_hex(session.last_screenshot_bytes) if wait_for_change else None
            poll_count = 0
            capture_ms = 0
            changed = False
            while True:
                capture_started = time.monotonic()
                observed = await action_session.observe(
                    settle_ms=max(0, settle_ms) if poll_count == 0 else 0
                )
                capture_ms += round((time.monotonic() - capture_started) * 1000)
                poll_count += 1
                image_bytes = self._read_image(observed.screenshot_path) if observed.ok else None
                visual_distance = hamming_distance_hex(baseline_hash, dhash_hex(image_bytes))
                changed = bool(
                    observed.ok
                    and (
                        (observed.hierarchy_ok and observed.elements_text != baseline_text)
                        or (visual_distance is not None and visual_distance >= _MEANINGFUL_SCREEN_CHANGE_BITS)
                    )
                )
                if not wait_for_change or changed or not observed.ok or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(poll_interval_ms / 1000, deadline - time.monotonic()))
            persist_started = time.monotonic()
            session.state.hierarchy_ok = bool(observed.hierarchy_ok)
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
                payload={
                    "settle_ms": settle_ms,
                    "observation_id": observation_id,
                    "wait_for_change": wait_for_change,
                    "poll_count": poll_count,
                },
                step_id=step_id,
                status="success" if observed.ok else "failed",
            )
            engine.record_step(
                pre_screenshot_bytes=image_bytes,
                foreground_app=foreground_app,
                action_taken={
                    "action": "observe_screen",
                    "args": {
                        "settle_ms": settle_ms,
                        "wait_for_change": wait_for_change,
                        **({"timeout_ms": timeout_ms, "poll_interval_ms": poll_interval_ms}
                           if wait_for_change else {}),
                    },
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
            payload = self._observation_payload(session, observed, observation_id)
            payload["timings_ms"] = {
                "device_capture": capture_ms,
                "persistence": round((time.monotonic() - persist_started) * 1000),
            }
            if wait_for_change:
                payload["wait_status"] = (
                    "error" if not observed.ok else "changed" if changed else "timeout"
                )
                payload["poll_count"] = poll_count
                payload["timings_ms"]["wait_elapsed"] = round(
                    (persist_started - wait_started) * 1000
                )
            return payload, image_bytes

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
        self._check_auto_runner(session)
        clean_action = action.strip().split(":")[-1]
        args = dict(arguments or {})
        async with session.operation_lock:
            session.last_activity = time.monotonic()
            if session.workflow_id:
                self._test_workflows.validate_case(session.workflow_id, case_id)
                self._test_workflows.require_prepared(session.workflow_id, case_id)
                if case_id in session.hybrid_cases and session.auto_runner_task is None:
                    workflow = self._test_workflows.snapshot(session.workflow_id)
                    handoff = workflow["execution_state"][case_id].get("handoff")
                    case = next(item for item in workflow["cases"] if item["id"] == case_id)
                    if (
                        handoff
                        and len(case["actions"]) - handoff["start_actions"]
                        >= handoff["max_actions"]
                    ):
                        return self._action_error(
                            session,
                            clean_action,
                            "Model action limit reached; resume the plan or record an inconclusive result.",
                            code=ActionCode.NO_PROGRESS,
                        )
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
            executor_started = time.monotonic()
            result = await session.executor.execute(
                clean_action,
                args,
                tool_call_id=str(uuid.uuid4()),
                state=session.state,
                index_elements=list(session.state.indexed_elements),
            )
            executor_ms = round((time.monotonic() - executor_started) * 1000)
            persist_started = time.monotonic()
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
            session.state.hierarchy_ok = result.metadata.get("hierarchy_ok") is True
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
                "timings_ms": {
                    **result.metadata.get("timings_ms", {}),
                    "executor_total": executor_ms,
                    "persistence": round((time.monotonic() - persist_started) * 1000),
                },
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
        self._check_auto_runner(session)
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
                if normalized_status == "passed":
                    self._test_workflows.require_prepared(session.workflow_id, case_id)
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
            running = self._sessions.get(session_id)
            if running:
                running.closing = True
        if (
            running
            and running.auto_runner_task
            and running.auto_runner_task is not asyncio.current_task()
        ):
            running.auto_runner_task.cancel()
            try:
                await running.auto_runner_task
            except asyncio.CancelledError:
                pass
        if running and running.auto_runner_task is not asyncio.current_task():
            async with running.run_lock:
                pass
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
            restoration_errors = await self._restore_environment(session)
            if restoration_errors:
                normalized_status = "failed"
            if session.workflow_id and normalized_status == "completed":
                counts = self._test_workflows.snapshot(session.workflow_id)["counts"]
                if counts["failed"] or counts["inconclusive"] or counts["blocked"] or counts["not_run"]:
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
