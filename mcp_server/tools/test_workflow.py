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

"""MCP surfaces for the source-linked external-agent test workflow."""

from typing import Any

from artemis.device_execution.test_workflow import TestWorkflowStore
from mcp_server.base import mcp

_store = TestWorkflowStore()


@mcp.tool()
def mobile_test_context(
    goal: str,
    repository_root: str,
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Associate requirements with inspected source before planning tests.

    The current development agent reads the repository, then supplies each
    requirement as `{id, description, code_refs}`. Every code reference must
    contain `{path, line, reason}`; ARTEMIS checks that the file and line exist
    under `repository_root`. No device is leased at this stage. Returns a
    `workflow_id` for `mobile_test_cases`.
    """
    return _store.create(goal=goal, repository_root=repository_root, requirements=requirements)


@mcp.tool()
def mobile_test_cases(
    workflow_id: str,
    cases: list[dict[str, Any]],
    profile: str = "flash",
) -> dict[str, Any]:
    """Persist core cases and a WebUI-visible test plan before device execution.

    The development agent generates cases from the test goal and the linked
    requirements. Each case has `id`, `title`, `requirement_ids`, `steps`
    (non-empty string list), `expected_result`, and optional `preconditions`.
    Use `profile='flash'` for direct paths. `profile='pro'` requires one or more
    `{id, assertion}` checkpoints per case, verified during execution. This
    external-agent profile does not invoke the autonomous Flash/Pro LLM graph.
    """
    return _store.set_cases(workflow_id, cases=cases, profile=profile)


@mcp.tool()
def mobile_test_report(workflow_id: str) -> dict[str, Any]:
    """Read the current per-case results and write a WebUI-visible report.

    Cases remain `not_run` until the associated `mobile_verify` calls record
    observable evidence. The report is also finalized when the device session
    closes, including failed or cancelled sessions.
    """
    return _store.report(workflow_id)
