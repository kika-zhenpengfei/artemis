"""Workflow startup admission is exclusive across independent MCP processes."""

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.device_execution.workflow_start import WorkflowStartError, WorkflowStartStore
from artemis.runtime import trace_store


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "Screen.kt").write_text("class Screen\n", encoding="utf-8")
    workflow_id = TestWorkflowStore().create(
        goal="Check screen",
        repository_root=str(source),
        requirements=[{
            "id": "R1", "description": "Screen",
            "code_refs": [{"path": "Screen.kt", "line": 1, "reason": "UI source"}],
        }],
    )["workflow_id"]
    TestWorkflowStore().set_cases(
        workflow_id,
        profile="flash",
        cases=[{
            "id": "C1", "title": "Open", "requirement_ids": ["R1"],
            "steps": ["Tap Debug"], "expected_result": "Debug visible",
        }],
    )
    WorkflowStartStore().confirm(workflow_id, "phone-a", "endpoint-a")
    return workflow_id


_CLAIM_PROCESS = """
import json
from pathlib import Path
import sys
import time

from artemis.device_execution.workflow_start import WorkflowStartError, WorkflowStartStore
from artemis.runtime import trace_store

trace_store.TRACES_DIR, workflow_id, coordination_dir, process_number = sys.argv[1:]
coordination = Path(coordination_dir)
(coordination / f"ready-{process_number}").touch()
deadline = time.monotonic() + 20
while not (coordination / "begin").exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("Parent did not release the claim barrier")
    time.sleep(0.01)
try:
    result = WorkflowStartStore().claim(workflow_id)
except WorkflowStartError as error:
    result = {"error": str(error), "status_code": error.status_code}
result_path = coordination / f"result-{process_number}.json"
temporary_path = result_path.with_suffix(".tmp")
temporary_path.write_text(json.dumps(result))
temporary_path.replace(result_path)
while not (coordination / "release").exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("Parent did not release the live startup owner")
    time.sleep(0.01)
"""


def _wait_for_files(paths, processes):
    deadline = time.monotonic() + 15
    while not all(path.exists() for path in paths):
        for process in processes:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"Claim subprocess exited early: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for claim subprocesses: {paths}")
        time.sleep(0.01)


@contextmanager
def competing_claims(tmp_path, workflow_id):
    coordination = tmp_path / "claims"
    coordination.mkdir()
    processes = []
    try:
        for process_number in range(2):
            processes.append(subprocess.Popen(
                [
                    sys.executable, "-c", _CLAIM_PROCESS,
                    str(trace_store.TRACES_DIR), workflow_id,
                    str(coordination), str(process_number),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ))
        _wait_for_files([coordination / f"ready-{index}" for index in range(2)], processes)
        (coordination / "begin").touch()
        result_paths = [coordination / f"result-{index}.json" for index in range(2)]
        _wait_for_files(result_paths, processes)
        yield [json.loads(path.read_text()) for path in result_paths]
    finally:
        # Keep the winning owner alive until assertions finish: a dead owner is
        # intentionally recoverable and must not look like duplicate admission.
        (coordination / "release").touch()
        for process in processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def test_only_one_process_claims_the_same_confirmed_workflow(tmp_path, workflow):
    with competing_claims(tmp_path, workflow) as results:
        claimed = [result for result in results if result.get("claim_token")]
        assert len(claimed) == 1, results
        assert claimed[0]["status"] == "starting"
        assert claimed[0]["device_serial"] == "phone-a"
        assert claimed[0]["adb_endpoint_id"] == "endpoint-a"
        rejected = [result for result in results if not result.get("claim_token")]
        assert len(rejected) == 1
        assert rejected[0].get("status_code") == 409, results
        assert WorkflowStartStore().status(workflow)["status"] == "starting"
        assert not (Path(trace_store.TRACES_DIR) / workflow / "status.json").exists()

    recovered = WorkflowStartStore().status(workflow)
    assert recovered["status"] == "awaiting_device"
    assert recovered["device_serial"] is None
    assert "interrupted" in recovered["error"]
    assert WorkflowStartStore().claim(workflow)["status"] == "awaiting_device"


@pytest.mark.parametrize("operation", ["confirm", "claim", "failed"])
def test_lock_timeout_does_not_change_or_claim_confirmation(workflow, monkeypatch, operation):
    starts = WorkflowStartStore()
    token = starts.claim(workflow)["claim_token"] if operation == "failed" else None
    confirmation = Path(trace_store.TRACES_DIR) / workflow / "device_start.json"
    before = confirmation.read_bytes()
    monkeypatch.setattr(trace_store, "_LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(trace_store, "_LOCK_POLL_SECONDS", 0.005)

    with trace_store._status_lock(str(confirmation)) as acquired:
        assert acquired
        with pytest.raises(WorkflowStartError) as failure:
            if operation == "confirm":
                starts.confirm(workflow, "phone-b", "endpoint-b")
            elif operation == "claim":
                starts.claim(workflow)
            else:
                starts.failed(workflow, token, "Interrupted")
        assert failure.value.status_code == 503
        assert confirmation.read_bytes() == before

    assert starts.status(workflow)["status"] == ("starting" if token else "confirmed")


@pytest.mark.parametrize("replacement_claimed", [False, True])
def test_late_failure_cannot_revoke_a_new_confirmation_or_claim(workflow, replacement_claimed):
    starts = WorkflowStartStore()
    first = starts.claim(workflow)
    starts.failed(workflow, first["claim_token"], "First startup failed")
    starts.confirm(workflow, "phone-b", "endpoint-b")
    if replacement_claimed:
        replacement = starts.claim(workflow)
        assert replacement["claim_token"] != first["claim_token"]

    late = starts.failed(workflow, first["claim_token"], "Late first-startup failure")
    expected_status = "starting" if replacement_claimed else "confirmed"
    assert late["status"] == expected_status
    assert late["device_serial"] == "phone-b"
    assert late["adb_endpoint_id"] == "endpoint-b"
    assert late["error"] is None
    assert WorkflowStartStore().status(workflow) == late


def test_process_death_after_workflow_binding_allows_confirmation_again(workflow):
    subprocess.run(
        [
            sys.executable, "-c", """
import sys
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.device_execution.workflow_start import WorkflowStartStore
from artemis.runtime import trace_store

trace_store.TRACES_DIR, workflow_id = sys.argv[1:]
WorkflowStartStore().claim(workflow_id)
TestWorkflowStore().bind(workflow_id, session_id=workflow_id, device_serial="phone-a")
# Simulate process death after binding but before startup admission is committed.
""",
            str(trace_store.TRACES_DIR), workflow,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )

    starts = WorkflowStartStore()
    pending = starts.list_pending()
    assert len(pending) == 1
    assert pending[0]["workflow_id"] == workflow
    assert pending[0]["status"] == "awaiting_device"
    recovered = starts.status(workflow)
    assert recovered["status"] == "awaiting_device"
    assert recovered["device_serial"] is None
    assert TestWorkflowStore().planned(workflow)["session_id"] is None
    starts.confirm(workflow, "phone-b", "endpoint-b")
    assert starts.claim(workflow)["device_serial"] == "phone-b"
