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

"""MCP tools for device execution driven by an external development agent."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from mcp.types import CallToolResult, ImageContent, TextContent

from artemis.device_execution import DeviceExecution, DeviceExecutionManager
from artemis.runtime.daemon_client import daemon_log_path, is_artemis_daemon, spawn_daemon
from artemis.runtime.server_lifecycle import is_port_in_use
from artemis.utils.logger import get_logger
from mcp_server.base import mcp

_manager: DeviceExecution = DeviceExecutionManager()
logger = get_logger(__name__)

_WEB_UI_HOST = "127.0.0.1"
_WEB_UI_DEFAULT_PORT = 8000
_WEB_UI_START_TIMEOUT_SECONDS = 8.0
_WEB_UI_POLL_INTERVAL_SECONDS = 0.2
_web_ui_start_lock = asyncio.Lock()


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _executor_mode_enabled() -> bool:
    execution_mode = os.getenv("ARTEMIS_EXECUTION_MODE", "").strip().lower()
    if execution_mode in {"executor", "autonomous"}:
        return execution_mode == "executor"
    return _env_flag("ARTEMIS_EXECUTOR_ONLY", default=False)


def _web_ui_port() -> int:
    value = os.getenv("ARTEMIS_UI_PORT") or os.getenv("ARTEMIS_DAEMON_PORT")
    if value is None:
        return _WEB_UI_DEFAULT_PORT
    try:
        port = int(value)
    except ValueError:
        logger.warning("Invalid ARTEMIS_UI_PORT %r; using %s.", value, _WEB_UI_DEFAULT_PORT)
        return _WEB_UI_DEFAULT_PORT
    if 1 <= port <= 65535:
        return port
    logger.warning("ARTEMIS_UI_PORT must be between 1 and 65535; using %s.", _WEB_UI_DEFAULT_PORT)
    return _WEB_UI_DEFAULT_PORT


async def _ensure_web_ui_started(
    *,
    timeout_seconds: float = _WEB_UI_START_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Best-effort lazy WebUI startup for external-agent device sessions."""
    executor_mode = _executor_mode_enabled()
    if not _env_flag("ARTEMIS_AUTO_START_UI", default=executor_mode):
        return {
            "enabled": False,
            "status": "disabled",
            "message": "Automatic ARTEMIS WebUI startup is disabled.",
        }

    port = _web_ui_port()
    url = f"http://localhost:{port}"
    open_browser = _env_flag("ARTEMIS_AUTO_OPEN_BROWSER", default=False)

    async with _web_ui_start_lock:
        if await asyncio.to_thread(is_artemis_daemon, _WEB_UI_HOST, port):
            if open_browser:
                await asyncio.to_thread(webbrowser.open, url)
            return {
                "enabled": True,
                "status": "running",
                "url": url,
                "port": port,
                "message": "ARTEMIS WebUI is already running.",
            }

        if await asyncio.to_thread(is_port_in_use, port, _WEB_UI_HOST):
            message = (
                f"Port {port} is occupied by a non-ARTEMIS process; "
                "device execution will continue without auto-starting the WebUI."
            )
            logger.warning(message)
            return {
                "enabled": True,
                "status": "port_conflict",
                "url": url,
                "port": port,
                "message": message,
            }

        process = await asyncio.to_thread(spawn_daemon, _WEB_UI_HOST, port)
        if process is None:
            message = "ARTEMIS WebUI could not be started; device execution will continue."
            logger.warning(message)
            return {
                "enabled": True,
                "status": "failed",
                "url": url,
                "port": port,
                "message": message,
                "log_path": str(daemon_log_path(port)),
            }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_seconds)
        while loop.time() < deadline:
            if await asyncio.to_thread(is_artemis_daemon, _WEB_UI_HOST, port):
                if open_browser:
                    await asyncio.to_thread(webbrowser.open, url)
                return {
                    "enabled": True,
                    "status": "started",
                    "url": url,
                    "port": port,
                    "pid": process.pid,
                    "message": "ARTEMIS WebUI started automatically.",
                }
            if process.poll() is not None:
                message = "ARTEMIS WebUI process exited before it became ready."
                logger.warning("%s See %s.", message, daemon_log_path(port))
                return {
                    "enabled": True,
                    "status": "failed",
                    "url": url,
                    "port": port,
                    "message": message,
                    "log_path": str(daemon_log_path(port)),
                }
            await asyncio.sleep(_WEB_UI_POLL_INTERVAL_SECONDS)

        return {
            "enabled": True,
            "status": "starting",
            "url": url,
            "port": port,
            "pid": process.pid,
            "message": "ARTEMIS WebUI is starting in the background; device execution will continue.",
            "log_path": str(daemon_log_path(port)),
        }


