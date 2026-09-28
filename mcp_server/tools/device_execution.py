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
import os
from typing import Any
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
    content: list[Any] = [
        TextContent(type="text", text=str(payload.get("message") or payload.get("status") or "OK"))
    ]
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
) -> dict[str, Any]:
    """Open, inspect, or close an external-agent-driven device session.

    Operations:
      - `open`: requires `goal` unless `workflow_id` names a prepared test plan;
        optionally pass `device_serial`. With several
        ready devices, first ask the user to choose a serial. Returns a
        `session_id`; then call `mobile_observe`.
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
        result = await _manager.open(
            goal=goal or "",
            device_serial=device_serial,
            session_id=session_id,
            lock_timeout_seconds=lock_timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            workflow_id=workflow_id,
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
async def mobile_observe(
    session_id: str,
    settle_ms: int = 400,
    include_image: bool = True,
) -> CallToolResult:
    """Observe the device and persist an observation step for the WebUI.

    Returns a screenshot, indexed `elements_text`, and an `observation_id`.
    Element indices are dynamic locators for the next `mobile_act` call. Always
    observe before the first screen-targeted action and after any uncertainty.
    """
    payload, image_bytes = await _manager.observe(session_id, settle_ms=settle_ms)
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
