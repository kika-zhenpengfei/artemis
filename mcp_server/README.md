# 🔌 Universal MCP Server for ARTEMIS

This directory contains the **Model Context Protocol (MCP)** server for **ARTEMIS**. It exposes mobile device automation to clients such as **Antigravity**, **Cursor**, **Claude Code**, **OpenClaw**, and **Windsurf**.

## 🏗️ Architecture

```
mcp_server/
├── __init__.py           # Package exports (mcp, main)
├── base.py               # Shared FastMCP instance
├── server.py             # Server entrypoint (stdio / sse)
├── rules.md              # AI Agent Testing Mindset & system prompt rules for IDEs
├── background/           # Background process execution
│   └── task_runner.py    # Subprocess runner for Artemis Agent
├── notifiers/            # Multi-environment notification adapters
│   ├── base.py           # BaseNotifier abstract class
│   ├── agentapi.py       # Jetski / Antigravity AgentAPI notifier (with session sorting & caching)
│   ├── desktop.py        # Native OS toast notifier (macOS/Linux/Windows, enabled by default)
│   ├── script.py         # Universal Script / Command Hook notifier (ARTEMIS_NOTIFY_CMD)
│   ├── webhook.py        # OpenClaw / CI / Discord / Slack webhook notifier
│   ├── file.py           # File audit logger (notifications.jsonl)
│   └── composite.py      # Multi-channel notification dispatcher
├── tools/                # Modular standard MCP tools
│   ├── device_execution.py # mobile_session / observe / act / verify
│   ├── test_workflow.py # source links / core cases / report
│   ├── task_runner.py    # mobile_run_task
│   ├── task_manager.py   # mobile_manage_task
│   ├── device_state.py   # mobile_get_device_state
│   ├── inspect_trace.py  # mobile_inspect_trace
│   └── diagnose.py       # mobile_diagnose
└── utils/                # Environment and device utilities
    ├── device_utils.py   # Cross-platform ADB and emulator resolver
    └── env_utils.py      # Python interpreter and process manager
```

The trace status store (traces directory layout and `status.json` lifecycle)
lives in the neutral base package as `artemis/runtime/trace_store.py`, shared
by the MCP server, the admin console, and spawned worker processes.

## 🔔 Multi-Platform Notification & Wakeup Support

When you start an asynchronous mobile test via `mobile_run_task`, Artemis MCP runs the job as a detached background process so the AI agent is not blocked. Upon completion or failure, Artemis dispatches alerts across **all available notification channels** so that both human developers and AI agents are notified immediately across different platforms:

| Platform / Environment | Primary Notification Mechanism | How It Works & Configuration |
| :--- | :--- | :--- |
| **Antigravity / Jetski** | **Reactive Wakeup (`AgentApiNotifier`)** | Automatically detects and caches the active IDE session credentials (`ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_CSRF_TOKEN`), sorting candidate processes by creation time to avoid stale PIDs, and calls `agentapi send-message` to wake up the agent directly in the conversation. |
| **Cursor / Windsurf / VS Code / Cline / Roo Code** | **Native Desktop Toast (`DesktopNotifier`) + File Audit (`FileNotifier`)** | Enabled **by default** across macOS (`osascript`), Linux (`notify-send`), and Windows (`powershell`). Pops up a system notification banner informing the developer whether the task completed or failed. The AI agent inspects `<trace_dir>/status.json` or uses `mobile_manage_task(action="status")`. Can be silenced via `ARTEMIS_DESKTOP_NOTIFY=false`. |
| **Claude Code / Desktop** | **Native Desktop Toast + File Audit** | Alerts the user via system desktop notification banner when background execution concludes, while maintaining a complete JSONL audit log in `notifications.jsonl`. |
| **OpenClaw / Slack / Discord / CI/CD** | **HTTP Webhook (`WebhookNotifier`)** | Sends structured JSON POST payloads to custom endpoints configured via `OPENCLAW_WEBHOOK_URL`, `MCP_NOTIFICATION_WEBHOOK`, or `ARTEMIS_WEBHOOK_URL`. |
| **Universal Custom IDE / CLI Hooks** | **Custom Script Hook (`ScriptNotifier`)** | Set `ARTEMIS_NOTIFY_CMD="my-script --title '{title}' --message '{message}' --trace-id '{trace_id}'"` in your environment to execute any custom command or script upon event completion. |

## 🧠 AI Agent Behavioral Rules (`rules.md`)

When connecting ARTEMIS to AI coding assistants like **Antigravity**, **Claude Code**, **Cursor**, or **Windsurf**, providing the assistant with domain-specific testing discipline is critical for generating reliable test code.

The included [`rules.md`](./rules.md) file contains the **Mobile Testing Mindset (ARTEMIS Integration)** guideline. It teaches the AI agent how to properly collaborate with ARTEMIS:

