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

"""Shared FastMCP instance definition for the mobile automation MCP server."""

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _server_lifespan(_server: FastMCP) -> AsyncIterator[dict]:
    """Finalize any device leases before the long-lived MCP process exits."""
    try:
        yield {}
    finally:
        try:
            from mcp_server.tools.device_execution import close_device_execution_sessions

            await close_device_execution_sessions()
        except Exception as exc:
            logger.warning("Could not close device execution sessions: %s", exc, exc_info=True)


try:
    from mcp.server.fastmcp.server import Settings as FastMCPSettings

    FastMCPSettings.model_rebuild()
except Exception as exc:
    # Version-compat shim: older/newer FastMCP releases may not expose
    # Settings or need the rebuild; the server works without it.
    logger.debug("FastMCP Settings.model_rebuild skipped: %s", exc, exc_info=True)

# Define the shared FastMCP instance for external IDE and agent clients.
mcp = FastMCP(
    "artemis",
    lifespan=_server_lifespan,
    instructions=(
        "ARTEMIS is an Android UI automation engine. For external-agent-driven execution, "
        "open a device lease with mobile_session, then use mobile_observe, mobile_act, and "
        "mobile_verify in an observe-act-verify loop, and always close the session. "
        "Use indexed elements before normalized coordinate fallback. "
        "For legacy autonomous execution, "
        "Use mobile_run_task to launch autonomous UI workflows on connected Android devices or emulators, "
        "mobile_manage_task to check status or steer execution, "
        "mobile_get_device_state to inspect real-time device screen/hierarchy, "
        "mobile_inspect_trace to inspect detailed execution steps and visual action overlays, "
        "and mobile_diagnose whenever a tool errors, no device is found, or the user says "
        "ARTEMIS is not working: it checks the environment and returns ordered fix steps."
    ),
)

# Tool registration is intentionally NOT done here: mcp_server/__init__.py
# imports mcp_server.tools (which imports this module), so importing this
# module from anywhere already registers every tool. Importing tools here as
# well would create a circular import (base -> tools -> base).
