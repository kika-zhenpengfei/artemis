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

"""Model Context Protocol (MCP) server command (artemis mcp)."""

import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import tomllib
from typing import Annotated, Protocol

from mcp_server.base import mcp as agent_mcp
import mcp_server.tools  # noqa: F401
from mcp_server.utils import env_utils
from artemis.mcp.adb_server import mcp as adb_mcp
from artemis.runtime import shutdown_awake_service, start_awake_service
from artemis.utils.logger import get_logger
from rich.console import Console
from rich.syntax import Syntax
import typer

logger = get_logger(__name__)
console = Console()

ARTEMIS_MCP_TOOLS = (
    "mobile_session",
    "mobile_observe",
    "mobile_prepare_case",
    "mobile_act",
    "mobile_run_plan",
    "mobile_verify",
    "mobile_webui_handoff",
    "mobile_test_context",
    "mobile_test_cases",
    "mobile_test_execution_plan",
    "mobile_test_report",
    "mobile_test_intakes",
    "mobile_run_task",
    "mobile_manage_task",
    "mobile_get_device_state",
    "mobile_inspect_trace",
    "mobile_diagnose",
)
ARTEMIS_EXECUTOR_TOOLS = tuple(
    name for name in ARTEMIS_MCP_TOOLS if name not in {"mobile_run_task", "mobile_manage_task"}
)

EXECUTION_MODES = frozenset({"executor", "autonomous"})


class DevelopmentAgentAdapter(Protocol):
    """Seam for development-agent-specific MCP configuration."""

    client_id: str
    aliases: tuple[str, ...]
    default_execution_mode: str

    def build_config(self, config_body: dict, config_with_cwd: dict) -> dict: ...


class CodexAgentAdapter:
    client_id = "codex"
    aliases = ("codex",)
    default_execution_mode = "executor"

    def build_config(self, config_body: dict, config_with_cwd: dict) -> dict:
        executor_only = config_body["env"]["ARTEMIS_EXECUTION_MODE"] == "executor"
        return {
            "mcp_servers": {
                "artemis": {
                    **config_with_cwd,
                    # Codex has no eager/deferred MCP setting. Requiring the
                    # server makes it initialize during host startup, while an
                    # explicit allow-list guarantees all Artemis tools are
                    # registered. The host may still present schemas through
                    # tool search to control context size.
                    "enabled": True,
                    "required": True,
                    "startup_timeout_sec": 120,
                    "tool_timeout_sec": 180,
                    "enabled_tools": list(
                        ARTEMIS_EXECUTOR_TOOLS if executor_only else ARTEMIS_MCP_TOOLS
                    ),
                }
            }
        }


class ZCodeAgentAdapter:
    client_id = "zcode"
    aliases = ("zcode", "z_code")
    default_execution_mode = "executor"

    def build_config(self, config_body: dict, config_with_cwd: dict) -> dict:
        # ZCode's native user config uses mcp.servers. PYTHONPATH and the
        # absolute interpreter path make cwd unnecessary and portable across
        # workspaces opened by the desktop app.
        return {
            "mcp": {
                "servers": {
                    "artemis": {
                        **config_body,
                        "enable": True,
                    }
                }
            }
        }


_DEVELOPMENT_AGENT_ADAPTERS: tuple[DevelopmentAgentAdapter, ...] = (
    CodexAgentAdapter(),
    ZCodeAgentAdapter(),
)
_DEVELOPMENT_AGENT_BY_ALIAS = {
    alias: adapter for adapter in _DEVELOPMENT_AGENT_ADAPTERS for alias in adapter.aliases
}


def _canonical_client(client: str) -> str:
    normalized = client.strip().lower().replace("-", "_")
    adapter = _DEVELOPMENT_AGENT_BY_ALIAS.get(normalized)
    return adapter.client_id if adapter else normalized


def _resolve_execution_mode(client: str, execution_mode: str | None) -> str:
    """Resolve execution mode independently from the selected client."""
    if execution_mode is not None:
        normalized_mode = execution_mode.strip().lower()
        if normalized_mode not in EXECUTION_MODES:
            raise ValueError("execution_mode must be 'executor' or 'autonomous'")
        return normalized_mode

    adapter = _DEVELOPMENT_AGENT_BY_ALIAS.get(client)
    return adapter.default_execution_mode if adapter else "autonomous"


def _eager_tools_config() -> dict[str, dict[str, bool]]:
    """Return the eager-loading extension used by clients that support it."""
    return {tool_name: {"eager": True} for tool_name in ARTEMIS_MCP_TOOLS}


