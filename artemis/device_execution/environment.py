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

"""Device-side preparation and preflight for black-box test cases."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import re
from typing import Any, Protocol

from artemis.device_execution.apk_metadata import find_aapt, parse_badging_package


_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_SETTING = re.compile(r"^[A-Za-z0-9_.-]+$")
_WRITABLE_SETTINGS = {
    ("global", "auto_time"),
    ("global", "auto_time_zone"),
    ("system", "time_12_24"),
    ("system", "screen_off_timeout"),
    ("system", "accelerometer_rotation"),
    ("system", "user_rotation"),
    ("system", "screen_brightness"),
}


def validate_environment(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {
        "package_name",
        "reset",
        "checks",
        "apk_path",
        "set_settings",
    }:
        raise ValueError("case.environment has unsupported fields")
    package = value.get("package_name")
    if not isinstance(package, str) or not _PACKAGE.fullmatch(package):
        raise ValueError("case.environment.package_name must be an Android package name")
    reset = value.get("reset", "none")
    if reset not in {"none", "clear_data", "reinstall"}:
        raise ValueError("case.environment.reset must be 'none', 'clear_data', or 'reinstall'")
    apk_path = value.get("apk_path")
    if reset == "reinstall":
        if not isinstance(apk_path, str) or not apk_path.strip():
            raise ValueError("case.environment.apk_path is required for reinstall")
        apk = Path(apk_path).expanduser().resolve(strict=True)
        if not apk.is_file() or apk.suffix.lower() != ".apk":
            raise ValueError("case.environment.apk_path must name an APK file")
        apk_path = str(apk)
        with apk.open("rb") as stream:
            apk_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    elif apk_path is not None:
        raise ValueError("case.environment.apk_path requires reset='reinstall'")
    else:
        apk_sha256 = None
    checks = value.get("checks", [])
    if not isinstance(checks, list):
        raise ValueError("case.environment.checks must be a list")
    clean_checks = []
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("each environment check must be an object")
        kind = check.get("kind")
        if kind == "setting_equals" and set(check) == {"kind", "namespace", "key", "value"}:
            if check["namespace"] not in {"system", "secure", "global"}:
                raise ValueError("setting namespace must be system, secure, or global")
            if not isinstance(check["key"], str) or not _SETTING.fullmatch(check["key"]):
                raise ValueError("setting key is invalid")
            if not isinstance(check["value"], str):
                raise ValueError("setting value must be a string")
        elif kind == "device_time_between" and set(check) == {"kind", "start", "end"}:
            try:
                start = datetime.fromisoformat(check["start"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(check["end"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("device time window must use ISO 8601 timestamps") from exc
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("device time window needs ordered timezone-aware timestamps")
        else:
            raise ValueError("unsupported environment check")
        clean_checks.append(dict(check))
    settings = value.get("set_settings", [])
    if not isinstance(settings, list):
        raise ValueError("case.environment.set_settings must be a list")
    clean_settings = []
    seen = set()
    for setting in settings:
        if not isinstance(setting, dict) or set(setting) != {"namespace", "key", "value"}:
            raise ValueError("each set_settings entry needs namespace, key, and value")
        identity = (setting["namespace"], setting["key"])
        if identity not in _WRITABLE_SETTINGS or identity in seen:
            raise ValueError("setting is not permitted or is duplicated")
        if not isinstance(setting["value"], str):
            raise ValueError("setting value must be a string")
        seen.add(identity)
        clean_settings.append(dict(setting))
    return {
        "package_name": package,
        "reset": reset,
        "checks": clean_checks,
        "apk_path": apk_path,
        "apk_sha256": apk_sha256,
        "set_settings": clean_settings,
    }


class EnvironmentAdapter(Protocol):
    async def run(self, serial: str, *args: str) -> str: ...


class AdbEnvironmentAdapter:
    async def run(self, serial: str, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "adb",
            "-s",
            serial,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError(f"ADB operation timed out: {args[0]}")
        result = stdout.decode(errors="replace").strip()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or result)
        return result


class DeviceEnvironment:
    def __init__(self, adapter: EnvironmentAdapter | None = None):
        self._adapter = adapter or AdbEnvironmentAdapter()

    async def _check_apk_package(self, apk_path: str, package: str) -> str:
        aapt = find_aapt()
        if not aapt:
            raise RuntimeError("aapt is required to verify the APK package before reinstall")
        process = await asyncio.create_subprocess_exec(
            aapt,
            "dump",
            "badging",
            apk_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("aapt timed out while checking the APK")
        if process.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "aapt failed")
        try:
            actual_package = parse_badging_package(stdout.decode(errors="replace"))
        except ValueError as exc:
            raise RuntimeError("Could not read the APK package name") from exc
        if actual_package != package:
            raise RuntimeError("APK package does not match case.environment.package_name")
        return actual_package

    async def prepare(self, serial: str, spec: dict[str, Any]) -> dict[str, Any]:
        package = spec["package_name"]
        evidence = []
        originals = []
        try:
            if spec["reset"] == "reinstall":
                with Path(spec["apk_path"]).open("rb") as stream:
                    current_sha256 = await asyncio.to_thread(
                        lambda: hashlib.file_digest(stream, "sha256").hexdigest()
                    )
                if current_sha256 != spec["apk_sha256"]:
                    raise RuntimeError("APK changed after the test plan was created")
                evidence.append(f"APK SHA-256: {current_sha256}")
                verified_package = await self._check_apk_package(spec["apk_path"], package)
                evidence.append(f"APK package verified: {verified_package}")
            installed = await self._adapter.run(serial, "shell", "pm", "path", package)
            if not installed.startswith("package:") and spec["reset"] != "reinstall":
                return {
                    "status": "blocked",
                    "reason": f"{package} is not installed",
                    "evidence": evidence,
                }
            if installed.startswith("package:"):
                evidence.append(f"Installed package: {package}; {installed.splitlines()[0]}")
            for check in spec["checks"]:
                if check["kind"] == "setting_equals":
                    actual = await self._adapter.run(
                        serial, "shell", "settings", "get", check["namespace"], check["key"]
                    )
                    evidence.append(f"{check['namespace']}.{check['key']}={actual}")
                    if actual != check["value"]:
                        return {
                            "status": "blocked",
                            "reason": f"System setting {check['key']} is {actual!r}, expected {check['value']!r}",
                            "evidence": evidence,
                        }
                else:
                    actual = await self._adapter.run(serial, "shell", "date", "+%s")
                    instant = datetime.fromtimestamp(int(actual), UTC)
                    start = datetime.fromisoformat(check["start"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(check["end"].replace("Z", "+00:00"))
                    evidence.append(f"Device time: {instant.isoformat()}")
                    if not start <= instant <= end:
                        return {
                            "status": "blocked",
                            "reason": "Device time is outside the required window",
                            "evidence": evidence,
                        }
            for setting in spec.get("set_settings", []):
                previous = await self._adapter.run(
                    serial, "shell", "settings", "get", setting["namespace"], setting["key"]
                )
                originals.append(
                    {"namespace": setting["namespace"], "key": setting["key"], "value": previous}
                )
            if spec["reset"] == "reinstall":
                installed_output = await self._adapter.run(
                    serial, "install", "-r", spec["apk_path"]
                )
                if "Success" not in installed_output:
                    raise RuntimeError(f"APK install returned: {installed_output}")
                evidence.append(f"APK installed: {spec['apk_path']}")
                installed = await self._adapter.run(serial, "shell", "pm", "path", package)
                if not installed.startswith("package:"):
                    raise RuntimeError(f"{package} was not found after APK installation")
            if spec["reset"] in {"clear_data", "reinstall"}:
                cleared = await self._adapter.run(serial, "shell", "pm", "clear", package)
                if cleared.strip() != "Success":
                    raise RuntimeError(f"pm clear returned: {cleared}")
                evidence.append(f"App data cleared: {package}")
            for setting in spec.get("set_settings", []):
                await self._adapter.run(
                    serial,
                    "shell",
                    "settings",
                    "put",
                    setting["namespace"],
                    setting["key"],
                    setting["value"],
                )
                actual = await self._adapter.run(
                    serial, "shell", "settings", "get", setting["namespace"], setting["key"]
                )
                if actual != setting["value"]:
                    raise RuntimeError(f"Could not set {setting['namespace']}.{setting['key']}")
                evidence.append(
                    f"Temporary setting: {setting['namespace']}.{setting['key']}={actual}"
                )
            return {"status": "ready", "reason": None, "evidence": evidence, "restore": originals}
        except (OSError, RuntimeError, ValueError) as exc:
            if originals:
                restoration = await self.restore(serial, originals)
                evidence.extend(restoration["evidence"])
            return {"status": "blocked", "reason": str(exc), "evidence": evidence}

    async def restore(self, serial: str, originals: list[dict[str, str]]) -> dict[str, Any]:
        evidence = []
        errors = []
        for setting in reversed(originals):
            operation = "delete" if setting["value"] == "null" else "put"
            command = (
                ("shell", "settings", operation, setting["namespace"], setting["key"])
                if operation == "delete"
                else (
                    "shell",
                    "settings",
                    operation,
                    setting["namespace"],
                    setting["key"],
                    setting["value"],
                )
            )
            try:
                await self._adapter.run(serial, *command)
                actual = await self._adapter.run(
                    serial, "shell", "settings", "get", setting["namespace"], setting["key"]
                )
                if actual != setting["value"]:
                    raise RuntimeError(f"Restored value is {actual!r}")
                evidence.append(f"Restored {setting['namespace']}.{setting['key']}={actual}")
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{setting['namespace']}.{setting['key']}: {exc}")
        return {
            "status": "restored" if not errors else "failed",
            "evidence": evidence,
            "errors": errors,
        }