async def close_device_execution_sessions() -> None:
    """Finalize leases when the MCP server shuts down."""
    manager = _manager
    close_all = getattr(manager, "close_all", None)
    if callable(close_all):
        await close_all(status="cancelled")


def _result(payload: dict[str, Any], image_bytes: bytes | None = None) -> CallToolResult:
    message = str(payload.get("message") or payload.get("status") or "OK")
    reason = payload.get("reason")
    if reason:
        message += f"\nReason: {reason}"
    if payload.get("wait_status"):
        message += f"\nScreen wait: {payload['wait_status']} after {payload['poll_count']} captures."
        if payload["wait_status"] == "timeout":
            message += " Change strategy or record the blocked precondition."
    observation = payload.get("observation")
    guidance = payload.get("scenario_guidance")
    if not guidance and isinstance(observation, dict):
        guidance = observation.get("scenario_guidance")
    if isinstance(guidance, dict) and guidance.get("prompt"):
        message += f"\n{guidance['prompt']}"
    content: list[Any] = [TextContent(type="text", text=message)]
    if image_bytes:
        mime_type = "image/png" if image_bytes.startswith(b"\x89PNG") else "image/jpeg"
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(image_bytes).decode("utf-8"),
                mimeType=mime_type,
            )
        )
    return CallToolResult(content=content, structuredContent=payload, isError=False)


@mcp.tool()
async def mobile_session(
    operation: str,
    goal: str | None = None,
    session_id: str | None = None,
    device_serial: str | None = None,
    status: str = "completed",
    summary: str | None = None,
    lock_timeout_seconds: float = 30.0,
    idle_timeout_seconds: float = 900.0,
    workflow_id: str | None = None,
    require_idle: bool = False,
    wait_timeout_seconds: float = 25.0,
) -> dict[str, Any]:
    """Open, inspect, or close an external-agent-driven device session.

    Operations:
      - `open` with `workflow_id`: waits for the user to select an idle device
        and confirm this workflow in WebUI Test Plans or Task Queue. It leases
        no device before confirmation. Each call waits up to
        `wait_timeout_seconds` (default 25, maximum 30). On `awaiting_device`,
        keep the Codex task active and call open again with the same workflow;
        only `status='running'` permits device actions. The per-workflow choice
        is authoritative; an explicit `device_serial` must match it.
      - `open` without a workflow requires `goal`. An explicit `device_serial`
        takes precedence over the saved global WebUI choice. An unavailable
        saved choice is rejected. With several ready devices and no selection,
        ask the user to choose. `require_idle` rejects a busy explicit target.
      - `status`: pass `session_id` for one session or omit it to list sessions.
      - `close`: pass `session_id`, terminal `status` (`completed`, `failed`, or
        `cancelled`) and an optional summary. Always close the session when done
        so the device lease is released and WebUI history is finalized.

    The session owns the device lock for its full lifetime. An idle watchdog
    cancels abandoned sessions after `idle_timeout_seconds` (minimum 60s).
    For a source-linked test run, call `mobile_test_context` and
    `mobile_test_cases` first, then open with their `workflow_id`.
    """
    normalized = operation.strip().lower()
    if normalized == "open":
        web_ui = await _ensure_web_ui_started()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(30.0, max(0.0, wait_timeout_seconds))
        while True:
            result = await _manager.open(
                goal=goal or "",
                device_serial=device_serial,
                session_id=session_id,
                lock_timeout_seconds=lock_timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
                workflow_id=workflow_id,
                require_idle=require_idle,
            )
            if not workflow_id or result.get("status") != "awaiting_device":
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(1.0, remaining))
        if result.get("status") == "awaiting_device":
            result["next"] = (
                "Wait for the user to select an idle device and confirm this workflow "
                "in WebUI Test Plans or Task Queue. Keep this Codex task active and "
                "call mobile_session(operation='open', workflow_id=...) again. "
                "Begin device actions only after status='running'."
            )
        result["web_ui"] = web_ui
        return result
    if normalized == "status":
        return await _manager.status(session_id)
    if normalized == "close":
        if not session_id:
            raise ValueError("session_id is required for operation='close'")
        return await _manager.close(session_id, status=status, summary=summary)
    raise ValueError("operation must be 'open', 'status', or 'close'")


