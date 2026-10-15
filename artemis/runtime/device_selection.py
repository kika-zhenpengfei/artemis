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

"""Cross-process execution-device preference, scoped to one ADB server."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from artemis.config.paths import get_temp_dir
from artemis.runtime.adb_endpoint import AdbEndpoint, current_adb_endpoint
from artemis.runtime.device_pool import DeviceStatus, device_pool
from artemis.runtime.trace_store import _atomic_write_json


class DeviceSelectionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def check_adb_endpoint(expected_id: str | None = None) -> AdbEndpoint:
    endpoint = current_adb_endpoint()
    if expected_id is not None and endpoint.identity != expected_id:
        raise DeviceSelectionError(
            "The ADB server changed. Refresh the device list and select a device again."
        )
    return endpoint


def _selection_path(endpoint: AdbEndpoint) -> Path:
    key = hashlib.sha256(endpoint.identity.encode("utf-8")).hexdigest()
    return get_temp_dir("device-selection") / f"{key}.json"


def get_selected_serial(endpoint: AdbEndpoint | None = None) -> str | None:
    endpoint = endpoint or current_adb_endpoint()
    try:
        data = json.loads(_selection_path(endpoint).read_text(encoding="utf-8"))
        serial = data["serial"]
        if data["endpoint"] != endpoint.identity or (
            serial is not None and (not isinstance(serial, str) or not serial.strip())
        ):
            raise ValueError("Invalid device selection")
        return serial
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Corrupt preferences must not silently enable automatic allocation.
        raise DeviceSelectionError(
            "Could not read the selected device. Select a device again.", 503
        ) from exc


def save_selected_serial(serial: str | None, endpoint: AdbEndpoint | None = None) -> None:
    endpoint = endpoint or current_adb_endpoint()
    if serial is not None:
        serial = serial.strip()
        if not serial:
            raise DeviceSelectionError("Device serial cannot be empty.")
    try:
        _atomic_write_json(
            str(_selection_path(endpoint)), {"endpoint": endpoint.identity, "serial": serial}
        )
    except OSError as exc:
        raise DeviceSelectionError("Could not save the selected device.", 503) from exc


def validate_idle_device(serial: str, devices: list[DeviceStatus] | None) -> None:
    if devices is None:
        raise DeviceSelectionError(
            "Could not list Android devices. Refresh the device list and try again.", 503
        )
    device = next((item for item in devices if item.serial == serial), None)
    if device is None:
        raise DeviceSelectionError(
            f"Selected Android device '{serial}' is not connected. Select a device again."
        )
    if device.state != "device":
        raise DeviceSelectionError(
            f"Selected Android device '{serial}' is not ready (state: '{device.state}')."
        )
    if device.is_busy or device.queued_count:
        raise DeviceSelectionError(
            f"Selected Android device '{serial}' is busy or reserved by another task."
        )


def _requested_selection(
    serial: str | None = None, *, require_idle: bool = False, endpoint: AdbEndpoint | None = None
) -> tuple[str | None, bool]:
    selected = get_selected_serial(endpoint) if serial is None else None
    resolved = serial if serial is not None else selected
    require_idle = require_idle or selected is not None
    if resolved is not None:
        resolved = resolved.strip()
        if not resolved:
            raise DeviceSelectionError("Device serial cannot be empty.")
    if require_idle:
        if resolved is None:
            raise DeviceSelectionError("Select an idle Android device before starting a test.")
    return resolved, require_idle


async def resolve_device_selection(
    serial: str | None = None,
    *,
    require_idle: bool = False,
    expected_adb_endpoint_id: str | None = None,
) -> tuple[str | None, bool]:
    endpoint = check_adb_endpoint(expected_adb_endpoint_id)
    resolved, require_idle = _requested_selection(
        serial, require_idle=require_idle, endpoint=endpoint
    )
    if require_idle and resolved is not None:
        validate_idle_device(resolved, await device_pool.try_list_devices_async())
    check_adb_endpoint(endpoint.identity)
    return resolved, require_idle


def resolve_device_selection_sync(
    serial: str | None = None, *, require_idle: bool = False
) -> tuple[str | None, bool]:
    endpoint = check_adb_endpoint()
    resolved, require_idle = _requested_selection(
        serial, require_idle=require_idle, endpoint=endpoint
    )
    if require_idle and resolved is not None:
        validate_idle_device(resolved, device_pool.try_list_devices())
    check_adb_endpoint(endpoint.identity)
    return resolved, require_idle
