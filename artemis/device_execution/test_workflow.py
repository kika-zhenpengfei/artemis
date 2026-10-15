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

"""Source-linked test plans and case results for external-agent device sessions.

The external agent reads the repository and authors the plan. This module checks
its references and persists the contract; it does not make LLM decisions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from artemis.runtime import trace_store
from artemis.agents.scenario_rules import scenario_guidance
from artemis.device_execution.hybrid_plan import validate_case_plan
from artemis.device_execution.environment import validate_environment
from artemis.device_execution.test_intake import TestIntakeStore
from artemis.device_execution.workflow_start import WorkflowStartStore


_WRITE_LOCK = threading.RLock()


def _source_revision(root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=5, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip())
        return {"commit": revision, "working_tree_dirty": dirty}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"commit": None, "working_tree_dirty": None}


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _text_list(value: Any, name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"{name} must be a {'non-empty ' if required else ''}list")
    return [_required_text(item, name) for item in value]


def _unique_ids(items: list[dict[str, Any]], name: str) -> None:
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{name} IDs must be unique")


class TestWorkflowStore:
    """Small persisted workflow shared by the MCP tools and device manager."""

    def list_prepared(self) -> list[dict[str, Any]]:
        """Find plans awaiting a device run; called only when the WebUI opens plans."""
        root = Path(trace_store.TRACES_DIR)
        if not root.is_dir():
            return []
        prepared = []
        for path in root.glob("*/test_workflow.json"):
            try:
                workflow = json.loads(path.read_text(encoding="utf-8"))
                if workflow.get("phase") == "planned":
                    plans = workflow.get("execution_plans", {})
                    runnable = bool(workflow["cases"]) and all(
                        case["id"] in plans
                        for case in workflow["cases"]
                    )
                    needs_codex = any(
                        step["kind"] == "model"
                        for plan in plans.values()
                        for step in plan["steps"]
                    )
                    prepared.append(
                        {
                            "workflow_id": workflow["workflow_id"],
                            "goal": workflow["goal"],
                            "profile": workflow["profile"],
                            "case_count": len(workflow["cases"]),
                            "runnable": runnable,
                            "needs_codex": needs_codex,
                            "created_at": workflow["created_at"],
                        }
                    )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(prepared, key=lambda item: item["created_at"], reverse=True)

    @staticmethod
    def _directory(workflow_id: str) -> Path:
        try:
            if str(UUID(workflow_id)) != workflow_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("workflow_id must be a canonical UUID") from exc
        return Path(trace_store.get_trace_dir(workflow_id))

    def _read(self, workflow_id: str) -> dict[str, Any]:
        path = self._directory(workflow_id) / "test_workflow.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Test workflow '{workflow_id}' does not exist") from exc

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _save(self, workflow: dict[str, Any]) -> None:
        path = self._directory(workflow["workflow_id"]) / "test_workflow.json"
        self._write(path, json.dumps(workflow, ensure_ascii=False, indent=2) + "\n")

    def create(
        self,
        *,
        goal: str,
        repository_root: str,
        requirements: list[dict[str, Any]],
        intake_id: str | None = None,
    ) -> dict[str, Any]:
        clean_goal = _required_text(goal, "goal")
        root = (
            Path(_required_text(repository_root, "repository_root"))
            .expanduser()
            .resolve(strict=True)
        )
        if not root.is_dir():
            raise ValueError("repository_root must be a directory")
        intake = (
            TestIntakeStore().validate_link(
                intake_id, goal=clean_goal, repository_root=str(root)
            )
            if intake_id
            else None
        )
        if not isinstance(requirements, list) or not requirements:
            raise ValueError("requirements must be a non-empty list")

        clean_requirements = []
        for requirement in requirements:
            if not isinstance(requirement, dict):
                raise ValueError("each requirement must be an object")
            refs = requirement.get("code_refs")
            if not isinstance(refs, list) or not refs:
                raise ValueError("each requirement must have at least one code_ref")
            clean_refs = []
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValueError("each code_ref must be an object")
                path = Path(_required_text(ref.get("path"), "code_ref.path"))
                source = (root / path).resolve(strict=True)
                if not source.is_relative_to(root) or not source.is_file():
                    raise ValueError(f"code_ref.path must name a file under {root}")
                line = ref.get("line")
                if type(line) is not int or line < 1:
                    raise ValueError("code_ref.line must be a positive integer")
                with source.open(encoding="utf-8", errors="replace") as stream:
                    if not any(index == line for index, _ in enumerate(stream, start=1)):
                        raise ValueError(f"code_ref.line {line} is outside {source}")
                clean_refs.append(
                    {
                        "path": str(source.relative_to(root)),
                        "line": line,
                        "reason": _required_text(ref.get("reason"), "code_ref.reason"),
                    }
                )
            clean_requirements.append(
                {
                    "id": _required_text(requirement.get("id"), "requirement.id"),
                    "description": _required_text(
                        requirement.get("description"), "requirement.description"
                    ),
                    "code_refs": clean_refs,
                }
            )
        _unique_ids(clean_requirements, "requirement")

        workflow_id = str(uuid4())
        guidance = scenario_guidance(clean_goal)
        workflow = {
            "workflow_id": workflow_id,
            "goal": clean_goal,
            "repository_root": str(root),
            "source_revision": _source_revision(root),
            "intake_id": intake_id,
            "submitted_apk": {
                "path": intake["apk_path"],
                "sha256": intake["apk_sha256"],
                "package_name": intake["package_name"],
            }
            if intake
            else None,
            "phase": "context",
            "profile": None,
            "requirements": clean_requirements,
            "scenario_rule_ids": guidance["rule_ids"],
            "cases": [],
            "actions": [],
            "results": [],
            "execution_plans": {},
            "execution_state": {},
            "preparation": {},
            "restoration": {},
            "session_id": None,
            "device_serial": None,
            "session_status": None,
            "created_at": time.time(),
        }
        with _WRITE_LOCK:
            self._save(workflow)
            if intake_id:
                TestIntakeStore().link(intake_id, workflow_id)
        return {
            "workflow_id": workflow_id,
            "phase": "context",
            "requirements": clean_requirements,
            "source_revision": workflow["source_revision"],
            "intake_id": intake_id,
            "submitted_apk": workflow["submitted_apk"],
            "scenario_guidance": guidance,
            "next": "Generate core cases with mobile_test_cases before opening a device session.",
        }

    def set_cases(
        self, workflow_id: str, *, cases: list[dict[str, Any]], profile: str
    ) -> dict[str, Any]:
        normalized_profile = _required_text(profile, "profile").lower()
        if normalized_profile not in {"flash", "pro"}:
            raise ValueError("profile must be 'flash' or 'pro'")
        if not isinstance(cases, list) or not cases:
            raise ValueError("cases must be a non-empty list")
        with _WRITE_LOCK, WorkflowStartStore().plan_update(workflow_id):
            workflow = self._read(workflow_id)
            if workflow["phase"] not in {"context", "planned"}:
                raise ValueError("cases cannot change after device execution starts")
            known_requirements = {item["id"] for item in workflow["requirements"]}
            clean_cases = []
            for case in cases:
                if not isinstance(case, dict):
                    raise ValueError("each case must be an object")
                requirement_ids = _text_list(
                    case.get("requirement_ids"), "case.requirement_ids", required=True
                )
                unknown = set(requirement_ids) - known_requirements
                if unknown:
                    raise ValueError(f"case references unknown requirements: {sorted(unknown)}")
                checkpoints = case.get("checkpoints", [])
                if not isinstance(checkpoints, list):
                    raise ValueError("case.checkpoints must be a list")
                clean_checkpoints = []
                for checkpoint in checkpoints:
                    if not isinstance(checkpoint, dict):
                        raise ValueError("each checkpoint must be an object")
                    clean_checkpoints.append(
                        {
                            "id": _required_text(checkpoint.get("id"), "checkpoint.id"),
                            "assertion": _required_text(
                                checkpoint.get("assertion"), "checkpoint.assertion"
                            ),
                        }
                    )
                _unique_ids(clean_checkpoints, "checkpoint")
                if normalized_profile == "pro" and not clean_checkpoints:
                    raise ValueError("Pro cases must declare at least one checkpoint")
                if normalized_profile == "flash" and clean_checkpoints:
                    raise ValueError("Flash cases cannot declare checkpoints; use Pro")
                clean_cases.append(
                    {
                        "id": _required_text(case.get("id"), "case.id"),
                        "title": _required_text(case.get("title"), "case.title"),
                        "requirement_ids": requirement_ids,
                        "preconditions": _text_list(
                            case.get("preconditions", []), "case.preconditions"
                        ),
                        "environment": validate_environment(case.get("environment")),
                        "steps": _text_list(case.get("steps"), "case.steps", required=True),
                        "expected_result": _required_text(
                            case.get("expected_result"), "case.expected_result"
                        ),
                        "checkpoints": clean_checkpoints,
                    }
                )
            _unique_ids(clean_cases, "case")
            workflow["profile"] = normalized_profile
            workflow["cases"] = clean_cases
            workflow["execution_plans"] = {}
            workflow["execution_state"] = {}
            workflow["preparation"] = {}
            workflow["restoration"] = {}
            workflow["phase"] = "planned"
            self._save(workflow)
            if workflow.get("intake_id"):
                TestIntakeStore().mark_planned(workflow["intake_id"])
            plan_path = self._directory(workflow_id) / "notes" / "test_plan.md"
            self._write(plan_path, self._plan_markdown(workflow))
            self._report(workflow)
        covered = {rid for case in clean_cases for rid in case["requirement_ids"]}
        return {
            "workflow_id": workflow_id,
            "phase": "planned",
            "profile": normalized_profile,
            "case_count": len(clean_cases),
            "uncovered_requirement_ids": sorted(known_requirements - covered),
            "plan_path": str(plan_path),
            "next": "Confirm a device for this workflow in the WebUI, then open mobile_session with workflow_id.",
        }

    def set_execution_plans(
        self, workflow_id: str, *, plans: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Validate the fixed/model boundary before a device is leased."""
        if not isinstance(plans, list) or not plans:
            raise ValueError("plans must be a non-empty list")
        with _WRITE_LOCK, WorkflowStartStore().plan_update(workflow_id):
            workflow = self.planned(workflow_id)
            cases = {case["id"]: case for case in workflow["cases"]}
            clean: dict[str, dict[str, Any]] = {}
            for plan in plans:
                if not isinstance(plan, dict) or set(plan) != {
                    "case_id",
                    "steps",
                    "final_assertion",
                }:
                    raise ValueError("each plan needs case_id, steps, and final_assertion")
                case_id = _required_text(plan["case_id"], "plan.case_id")
                if case_id not in cases or case_id in clean:
                    raise ValueError("plan.case_id must identify an unplanned case")
                clean[case_id] = validate_case_plan(
                    cases[case_id],
                    {"steps": plan["steps"], "final_assertion": plan["final_assertion"]},
                )
            workflow["execution_plans"] = clean
            workflow["execution_state"] = {
                case_id: {
                    "cursor": 0,
                    "handoff": None,
                    "metrics": {
                        "fixed_actions": 0,
                        "device_action_ms": 0,
                        "post_action_observe_ms": 0,
                        "handoffs": 0,
                        "handoff_wait_ms": 0,
                    },
                }
                for case_id in clean
            }
            self._save(workflow)
            plan_path = self._directory(workflow_id) / "notes" / "test_plan.md"
            self._write(plan_path, self._plan_markdown(workflow))
            self._report(workflow)
            return {
                "workflow_id": workflow_id,
                "planned_cases": list(clean),
                "plan_path": str(plan_path),
            }

    def execution(self, workflow_id: str, case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        workflow = self._read(workflow_id)
        if workflow["phase"] != "running":
            raise ValueError("test workflow is not running")
        plan = workflow.get("execution_plans", {}).get(case_id)
        if plan is None:
            raise ValueError("case has no executable plan")
        return plan, workflow["execution_state"][case_id]

    def save_execution_state(self, workflow_id: str, case_id: str, state: dict[str, Any]) -> None:
        with _WRITE_LOCK:
            workflow = self._read(workflow_id)
            if workflow["phase"] != "running" or case_id not in workflow.get("execution_plans", {}):
                raise ValueError("case has no running executable plan")
            workflow["execution_state"][case_id] = state
            self._save(workflow)

    def planned(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._read(workflow_id)
        if workflow["phase"] != "planned":
            raise ValueError("Generate cases with mobile_test_cases before opening the session")
        return workflow

    def bind(self, workflow_id: str, *, session_id: str, device_serial: str) -> None:
        with _WRITE_LOCK:
            workflow = self.planned(workflow_id)
            workflow["phase"] = "running"
            workflow["session_id"] = session_id
            workflow["device_serial"] = device_serial
            self._save(workflow)
            if workflow.get("intake_id"):
                TestIntakeStore().set_status(workflow["intake_id"], "running")

    def validate_verification(
        self,
        workflow_id: str,
        *,
        case_id: str | None,
        checkpoint_id: str | None,
        assertion: str,
    ) -> None:
        case = self.validate_case(workflow_id, case_id)
        if checkpoint_id:
            checkpoint = next(
                (item for item in case["checkpoints"] if item["id"] == checkpoint_id), None
            )
            if checkpoint is None:
                raise ValueError("checkpoint_id must identify a checkpoint in the case")
            expected = checkpoint["assertion"]
        else:
            expected = case["expected_result"]
        if assertion != expected:
            raise ValueError("assertion must match the planned case result or checkpoint")

    def validate_case(self, workflow_id: str, case_id: str | None) -> dict[str, Any]:
        workflow = self._read(workflow_id)
        if workflow["phase"] != "running":
            raise ValueError("test workflow is not running")
        case = next((item for item in workflow["cases"] if item["id"] == case_id), None)
        if case is None:
            raise ValueError("case_id must identify a planned test case")
        return case

    def record_preparation(
        self, workflow_id: str, case_id: str, result: dict[str, Any]
    ) -> None:
        with _WRITE_LOCK:
            self.validate_case(workflow_id, case_id)
            workflow = self._read(workflow_id)
            workflow["results"] = [
                item for item in workflow["results"] if item["case_id"] != case_id
            ]
            workflow["actions"] = [
                item for item in workflow["actions"] if item["case_id"] != case_id
            ]
            if result["status"] == "ready":
                workflow.setdefault("restoration", {}).pop(case_id, None)
            state = workflow.get("execution_state", {}).get(case_id)
            if state is not None:
                state["cursor"] = 0
                state["handoff"] = None
                state.pop("done", None)
                state.pop("terminal_status", None)
                state["metrics"] = {
                    "fixed_actions": 0,
                    "device_action_ms": 0,
                    "post_action_observe_ms": 0,
                    "handoffs": 0,
                    "handoff_wait_ms": 0,
                }
            workflow.setdefault("preparation", {})[case_id] = {
                **result,
                "recorded_at": time.time(),
            }
            self._save(workflow)
            self._report(workflow)

    def require_prepared(self, workflow_id: str, case_id: str) -> None:
        case = self.validate_case(workflow_id, case_id)
        if case.get("environment"):
            result = self._read(workflow_id).get("preparation", {}).get(case_id)
            if not result or result["status"] != "ready":
                raise ValueError("case environment is not ready; call mobile_prepare_case first")

    def record_restoration(self, workflow_id: str, case_id: str, result: dict[str, Any]) -> None:
        with _WRITE_LOCK:
            workflow = self._read(workflow_id)
            workflow.setdefault("restoration", {})[case_id] = {
                **result,
                "recorded_at": time.time(),
            }
            self._save(workflow)
            self._report(workflow)

    def record_action(
        self,
        workflow_id: str,
        *,
        case_id: str,
        action: str,
        status: str,
        step_id: str,
    ) -> None:
        with _WRITE_LOCK:
            self.validate_case(workflow_id, case_id)
            workflow = self._read(workflow_id)
            workflow["actions"].append(
                {"case_id": case_id, "action": action, "status": status, "step_id": step_id}
            )
            self._save(workflow)

    def record_scenario_rules(self, workflow_id: str, rule_ids: list[str]) -> None:
        """Keep candidate rules supplied during the test workflow."""
        if not rule_ids:
            return
        with _WRITE_LOCK:
            workflow = self._read(workflow_id)
            existing = list(workflow.get("scenario_rule_ids", []))
            known = set(existing)
            additions = []
            for rule_id in rule_ids:
                if rule_id not in known:
                    additions.append(rule_id)
                    known.add(rule_id)
            if additions:
                workflow["scenario_rule_ids"] = existing + additions
                self._save(workflow)

    def record_verification(
        self,
        workflow_id: str,
        *,
        case_id: str,
        checkpoint_id: str | None,
        status: str,
        assertion: str,
        evidence: str,
        step_id: str,
        observation_id: str | None,
    ) -> dict[str, Any]:
        with _WRITE_LOCK:
            self.validate_verification(
                workflow_id,
                case_id=case_id,
                checkpoint_id=checkpoint_id,
                assertion=assertion,
            )
            workflow = self._read(workflow_id)
            workflow["results"].append(
                {
                    "case_id": case_id,
                    "checkpoint_id": checkpoint_id,
                    "status": status,
                    "assertion": assertion,
                    "evidence": evidence,
                    "step_id": step_id,
                    "observation_id": observation_id,
                    "recorded_at": time.time(),
                }
            )
            self._save(workflow)
            self._report(workflow)
            return {"case_id": case_id, "case_status": self._case_status(workflow, case_id)}

    def finish(self, workflow_id: str, *, session_status: str) -> dict[str, Any]:
        with _WRITE_LOCK:
            workflow = self._read(workflow_id)
            workflow["phase"] = "finished"
            workflow["session_status"] = session_status
            self._save(workflow)
            if workflow.get("intake_id"):
                TestIntakeStore().set_status(workflow["intake_id"], "finished")
            return self._report(workflow)

    def report(self, workflow_id: str) -> dict[str, Any]:
        with _WRITE_LOCK:
            return self._report(self._read(workflow_id))

    def snapshot(self, workflow_id: str) -> dict[str, Any]:
        """Return current case results without writing files (for WebUI polling)."""
        with _WRITE_LOCK:
            return self._report(self._read(workflow_id), persist=False)

    def _report(self, workflow: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        case_results = []
        for case in workflow["cases"]:
            case_results.append(
                {
                    **case,
                    "status": self._case_status(workflow, case["id"]),
                    "preparation": workflow.get("preparation", {}).get(case["id"]),
                    "restoration": workflow.get("restoration", {}).get(case["id"]),
                    "actions": [
                        item
                        for item in workflow.get("actions", [])
                        if item["case_id"] == case["id"]
                    ],
                    "verifications": [
                        item for item in workflow["results"] if item["case_id"] == case["id"]
                    ],
                }
            )
        counts = {
            status: sum(case["status"] == status for case in case_results)
            for status in ("passed", "failed", "inconclusive", "blocked", "not_run")
        }
        report_path = self._directory(workflow["workflow_id"]) / "notes" / "test_report.md"
        if persist:
            self._write(report_path, self._report_markdown(workflow, case_results, counts))
        return {
            "workflow_id": workflow["workflow_id"],
            "goal": workflow["goal"],
            "repository_root": workflow["repository_root"],
            "source_revision": workflow.get("source_revision"),
            "intake_id": workflow.get("intake_id"),
            "submitted_apk": workflow.get("submitted_apk"),
            "phase": workflow["phase"],
            "profile": workflow["profile"],
            "session_id": workflow["session_id"],
            "device_serial": workflow["device_serial"],
            "session_status": workflow["session_status"],
            "counts": counts,
            "requirements": workflow["requirements"],
            "scenario_rule_ids": workflow.get("scenario_rule_ids", []),
            "execution_plans": workflow.get("execution_plans", {}),
            "execution_state": workflow.get("execution_state", {}),
            "preparation": workflow.get("preparation", {}),
            "restoration": workflow.get("restoration", {}),
            "cases": case_results,
            "report_path": str(report_path),
        }

    @staticmethod
    def _case_status(workflow: dict[str, Any], case_id: str) -> str:
        case = next(item for item in workflow["cases"] if item["id"] == case_id)
        preparation = workflow.get("preparation", {}).get(case_id)
        if preparation and preparation["status"] == "blocked":
            return "blocked"
        results = [item for item in workflow["results"] if item["case_id"] == case_id]
        latest = {item["checkpoint_id"]: item["status"] for item in results}
        required = [None]
        if workflow["profile"] == "pro":
            required.extend(item["id"] for item in case["checkpoints"])
        statuses = [latest.get(item) for item in required]
        if "failed" in statuses:
            return "failed"
        if "inconclusive" in statuses:
            return "inconclusive"
        if all(status == "passed" for status in statuses):
            return "passed"
        return "not_run"

    @staticmethod
    def _plan_markdown(workflow: dict[str, Any]) -> str:
        revision = workflow.get("source_revision") or {}
        lines = [
            f"# Test plan: {workflow['goal']}", "", f"Profile: {workflow['profile']}",
            f"Source commit: {revision.get('commit') or 'unavailable'}",
            f"Working tree dirty: {revision.get('working_tree_dirty')}", "",
        ]
        if workflow.get("submitted_apk"):
            lines.insert(-1, f"Submitted APK SHA-256: {workflow['submitted_apk']['sha256']}")
        lines.append(
            "Candidate scenario rules: "
            + (", ".join(workflow.get("scenario_rule_ids", [])) or "none")
        )
        lines.append("")
        lines.append("## Requirements and code")
        lines.append("")
        for requirement in workflow["requirements"]:
            lines.append(f"- {requirement['id']}: {requirement['description']}")
            for ref in requirement["code_refs"]:
                lines.append(f"  - `{ref['path']}:{ref['line']}` — {ref['reason']}")
        lines.extend(["", "## Core cases", ""])
        for case in workflow["cases"]:
            lines.extend(
                [
                    f"### {case['id']}: {case['title']}",
                    "",
                    f"Requirements: {', '.join(case['requirement_ids'])}",
                    "",
                ]
            )
            for precondition in case["preconditions"]:
                lines.append(f"- Precondition: {precondition}")
            if case.get("environment"):
                lines.append(f"- Environment: `{json.dumps(case['environment'], ensure_ascii=False)}`")
            for index, step in enumerate(case["steps"], start=1):
                lines.append(f"{index}. {step}")
            lines.extend(["", f"Expected: {case['expected_result']}", ""])
            for checkpoint in case["checkpoints"]:
                lines.append(f"- Checkpoint {checkpoint['id']}: {checkpoint['assertion']}")
            if case["checkpoints"]:
                lines.append("")
            execution = workflow.get("execution_plans", {}).get(case["id"])
            if execution:
                lines.append("Execution:")
                for index, item in enumerate(execution["steps"], start=1):
                    label = item["action"] if item["kind"] == "auto" else item["goal"]
                    lines.append(f"- {index}. {item['kind']}: {label}")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _report_markdown(
        workflow: dict[str, Any], cases: list[dict[str, Any]], counts: dict[str, int]
    ) -> str:
        lines = [
            f"# Test report: {workflow['goal']}",
            "",
            f"Profile: {workflow['profile'] or 'unplanned'}",
            f"Phase: {workflow['phase']}",
            f"Device: {workflow['device_serial'] or 'not started'}",
            f"Session: {workflow['session_id'] or 'not started'}",
            f"Source commit: {(workflow.get('source_revision') or {}).get('commit') or 'unavailable'}",
            f"Submitted APK SHA-256: {(workflow.get('submitted_apk') or {}).get('sha256') or 'none'}",
            "Candidate scenario rules: "
            + (", ".join(workflow.get("scenario_rule_ids", [])) or "none"),
            "",
            "## Summary",
            "",
            ", ".join(f"{key}: {value}" for key, value in counts.items()),
            "",
            "## Requirements and code",
            "",
        ]
        for requirement in workflow["requirements"]:
            lines.append(f"- {requirement['id']}: {requirement['description']}")
            for ref in requirement["code_refs"]:
                lines.append(f"  - `{ref['path']}:{ref['line']}` — {ref['reason']}")
        lines.extend(
            [
                "",
                "## Cases",
                "",
            ]
        )
        for case in cases:
            lines.extend(
                [
                    f"### {case['id']}: {case['title']} — {case['status']}",
                    "",
                    f"Requirements: {', '.join(case['requirement_ids'])}",
                    f"Expected: {case['expected_result']}",
                    "",
                ]
            )
            for precondition in case["preconditions"]:
                lines.append(f"- Precondition: {precondition}")
            if case.get("environment"):
                lines.append(f"- Environment: `{json.dumps(case['environment'], ensure_ascii=False)}`")
            preparation = case.get("preparation")
            if preparation:
                lines.append(f"- Preparation: {preparation['status']}")
                if preparation.get("reason"):
                    lines.append(f"- Blocker: {preparation['reason']}")
                for item in preparation.get("evidence", []):
                    lines.append(f"- Environment evidence: {item}")
            restoration = case.get("restoration")
            if restoration:
                lines.append(f"- Setting restoration: {restoration['status']}")
                for item in restoration.get("errors", []):
                    lines.append(f"- Restoration error: {item}")
            for index, step in enumerate(case["steps"], start=1):
                lines.append(f"{index}. {step}")
            if case["preconditions"] or case["steps"]:
                lines.append("")
            execution = workflow.get("execution_plans", {}).get(case["id"])
            progress = workflow.get("execution_state", {}).get(case["id"])
            if execution and progress:
                metrics = progress.get("metrics", {})
                lines.append(
                    f"Execution: {progress['cursor']}/{len(execution['steps'])} steps; "
                    f"fixed actions: {metrics.get('fixed_actions', 0)}; "
                    f"device action: {metrics.get('device_action_ms', 0)} ms; "
                    f"post-action observation: {metrics.get('post_action_observe_ms', 0)} ms; "
                    f"handoff wait: {metrics.get('handoff_wait_ms', 0)} ms"
                )
                if progress.get("handoff"):
                    lines.append(f"Model handoff: {progress['handoff']['reason']}")
                lines.append("")
            for action in case["actions"]:
                lines.append(
                    f"- Action: {action['action']} — {action['status']} "
                    f"(trace step {action['step_id']})"
                )
            if not case["verifications"]:
                lines.extend(["No device verification recorded.", ""])
            for result in case["verifications"]:
                target = result["checkpoint_id"] or "final"
                lines.extend(
                    [
                        f"- {target}: {result['status']} — {result['assertion']}",
                        f"  Evidence: {result['evidence']}",
                        f"  Trace step: {result['step_id']}; observation: "
                        f"{result['observation_id'] or 'none'}",
                    ]
                )
            if case["verifications"]:
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"
