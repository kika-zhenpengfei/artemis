"""Shared guidance and progress limits for recurring mobile test scenes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.operator.prompts import ObservationPromptComponent, PromptBuilder
from artemis.agents import scenario_rules
from artemis.agents.scenario_rules import (
    NoProgressGuard,
    load_scenario_config,
    scenario_guidance,
    screen_fingerprint,
)
from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.agents.validator.execution_loop import _process_action
from artemis.device_execution.test_workflow import TestWorkflowStore
from artemis.graph.state import State
from artemis.runtime import trace_store


def test_guidance_offers_scenes_for_the_agent_to_select():
    ad = scenario_guidance("Open the app", "跳过  广告")
    assert ad["rule_ids"] == ["开屏广告", "插屏或激励视频广告", "键盘推荐词"]
    assert "只执行适用的处理" in ad["prompt"]
    assert "跳过" in ad["prompt"]
    assert "全屏广告" in ad["prompt"]

    keyboard = scenario_guidance("测试键盘推荐词", "")
    assert keyboard["rule_ids"] == ad["rule_ids"]
    assert "input_text" in keyboard["prompt"]
    assert "点击目标键盘" in keyboard["prompt"]

    unrelated = scenario_guidance("Open Settings", "Wi-Fi")
    assert unrelated["rule_ids"] == ad["rule_ids"]
    assert "候选项，不代表场景已命中" in unrelated["prompt"]


def test_public_config_and_bundled_fallback(tmp_path, monkeypatch):
    assert scenario_rules._SOURCE_CONFIG.name == "scenario_prompts.json"
    assert scenario_rules._SOURCE_CONFIG.parent.name == "config"
    assert load_scenario_config()["scenarios"][0]["title"] == "开屏广告"

    bundled = tmp_path / "scenario_prompts.json"
    bundled.write_text(
        scenario_rules._SOURCE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(scenario_rules, "_SOURCE_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(scenario_rules, "get_bundled_config_path", lambda _name: bundled)
    assert load_scenario_config()["scenarios"][0]["title"] == "开屏广告"


def test_no_progress_guard_blocks_third_identical_action_on_same_screen():
    guard = NoProgressGuard(max_same_screen_attempts=2)
    action = {"target": [500, 300]}

    assert guard.check("click", action, "screen-a") is None
    assert guard.check("click", action, "screen-a") is None
    assert "same screen" in guard.check("click", action, "screen-a")
    assert guard.check("click", action, "screen-b") is None
    assert guard.check("click", {"target": [300, 300]}, "screen-b") is None


def test_no_progress_guard_needs_an_observed_screen():
    guard = NoProgressGuard(max_same_screen_attempts=2)
    for _ in range(3):
        assert guard.check("click", {"target": [500, 300]}, None) is None


def test_visual_change_counts_as_progress_with_unchanged_hierarchy():
    before = screen_fingerprint(screen_text="same UI", screenshot_bytes=b"before")
    after = screen_fingerprint(screen_text="same UI", screenshot_bytes=b"after")
    assert before != after


def test_invalid_scenario_file_reports_the_bad_field(tmp_path, monkeypatch):
    config_path = tmp_path / "scenarios.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "max_same_screen_attempts": 2,
                "scenarios": [
                    {
                        "title": "错误规则",
                        "when": "",
                        "instructions": ["执行操作"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARTEMIS_SCENARIO_PROMPTS_FILE", str(config_path))

    with pytest.raises(ValueError, match="Invalid when"):
        load_scenario_config()


def test_scenario_rule_ids_are_recorded_in_test_report(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    (tmp_path / "source.kt").write_text("class Example\n", encoding="utf-8")
    store = TestWorkflowStore()
    context = store.create(
        goal="测试键盘推荐词",
        repository_root=str(tmp_path),
        requirements=[
            {
                "id": "R1",
                "description": "Suggestions appear after typing",
                "code_refs": [
                    {"path": "source.kt", "line": 1, "reason": "Keyboard behavior"}
                ],
            }
        ],
    )
    workflow_id = context["workflow_id"]
    store.set_cases(
        workflow_id,
        profile="flash",
        cases=[
            {
                "id": "C1",
                "title": "Suggestion",
                "requirement_ids": ["R1"],
                "steps": ["Tap keys"],
                "expected_result": "Suggestion visible",
            }
        ],
    )
    store.record_scenario_rules(workflow_id, ["开屏广告", "键盘推荐词"])
    report = store.report(workflow_id)

    assert report["scenario_rule_ids"] == ["开屏广告", "插屏或激励视频广告", "键盘推荐词"]
    assert "Candidate scenario rules: 开屏广告, 插屏或激励视频广告, 键盘推荐词" in (
        tmp_path / "traces" / workflow_id / "notes" / "test_report.md"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_flash_and_pro_observations_use_the_same_keyboard_rule():
    state = State(initial_goal="测试键盘推荐词")
    builder = PromptBuilder()
    await ObservationPromptComponent()(
        builder,
        state,
        Mock(),
        latest_screenshot_b64="",
        minimal_list="[1] 试用键盘",
    )
    assert "键盘推荐词" in str(builder.human_parts)
    assert builder.ephemeral_indices

    runner = object.__new__(FlashRunner)
    runner.goal = state.initial_goal
    ledger = Mock()
    ledger.elapsed_label.return_value = "T+00:01"
    tail = runner._build_tail(ledger, 1, None, "[1] 试用键盘")
    assert "键盘推荐词" in str(tail.content)


@pytest.mark.asyncio
async def test_pro_validator_rejects_third_unchanged_tap():
    node = SimpleNamespace(
        _validate_action_precondition_pixel=AsyncMock(
            return_value=(True, ValidationErrorCategory.NONE, "")
        ),
        _exec_action=AsyncMock(return_value=(True, "")),
    )
    session = SimpleNamespace(screenshot_b64=AsyncMock(return_value="c2NyZWVu"))
    state = State(initial_goal="Open app")
    action = {"action": "tap", "coordinates": [100, 200]}

    first = await _process_action(node, state, session, action, "tap", "c2NyZWVu", burst=False)
    second = await _process_action(node, state, session, action, "tap", "c2NyZWVu", burst=False)
    blocked = await _process_action(node, state, session, action, "tap", "c2NyZWVu", burst=False)

    assert first.success and second.success
    assert not blocked.success
    assert "No progress" in blocked.error_msg
    assert node._exec_action.await_count == 2
