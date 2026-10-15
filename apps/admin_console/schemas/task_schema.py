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

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class RunRequest(BaseModel):
    goal: str | None = None
    goals: list[str] | None = None
    profile: str | None = "flash"
    expected_output: str | None = None
    enable_outputter: bool | None = None
    # Pro-profile tuning (ignored by the Flash profile): a coarse Checker preset
    # ('off' | 'final' | 'checkpoints' | 'strict') and the Explorer perception
    # version used by the Operator ('flash' | 'pro' | 'ultra').
    verification_level: str | None = None
    explorer_mode: str | None = None
    locked_app_package: str | None = None
    app_path: str | None = None
    device_serial: str | None = None
    require_idle: bool = False
    expected_adb_endpoint_id: str | None = None
    ingress: str | None = "frontend"
    session_id: str | None = None
    conversation_id: str | None = None


class TaskBatchItemRequest(BaseModel):
    goal: str = Field(min_length=1)
    profile: Literal["flash", "pro"] = "flash"
    device_serial: str = Field(min_length=1)

    @field_validator("goal", "device_serial", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class TaskBatchRequest(BaseModel):
    batch_id: UUID
    expected_adb_endpoint_id: str = Field(min_length=1)
    items: list[TaskBatchItemRequest] = Field(min_length=1)

    @field_validator("expected_adb_endpoint_id", mode="before")
    @classmethod
    def strip_endpoint(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def unique_devices(self):
        serials = [item.device_serial for item in self.items]
        if len(serials) != len(set(serials)):
            raise ValueError("Each task in a batch must select a different device")
        return self


class ReplayRequest(BaseModel):
    device_id: str
    user_submits: dict
    tool_name: str = "ask_explorer"
    replay_id: str | None = None


class StopRequest(BaseModel):
    session_id: str | None = None
    device_id: str | None = None
    all: bool = False