def _get_vscode_user_dir() -> Path:
    """Returns the platform-specific VS Code user configuration directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Code" / "User"
    else:
        return Path.home() / ".config" / "Code" / "User"


def _get_config_snippet(
    client: str,
    python_exe: str,
    project_root: str,
    execution_mode: str | None = None,
) -> dict:
    """Generates MCP configuration dictionary for the specified client."""
    client = _canonical_client(client)
    resolved_mode = _resolve_execution_mode(client, execution_mode)
    executor_only = resolved_mode == "executor"
    server_env = {
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": project_root,
        "ARTEMIS_DESKTOP_NOTIFY": "true",
        "ARTEMIS_AGENT_CLIENT": client,
        "ARTEMIS_EXECUTION_MODE": resolved_mode,
        # Keep the old flag during migration for older server builds.
        "ARTEMIS_EXECUTOR_ONLY": "1" if executor_only else "0",
    }
    if executor_only:
        ui_port = os.environ.get("ARTEMIS_UI_PORT") or "8000"
        server_env.update(
            {
                "ARTEMIS_AUTO_START_UI": os.environ.get("ARTEMIS_AUTO_START_UI") or "true",
                "ARTEMIS_UI_PORT": ui_port,
                "ARTEMIS_DAEMON_PORT": os.environ.get("ARTEMIS_DAEMON_PORT") or ui_port,
                "ARTEMIS_AUTO_OPEN_BROWSER": os.environ.get("ARTEMIS_AUTO_OPEN_BROWSER") or "false",
            }
        )
        if os.environ.get("ARTEMIS_TRACES_DIR"):
            server_env["ARTEMIS_TRACES_DIR"] = os.environ["ARTEMIS_TRACES_DIR"]
        if client == "codex" and os.environ.get("PATH"):
            # Desktop apps may not inherit the shell paths used to install ADB.
            server_env["PATH"] = os.environ["PATH"]
    config_body = {
        "command": python_exe,
        "args": ["-m", "mcp_server"],
        "env": server_env,
    }
    config_with_cwd = {**config_body, "cwd": project_root}

    development_agent = _DEVELOPMENT_AGENT_BY_ALIAS.get(client)
    if development_agent:
        return development_agent.build_config(config_body, config_with_cwd)

    if client == "antigravity":
        return {
            "mcpServers": {
                "artemis": {
                    **config_with_cwd,
                    # This is the documented way to expose every tool. Avoid
                    # proprietary eager fields in the current config so a
                    # strict parser can always load the server.
                    "disabledTools": [],
                }
            }
        }
    elif client == "jetski":
        return {
            "mcpServers": {
                "artemis": {
                    **config_with_cwd,
                    "disabledTools": [],
                    # Retain the legacy Jetski eager extension only in its
                    # legacy config file. Current Antigravity gets the strict,
                    # documented configuration above as a safe fallback.
                    "tools": _eager_tools_config(),
                }
            }
        }
    elif client == "openclaw":
        return {
            "mcp": {
                "servers": {
                    "artemis": {
                        **config_with_cwd,
                        "enabled": True,
                        "connectionTimeoutMs": 120000,
                    }
                }
            }
        }
    elif client == "vscode":
        return {"servers": {"artemis": {**config_with_cwd, "type": "stdio"}}}
    elif client in ("claude", "claude_code", "claude_desktop"):
        return {"mcpServers": {"artemis": {**config_body, "type": "stdio"}}}
    elif client == "cline":
        return {
            "mcpServers": {
                "artemis": {
                    **config_body,
                    "disabled": False,
                    "autoApprove": [],
                    "timeout": 120,
                }
            }
        }
    elif client in ("roo", "roo_code"):
        return {
            "mcpServers": {
                "artemis": {
                    **config_body,
                    "disabled": False,
                    "alwaysAllow": [],
                    "timeout": 120,
                }
            }
        }
    else:  # cursor, windsurf, generic
        return {"mcpServers": {"artemis": config_body}}


def _parse_json_lenient(text: str) -> dict | None:
    """Parses JSON or JSONC (JSON with comments / trailing commas) safely.

    Returns ``None`` when the text cannot be parsed as a JSON object, so callers
    can tell a genuinely empty ``{}`` file apart from a corrupt one.
    """
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        # Not strict JSON; fall through to the lenient JSONC cleanup below.
        pass
    # Remove // single-line comments
    cleaned = re.sub(r"//.*", "", text)
    # Remove /* ... */ multi-line comments
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    # Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _merge_json_file(
    file_path: Path,
    server_name: str,
    server_config: dict,
    key_name: str | tuple[str, ...] = "mcpServers",
    remove_paths: tuple[tuple[str, ...], ...] = (),
    strict_parse: bool = False,
) -> bool:
    """Merges server configuration into a target JSON file without overwriting existing servers.

    With ``strict_parse=True``, an existing non-empty file that cannot be parsed
    aborts the merge instead of being replaced. Use this for files that hold
    state beyond MCP config (e.g. ~/.claude.json), where a degraded rewrite
    would destroy unrelated data.
    """
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if file_path.exists() and file_path.stat().st_size > 0:
            raw_text = file_path.read_text(encoding="utf-8")
            parsed = _parse_json_lenient(raw_text)
            data = parsed or {}
            if parsed is None and raw_text.strip():
                if strict_parse:
                    logger.warning(
                        f"Existing JSON in {file_path} could not be parsed; refusing to rewrite it. "
                        "Fix or remove the file, then re-run the install."
                    )
                    return False
                # Backup unparseable existing file to prevent accidental loss
                backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                try:
                    backup_path.write_text(raw_text, encoding="utf-8")
                    logger.warning(
                        f"Existing JSON in {file_path} could not be parsed; backup created at {backup_path}"
                    )
                except OSError as exc:
                    logger.warning(
                        f"Existing JSON in {file_path} could not be parsed AND the backup to"
                        f" {backup_path} failed ({exc}); refusing to rewrite it."
                        " Fix or remove the file, then re-run the install."
                    )
                    return False

        for remove_path in remove_paths:
            parent = data
            for part in remove_path[:-1]:
                child = parent.get(part)
                if not isinstance(child, dict):
                    parent = {}
                    break
                parent = child
            if parent:
                parent.pop(remove_path[-1], None)

        key_path = (key_name,) if isinstance(key_name, str) else key_name
        target = data
        for part in key_path:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]

        target[server_name] = server_config
        file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not update MCP config file {file_path}: {e}")
        return False


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    """Serialize MCP settings without discarding user-defined fields."""
    if isinstance(value, dict):
        pairs = (f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + ", ".join(pairs) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise ValueError(f"Unsupported Codex MCP setting type: {type(value).__name__}")


def _backup_file(file_path: Path) -> Path:
    """Keep each distinct pre-install version, including its file permissions."""
    original = file_path.read_bytes()
    backup_path = file_path.with_name(file_path.name + ".bak")
    suffix = 1
    while backup_path.exists():
        if backup_path.read_bytes() == original:
            return backup_path
        backup_path = file_path.with_name(f"{file_path.name}.bak.{suffix}")
        suffix += 1
    shutil.copy2(file_path, backup_path)
    return backup_path


def _codex_toml_block(server_config: dict) -> str:
    """Renders a managed Codex MCP server block using TOML-compatible JSON strings."""
    command = json.dumps(str(server_config["command"]), ensure_ascii=False)
    args = json.dumps(server_config.get("args", []), ensure_ascii=False)
    cwd = json.dumps(str(server_config["cwd"]), ensure_ascii=False)
    env = server_config.get("env", {})
    lines = [
        "# BEGIN ARTEMIS MCP CONFIG",
        "[mcp_servers.artemis]",
        f"command = {command}",
        f"args = {args}",
        f"cwd = {cwd}",
    ]
    for key in ("enabled", "required"):
        if key in server_config:
            lines.append(f"{key} = {'true' if server_config[key] else 'false'}")
    for key in ("startup_timeout_sec", "tool_timeout_sec"):
        if key in server_config:
            lines.append(f"{key} = {_toml_value(server_config[key])}")
    if "enabled_tools" in server_config:
        lines.append("enabled_tools = [")
        lines.extend(
            f"  {json.dumps(tool_name, ensure_ascii=False)},"
            for tool_name in server_config["enabled_tools"]
        )
        lines.append("]")
    managed_keys = {
        "command", "args", "cwd", "env", "enabled", "required",
        "startup_timeout_sec", "tool_timeout_sec", "enabled_tools",
    }
    for key, value in server_config.items():
        if key not in managed_keys:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    if env:
        lines.extend(["", "[mcp_servers.artemis.env]"])
        for key, value in env.items():
            lines.append(f"{_toml_key(key)} = {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("# END ARTEMIS MCP CONFIG")
    return "\n".join(lines) + "\n"


def _merge_codex_toml(file_path: Path, server_config: dict) -> bool:
    """Adds or updates Artemis in Codex config.toml while preserving unrelated settings."""
    try:
        existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
        # Parse before removing any old Artemis block: corrupt content must not
        # become apparently valid merely because the damaged lines were removed.
        existing_data = tomllib.loads(existing)
        old_server = existing_data.get("mcp_servers", {}).get("artemis", {})
        merged_server = {**old_server, **server_config}
        for key in ("startup_timeout_sec", "tool_timeout_sec"):
            if key in old_server:
                merged_server[key] = old_server[key]
        merged_env = {**server_config.get("env", {}), **old_server.get("env", {})}
        current_path = server_config.get("env", {}).get("PATH")
        if current_path:
            path_parts = current_path.split(os.pathsep)
            path_parts.extend(old_server.get("env", {}).get("PATH", "").split(os.pathsep))
            merged_env["PATH"] = os.pathsep.join(dict.fromkeys(part for part in path_parts if part))
        for key in (
            "PYTHONPATH", "PYTHONUNBUFFERED", "ARTEMIS_AGENT_CLIENT",
            "ARTEMIS_EXECUTION_MODE", "ARTEMIS_EXECUTOR_ONLY",
        ):
            if key in server_config.get("env", {}):
                merged_env[key] = server_config["env"][key]
        # Older configurations had only UI_PORT; keep the daemon on that port.
        if "ARTEMIS_UI_PORT" in merged_env and "ARTEMIS_DAEMON_PORT" not in old_server.get("env", {}):
            merged_env["ARTEMIS_DAEMON_PORT"] = merged_env["ARTEMIS_UI_PORT"]
        merged_server["env"] = merged_env
        managed_block = _codex_toml_block(merged_server)
        begin_marker = "# BEGIN ARTEMIS MCP CONFIG"
        end_marker = "# END ARTEMIS MCP CONFIG"

        if begin_marker in existing and end_marker in existing:
            managed_pattern = re.compile(
                rf"(?ms)^{re.escape(begin_marker)}\n.*?^{re.escape(end_marker)}\n?"
            )
            updated = managed_pattern.sub(lambda _match: managed_block, existing, count=1)
        else:
            # Remove an older, unmanaged Artemis table before adding the managed block.
            kept_lines: list[str] = []
            skipping_artemis = False
            header_pattern = re.compile(r"^\s*\[{1,2}\s*(.+?)\s*\]{1,2}\s*(?:#.*)?$")
            for line in existing.splitlines(keepends=True):
                header_match = header_pattern.match(line.rstrip("\r\n"))
                if header_match:
                    normalized = re.sub(r"[\s\"']", "", header_match.group(1))
                    skipping_artemis = normalized == "mcp_servers.artemis" or normalized.startswith(
                        "mcp_servers.artemis."
                    )
                if not skipping_artemis:
                    kept_lines.append(line)
            preserved = "".join(kept_lines).rstrip()
            updated = f"{preserved}\n\n{managed_block}" if preserved else managed_block

        expected = dict(existing_data)
        expected["mcp_servers"] = {**existing_data.get("mcp_servers", {}), "artemis": merged_server}
        # Text replacement cannot handle every valid TOML spelling. Reject any
        # merge that changes unrelated data instead of guessing at its structure.
        if tomllib.loads(updated) != expected:
            raise ValueError("Codex TOML cannot be safely merged; existing settings were preserved")
        if updated != existing:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if file_path.exists():
                _backup_file(file_path)
            file_path.write_text(updated, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not update Codex MCP config file {file_path}: {e}")
        return False


def _remove_rules_block(file_path: Path) -> bool:
    """Removes a previously injected managed ARTEMIS block from a rules file.

    Used to migrate clients that load both their main rules file and a
    standalone rule file (e.g. Claude Code loads ~/.claude/CLAUDE.md AND
    ~/.claude/rules/*.md), where keeping the injected block would duplicate
    the rules in the model's context. If the file contains nothing but the
    managed block, the file is deleted entirely.
    """
    try:
        if not file_path.exists():
            return True
        begin_marker = "<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->"
        end_marker = "<!-- END ARTEMIS MOBILE TESTING RULES -->"
        existing = file_path.read_text(encoding="utf-8")
        if begin_marker not in existing or end_marker not in existing:
            return True
        prefix = existing.split(begin_marker)[0]
        suffix = existing.split(end_marker)[1]
        remainder = f"{prefix.rstrip()}\n\n{suffix.lstrip()}".strip()
        if remainder:
            file_path.write_text(remainder + "\n", encoding="utf-8")
        else:
            file_path.unlink()
        return True
    except Exception as e:
        logger.warning(f"Could not remove rules block from {file_path}: {e}")
        return False


def _inject_rules_block(file_path: Path, content: str, *, backup: bool = False) -> bool:
    """Injects or updates a managed ARTEMIS block in a global rules file without overwriting user content."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        begin_marker = "<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->"
        end_marker = "<!-- END ARTEMIS MOBILE TESTING RULES -->"
        new_block = f"{begin_marker}\n{content.strip()}\n{end_marker}\n"

        existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""

        if begin_marker in existing and end_marker in existing:
            prefix = existing.split(begin_marker)[0]
            suffix = existing.split(end_marker)[1]
            updated = f"{prefix.rstrip()}\n\n{new_block}\n{suffix.lstrip()}".strip() + "\n"
        else:
            updated = f"{existing.rstrip()}\n\n{new_block}".strip() + "\n"

        if existing == updated:
            return True

        if backup and file_path.exists():
            _backup_file(file_path)
        file_path.write_text(updated, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not inject rules block into {file_path}: {e}")
        return False


def _write_rule_file(file_path: Path, content: str) -> bool:
    """Writes or overwrites a standalone rule file."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        new_text = content.strip() + "\n"
        if file_path.exists():
            try:
                if file_path.read_text(encoding="utf-8") == new_text:
                    return True
            except (OSError, UnicodeDecodeError):
                # Unreadable existing file: skip the no-op check and rewrite it.
                pass
        file_path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not write rule file {file_path}: {e}")
        return False


def _write_cursor_mdc(file_path: Path, content: str) -> bool:
    """Writes a Cursor .mdc rule file with required YAML frontmatter."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        mdc_header = (
            "---\n"
            "description: Artemis Mobile Testing Mindset & Rules\n"
            "globs: **/*\n"
            "alwaysApply: true\n"
            "---\n\n"
        )
        new_text = mdc_header + content.strip() + "\n"
        if file_path.exists():
            try:
                if file_path.read_text(encoding="utf-8") == new_text:
                    return True
            except (OSError, UnicodeDecodeError):
                # Unreadable existing file: skip the no-op check and rewrite it.
                pass
        file_path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not write Cursor rule file {file_path}: {e}")
        return False


