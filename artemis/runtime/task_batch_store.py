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

"""Shared batch history mutation boundary for storage and task admission."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time

from artemis.runtime import trace_store
from artemis.runtime.process_probe import pid_is_alive

active_batches: set[str] = set()


class TaskBatchHistoryError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def request_fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def manifest_fingerprint(manifest: dict) -> str:
    return manifest.get("request_fingerprint") or request_fingerprint(manifest["request"])


@contextmanager
def batch_history_lock(root: Path):
    with trace_store._status_lock(str(root / "_task_batches" / ".history")) as acquired:
        if not acquired:
            raise TaskBatchHistoryError("Task batch history is being updated; try again", 503)
        yield


def is_submitting(manifest: dict, path: Path) -> bool:
    if manifest.get("submission_complete", True):
        return False
    owner = manifest.get("owner_pid")
    if owner == os.getpid():
        return str(path) in active_batches
    return bool(owner) and pid_is_alive(owner, manifest.get("owner_created_at") or None)


@contextmanager
def delete_batch_history(root: Path, session_id: str | None = None):
    """Prune batch views before data deletion, keeping fingerprints against replay."""
    with batch_history_lock(root):
        affected = []
        try:
            for path in (root / "_task_batches").glob("*.json"):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if manifest.get("deleted"):
                    continue
                items = manifest["items"]
                if session_id is not None and not any(
                    item["session_id"] == session_id for item in items
                ):
                    continue
                if is_submitting(manifest, path):
                    raise TaskBatchHistoryError(
                        "Task batch submission is still in progress. Wait for it to finish before deleting history.",
                        409,
                    )
                remaining = (
                    [item for item in items if item["session_id"] != session_id]
                    if session_id
                    else []
                )
                fingerprint = manifest_fingerprint(manifest)
                if remaining:
                    manifest.pop("request", None)
                    manifest.update(
                        request_fingerprint=fingerprint, items=remaining, updated_at=time.time()
                    )
                else:
                    manifest = {
                        "batch_id": manifest["batch_id"],
                        "request_fingerprint": fingerprint,
                        "deleted": True,
                    }
                affected.append((path, manifest))
            # Admission and progress writes use this same lock. A crash after
            # pruning can leave session data, but cannot make a retry execute it again.
            for path, manifest in affected:
                trace_store._atomic_write_json(str(path), manifest)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TaskBatchHistoryError("Could not update task batch history", 503) from exc
        yield
