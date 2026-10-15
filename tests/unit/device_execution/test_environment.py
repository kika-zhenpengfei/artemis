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

"""Black-box case preparation should stop before changing an invalid device state."""

import asyncio
import pytest

from artemis.device_execution.environment import DeviceEnvironment, validate_environment


class FakeAdb:
    def __init__(self, *, setting: str = "1"):
        self.commands = []
        self.setting = setting

    async def run(self, serial: str, *args: str) -> str:
        self.commands.append((serial, *args))
        if args[:3] == ("shell", "pm", "path"):
            return "package:/data/app/example/base.apk"
        if args[:3] == ("shell", "settings", "get"):
            return self.setting
        if args[:3] == ("shell", "settings", "put"):
            self.setting = args[5]
            return ""
        if args[:3] == ("shell", "settings", "delete"):
            self.setting = "null"
            return ""
        if args[:3] == ("shell", "pm", "clear"):
            return "Success"
        raise AssertionError(args)


def test_setting_blocker_prevents_data_reset():
    adapter = FakeAdb(setting="0")
    spec = validate_environment(
        {
            "package_name": "com.example.app",
            "reset": "clear_data",
            "checks": [
                {"kind": "setting_equals", "namespace": "global", "key": "auto_time", "value": "1"}
            ],
        }
    )
    result = asyncio.run(DeviceEnvironment(adapter).prepare("device-1", spec))
    assert result["status"] == "blocked"
    assert "auto_time" in result["reason"]
    assert not any(command[1:4] == ("shell", "pm", "clear") for command in adapter.commands)


def test_ready_environment_resets_data_and_retains_evidence():
    adapter = FakeAdb()
    spec = validate_environment({"package_name": "com.example.app", "reset": "clear_data"})
    result = asyncio.run(DeviceEnvironment(adapter).prepare("device-1", spec))
    assert result["status"] == "ready"
    assert any("App data cleared" in item for item in result["evidence"])
    assert adapter.commands[-1] == ("device-1", "shell", "pm", "clear", "com.example.app")


def test_temporary_setting_is_restored_after_case():
    adapter = FakeAdb(setting="1")
    spec = validate_environment(
        {
            "package_name": "com.example.app",
            "set_settings": [{"namespace": "global", "key": "auto_time", "value": "0"}],
        }
    )

    async def scenario():
        environment = DeviceEnvironment(adapter)
        prepared = await environment.prepare("device-1", spec)
        assert prepared["status"] == "ready"
        assert adapter.setting == "0"
        restored = await environment.restore("device-1", prepared["restore"])
        assert restored["status"] == "restored"
        assert adapter.setting == "1"

    asyncio.run(scenario())


def test_reinstall_requires_verified_apk(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"APK")
    spec = validate_environment(
        {"package_name": "com.example.app", "reset": "reinstall", "apk_path": str(apk)}
    )
    adapter = FakeAdb()
    environment = DeviceEnvironment(adapter)

    async def mismatched(_path: str, _package: str) -> str:
        raise RuntimeError("APK package does not match")

    environment._check_apk_package = mismatched
    result = asyncio.run(environment.prepare("device-1", spec))
    assert result["status"] == "blocked"
    assert adapter.commands == []


def test_reinstall_rejects_apk_changed_after_planning(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"before")
    spec = validate_environment(
        {"package_name": "com.example.app", "reset": "reinstall", "apk_path": str(apk)}
    )
    apk.write_bytes(b"after")
    adapter = FakeAdb()
    result = asyncio.run(DeviceEnvironment(adapter).prepare("device-1", spec))
    assert result["status"] == "blocked"
    assert "APK changed" in result["reason"]
    assert adapter.commands == []


def test_invalid_environment_spec_is_rejected():
    with pytest.raises(ValueError, match="Android package"):
        validate_environment({"package_name": "bad package", "reset": "clear_data"})
    with pytest.raises(ValueError, match="ordered timezone-aware"):
        validate_environment(
            {
                "package_name": "com.example.app",
                "checks": [
                    {
                        "kind": "device_time_between",
                        "start": "2026-01-02T00:00:00Z",
                        "end": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        )
