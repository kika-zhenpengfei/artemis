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

"""MCP Tools package for ARTEMIS."""

from mcp_server.tools.device_state import mobile_get_device_state
from mcp_server.tools.device_execution import (
    mobile_act,
    mobile_observe,
    mobile_prepare_case,
    mobile_run_plan,
    mobile_session,
    mobile_verify,
    mobile_webui_handoff,
)
from mcp_server.tools.diagnose import mobile_diagnose
from mcp_server.tools.inspect_trace import mobile_inspect_trace
from mcp_server.tools.task_manager import mobile_manage_task
from mcp_server.tools.task_runner import mobile_run_task
from mcp_server.tools.test_workflow import (
    mobile_test_cases,
    mobile_test_context,
    mobile_test_execution_plan,
    mobile_test_report,
    mobile_test_intakes,
)

__all__ = [
    "mobile_run_task",
    "mobile_manage_task",
    "mobile_get_device_state",
    "mobile_inspect_trace",
    "mobile_diagnose",
    "mobile_session",
    "mobile_observe",
    "mobile_prepare_case",
    "mobile_act",
    "mobile_run_plan",
    "mobile_verify",
    "mobile_webui_handoff",
    "mobile_test_context",
    "mobile_test_cases",
    "mobile_test_execution_plan",
    "mobile_test_report",
    "mobile_test_intakes",
]
