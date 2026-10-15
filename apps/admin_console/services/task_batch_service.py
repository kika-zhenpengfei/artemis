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

"""Persist batch admission once; derive execution results from existing tasks."""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
import json
import logging
import os
from pathlib import Path
import time
from typing import Any
from uuid import UUID, uuid5

from fastapi import HTTPException

from artemis.runtime import DeviceExecutionLock, trace_store
from artemis.runtime.process_probe import pid_is_alive
from artemis.runtime.task_batch_store import (
    TaskBatchHistoryError as TaskBatchError,
    active_batches as _active_batches,
    batch_history_lock,
    manifest_fingerprint,
    request_fingerprint,
)

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.schemas.task_schema import RunRequest, TaskBatchRequest
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.schemas.task_schema import RunRequest, TaskBatchRequest

logger = logging.getLogger(__name__)
_TERMINAL = {"completed", "failed", "cancelled"}


class TaskBatchService:
    def __init__(self):
        self._submissions: set[asyncio.Task] = set()

    @staticmethod
    def _directory() -> Path:
        return Path(trace_store.TRACES_DIR) / "_task_batches"

    def _path(self, batch_id: str) -> Path:
        try:
            canonical = str(UUID(batch_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise TaskBatchError("batch_id must be a UUID", 422) from exc
        return self._directory() / f"{canonical}.json"

    def _read(self, batch_id: str) -> dict[str, Any]:
        try:
            data = json.loads(self._path(batch_id).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or (
                not data.get("deleted") and not isinstance(data.get("items"), list)
            ):
                raise ValueError("Invalid batch manifest")
            return data
        except FileNotFoundError as exc:
            raise TaskBatchError("Task batch does not exist", 404) from exc
        except (OSError, ValueError) as exc:
            raise TaskBatchError("Could not read task batch", 503) from exc

    def _save(self, manifest: dict[str, Any]) -> None:
        try:
            trace_store._atomic_write_json(str(self._path(manifest["batch_id"])), manifest)
        except OSError as exc:
            raise TaskBatchError("Could not save task batch", 503) from exc

    def _claim(self, request: TaskBatchRequest) -> tuple[dict[str, Any], bool]:
        payload = request.model_dump(mode="json")
        path = self._path(str(request.batch_id))
        # This lock covers only the durable admission record, never a device probe.
        with batch_history_lock(Path(trace_store.TRACES_DIR)):
            if path.exists():
                existing = self._read(str(request.batch_id))
                if manifest_fingerprint(existing) != request_fingerprint(payload):
                    raise TaskBatchError("This batch_id was already used for different tasks", 409)
                if existing.get("deleted"):
                    raise TaskBatchError(
                        "This task batch history was deleted. Start a new batch instead.", 410
                    )
                return existing, False
            now = time.time()
            manifest = {
                "batch_id": str(request.batch_id),
                "request": payload,
                "created_at": now,
                "updated_at": now,
                "expected_adb_endpoint_id": request.expected_adb_endpoint_id,
                "owner_pid": os.getpid(),
                "owner_created_at": trace_store._own_create_time(),
                "submission_complete": False,
                "items": [
                    {
                        "index": index,
                        "session_id": str(uuid5(request.batch_id, f"task:{index}")),
                        **item.model_dump(),
                        "admission_status": "pending",
                        "error": None,
                    }
                    for index, item in enumerate(request.items)
                ],
            }
            self._save(manifest)
            _active_batches.add(str(path))
            return manifest, True

    def _save_progress(self, manifest: dict[str, Any]) -> None:
        with batch_history_lock(Path(trace_store.TRACES_DIR)):
            current = self._read(manifest["batch_id"])
            if current.get("deleted"):
                return
            updates = {item["session_id"]: item for item in manifest["items"]}
            current["items"] = [updates.get(item["session_id"], item) for item in current["items"]]
            current["submission_complete"] = manifest["submission_complete"]
            current["updated_at"] = manifest["updated_at"]
            self._save(current)

    async def submit(
        self, request: TaskBatchRequest, run: Callable[[RunRequest], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        # Protect the initial durable claim too: a disconnect can happen during fsync.
        task = asyncio.create_task(self._submit(request, run))
        self._submissions.add(task)
        task.add_done_callback(self._submission_finished)
        return await asyncio.shield(task)

    async def _submit(self, request, run) -> dict[str, Any]:
        manifest, created = await asyncio.to_thread(self._claim, request)
        if created:
            key = str(self._path(manifest["batch_id"]))
            try:
                await self._dispatch(manifest, run)
            finally:
                _active_batches.discard(key)
        return await asyncio.to_thread(self.get, str(request.batch_id))

    def _submission_finished(self, task: asyncio.Task) -> None:
        self._submissions.discard(task)
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            logger.error(
                "Task batch admission failed", exc_info=(type(exc), exc, exc.__traceback__)
            )

    async def _dispatch(self, manifest, run) -> None:
        save_lock = asyncio.Lock()

        async def submit_item(item):
            admission, error = "unknown", None
            try:
                response = await run(
                    RunRequest(
                        goal=item["goal"],
                        profile=item["profile"],
                        device_serial=item["device_serial"],
                        require_idle=True,
                        expected_adb_endpoint_id=manifest["expected_adb_endpoint_id"],
                        session_id=item["session_id"],
                        ingress="frontend",
                    )
                )
                if response.get("status") == "rejected":
                    admission, error = "rejected", response.get("error") or "Task was rejected"
                elif any(
                    task.get("session_id") == item["session_id"]
                    for task in response.get("tasks", [])
                ):
                    admission = "accepted"
                else:
                    error = "Task admission could not be confirmed; this item will not be retried automatically"
            except HTTPException as exc:
                admission, error = "rejected", str(exc.detail)
            except Exception as exc:
                logger.exception(
                    "Could not confirm batch task admission for %s", item["session_id"]
                )
                error = str(exc) or "Task admission could not be confirmed"
            async with save_lock:
                item.update(admission_status=admission, error=error)
                manifest["updated_at"] = time.time()
                await asyncio.to_thread(self._save_progress, manifest)

        results = await asyncio.gather(
            *(submit_item(item) for item in manifest["items"]), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        manifest["submission_complete"] = True
        manifest["updated_at"] = time.time()
        await asyncio.to_thread(self._save_progress, manifest)

    @staticmethod
    def _item_view(item, queued, active, submitting, paused):
        result = {
            **item,
            "status": "unknown",
            "result": None,
            "verification_status": "not_reported",
        }
        if item["admission_status"] == "rejected":
            result["status"] = "rejected"
            return result
        sid = item["session_id"]
        session = session_repo.get_session_by_id(sid) or {}
        trace = trace_store.read_status(sid) or {}
        session_status = session.get("status")
        trace_status = trace.get("status")
        session_status = "completed" if session_status == "success" else session_status
        trace_status = "completed" if trace_status == "success" else trace_status
        if session_status in _TERMINAL:
            result["status"] = session_status
        elif trace_status in _TERMINAL:
            result["status"] = trace_status
        elif session_status == "paused" or trace_status == "paused" or queued.get(sid) == "paused":
            result["status"] = "paused"
        elif sid in active or any(
            record.get("status") == "running" and session_repo.process_is_alive(record.get("pid"))
            for record in (session, trace)
        ):
            result["status"] = "running"
        elif sid in queued:
            result["status"] = "queued"
        if paused and result["status"] in {"queued", "running"}:
            result["status"] = "paused"
        if result["status"] != "unknown":
            result["admission_status"] = "accepted"
        elif item["admission_status"] == "pending" and not submitting:
            result["admission_status"] = "unknown"
        result["result"] = trace.get("result")
        result["error"] = trace.get("error") or item.get("error")
        return result

    def _view(self, manifest):
        queue = list(state.queue_items) + DeviceExecutionLock.get_queued_tasks()
        paused = state.is_paused
        queued = {
            str(item.get("session_id")): item.get("status")
            for item in queue
            if item.get("status") in {"pending", "queued", "paused"}
        }
        active = {str(item.get("session_id")) for item in queue if item.get("status") == "running"}
        active.update(str(sid) for sid in list(state.active_runs))
        active.update(
            str(owner.session_id) for owner in DeviceExecutionLock.get_active_owners().values()
        )
        submitting = not manifest["submission_complete"] and pid_is_alive(
            manifest["owner_pid"], manifest["owner_created_at"] or None
        )
        if manifest["owner_pid"] == os.getpid():
            submitting = submitting and str(self._path(manifest["batch_id"])) in _active_batches
        items = [
            self._item_view(item, queued, active, submitting, paused) for item in manifest["items"]
        ]
        admissions = Counter(item["admission_status"] for item in items)
        counts = Counter(item["status"] for item in items)
        submission_status = (
            "submitting"
            if submitting
            else "unknown"
            if admissions["pending"] or admissions["unknown"]
            else "accepted"
            if admissions["accepted"] == len(items)
            else "rejected"
            if admissions["rejected"] == len(items)
            else "partial"
        )
        status = (
            "submitting"
            if submitting
            else "running"
            if counts["running"]
            else "queued"
            if counts["queued"]
            else "paused"
            if counts["paused"]
            else "unknown"
            if counts["unknown"]
            else "finished"
        )
        return {
            **{
                key: manifest[key]
                for key in ("batch_id", "created_at", "updated_at", "expected_adb_endpoint_id")
            },
            "submission_status": submission_status,
            "status": status,
            "items": items,
            "summary": {
                "total": len(items),
                "accepted": admissions["accepted"],
                "rejected": admissions["rejected"],
                "pending": admissions["pending"],
                **{
                    key: counts[key]
                    for key in (
                        "queued",
                        "running",
                        "paused",
                        "completed",
                        "failed",
                        "cancelled",
                        "unknown",
                    )
                },
            },
        }

    def get(self, batch_id: str) -> dict[str, Any]:
        try:
            manifest = self._read(batch_id)
            if manifest.get("deleted"):
                raise TaskBatchError("This task batch history was deleted", 410)
            return self._view(manifest)
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskBatchError("Invalid task batch manifest", 503) from exc

    def list(self) -> list[dict[str, Any]]:
        batches = []
        for path in self._directory().glob("*.json"):
            try:
                batches.append(self.get(path.stem))
            except TaskBatchError as exc:
                if exc.status_code != 410:
                    raise
        return sorted(batches, key=lambda batch: batch["created_at"], reverse=True)


task_batch_service = TaskBatchService()