@mcp.tool()
async def mobile_prepare_case(session_id: str, case_id: str) -> dict[str, Any]:
    """Prepare a case's declared black-box device state before acting.

    Runs under the leased device lock. A case may declare an Android package,
    data reset or verified APK reinstall, temporary system settings,
    system-setting checks, and a device-time window.
    Returns `ready` or `blocked` with evidence. Preparation invalidates the
    previous observation, so call `mobile_observe` afterwards. A blocked case
    cannot run a fixed plan until its precondition is resolved and preparation
    is repeated.
    """
    return await _manager.prepare_case(session_id, case_id=case_id)


def _webui_handoff_request(workflow_id: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    suffix = "run_status" if operation == "status" else "control"
    encoded_id = urllib.parse.quote(workflow_id, safe="")
    url = f"http://{_WEB_UI_HOST}:{_web_ui_port()}/api/test_workflows/{encoded_id}/{suffix}"
    data = None if operation == "status" else json.dumps({"operation": operation, **arguments}).encode()
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Artemis-MCP-Handoff"},
        method="GET" if operation == "status" else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=65) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail", str(exc))
        except (OSError, ValueError):
            detail = str(exc)
        raise ValueError(f"WebUI handoff failed: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the ARTEMIS WebUI handoff: {exc.reason}") from exc


@mcp.tool()
async def mobile_webui_handoff(
    workflow_id: str,
    operation: str,
    action: str | None = None,
    arguments: dict[str, Any] | None = None,
    observation_id: str | None = None,
    case_id: str | None = None,
    checkpoint_id: str | None = None,
    assertion: str | None = None,
    status: str | None = None,
    evidence: str | None = None,
    resume_token: str | None = None,
    settle_ms: int = 0,
    wait_for_change: bool = False,
    timeout_ms: int = 15000,
    poll_interval_ms: int = 1000,
    include_image: bool = True,
) -> CallToolResult:
    """Let Codex resolve a paused WebUI test on its existing device lease.

    Use `status` to read the handoff and active `case_id`, then `observe`,
    `act` with the latest observation ID, and `verify` for a Pro checkpoint.
    Call `resume` with the handoff's `resume_token` to continue fixed steps;
    poll `status` until the next handoff or final report. `close` releases the
    WebUI-owned device lease if the workflow cannot continue. No separate
    ARTEMIS LLM key is needed because Codex makes the dynamic decisions.
    """
    normalized = operation.strip().lower()
    if normalized not in {"status", "observe", "act", "verify", "resume", "close"}:
        raise ValueError("operation must be status, observe, act, verify, resume, or close")
    if not await asyncio.to_thread(is_artemis_daemon, _WEB_UI_HOST, _web_ui_port()):
        raise RuntimeError("ARTEMIS WebUI is not running on the configured local port")
    payload = await asyncio.to_thread(
        _webui_handoff_request,
        workflow_id,
        normalized,
        {
            "action": action,
            "arguments": arguments,
            "observation_id": observation_id,
            "case_id": case_id,
            "checkpoint_id": checkpoint_id,
            "assertion": assertion,
            "status": status,
            "evidence": evidence,
            "resume_token": resume_token,
            "settle_ms": settle_ms,
            "wait_for_change": wait_for_change,
            "timeout_ms": timeout_ms,
            "poll_interval_ms": poll_interval_ms,
        },
    )
    encoded_image = payload.pop("image_base64", None)
    image_bytes = base64.b64decode(encoded_image) if encoded_image and include_image else None
    return _result(payload, image_bytes)