1. **The Runnable Code Principle & Active Exploration**: Instructs the AI to never guess or hallucinate UI transitions. Instead, it must first explore the live app via ARTEMIS MCP tools (`mobile_run_task`, etc.) to discover and verify real-world interaction paths before writing test scripts.
2. **Flash vs. Pro Routing Strategy**: Guides the AI to choose **Flash** for rapid, straightforward UI actions (no step cap by default) and **Pro** for tasks that need a persistent plan, verified checkpoints, polling loops, or ADB / video / log diagnosis.
3. **Latency & Timing Compensation**: Clarifies the difference between AI exploratory latency (e.g., model decision intervals) and the deterministic timing requirements of final test code.
4. **"Dynamic-First, Coordinate-Fallback" Locator Pattern**: Teaches the AI to prioritize dynamic UI locators (Resource IDs, OCR text, semantics) for layout resilience, while implementing absolute coordinate fallbacks for maximum execution reliability.
5. **Environment Self-Diagnosis**: Tells the AI to call `mobile_diagnose` first whenever a tool errors or the user says ARTEMIS is not working, and how to act on the returned fix list (run local commands itself, relay device prompts to the user, never move API keys through the chat, reload the MCP server after config changes).

### How Global Rules & MCP are Installed
When you run `uv run artemis mcp --install all` (or target a specific IDE like `--install cursor`, `--install claude`, etc.), the installer **automatically synchronizes both global MCP server configuration and global testing rules** into the corresponding user home directories (without creating project-level rule files in your workspace):
* **Antigravity / Jetski**: Automatically installs global rules to `~/.gemini/rules/artemis.md`.
* **Cursor**: Automatically installs global YAML-frontmatter rule file at `~/.cursor/rules/artemis.mdc`.
* **Claude Code / Desktop**: Automatically installs global rules to `~/.claude/rules/artemis.md`.
* **Windsurf**: Automatically installs global rules to `~/.codeium/windsurf/rules/artemis.md`.
* **VS Code**: Automatically installs global rules to `~/.vscode/rules/artemis.md`.
* **Cline / Roo Code**: Automatically installs global rules to `~/.cline/rules/artemis.md` and `~/.roo/rules/artemis.md`.
* **OpenClaw**: Automatically installs global rules to `~/.openclaw/rules/artemis.md`.
* **Codex**: Installs the managed block into `~/.codex/AGENTS.md` (or an active override file).
* **ZCode**: Installs the managed block into `~/.zcode/AGENTS.md`.

