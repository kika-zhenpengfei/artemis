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

"""Run source-linked plans in the WebUI and pause for Codex on dynamic screens."""

from __future__ import annotations

import asyncio
from typing import Any

from artemis.device_execution.manager import DeviceExecution, DeviceExecutionManager
from artemis.device_execution.test_workflow import TestWorkflowStore


class PreparedWorkflowRunner:
    def __init__(
        self,
        manager: DeviceExecution | None = None,
        store: TestWorkflowStore | None = None,
    ):
        self._manager = manager or DeviceExecutionManager(agent_client="webui")
        self._store = store or TestWorkflowStore()
        self._workflow: dict[str, Any] | None = None
        self._case_index = 0
        self._case_prepared = False
        self._resume_token: str | None = None
        self._handoff_image: bytes | None = None
        self._terminal_status = "completed"
        self.control_lock = asyncio.Lock()

    def validate(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._store.planned(workflow_id)
        plans = workflow.get("execution_plans", {})
        missing = [case["id"] for case in workflow["cases"] if case["id"] not in plans]
        if missing:
            raise ValueError(f"Cases lack executable plans: {', '.join(missing)}")
        return workflow

    async def run(
        self, workflow_id: str, *, device_serial: str | None = None, require_idle: bool = False,
        expected_adb_endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        self._workflow = self.validate(workflow_id)
        opened = await self._manager.open(
            goal="",
            workflow_id=workflow_id,
            device_serial=device_serial,
            require_idle=require_idle,
            expected_adb_endpoint_id=expected_adb_endpoint_id,
            idle_timeout_seconds=3600,
        )
        if opened.get("status") == "awaiting_device":
            self._workflow = None
            return opened
        try:
            self._workflow = self._store.snapshot(workflow_id)
            return await self._advance(workflow_id)
        except BaseException:
            await self._manager.close(
                workflow_id, status="failed", summary="Prepared test workflow stopped unexpectedly."
            )
            raise

    async def resume(self, workflow_id: str, resume_token: str) -> dict[str, Any]:
        if self._workflow is None or self._workflow["workflow_id"] != workflow_id:
            raise ValueError("This WebUI test workflow is not running")
        if not self._resume_token or resume_token != self._resume_token:
            raise ValueError("resume_token must match the active Codex handoff")
        try:
            return await self._advance(workflow_id)
        except BaseException:
            await self._manager.close(
                workflow_id, status="failed", summary="Prepared test workflow stopped unexpectedly."
            )
            raise

    async def close(self, workflow_id: str, *, status: str = "cancelled") -> dict[str, Any]:
        self._resume_token = None
        return await self._manager.close(
            workflow_id, status=status, summary="WebUI test workflow closed during Codex handoff."
        )

    @property
    def handoff_image(self) -> bytes | None:
        return self._handoff_image

    @property
    def active_case_id(self) -> str | None:
        if self._workflow is None or not self._resume_token:
            return None
        cases = self._workflow["cases"]
        return cases[self._case_index]["id"] if self._case_index < len(cases) else None

    @property
    def manager(self) -> DeviceExecution:
        return self._manager

    async def _advance(self, workflow_id: str) -> dict[str, Any]:
        assert self._workflow is not None
        cases = self._workflow["cases"]
        while self._case_index < len(cases):
            case = cases[self._case_index]
            case_id = case["id"]
            if not self._case_prepared:
                preparation = await self._manager.prepare_case(workflow_id, case_id=case_id)
                if preparation["status"] == "blocked":
                    self._terminal_status = "failed"
                    self._case_index += 1
                    continue
                self._case_prepared = True
            for _ in range(100):
                result, image = await self._manager.run_plan(
                    workflow_id, case_id=case_id, resume_token=self._resume_token
                )
                self._resume_token = None
                if result["status"] == "continued":
                    continue
                if result["status"] == "handoff":
                    self._resume_token = result["resume_token"]
                    self._handoff_image = image
                    return {
                        "workflow_id": workflow_id,
                        "status": "handoff",
                        "case_id": case_id,
                        "handoff": result,
                    }
                if result["status"] != "completed":
                    self._terminal_status = "failed"
                break
            else:
                self._store.record_verification(
                    workflow_id,
                    case_id=case_id,
                    checkpoint_id=None,
                    status="inconclusive",
                    assertion=case["expected_result"],
                    evidence="Fixed runner exceeded 100 continuation calls.",
                    step_id="none",
                    observation_id=None,
                )
                self._terminal_status = "failed"
            self._case_index += 1
            self._case_prepared = False
            self._handoff_image = None
        self._resume_token = None
        return await self._manager.close(
            workflow_id, status=self._terminal_status,
            summary="Prepared test workflow finished.",
        )