@mcp.tool()
async def mobile_observe(
    session_id: str,
    settle_ms: int = 400,
    include_image: bool = True,
    wait_for_change: bool = False,
    timeout_ms: int = 15000,
    poll_interval_ms: int = 1000,
) -> CallToolResult:
    """Observe the device and persist an observation step for the WebUI.

    Returns a screenshot, indexed `elements_text`, and an `observation_id`.
    Element indices are dynamic locators for the next `mobile_act` call. Always
    observe before the first screen-targeted action and after any uncertainty.
    `mobile_act` already returns the post-action observation; do not immediately
    observe again. For a known text condition, use `mobile_act(wait_for_text)`.
    For an image-only dynamic screen, `wait_for_change=True` polls within one
    call until a meaningful visual/UI change or `timeout_ms` (max 30000).
    It requires a prior observation and records only the final poll as a step.
    On timeout, choose a different strategy or record the blocker instead of
    repeating the same wait. `include_image=False` only omits the returned image.
    """
    payload, image_bytes = await _manager.observe(
        session_id,
        settle_ms=settle_ms,
        wait_for_change=wait_for_change,
        timeout_ms=timeout_ms,
        poll_interval_ms=poll_interval_ms,
    )
    return _result(payload, image_bytes if include_image else None)


@mcp.tool()
async def mobile_act(
    session_id: str,
    action: str,
    arguments: dict[str, Any] | None = None,
    observation_id: str | None = None,
    include_image: bool = True,
    case_id: str | None = None,
) -> CallToolResult:
    """Execute one device action, observe its result, and persist a WebUI step.

    `action` supports the actuator capabilities returned by `mobile_session`,
    including `click`, `click_sequence`, `long_press`, `input_text`, `swipe`,
    `press_key`, `manage_app`, `wait_for_delay`, `wait_for_text`, `open_link`,
    `erase_one_char`, and `focus_and_clear_text`.

    `arguments` uses the existing ARTEMIS action dialect. Prefer the element
    index from the latest `mobile_observe` response, for example
    `{"target": 3}`. Coordinate fallback uses normalized 0-1000 coordinates
    plus `target_description`, for example
    `{"target": [500, 700], "target_description": "Debug button"}`.
    Screen-targeted actions must pass the latest `observation_id`; stale or
    missing IDs are rejected. Successful actions return a fresh observation
    and screenshot, whose ID must be used for the next screen-targeted action.
    Planned test sessions must also pass the active `case_id` for every action.
    """
    payload, image_bytes = await _manager.act(
        session_id,
        action=action,
        arguments=arguments,
        observation_id=observation_id,
        case_id=case_id,
    )
    return _result(payload, image_bytes if include_image else None)


@mcp.tool()
async def mobile_run_plan(
    session_id: str,
    case_id: str,
    resume_token: str | None = None,
    include_image: bool = True,
) -> CallToolResult:
    """Run a compiled case's fixed steps until completion or a model handoff.

    A handoff includes its current screenshot/UI, reason, expected return state,
    and `resume_token`. Resolve the dynamic screen with `mobile_act`, then call
    this tool again with that token. Close the device session after all cases.
    Long fixed paths return `status="continued"` after at most five actions or
    about 20 seconds; call again without a token to execute the next segment.
    """
    payload, image_bytes = await _manager.run_plan(
        session_id, case_id=case_id, resume_token=resume_token
    )
    return _result(payload, image_bytes if include_image else None)


@mcp.tool()
async def mobile_verify(
    session_id: str,
    assertion: str,
    status: str,
    evidence: str,
    observation_id: str | None = None,
    case_id: str | None = None,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """Persist an external-agent verification result into the ARTEMIS WebUI timeline.

    Use only after inspecting the returned screenshot/UI elements. `status` is
    `passed`, `failed`, or `inconclusive`; `evidence` must state the observable
    device evidence rather than inferred success. Pass the latest
    `observation_id` when the verdict is based on the current screen.
    For a planned run, pass `case_id`; Pro cases also use `checkpoint_id` for
    each declared checkpoint, then omit it for the case's final expected result.
    `assertion` must match that planned checkpoint or final expected result.
    """
    return await _manager.verify(
        session_id,
        assertion=assertion,
        status=status,
        evidence=evidence,
        observation_id=observation_id,
        case_id=case_id,
        checkpoint_id=checkpoint_id,
    )
