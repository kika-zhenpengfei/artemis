"""Persist device confirmation separately from a source-linked test plan."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
from uuid import UUID, uuid4

from artemis.runtime import trace_store
from artemis.runtime.process_probe import pid_is_alive

logger = logging.getLogger(__name__)


class WorkflowStartError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class WorkflowStartStore:
    """A confirmation is an execution intent, not a device reservation."""

    @staticmethod
    def _directory(workflow_id: str) -> Path:
        try:
            if str(UUID(workflow_id)) != workflow_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise WorkflowStartError("workflow_id must be a canonical UUID", 422) from exc
        return Path(trace_store.get_trace_dir(workflow_id))

    @classmethod
    def _path(cls, workflow_id: str) -> Path:
        return cls._directory(workflow_id) / "device_start.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise WorkflowStartError("Could not read workflow device confirmation.", 503) from exc

    def _workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self._read(self._directory(workflow_id) / "test_workflow.json")
        if workflow is None:
            raise WorkflowStartError("Test workflow does not exist.", 404)
        return workflow

    @contextmanager
    def _locked(self, workflow_id: str):
        with trace_store._status_lock(str(self._path(workflow_id))) as acquired:
            if not acquired:
                raise WorkflowStartError("Workflow device confirmation is busy. Try again.", 503)
            yield

    @staticmethod
    def _fingerprint(workflow: dict[str, Any]) -> str:
        keys = (
            "goal", "repository_root", "source_revision", "intake_id", "submitted_apk",
            "requirements", "profile", "cases", "execution_plans",
        )
        payload = {key: workflow.get(key) for key in keys}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = time.time()
        trace_store._atomic_write_json(str(self._path(record["workflow_id"])), record)

    def _waiting(self, workflow: dict[str, Any], error: str | None = None) -> dict[str, Any]:
        return {
            "workflow_id": workflow["workflow_id"], "status": "awaiting_device",
            "device_serial": None, "adb_endpoint_id": None, "error": error,
            "plan_fingerprint": self._fingerprint(workflow),
        }

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in (
            "workflow_id", "status", "device_serial", "adb_endpoint_id", "error",
            "confirmed_at", "updated_at",
        )}

    def _state(self, workflow: dict[str, Any]) -> dict[str, Any]:
        record = self._read(self._path(workflow["workflow_id"]))
        if record and record.get("status") == "starting" and not pid_is_alive(
            record.get("owner_pid"), record.get("owner_created_at"),
        ):
            self._undo_start_binding(workflow)
            record = self._waiting(workflow, "The previous startup was interrupted. Confirm a device again.")
            self._save(record)
        if workflow["phase"] in {"running", "finished"}:
            return {
                **(record or self._waiting(workflow)), "status": "running",
                "device_serial": workflow.get("device_serial"),
            }
        if workflow["phase"] != "planned":
            raise WorkflowStartError("Generate test cases before confirming a device.")
        if record is None:
            record = self._waiting(workflow)
            self._save(record)
        elif record.get("plan_fingerprint") != self._fingerprint(workflow):
            record = self._waiting(workflow, "The test plan changed. Confirm a device again.")
            self._save(record)
        return record

    def _undo_start_binding(self, workflow: dict[str, Any]) -> None:
        if workflow["phase"] == "running":
            workflow.update(phase="planned", session_id=None, device_serial=None)
            trace_store._atomic_write_json(
                str(self._directory(workflow["workflow_id"]) / "test_workflow.json"), workflow,
            )

    @contextmanager
    def plan_update(self, workflow_id: str):
        """Serialize plan edits with confirmation and startup claims."""
        with self._locked(workflow_id):
            workflow = self._workflow(workflow_id)
            record = self._state(workflow) if workflow["phase"] != "context" else None
            if workflow["phase"] not in {"context", "planned"}:
                raise WorkflowStartError("The test workflow has already started.")
            if record and record.get("status") in {"starting", "running"}:
                raise WorkflowStartError("The test workflow is already starting or running.")
            yield
            workflow = self._workflow(workflow_id)
            if workflow["phase"] == "planned":
                self._state(workflow)

    def status(self, workflow_id: str) -> dict[str, Any]:
        with self._locked(workflow_id):
            return self._public(self._state(self._workflow(workflow_id)))

    def confirm(
        self, workflow_id: str, device_serial: str, adb_endpoint_id: str,
    ) -> dict[str, Any]:
        if not isinstance(device_serial, str) or not device_serial.strip():
            raise WorkflowStartError("Select an Android device.", 422)
        if not isinstance(adb_endpoint_id, str) or not adb_endpoint_id.strip():
            raise WorkflowStartError("An ADB endpoint is required.", 422)
        with self._locked(workflow_id):
            record = self._state(self._workflow(workflow_id))
            if record["status"] in {"starting", "running"}:
                raise WorkflowStartError("The test workflow is already starting or running.")
            record.update(
                status="confirmed", device_serial=device_serial.strip(),
                adb_endpoint_id=adb_endpoint_id.strip(), error=None, confirmed_at=time.time(),
            )
            self._save(record)
            return self._public(record)

    def claim(
        self, workflow_id: str, *, device_serial: str | None = None,
        adb_endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        with self._locked(workflow_id):
            record = self._state(self._workflow(workflow_id))
            if record["status"] in {"starting", "running"}:
                raise WorkflowStartError("The test workflow is already starting or running.")
            if record["status"] != "confirmed":
                return self._public(record)
            if device_serial is not None and device_serial != record["device_serial"]:
                raise WorkflowStartError("device_serial must match the confirmed device.")
            if adb_endpoint_id is not None and adb_endpoint_id != record["adb_endpoint_id"]:
                raise WorkflowStartError("ADB endpoint must match the confirmed device.")
            record.update(
                status="starting", claim_token=uuid4().hex, owner_pid=os.getpid(),
                owner_created_at=trace_store._own_create_time(),
            )
            self._save(record)
            return {**self._public(record), "claim_token": record["claim_token"]}

    def started(self, workflow_id: str, token: str) -> dict[str, Any]:
        with self._locked(workflow_id):
            workflow = self._workflow(workflow_id)
            record = self._read(self._path(workflow_id))
            if not record or record.get("claim_token") != token or record["status"] != "starting":
                raise WorkflowStartError("The workflow startup claim is no longer valid.")
            if record["plan_fingerprint"] != self._fingerprint(workflow):
                raise WorkflowStartError("The test plan changed during startup.")
            if workflow["phase"] != "running":
                raise WorkflowStartError("The workflow has not opened its device session.")
            record.update(status="running", error=None)
            self._save(record)
            return self._public(record)

    def failed(self, workflow_id: str, token: str, error: str) -> dict[str, Any]:
        with self._locked(workflow_id):
            workflow = self._workflow(workflow_id)
            record = self._read(self._path(workflow_id))
            if record and record.get("claim_token") == token and record["status"] == "starting":
                # The caller has closed its runtime; no case has executed yet.
                self._undo_start_binding(workflow)
                record = self._waiting(workflow, error)
                self._save(record)
            return self._public(record or self._waiting(workflow))

    def list_pending(self) -> list[dict[str, Any]]:
        pending = []
        for path in Path(trace_store.TRACES_DIR).glob("*/device_start.json"):
            try:
                with self._locked(path.parent.name):
                    workflow = self._workflow(path.parent.name)
                    state = self._public(self._state(workflow))
                    if workflow["phase"] != "planned":
                        continue
                    pending.append({
                        **state, "goal": workflow["goal"],
                        "profile": workflow["profile"], "case_count": len(workflow["cases"]),
                    })
            except WorkflowStartError:
                logger.warning("Could not read pending workflow %s", path.parent.name, exc_info=True)
        return sorted(pending, key=lambda item: item.get("updated_at") or 0, reverse=True)