*(Note: If you prefer manual configuration, you can also copy the contents of [`rules.md`](./rules.md) into your IDE's system prompt or global rules settings.)*

## 🛠️ MCP Tools Overview

* **`mobile_test_context`**: Persists requirement-to-source links checked against actual repository files and lines before a device lease is acquired.
* **`mobile_test_cases`**: Persists core cases tied to those requirements. Selects an external-agent `flash` or `pro` workflow; Pro cases declare checkpoints. The generated plan appears in the WebUI's Plans & Cases panel.
* **`mobile_session`**: Opens, inspects, or closes a device lease controlled by the current external Agent session. Pass the prepared `workflow_id` to run its cases. On the first `open`, executor mode lazily starts the WebUI when `ARTEMIS_AUTO_START_UI=true` (default for generated Codex and ZCode config), reuses an existing ARTEMIS server, and reports port conflicts without blocking device execution. The device lock is held for the session lifetime and the resulting session appears in the WebUI with its originating Agent client.
* **`mobile_observe`**: Captures the current screenshot and indexed UI elements, returns a fresh `observation_id`, and persists an observation step.
* **`mobile_act`**: Executes one action through the shared ARTEMIS actuator, then observes and persists the result. Screen actions require the latest `observation_id`; use indexed elements first and normalized coordinates as fallback. Planned runs also require `case_id` and link actions to the case.
* **`mobile_verify`**: Records an evidence-backed `passed`, `failed`, or `inconclusive` verification in the same WebUI timeline. Planned runs require `case_id` and the latest `observation_id`; Pro checkpoints use `checkpoint_id`.
* **`mobile_test_report`**: Returns per-case results and writes a Markdown report into the session notes. Closing a planned session finalizes the report automatically.
* **`mobile_run_task`**: Asynchronously launches an autonomous mobile automation task (`Flash` or `Pro` model) with optional `device_serial` targeting. Pro runs can be tuned with `verification_level` (`off` | `final` | `checkpoints` | `strict` — how much the Checker audits) and `explorer_mode` (`flash` | `pro` | `ultra` — the Operator's perception depth).
* **`mobile_manage_task`**: Manages task lifecycle (`status`, `stop`, `inject_instruction`), returning task state and assigned `device_serial`. Pass `release_loop=True` with `inject_instruction` to gracefully end a `[Loop:continuous]` monitoring task — this explicit signal (not "please stop" wording) is what unlocks the loop milestone's completion.
* **`mobile_get_device_state`**: Real-time observer (`screenshot` or OCR+XML `hierarchy`) with optional `device_serial`.
* **`mobile_inspect_trace`**: Granular trace inspection, visual action overlays, agent reasoning, and `device_serial` tracking.
* **`mobile_diagnose`**: Environment doctor for the IDE. Reuses the readiness probes behind `artemis doctor` and the web console's device wizard (Python runtime, config, LLM credentials, ADB / devices / RSA keys / emulators, video toolchain) plus an MCP-host probe (server interpreter vs project `.venv`, detected MCP client, `.env` location, traces directory, daemon port collisions). Returns a `verdict` (`ready` | `degraded` | `blocked`), an ordered `next_steps` fix list the AI agent can act on (`Run:` one-command lines vs `Guidance:` for the user), scrubbed per-check details, `tasks` holding or waiting for devices, log paths, and the most recent failed task with its `recent_errors`. Optional deep checks: `verify_credentials=true` (live API-key validation, ~12s, result in `credentials`), `probe_device=true` (end-to-end screenshot + UIAutomator hierarchy, ~20s, result in `device_probe`), `launch_avd="<name>"` (boots an installed AVD in the background; re-run after ~60s). `attempt_fix=true` applies the safe self-heals (regenerate corrupted ADB keys, restart the ADB server, clear stale device locks / queue tickets left by crashed runners).

Recurring scene guidance is configured in `config/scenario_prompts.json` (override with `ARTEMIS_SCENARIO_PROMPTS_FILE`). Each scene has a unique Chinese `title`, a natural-language `when` condition, and Chinese `instructions`. Flash, Pro, and the external executor receive the candidate rules in their existing prompts or observation responses; the current agent decides which rule applies from the goal and observed screen, without an extra model call. Reports list supplied candidate rules, not automatically inferred matches. An unchanged screen and identical action are allowed twice; a third attempt is rejected with a `NO_PROGRESS` result. Keyboard suggestion rules require real taps on the target IME, since text injection may bypass its recommendation path.

### Source-linked executor test workflow

1. The development agent inspects requirements and repository code, then calls `mobile_test_context(goal, repository_root, requirements)` with requirement IDs and real `{path, line, reason}` code references. This creates a workflow without locking a phone.
2. It derives core cases and calls `mobile_test_cases(workflow_id, cases, profile="flash" | "pro")`. Each case includes requirement IDs, preconditions, steps, and an expected result. Pro cases also declare checkpoints. The plan is saved to `traces/<workflow_id>/notes/test_plan.md`.
3. It opens `mobile_session(operation="open", workflow_id=workflow_id)`, observes the screen, and executes each case with `mobile_act(..., case_id=...)`. It records the final result with `mobile_verify(..., case_id=..., assertion=<expected_result>, observation_id=...)`; Pro also verifies each checkpoint using `checkpoint_id`.
4. It closes the session and calls `mobile_test_report(workflow_id)`. A case only passes after its final expected result and all Pro checkpoints pass. The report is saved to `traces/<workflow_id>/notes/test_report.md`, and incomplete or failed cases make an otherwise completed session fail.

The WebUI's **Plans & Cases** panel lists prepared plans before device execution. Once a plan is selected, it displays its cases, requirement/code links, action traces, verification evidence, and live results. Autonomous `mobile_run_task` Flash/Pro runs retain their existing behavior.

### 📱 Device Selection & Multi-Device Execution
ARTEMIS supports parallel execution across multiple connected Android devices and emulators:
1. **Direct Device Specification**: Provide `device_serial` explicitly (e.g. `device_serial="63191FDKX00062"` or `device_serial="emulator-5554"`). Per-device execution locks allow distinct devices to run in parallel.
2. **Automatic Device Selection**: Omit `device_serial` to let ARTEMIS auto-select an available ready device from the device pool.
* **User Choice Priority**: When multiple devices/emulators are connected, AI agents should prioritize asking the user which device to run on.
* **Device Diagnosis**: Inspect connected hardware, authorization status, and serials anytime via `adb devices` or `adb devices -l`.

## 🚀 How to Run & Configure

### CLI Execution
```bash
# Start server over stdio
python -m mcp_server

# Or via Artemis CLI
uv run artemis mcp
```

### IDE Configuration

#### One-Click Auto Install (Recommended)
Run the automated installer to configure your Agent or IDE:
```bash
uv run artemis mcp --install all
# Or install the client-driven executor specifically:
uv run artemis mcp --install codex --mode executor
uv run artemis mcp --install zcode --mode executor
```

#### Manual JSON Config Generation
Run `uv run artemis mcp --generate-config zcode` (or `all`) to output ready-to-use configuration with resolved `.venv` Python and project paths.