def install_rules(client: str, project_root: str) -> list[str]:
    """Auto-installs ARTEMIS testing rules globally into user IDE rule directories across any OS."""
    client = _canonical_client(client)
    installed_paths: list[str] = []
    rules_src = Path(project_root) / "mcp_server" / "rules.md"
    if not rules_src.exists():
        alt_src = Path(__file__).resolve().parents[4] / "mcp_server" / "rules.md"
        if alt_src.exists():
            rules_src = alt_src
        else:
            logger.warning(f"Could not locate rules.md at {rules_src}")
            return []

    try:
        raw_rules = rules_src.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Could not read rules file {rules_src}: {e}")
        return []

    targets = (
        [
            "antigravity",
            "cursor",
            "claude",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "openclaw",
            "codex",
            "zcode",
        ]
        if client == "all"
        else [client]
    )

    for target in targets:
        if target in ("antigravity", "jetski"):
            gemini_md = Path.home() / ".gemini" / "GEMINI.md"
            global_rule = Path.home() / ".gemini" / "rules" / "artemis.md"
            if _inject_rules_block(gemini_md, raw_rules):
                installed_paths.append(str(gemini_md))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "cursor":
            cursor_rules_file = Path.home() / ".cursorrules"
            global_mdc = Path.home() / ".cursor" / "rules" / "artemis.mdc"
            if _inject_rules_block(cursor_rules_file, raw_rules):
                installed_paths.append(str(cursor_rules_file))
            if _write_cursor_mdc(global_mdc, raw_rules):
                installed_paths.append(str(global_mdc))
        elif target in ("claude", "claude_code"):
            # Claude Code loads BOTH ~/.claude/CLAUDE.md and ~/.claude/rules/*.md
            # into context every session, so install the rules in exactly one
            # place (the standalone rule file) and migrate away any block a
            # previous version injected into CLAUDE.md.
            claude_md = Path.home() / ".claude" / "CLAUDE.md"
            global_rule = Path.home() / ".claude" / "rules" / "artemis.md"
            _remove_rules_block(claude_md)
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "claude_desktop":
            # Claude Desktop only reads ~/.claude/CLAUDE.md (not rules/*.md), so
            # the rules block stays injected there for this target.
            claude_md = Path.home() / ".claude" / "CLAUDE.md"
            if _inject_rules_block(claude_md, raw_rules):
                installed_paths.append(str(claude_md))
        elif target == "windsurf":
            global_rule = Path.home() / ".codeium" / "windsurf" / "rules" / "artemis.md"
            global_mem = Path.home() / ".codeium" / "windsurf" / "memories" / "global_rules.md"
            if _inject_rules_block(global_mem, raw_rules):
                installed_paths.append(str(global_mem))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "vscode":
            global_rule = Path.home() / ".vscode" / "rules" / "artemis.md"
            user_rule = _get_vscode_user_dir() / "rules" / "artemis.md"
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
            if _write_rule_file(user_rule, raw_rules):
                installed_paths.append(str(user_rule))
        elif target == "cline":
            clinerules = Path.home() / ".clinerules"
            global_rule = Path.home() / ".cline" / "rules" / "artemis.md"
            if _inject_rules_block(clinerules, raw_rules):
                installed_paths.append(str(clinerules))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target in ("roo", "roo_code"):
            roorules = Path.home() / ".roorules"
            global_rule = Path.home() / ".roo" / "rules" / "artemis.md"
            if _inject_rules_block(roorules, raw_rules):
                installed_paths.append(str(roorules))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "openclaw":
            openclaw_md = Path.home() / ".openclaw" / "OPENCLAW.md"
            global_rule = Path.home() / ".openclaw" / "rules" / "artemis.md"
            if _inject_rules_block(openclaw_md, raw_rules):
                installed_paths.append(str(openclaw_md))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "codex":
            codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
            override_file = codex_home / "AGENTS.override.md"
            agents_file = (
                override_file
                if override_file.exists() and override_file.read_text(encoding="utf-8").strip()
                else codex_home / "AGENTS.md"
            )
            if not _inject_rules_block(agents_file, raw_rules, backup=True):
                raise RuntimeError(f"Could not install Codex rules at {agents_file}")
            installed_paths.append(str(agents_file))
        elif target == "zcode":
            agents_file = Path.home() / ".zcode" / "AGENTS.md"
            if _inject_rules_block(agents_file, raw_rules):
                installed_paths.append(str(agents_file))

    return installed_paths


