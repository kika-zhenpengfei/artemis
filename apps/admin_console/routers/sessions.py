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

import asyncio
import base64
import json
import time
from uuid import UUID
from typing import Any
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from artemis.config import DB_PATH, TRACES_PATH
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.device_execution.test_intake import TestIntakeStore, browse_repository_directory
from artemis.device_execution.workflow_runner import PreparedWorkflowRunner
from artemis.device_execution.workflow_start import WorkflowStartError, WorkflowStartStore
from artemis.runtime import trace_store
from artemis.runtime.task_batch_store import TaskBatchHistoryError
from artemis.runtime.device_selection import DeviceSelectionError, check_adb_endpoint, resolve_device_selection

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.database.repositories.trace_repository import trace_repo
    from admin_console.services.media_service import media_service
    from admin_console.services.model_service import model_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.database.repositories.trace_repository import trace_repo
    from apps.admin_console.services.media_service import media_service
    from apps.admin_console.services.model_service import model_service


router = APIRouter(tags=["sessions"])
_prepared_runs: dict[str, asyncio.Task] = {}
_prepared_runners: dict[str, PreparedWorkflowRunner] = {}


class PreparedRunRequest(BaseModel):
    device_serial: str | None = None
    require_idle: bool = False
    expected_adb_endpoint_id: str | None = None


class WorkflowDeviceRequest(BaseModel):
    device_serial: str = Field(min_length=1)
    expected_adb_endpoint_id: str = Field(min_length=1)


class PreparedControlRequest(BaseModel):
    operation: str
    action: str | None = None
    arguments: dict[str, Any] | None = None
    observation_id: str | None = None
    case_id: str | None = None
    checkpoint_id: str | None = None
    assertion: str | None = None
    status: str | None = None
    evidence: str | None = None
    resume_token: str | None = None
    settle_ms: int = 0
    wait_for_change: bool = False
    timeout_ms: int = 15000
    poll_interval_ms: int = 1000


