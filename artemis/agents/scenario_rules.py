"""Shared, selectively rendered guidance for recurring mobile test scenes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from artemis.resources import get_bundled_config_path


_SOURCE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "scenario_prompts.json"


def load_scenario_config() -> dict[str, Any]:
    """Read the public prompt file, or a caller-selected override."""
    override = os.environ.get("ARTEMIS_SCENARIO_PROMPTS_FILE")
    if override:
        path = Path(override)
    elif _SOURCE_CONFIG.is_file():
        path = _SOURCE_CONFIG
    else:
        path = get_bundled_config_path("scenario_prompts.json")
    if path is None:
        raise FileNotFoundError("config/scenario_prompts.json is missing")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError(f"Invalid scenario prompt config: {path}")
    limit = config.get("max_same_screen_attempts")
    if type(limit) is not int or limit < 1:
        raise ValueError(f"Invalid max_same_screen_attempts in {path}")
    rules = config.get("scenarios")
    if not isinstance(rules, list):
        raise ValueError(f"Invalid scenarios in {path}")
    titles = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError(f"Invalid scenario in {path}")
        for field in ("title", "when"):
            if not isinstance(rule.get(field), str) or not rule[field].strip():
                raise ValueError(f"Invalid {field} in {path}")
        if rule["title"] in titles:
            raise ValueError(f"Duplicate scenario title in {path}: {rule['title']}")
        titles.add(rule["title"])
        instructions = rule.get("instructions")
        if not isinstance(instructions, list) or not instructions or not all(
            isinstance(value, str) and value.strip() for value in instructions
        ):
            raise ValueError(f"Invalid instructions in {path}: {rule['title']}")
    return config


def scenario_guidance(goal: str, screen_text: str = "") -> dict[str, Any]:
    """Ask the current agent to select relevant rules from the public catalog."""
    config = load_scenario_config()
    rules = config["scenarios"]
    lines = [
        "--- ARTEMIS 场景处理候选规则 ---",
        "请结合测试目标、当前截图和界面元素，自行判断是否适用下列场景。",
        "这些规则只是候选项，不代表场景已命中；只执行适用的处理，无法判断时先观察并记录证据。",
    ]
    for rule in rules:
        lines.append(f"{rule['title']}：适用条件：{rule['when']}")
        lines.extend(f"- {instruction}" for instruction in rule["instructions"])
    return {
        "rule_ids": [rule["title"] for rule in rules],
        "prompt": "\n".join(lines),
    }


def screen_fingerprint(
    *, screen_text: str | None = None, screenshot_bytes: bytes | None = None
) -> str | None:
    """Use all available observations so visual changes count as progress."""
    if not screen_text and not screenshot_bytes:
        return None
    digest = hashlib.sha256()
    if screen_text:
        digest.update(screen_text.encode("utf-8"))
    if screenshot_bytes:
        digest.update(screenshot_bytes)
    return digest.hexdigest()


class NoProgressGuard:
    """Stop a third identical action against an unchanged observed screen."""

    def __init__(self, max_same_screen_attempts: int | None = None):
        limit = (
            load_scenario_config()["max_same_screen_attempts"]
            if max_same_screen_attempts is None
            else max_same_screen_attempts
        )
        if type(limit) is not int or limit < 1:
            raise ValueError("max_same_screen_attempts must be a positive integer")
        self.limit = limit
        self._last_key: tuple[str, str, str] | None = None
        self._attempts = 0

    def check(
        self, action: str, arguments: dict[str, Any], fingerprint: str | None
    ) -> str | None:
        if not fingerprint:
            self._last_key = None
            self._attempts = 0
            return None
        key = (action, json.dumps(arguments, sort_keys=True, default=str), fingerprint)
        if key != self._last_key:
            self._last_key = key
            self._attempts = 1
            return None
        if self._attempts >= self.limit:
            return (
                f"No progress: {self.limit} identical {action} actions on the same screen. "
                "Re-observe, choose a different strategy, or report the blocked precondition."
            )
        self._attempts += 1
        return None