def install_mcp_config(
    client: str,
    python_exe: str,
    project_root: str,
    execution_mode: str | None = None,
    *,
    include_rules: bool = True,
) -> list[str]:
    """Install MCP configuration, optionally including global testing rules."""
    client = _canonical_client(client)
    installed_paths: list[str] = []
    targets = (
        [
            "antigravity",
            "cursor",
            "claude",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "openclaw",
            "codex",
            "zcode",
        ]
        if client == "all"
        else [client]
    )

    for target in targets:
        snippet = _get_config_snippet(
            target,
            python_exe,
            project_root,
            execution_mode=execution_mode,
        )
        if target in ("antigravity", "jetski"):
            current_server_cfg = _get_config_snippet(
                "antigravity", python_exe, project_root, execution_mode
            )["mcpServers"]["artemis"]
            legacy_server_cfg = _get_config_snippet(
                "jetski", python_exe, project_root, execution_mode
            )["mcpServers"]["artemis"]
            jetski_path = Path.home() / ".gemini" / "jetski" / "mcp_config.json"
            antigravity_legacy_path = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
            config_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
            if _merge_json_file(jetski_path, "artemis", legacy_server_cfg):
                installed_paths.append(str(jetski_path))
            if _merge_json_file(antigravity_legacy_path, "artemis", current_server_cfg):
                installed_paths.append(str(antigravity_legacy_path))
            if _merge_json_file(config_path, "artemis", current_server_cfg):
                installed_paths.append(str(config_path))
        elif target in ("claude", "claude_code", "claude_desktop"):
            server_cfg = snippet["mcpServers"]["artemis"]
            if sys.platform == "darwin":
                claude_path = (
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "Claude"
                    / "claude_desktop_config.json"
                )
            elif sys.platform == "win32":
                appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
                claude_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
            else:
                claude_path = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
            if _merge_json_file(claude_path, "artemis", server_cfg):
                installed_paths.append(str(claude_path))

            # Also install to Claude Code CLI global config (~/.claude.json).
            # This file holds Claude Code state well beyond MCP config, so a
            # parse failure must abort the merge rather than rewrite the file.
            claude_code_path = Path.home() / ".claude.json"
            if _merge_json_file(claude_code_path, "artemis", server_cfg, strict_parse=True):
                installed_paths.append(str(claude_code_path))
        elif target == "cursor":
            server_cfg = snippet["mcpServers"]["artemis"]
            cursor_path = Path.home() / ".cursor" / "mcp.json"
            if _merge_json_file(cursor_path, "artemis", server_cfg):
                installed_paths.append(str(cursor_path))
        elif target == "windsurf":
            server_cfg = snippet["mcpServers"]["artemis"]
            windsurf_path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
            if _merge_json_file(windsurf_path, "artemis", server_cfg):
                installed_paths.append(str(windsurf_path))
        elif target == "vscode":
            server_cfg = snippet["servers"]["artemis"]
            vscode_path = _get_vscode_user_dir() / "mcp.json"
            copilot_path = Path.home() / ".copilot" / "mcp-config.json"
            if _merge_json_file(vscode_path, "artemis", server_cfg, key_name="servers"):
                installed_paths.append(str(vscode_path))
            if _merge_json_file(copilot_path, "artemis", server_cfg, key_name="servers"):
                installed_paths.append(str(copilot_path))
        elif target == "cline":
            server_cfg = snippet["mcpServers"]["artemis"]
            cline_paths = (
                Path.home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json",
                Path.home() / ".cline" / "mcp.json",
                _get_vscode_user_dir()
                / "globalStorage"
                / "saoudrizwan.claude-dev"
                / "settings"
                / "cline_mcp_settings.json",
            )
            for cline_path in cline_paths:
                if _merge_json_file(cline_path, "artemis", server_cfg):
                    installed_paths.append(str(cline_path))
        elif target in ("roo", "roo_code"):
            server_cfg = snippet["mcpServers"]["artemis"]
            roo_settings_dir = (
                _get_vscode_user_dir() / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings"
            )
            roo_paths = (
                roo_settings_dir / "mcp_settings.json",
                roo_settings_dir / "cline_mcp_settings.json",
                Path(project_root) / ".roo" / "mcp.json",
            )
            for roo_path in roo_paths:
                if _merge_json_file(roo_path, "artemis", server_cfg):
                    installed_paths.append(str(roo_path))
        elif target == "openclaw":
            server_cfg = snippet["mcp"]["servers"]["artemis"]
            openclaw_path = Path.home() / ".openclaw" / "openclaw.json"
            if _merge_json_file(
                openclaw_path,
                "artemis",
                server_cfg,
                key_name=("mcp", "servers"),
                remove_paths=(("plugins", "artemis_mcp"),),
            ):
                installed_paths.append(str(openclaw_path))
        elif target == "codex":
            server_cfg = snippet["mcp_servers"]["artemis"]
            codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
            codex_path = codex_home / "config.toml"
            if not _merge_codex_toml(codex_path, server_cfg):
                raise RuntimeError(f"Could not safely install Codex MCP config at {codex_path}")
            installed_paths.append(str(codex_path))
        elif target == "zcode":
            server_cfg = snippet["mcp"]["servers"]["artemis"]
            zcode_path = Path.home() / ".zcode" / "cli" / "config.json"
            if _merge_json_file(
                zcode_path,
                "artemis",
                server_cfg,
                key_name=("mcp", "servers"),
                strict_parse=True,
            ):
                installed_paths.append(str(zcode_path))

    if include_rules:
        rules_paths = install_rules(client, project_root)
        if client == "codex" and not rules_paths:
            raise RuntimeError("Codex MCP configuration was installed, but testing rules could not be installed")
        installed_paths.extend(rules_paths)

    unique_paths: list[str] = []
    for p in installed_paths:
        if p not in unique_paths:
            unique_paths.append(p)
    return unique_paths


