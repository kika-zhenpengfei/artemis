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

"""Read package identity from an uploaded Android APK."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_BADGING_PACKAGE = re.compile(r"^package: name='([^']+)'", re.MULTILINE)


def find_aapt() -> str | None:
    found = shutil.which("aapt")
    if found:
        return found
    roots = [os.environ.get(name) for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT")]
    adb = shutil.which("adb")
    if adb:
        roots.append(str(Path(adb).resolve().parent.parent))
    for root in roots:
        if not root:
            continue
        candidates = sorted((Path(root) / "build-tools").glob("*/aapt"), reverse=True)
        if candidates:
            return str(candidates[0])
    return None


def parse_badging_package(output: str) -> str:
    match = _BADGING_PACKAGE.search(output)
    if not match or not _PACKAGE.fullmatch(match.group(1)):
        raise ValueError("Could not read a valid Android package name from the APK")
    return match.group(1)


def read_apk_package_name(apk_path: Path) -> str:
    aapt = find_aapt()
    if not aapt:
        raise ValueError("Android SDK aapt is required to read the APK package name")
    try:
        result = subprocess.run(
            [aapt, "dump", "badging", str(apk_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Could not read the APK package name with aapt") from exc
    if result.returncode != 0:
        raise ValueError("Could not read the APK package name with aapt")
    return parse_badging_package(result.stdout)