@router.get("/api/source_repositories/browse")
async def browse_source_repositories(request: Request, path: str | None = None):
    """Browse directories on the same local machine as the WebUI server."""
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Source directory browsing is local only")
    try:
        return await asyncio.to_thread(browse_repository_directory, path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/test_intakes")
async def create_test_intake(
    goal: str = Form(...),
    repository_root: str = Form(...),
    apk: UploadFile = File(...),
):
    """Store one APK and the exact local source checkout for Codex planning."""
    try:
        return await asyncio.to_thread(
            TestIntakeStore().create,
            goal=goal, repository_root=repository_root,
            apk=apk.file,
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await apk.close()


@router.get("/api/test_intakes")
async def list_test_intakes():
    return await asyncio.to_thread(TestIntakeStore().list)


@router.get("/api/test_intakes/{intake_id}")
async def get_test_intake(intake_id: str):
    try:
        return await asyncio.to_thread(TestIntakeStore().read, intake_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/test_workflows/prepared")
async def list_prepared_test_workflows():
    """Plans are visible before they acquire a device session."""
    return await asyncio.to_thread(TestWorkflowStore().list_prepared)


@router.get("/api/test_workflows/waiting")
async def list_waiting_test_workflows():
    """Expose prepared workflows independently of running device sessions."""
    return {"workflows": await asyncio.to_thread(WorkflowStartStore().list_pending)}


@router.post("/api/test_workflows/{workflow_id}/confirm_device")
async def confirm_test_workflow_device(workflow_id: str, request: WorkflowDeviceRequest):
    """Confirm this workflow's device; its waiting Codex process starts execution."""
    store = WorkflowStartStore()
    try:
        await asyncio.to_thread(store.status, workflow_id)
        endpoint = check_adb_endpoint(request.expected_adb_endpoint_id)
        device_serial, _ = await resolve_device_selection(
            request.device_serial, require_idle=True,
            expected_adb_endpoint_id=endpoint.identity,
        )
        return await asyncio.to_thread(
            store.confirm, workflow_id, device_serial, endpoint.identity,
        )
    except (DeviceSelectionError, WorkflowStartError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/test_workflows/{workflow_id}/run")
async def run_prepared_test_workflow(workflow_id: str, request: PreparedRunRequest):
    """Launch a source-linked fixed plan; the existing report endpoint tracks progress."""
    active = _prepared_runs.get(workflow_id)
    if active and not active.done():
        raise HTTPException(status_code=409, detail="This test workflow is already running")
    if workflow_id in _prepared_runners:
        raise HTTPException(status_code=409, detail="This test workflow is waiting for Codex")
    runner = PreparedWorkflowRunner()
    try:
        runner.validate(workflow_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        start = await asyncio.to_thread(WorkflowStartStore().status, workflow_id)
        if start["status"] == "awaiting_device":
            return start
        if start["status"] != "confirmed":
            raise WorkflowStartError("This test workflow is already starting or running")
        if request.device_serial and request.device_serial != start["device_serial"]:
            raise WorkflowStartError("Device must match this workflow's WebUI confirmation")
        endpoint = check_adb_endpoint(start["adb_endpoint_id"])
        check_adb_endpoint(request.expected_adb_endpoint_id)
        device_serial, require_idle = await resolve_device_selection(
            start["device_serial"], require_idle=True,
            expected_adb_endpoint_id=endpoint.identity,
        )
    except (DeviceSelectionError, WorkflowStartError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Device enumeration yields; another request may have started this workflow.
    active = _prepared_runs.get(workflow_id)
    if active and not active.done():
        raise HTTPException(status_code=409, detail="This test workflow is already running")
    if workflow_id in _prepared_runners:
        raise HTTPException(status_code=409, detail="This test workflow is waiting for Codex")

    async def execute():
        try:
            result = await runner.run(
                workflow_id, device_serial=device_serial,
                **({"require_idle": True} if require_idle else {}),
                **({"expected_adb_endpoint_id": endpoint.identity} if require_idle or request.expected_adb_endpoint_id else {}),
            )
        except Exception as exc:
            result = {"workflow_id": workflow_id, "status": "failed", "error": str(exc)}
        if result["status"] != "handoff":
            _prepared_runners.pop(workflow_id, None)
        return result

    _prepared_runners[workflow_id] = runner
    _prepared_runs[workflow_id] = asyncio.create_task(execute())
    return {"workflow_id": workflow_id, "status": "starting"}


@router.get("/api/test_workflows/{workflow_id}/run_status")
async def prepared_test_workflow_run_status(workflow_id: str):
    task = _prepared_runs.get(workflow_id)
    if task is None:
        return {"workflow_id": workflow_id, "status": "not_started"}
    if not task.done():
        return {"workflow_id": workflow_id, "status": "running"}
    if task.cancelled():
        return {"workflow_id": workflow_id, "status": "cancelled"}
    result = task.result()
    if result.get("status") == "handoff":
        runner = _prepared_runners.get(workflow_id)
        if runner:
            lease = await runner.manager.status(workflow_id)
            if lease["status"] == "closed":
                _prepared_runners.pop(workflow_id, None)
                return {
                    "workflow_id": workflow_id,
                    "status": "failed",
                    "error": "Codex handoff expired because its device session closed.",
                }
    return result


@router.post("/api/test_workflows/{workflow_id}/control")
async def control_prepared_test_workflow(
    workflow_id: str, command: PreparedControlRequest, request: Request
):
    """Local MCP bridge to the device lease retained during a Codex handoff."""
    if request.client and request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Codex handoff control is local only")
    runner = _prepared_runners.get(workflow_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="WebUI test workflow is not active")
    task = _prepared_runs.get(workflow_id)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Fixed plan execution is still running")
    if runner.active_case_id is None:
        raise HTTPException(status_code=409, detail="No Codex handoff is active")
    if command.case_id and command.case_id != runner.active_case_id:
        raise HTTPException(status_code=422, detail="case_id must match the active handoff")
    operation = command.operation.strip().lower()
    async with runner.control_lock:
        active_task = _prepared_runs.get(workflow_id)
        if active_task and not active_task.done():
            raise HTTPException(status_code=409, detail="Fixed plan execution is still running")
        if runner.active_case_id is None:
            raise HTTPException(status_code=409, detail="No Codex handoff is active")
        try:
            if operation == "observe":
                result, image = await runner.manager.observe(
                    workflow_id, settle_ms=command.settle_ms,
                    wait_for_change=command.wait_for_change,
                    timeout_ms=command.timeout_ms,
                    poll_interval_ms=command.poll_interval_ms,
                )
            elif operation == "act":
                if not command.action:
                    raise ValueError("action is required")
                result, image = await runner.manager.act(
                    workflow_id, action=command.action, arguments=command.arguments,
                    observation_id=command.observation_id, case_id=runner.active_case_id,
                )
            elif operation == "verify":
                if not command.assertion or not command.status or not command.evidence:
                    raise ValueError("assertion, status, and evidence are required")
                result = await runner.manager.verify(
                    workflow_id, assertion=command.assertion, status=command.status,
                    evidence=command.evidence, observation_id=command.observation_id,
                    case_id=runner.active_case_id, checkpoint_id=command.checkpoint_id,
                )
                image = None
            elif operation == "resume":
                if not command.resume_token:
                    raise ValueError("resume_token is required")

                async def continue_run():
                    async with runner.control_lock:
                        try:
                            resumed = await runner.resume(workflow_id, command.resume_token)
                        except Exception as exc:
                            resumed = {
                                "workflow_id": workflow_id, "status": "failed", "error": str(exc)
                            }
                    if resumed["status"] != "handoff":
                        _prepared_runners.pop(workflow_id, None)
                    return resumed

                _prepared_runs[workflow_id] = asyncio.create_task(continue_run())
                return {"workflow_id": workflow_id, "status": "starting"}
            elif operation == "close":
                result = await runner.close(workflow_id, status=command.status or "cancelled")
                _prepared_runners.pop(workflow_id, None)
                _prepared_runs[workflow_id] = asyncio.create_task(asyncio.sleep(0, result=result))
                image = None
            else:
                raise ValueError("operation must be observe, act, verify, resume, or close")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if image:
        result["image_base64"] = base64.b64encode(image).decode("ascii")
    return result


@router.get("/api/sessions")
async def list_sessions():
    # The body does blocking work (sqlite queries, filesystem scans, ffmpeg
    # conversion on first sight of a new recording), so run it off the event
    # loop — the frontend polls this endpoint and it must not stall other
    # requests.
    try:
        return await asyncio.to_thread(_list_sessions_sync)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _list_sessions_sync():
    rows = session_repo.get_all_sessions()
    video_rec_map = session_repo.get_video_recordings_map()
    latest_recordings = session_repo.get_latest_video_recordings_map()
    agent_names_by_session = session_repo.get_agent_trace_names_map()
    video_idx = media_service.build_video_index()
    default_model_info = model_service.get_active_model_info()

    orphaned_ids = []
    unresolved_profiles = []
    result = []

    try:
        from artemis.runtime import DeviceExecutionLock

        active_owners = DeviceExecutionLock.get_active_owners()
        active_owner_sids = {
            str(owner.session_id) for owner in active_owners.values() if owner.session_id
        }
    except Exception:
        active_owner_sids = set()

    for row_dict in rows:
        s_id = str(row_dict.get("session_id"))
        recording = latest_recordings.get(s_id)
        d_info_raw = row_dict.get("device_info")
        device_id = None
        if d_info_raw:
            try:
                d_info = json.loads(d_info_raw) if isinstance(d_info_raw, str) else d_info_raw
                if isinstance(d_info, dict):
                    device_id = d_info.get("device_id") or d_info.get("device_serial")
            except (ValueError, TypeError):
                # Malformed device_info JSON: fall back to the recording's device_id.
                pass
        if not device_id and recording:
            device_id = recording.get("device_id")
        row_dict["device_id"] = device_id
        row_dict["device_serial"] = device_id

        if row_dict.get("status") == "running":
            is_active = (
                s_id in active_owner_sids
                or s_id in state.active_connections
                or (state.is_running and s_id == str(state.active_session_id))
            )
            worker_is_alive = session_repo.process_is_alive(row_dict.get("pid"))
            if not is_active and not worker_is_alive:
                row_dict["status"] = "failed"
                row_dict["end_time"] = time.time()
                orphaned_ids.append(s_id)

        recording_status = str((recording or {}).get("status") or "unavailable")
        resolved_v_url = (
            media_service.resolve_video_url(row_dict, video_rec_map, video_idx)
            if recording_status != "recording"
            else None
        )
        if resolved_v_url:
            recording_status = "ready"
            row_dict["video_url"] = resolved_v_url
        else:
            row_dict["video_url"] = None
        row_dict["recording_status"] = recording_status

        agent_names = agent_names_by_session.get(s_id, [])
        sess_profile = model_service.resolve_session_profile(
            row_dict, None, state.current_profile, agent_names=agent_names
        )
        row_dict["model_info"] = (
            model_service.get_active_model_info(sess_profile)
            if sess_profile
            else default_model_info
        )
        if not sess_profile:
            unresolved_profiles.append(row_dict)
        result.append(row_dict)

    # Most sessions carry a profile in device_info or an agent trace name.
    # Only inspect the much larger LLM payload for legacy rows that remain
    # ambiguous after those cheap checks.
    if unresolved_profiles:
        unresolved_ids = [str(row.get("session_id")) for row in unresolved_profiles]
        llm_traces_by_session = session_repo.get_llm_traces_for_profiles_map(unresolved_ids)
        for row_dict in unresolved_profiles:
            s_id = str(row_dict.get("session_id"))
            llm_traces = llm_traces_by_session.get(s_id)
            if not llm_traces:
                continue
            sess_profile = model_service.resolve_session_profile(
                row_dict, llm_traces, state.current_profile
            )
            if sess_profile:
                row_dict["model_info"] = model_service.get_active_model_info(sess_profile)

    if orphaned_ids:
        try:
            harvested = session_repo.harvest_orphaned_sessions(orphaned_ids)
            print(f"[list_sessions] Auto-harvested {harvested} orphaned running session(s).")
            try:
                for o_id in orphaned_ids:
                    if trace_store.read_status(str(o_id)):
                        trace_store.update_trace_status(
                            str(o_id),
                            "failed",
                            error="Process terminated unexpectedly (auto-harvested).",
                        )
            except OSError as e:
                # read_status never raises; this guards the lock/write side
                # of update_trace_status.
                print(f"[list_sessions] Could not mark harvested traces failed: {e}")
        except Exception as e:
            print(f"[list_sessions] Failed to update orphaned sessions: {e}")

    return result


@router.get("/api/sessions/{session_id}")
async def get_session_details(session_id: str):
    """Retrieve details for a single automation session."""
    row = session_repo.get_session_by_id(session_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return dict(row)


@router.get("/api/sessions/{session_id}/test_workflow")
async def get_session_test_workflow(session_id: str):
    """Return source-linked cases and their current device verdicts."""
    try:
        return await asyncio.to_thread(TestWorkflowStore().snapshot, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/sessions/{session_id}/usage")
async def get_session_usage(session_id: str):
    """Session-wide LLM token totals, live executor context size and run tuning."""
    try:
        return await asyncio.to_thread(session_repo.get_session_usage, session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sessions/{session_id}/tree")
async def get_tree(session_id: str):
    try:
        return trace_repo.get_trace_tree(session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sessions/{session_id}/background_tasks")
async def get_session_background_tasks(session_id: str):
    try:
        return session_repo.get_background_tasks(session_id)
    except Exception:
        return []


@router.get("/api/sessions/{session_id}/startup_progress")
async def get_session_startup_progress(session_id: str):
    try:
        return state.get_startup_progress(session_id)
    except Exception:
        return []


@router.post("/api/cleanup")
async def cleanup_history_endpoint():
    try:
        from artemis.data_engine.storage import StorageManager

        _ensure_history_inactive()
        storage = StorageManager(DB_PATH, TRACES_PATH)
        storage.clear_all_data()
        return {
            "status": "success",
            "message": "History cleaned up successfully",
        }
    except (HTTPException, TaskBatchHistoryError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=getattr(exc, "detail", str(exc))) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/sessions/{session_id}/delete")
async def delete_session_endpoint(session_id: str):
    try:
        from artemis.data_engine.storage import StorageManager

        _ensure_history_inactive(session_id)
        storage = StorageManager(DB_PATH, TRACES_PATH)
        storage.delete_session(UUID(session_id))
        return {
            "status": "success",
            "message": f"Session {session_id} deleted successfully",
        }
    except (HTTPException, TaskBatchHistoryError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=getattr(exc, "detail", str(exc))) from exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _ensure_history_inactive(session_id: str | None = None) -> None:
    active = set(state.active_runs) | set(state.active_connections)
    active.update(
        str(item.get("session_id")) for item in state.queue_items
        if item.get("status") in {"pending", "queued", "running", "paused"}
    )
    blocked = bool(active) if session_id is None else session_id in active
    if blocked:
        raise HTTPException(status_code=409, detail="Stop the running or queued tasks before deleting their history.")