def mcp_command(
    server_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help="Type of MCP server to start: 'agent' (default, universal IDE mobile agent), 'adb' (raw adb), 'xml' (xml fuzzy search).",
        ),
    ] = "agent",
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            help="MCP transport protocol ('stdio' or 'sse').",
        ),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Host address when running with SSE transport.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port number when running with SSE transport.",
        ),
    ] = 8001,
    install_config: Annotated[
        str | None,
        typer.Option(
            "--install",
            "-i",
            help="Auto-install and merge ARTEMIS MCP configuration and testing rules into 'codex', 'zcode', another supported client, or 'all'.",
        ),
    ] = None,
    generate_config: Annotated[
        str | None,
        typer.Option(
            "--generate-config",
            "-g",
            help="Output ready-to-use MCP configuration for 'codex', 'zcode', another supported client, or 'all'.",
        ),
    ] = None,
    execution_mode: Annotated[
        str | None,
        typer.Option(
            "--mode",
            help=(
                "ARTEMIS execution mode: 'executor' lets the selected development agent "
                "drive device tools; 'autonomous' uses ARTEMIS Flash/Pro. "
                "Codex and ZCode default to executor."
            ),
        ),
    ] = None,
    skip_rules: Annotated[
        bool,
        typer.Option(
            "--skip-rules",
            help="With --install, configure only MCP without creating or modifying global agent rules.",
        ),
    ] = False,
) -> None:
    """Launch or configure the ARTEMIS Model Context Protocol (MCP) server."""
    project_root = env_utils.get_project_root()
    python_exe = env_utils.resolve_python_executable(project_root)

    if execution_mode is not None and execution_mode.strip().lower() not in EXECUTION_MODES:
        console.print(
            f"[bold red]Unsupported execution mode: '{execution_mode}'. "
            "Use 'executor' or 'autonomous'.[/bold red]"
        )
        raise typer.Exit(1)

    if install_config:
        client = _canonical_client(install_config)
        if client not in (
            "antigravity",
            "jetski",
            "claude",
            "claude_code",
            "claude_desktop",
            "cursor",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "roo_code",
            "openclaw",
            "codex",
            "zcode",
            "all",
        ):
            console.print(
                f"[bold red]Unsupported install target: '{install_config}'. Use 'antigravity', 'claude', 'cursor', 'windsurf', 'vscode', 'cline', 'roo', 'openclaw', 'codex', 'zcode', or 'all'.[/bold red]"
            )
            raise typer.Exit(1)
        try:
            installed_paths = install_mcp_config(
                client,
                python_exe,
                project_root,
                execution_mode=execution_mode,
                include_rules=not skip_rules,
            )
        except (RuntimeError, OSError, UnicodeError) as exc:
            console.print(f"[bold red]ARTEMIS MCP installation failed: {exc}[/bold red]")
            raise typer.Exit(1) from exc
        installed_kind = "configuration" if skip_rules else "configuration & rules"
        console.print(
            f"[bold green]✔ Successfully installed ARTEMIS MCP server {installed_kind} to:[/bold green]"
        )
        for path in installed_paths:
            console.print(f"  • [cyan]{path}[/cyan]")
        console.print(
            "\n[dim]Please restart or reload your IDE window to activate the Artemis MCP tools.[/dim]"
        )
        raise typer.Exit(0)

    if generate_config:
        client = _canonical_client(generate_config)
        if client == "all":
            all_configs = {
                "antigravity (~/.gemini/jetski/mcp_config.json)": _get_config_snippet(
                    "antigravity", python_exe, project_root, execution_mode
                ),
                "cursor (.cursor/mcp.json)": _get_config_snippet(
                    "cursor", python_exe, project_root, execution_mode
                ),
                "claude (claude_desktop_config.json & ~/.claude.json)": _get_config_snippet(
                    "claude", python_exe, project_root, execution_mode
                ),
                "windsurf (~/.codeium/windsurf/mcp_config.json)": _get_config_snippet(
                    "windsurf", python_exe, project_root, execution_mode
                ),
                "vscode (mcp.json)": _get_config_snippet(
                    "vscode", python_exe, project_root, execution_mode
                ),
                "cline (cline_mcp_settings.json)": _get_config_snippet(
                    "cline", python_exe, project_root, execution_mode
                ),
                "roo (cline_mcp_settings.json)": _get_config_snippet(
                    "roo", python_exe, project_root, execution_mode
                ),
                "openclaw (openclaw.json)": _get_config_snippet(
                    "openclaw", python_exe, project_root, execution_mode
                ),
                "codex (~/.codex/config.toml)": _get_config_snippet(
                    "codex", python_exe, project_root, execution_mode
                ),
                "zcode (~/.zcode/cli/config.json)": _get_config_snippet(
                    "zcode", python_exe, project_root, execution_mode
                ),
            }
            json_str = json.dumps(all_configs, indent=2)
            syntax_language = "json"
        elif client == "codex":
            server_cfg = _get_config_snippet("codex", python_exe, project_root, execution_mode)[
                "mcp_servers"
            ]["artemis"]
            json_str = _codex_toml_block(server_cfg)
            syntax_language = "toml"
        else:
            snippet = _get_config_snippet(client, python_exe, project_root, execution_mode)
            json_str = json.dumps(snippet, indent=2)
            syntax_language = "json"

        syntax = Syntax(json_str, syntax_language, theme="monokai", line_numbers=False)
        console.print(f"[bold cyan]MCP Configuration for {client.upper()}:[/bold cyan]")
        console.print(syntax)
        raise typer.Exit(0)

    threading.Thread(target=start_awake_service, daemon=True, name="artemis-awake-init").start()
    try:
        st = server_type.lower()
        if st in ("agent", "mobile", "artemis", "default"):
            logger.info(f"Starting Artemis Mobile Agent MCP Server over {transport}...")
            if transport.lower() == "sse":
                agent_mcp.run(transport="sse", host=host, port=port)
            else:
                agent_mcp.run(transport="stdio")
        elif st == "adb":
            if transport.lower() == "sse":
                logger.info(f"Starting Artemis ADB MCP Server over {transport}...")
                adb_mcp.run(transport="sse", host=host, port=port)
            else:
                # stdio carries the MCP JSON-RPC stream: redirect logging and detach
                # from the parent DataEngine before any output can corrupt it.
                from artemis.mcp.adb_server import configure_stdio_mode

                configure_stdio_mode()
                logger.info("Starting Artemis ADB MCP Server over stdio...")
                adb_mcp.run(transport="stdio")
        elif st == "xml":
            from artemis.mcp.xml_search_server import mcp as xml_mcp

            logger.info(f"Starting Artemis XML Fuzzy Search MCP Server over {transport}...")
            if transport.lower() == "sse":
                xml_mcp.run(transport="sse", host=host, port=port)
            else:
                xml_mcp.run(transport="stdio")
        else:
            logger.error(
                f"Unsupported MCP server type: {server_type}. Use 'agent', 'adb', or 'xml'."
            )
            raise typer.Exit(1)
    finally:
        shutdown_awake_service()
