"""Validated, deterministic steps for external-agent hybrid test runs."""

from __future__ import annotations

from typing import Any


_TARGET_ACTIONS = {"click", "long_press", "input_text"}
_ACTIONS = _TARGET_ACTIONS | {"press_key", "wait_for_delay", "manage_app"}
_LOCATOR_FIELDS = {"resource_id", "text", "class"}
_ARGUMENTS = {
    "click": {"times", "delay_ms"},
    "long_press": {"duration_ms"},
    "input_text": {"text", "clear_exist"},
    "press_key": {"key"},
    "wait_for_delay": {"time_in_ms"},
    "manage_app": {"action", "app_name"},
}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def locator(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value or set(value) - _LOCATOR_FIELDS:
        raise ValueError(f"{name} must contain resource_id, text, or class")
    result = {key: _text(item, f"{name}.{key}") for key, item in value.items()}
    if set(result) == {"class"}:
        raise ValueError(f"{name} needs resource_id or text to identify a control")
    return result


def condition(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"state", "locator"}:
        raise ValueError(f"{name} must have state and locator")
    state = value["state"]
    if state not in {"visible", "absent"}:
        raise ValueError(f"{name}.state must be visible or absent")
    return {"state": state, "locator": locator(value["locator"], f"{name}.locator")}


def matching_index(elements: list[dict[str, Any]], target: dict[str, str]) -> int | None:
    matches = [
        index
        for index, element in enumerate(elements, start=1)
        if all(str(element.get(key) or "") == value for key, value in target.items())
    ]
    return matches[0] if len(matches) == 1 else None


def condition_met(elements: list[dict[str, Any]], expected: dict[str, Any]) -> bool:
    matches = [
        element
        for element in elements
        if all(str(element.get(key) or "") == value for key, value in expected["locator"].items())
    ]
    return bool(matches) if expected["state"] == "visible" else not matches


def validate_case_plan(case: dict[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"steps", "final_assertion"}:
        raise ValueError("case execution plan needs steps and final_assertion")
    steps = value["steps"]
    if not isinstance(steps, list) or not steps or len(steps) > 50:
        raise ValueError("execution steps must contain 1 to 50 entries")
    known_checkpoints = {item["id"] for item in case["checkpoints"]}
    assigned: set[str] = set()
    clean_steps = []
    for index, step in enumerate(steps):
        name = f"steps[{index}]"
        if not isinstance(step, dict):
            raise ValueError(f"{name} must be an object")
        kind = step.get("kind")
        checkpoint_id = step.get("checkpoint_id")
        if checkpoint_id is not None:
            if checkpoint_id not in known_checkpoints or checkpoint_id in assigned:
                raise ValueError(f"{name}.checkpoint_id must identify an unused case checkpoint")
            assigned.add(checkpoint_id)
        if kind == "auto":
            if set(step) - {
                "kind",
                "action",
                "locator",
                "arguments",
                "expect",
                "timeout_ms",
                "checkpoint_id",
            }:
                raise ValueError(f"{name} contains unsupported fields")
            action = _text(step.get("action"), f"{name}.action")
            if action not in _ACTIONS:
                raise ValueError(f"{name}.action is not supported by fixed execution")
            arguments = step.get("arguments", {})
            if not isinstance(arguments, dict) or "target" in arguments:
                raise ValueError(f"{name}.arguments must be an object without target")
            if set(arguments) - _ARGUMENTS[action]:
                raise ValueError(f"{name}.arguments contains unsupported fields")
            if action == "click":
                times = arguments.get("times", 1)
                delay = arguments.get("delay_ms", 100)
                if type(times) is not int or not 1 <= times <= 2:
                    raise ValueError(f"{name}.arguments.times must be 1 or 2")
                if type(delay) is not int or not 0 <= delay <= 1000:
                    raise ValueError(f"{name}.arguments.delay_ms must be between 0 and 1000")
            if action == "long_press":
                duration = arguments.get("duration_ms", 1000)
                if type(duration) is not int or not 100 <= duration <= 3000:
                    raise ValueError(f"{name}.arguments.duration_ms must be between 100 and 3000")
            if action == "wait_for_delay":
                delay = arguments.get("time_in_ms", 1000)
                if type(delay) is not int or not 0 <= delay <= 10000:
                    raise ValueError(f"{name}.time_in_ms must be between 0 and 10000")
            if action == "manage_app":
                if arguments.get("action", "launch") not in {"launch", "stop"}:
                    raise ValueError(f"{name}.arguments.action must be launch or stop")
                _text(arguments.get("app_name"), f"{name}.arguments.app_name")
            target = (
                locator(step.get("locator"), f"{name}.locator")
                if action in _TARGET_ACTIONS
                else None
            )
            if action not in _TARGET_ACTIONS and "locator" in step:
                raise ValueError(f"{name}.locator is only valid for targeted actions")
            if action == "input_text":
                entered = _text(arguments.get("text"), f"{name}.arguments.text")
                if len(entered) > 500:
                    raise ValueError(f"{name}.arguments.text must be at most 500 characters")
            if action == "press_key":
                _text(arguments.get("key", "BACK"), f"{name}.arguments.key")
            timeout = step.get("timeout_ms", 3000)
            if type(timeout) is not int or not 0 <= timeout <= 15000:
                raise ValueError(f"{name}.timeout_ms must be between 0 and 15000")
            clean_steps.append(
                {
                    "kind": "auto",
                    "action": action,
                    "locator": target,
                    "arguments": arguments,
                    "expect": condition(step.get("expect"), f"{name}.expect"),
                    "timeout_ms": timeout,
                    "checkpoint_id": checkpoint_id,
                }
            )
        elif kind == "model":
            if set(step) - {
                "kind",
                "goal",
                "if_visible",
                "resume_when",
                "max_actions",
                "checkpoint_id",
            }:
                raise ValueError(f"{name} contains unsupported fields")
            max_actions = step.get("max_actions", 4)
            if type(max_actions) is not int or not 1 <= max_actions <= 10:
                raise ValueError(f"{name}.max_actions must be between 1 and 10")
            if checkpoint_id and "if_visible" in step:
                raise ValueError(f"{name}.if_visible cannot skip a required Pro checkpoint")
            resume_when = condition(step.get("resume_when"), f"{name}.resume_when")
            marker = (
                locator(step["if_visible"], f"{name}.if_visible") if "if_visible" in step else None
            )
            if marker and resume_when == {"state": "visible", "locator": marker}:
                raise ValueError(f"{name}.resume_when must differ from the trigger")
            clean_steps.append(
                {
                    "kind": "model",
                    "goal": _text(step.get("goal"), f"{name}.goal"),
                    "if_visible": marker,
                    "resume_when": resume_when,
                    "max_actions": max_actions,
                    "checkpoint_id": checkpoint_id,
                }
            )
        else:
            raise ValueError(f"{name}.kind must be auto or model")
    if assigned != known_checkpoints:
        raise ValueError("every Pro checkpoint must be assigned to an execution step")
    return {
        "steps": clean_steps,
        "final_assertion": condition(value["final_assertion"], "final_assertion"),
    }
