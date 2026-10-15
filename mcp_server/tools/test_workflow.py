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
from artemis.device_execution.test_intake import TestIntakeStore
from mcp_server.base import mcp

_store = TestWorkflowStore()
_intakes = TestIntakeStore()


@mcp.tool()
def mobile_test_intakes(intake_id: str | None = None) -> dict[str, Any]:
    """List WebUI APK/source submissions or read one intake before planning.

    An intake contains the test goal, local repository root, recorded Git HEAD,
    package name, and uploaded APK path/SHA-256. Use the goal to locate and
    inspect relevant requirements and code in that repository; the recorded
    HEAD identifies the source version, not the files to test. Then call
    `mobile_test_context(..., intake_id=...)` and generate cases and execution
    plans. This read-only tool does not lease a device.
    """
    if intake_id:
        return _intakes.read(intake_id)
    return {"intakes": _intakes.list()}


@mcp.tool()
def mobile_test_context(
    goal: str,
    repository_root: str,
    requirements: list[dict[str, Any]],
    intake_id: str | None = None,
) -> dict[str, Any]:
    """Associate requirements with inspected source before planning tests.

    The current development agent reads the repository, then supplies each
    requirement as `{id, description, code_refs}`. Every code reference must
    contain `{path, line, reason}`; ARTEMIS checks that the file and line exist
    under `repository_root`. No device is leased at this stage. Returns a
    `workflow_id` for `mobile_test_cases`. Pass `intake_id` when the APK, goal,
    package, and source commit were submitted from the WebUI; they are then
    linked to the source-checked workflow.
    """
    return _store.create(
        goal=goal, repository_root=repository_root,
        requirements=requirements, intake_id=intake_id,
    )


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
    Optional `environment` has `package_name`, `reset` (`none`, `clear_data`,
    or `reinstall`), `checks`, and optional `set_settings`.
    `reset='reinstall'` additionally requires a host `apk_path` whose package
    matches the declared package. `set_settings` temporarily changes supported
    Android time/display settings and restores them when the next case starts
    or the session closes. Checks support `{kind:'setting_equals', namespace, key, value}` and
    `{kind:'device_time_between', start, end}` (timezone-aware ISO 8601).
    Call `mobile_prepare_case` before running a case with an environment.
    Use `profile='flash'` for direct paths. `profile='pro'` requires one or more
    `{id, assertion}` checkpoints per case, verified during execution. This
    external-agent profile does not invoke the autonomous Flash/Pro LLM graph.
    This publishes the workflow to the WebUI waiting list. Next call
    `mobile_session(operation='open', workflow_id=...)` and keep waiting while
    it returns `awaiting_device`. The user selects and confirms a device for
    this workflow in WebUI before execution can start.
    """
    return _store.set_cases(workflow_id, cases=cases, profile=profile)


@mcp.tool()
def mobile_test_execution_plan(
    workflow_id: str,
    plans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compile fixed actions and model checkpoints before opening a device.

    Each plan has `case_id`, `steps`, and `final_assertion`. An `auto` step has
    `action`, optional `locator` for a screen target, `arguments`, `expect`,
    and optional `timeout_ms` / `checkpoint_id`. A `model` step has `goal`,
    `resume_when`, and optional `if_visible`, `max_actions`, `checkpoint_id`.
    For image-only ads, omit `if_visible` or use a marker only when the normal
    return screen in `resume_when` can confirm that the ad is gone.
    Conditions use `{state: visible|absent, locator: {resource_id|text|class}}`.
    Locators must identify a stable control; transient element indices are not
    accepted in plans. Cases without a compiled plan can use the manual path.
    """
    return _store.set_execution_plans(workflow_id, plans=plans)


@mcp.tool()
def mobile_test_report(workflow_id: str) -> dict[str, Any]:
    """Read the current per-case results and write a WebUI-visible report.

    Cases remain `not_run` until the associated `mobile_verify` calls record
    observable evidence. The report is also finalized when the device session
    closes, including failed or cancelled sessions.
    """
    return _store.report(workflow_id)
