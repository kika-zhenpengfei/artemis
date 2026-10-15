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
* **`mobile_test_intakes`**: Lists or reads stored APK plus Git HEAD submissions, so Codex can plan them with real source references.
* **`mobile_test_cases`**: Persists core cases tied to those requirements. Selects an external-agent `flash` or `pro` workflow; Pro cases declare checkpoints. The generated plan appears in the WebUI's Plans & Cases panel.
* **`mobile_test_execution_plan`**: Optionally compiles validated fixed actions, model handoffs, and observable assertions for each eligible case before the device session starts.
* **`mobile_prepare_case`**: Runs declared black-box device preparation and preflight under the existing device lease. It returns `ready` or `blocked` with evidence, and invalidates the previous screen observation.
* **`mobile_webui_handoff`**: Lets the current Codex session operate a WebUI-owned device lease paused at a dynamic step. Use `status` → `observe` / `act` / optional checkpoint `verify` → `resume`; use `close` to release the lease when the workflow cannot continue.
* **`mobile_session`**: Opens, inspects, or closes a device lease controlled by the current external Agent session. Pass the prepared `workflow_id` to run its cases. For a source-linked workflow, `open` waits for its WebUI device confirmation and returns `awaiting_device` on a bounded timeout; the agent keeps waiting until `running`. On the first `open`, executor mode lazily starts the WebUI when `ARTEMIS_AUTO_START_UI=true` (default for generated Codex and ZCode config), reuses an existing ARTEMIS server, and reports port conflicts. A workflow still requires confirmation before it can run. The device lock is held for the session lifetime and the resulting session appears in the WebUI with its originating Agent client.
* **`mobile_observe`**: Captures the current screenshot and indexed UI elements, returns a fresh `observation_id`, and persists an observation step.
  When waiting for an image-only dynamic screen, `wait_for_change=true` polls for up to 30 seconds inside one MCP call and persists only the final observation. The result reports `wait_status`, `poll_count`, and elapsed time. A known text condition can use `mobile_act(action="wait_for_text")` instead. Reuse the observation returned by `mobile_act` after ordinary actions.
* **`mobile_act`**: Executes one action through the shared ARTEMIS actuator, then observes and persists the result. Screen actions require the latest `observation_id`; use indexed elements first and normalized coordinates as fallback. Planned runs also require `case_id` and link actions to the case.
* **`mobile_run_plan`**: Runs a compiled case's fixed steps inside the ARTEMIS process, with fresh control resolution and bounded condition waits. Returns a screenshot and `resume_token` when a dynamic screen or failed expectation needs the current agent's decision.
* **`mobile_verify`**: Records an evidence-backed `passed`, `failed`, or `inconclusive` verification in the same WebUI timeline. Planned runs require `case_id` and the latest `observation_id`; Pro checkpoints use `checkpoint_id`.
* **`mobile_test_report`**: Returns per-case results and writes a Markdown report into the session notes. Closing a planned session finalizes the report automatically.
* **`mobile_run_task`**: Asynchronously launches an autonomous mobile automation task (`Flash` or `Pro` model) with optional `device_serial` targeting. Pro runs can be tuned with `verification_level` (`off` | `final` | `checkpoints` | `strict` — how much the Checker audits) and `explorer_mode` (`flash` | `pro` | `ultra` — the Operator's perception depth).
* **`mobile_manage_task`**: Manages task lifecycle (`status`, `stop`, `inject_instruction`), returning task state and assigned `device_serial`. Pass `release_loop=True` with `inject_instruction` to gracefully end a `[Loop:continuous]` monitoring task — this explicit signal (not "please stop" wording) is what unlocks the loop milestone's completion.
* **`mobile_get_device_state`**: Real-time observer (`screenshot` or OCR+XML `hierarchy`) with optional `device_serial`.
* **`mobile_inspect_trace`**: Granular trace inspection, visual action overlays, agent reasoning, and `device_serial` tracking.
* **`mobile_diagnose`**: Environment doctor for the IDE. Reuses the readiness probes behind `artemis doctor` and the web console's device wizard (Python runtime, config, LLM credentials, ADB / devices / RSA keys / emulators, video toolchain) plus an MCP-host probe (server interpreter vs project `.venv`, detected MCP client, `.env` location, traces directory, daemon port collisions). Returns a `verdict` (`ready` | `degraded` | `blocked`), an ordered `next_steps` fix list the AI agent can act on (`Run:` one-command lines vs `Guidance:` for the user), scrubbed per-check details, `tasks` holding or waiting for devices, log paths, and the most recent failed task with its `recent_errors`. Optional deep checks: `verify_credentials=true` (live API-key validation, ~12s, result in `credentials`), `probe_device=true` (end-to-end screenshot + UIAutomator hierarchy, ~20s, result in `device_probe`), `launch_avd="<name>"` (boots an installed AVD in the background; re-run after ~60s). `attempt_fix=true` applies the safe self-heals (regenerate corrupted ADB keys, restart the ADB server, clear stale device locks / queue tickets left by crashed runners).

Recurring scene guidance is configured in `config/scenario_prompts.json` (override with `ARTEMIS_SCENARIO_PROMPTS_FILE`). Each scene has a unique Chinese `title`, a natural-language `when` condition, and Chinese `instructions`. Flash, Pro, and the external executor receive the candidate rules in their existing prompts or observation responses; the current agent decides which rule applies from the goal and observed screen, without an extra model call. Reports list supplied candidate rules, not automatically inferred matches. An unchanged screen and identical action are allowed twice; a third attempt is rejected with a `NO_PROGRESS` result. Keyboard suggestion rules require real taps on the target IME, since text injection may bypass its recommendation path.

### Source-linked executor test workflow

1. The development agent inspects requirements and repository code, then calls `mobile_test_context(goal, repository_root, requirements)` with requirement IDs and real `{path, line, reason}` code references. This creates a workflow without locking a phone and records the repository's current commit and working-tree state when available.
2. It derives core cases and calls `mobile_test_cases(workflow_id, cases, profile="flash" | "pro")`. Each case includes requirement IDs, preconditions, steps, and an expected result. Pro cases also declare checkpoints. The plan is saved to `traces/<workflow_id>/notes/test_plan.md`.
   For a repeatable path, call `mobile_test_execution_plan(workflow_id, plans)` before opening the device. A plan contains `case_id`, `steps`, and `final_assertion`. Fixed steps use `kind: "auto"`, `action`, a stable `locator` for targeted actions, `expect`, and a bounded `timeout_ms` (0–15000). Dynamic steps use `kind: "model"`, `goal`, optional `if_visible`, `resume_when`, and `max_actions` (1–10). For image-only full-screen ads, omit `if_visible` unless its absence plus `resume_when` proves the normal page is back. Conditions are `{ "state": "visible" | "absent", "locator": { "resource_id": "..." } }`; `text` and `class` are also supported. Pro checkpoints must each be assigned to one step. The server rejects transient element indices, missing assertions, and unbounded waits.
3. It calls `mobile_session(operation="open", workflow_id=workflow_id)` and waits. The WebUI **Test Plans** or **Task Queue** shows a separate device selector and confirmation button for each prepared workflow. The user selects an idle device and confirms that workflow. Each open call waits up to 25 seconds (configurable with `wait_timeout_seconds`, maximum 30); on `awaiting_device`, the agent keeps the task active and calls open again. Only `running` means a device lease exists. Confirmation binds the workflow, plan revision, serial, and ADB endpoint; editing the plan requires confirmation again. Devices are checked again at startup, with a busy/offline device returning the workflow to waiting instead of switching phones. For a case declaring `environment`, it calls `mobile_prepare_case(session_id, case_id)` first, then observes the screen and executes with `mobile_act(..., case_id=...)`. It records the final result with `mobile_verify(..., case_id=..., assertion=<expected_result>, observation_id=...)`; Pro also verifies each checkpoint using `checkpoint_id`.
   For a compiled case, call `mobile_run_plan(session_id, case_id)` instead. It resolves every fixed control against the latest UI hierarchy, executes actions and assertions locally, and records results in the same trace and report. `status: "continued"` means a bounded segment finished; call again without a token. If it returns `status: "handoff"`, inspect the returned screenshot/UI and scenario guidance, resolve the dynamic state with `mobile_act`, then call `mobile_run_plan(..., resume_token=<returned token>)`. The runner does not repeat an action whose postcondition failed; it first checks whether the model restored the expected state. `mobile_session(operation="close")` cancels an active fixed runner and releases the device.
4. It closes the session and calls `mobile_test_report(workflow_id)`. A case only passes after its final expected result and all Pro checkpoints pass. The report is saved to `traces/<workflow_id>/notes/test_report.md`, and incomplete or failed cases make an otherwise completed session fail.

The WebUI's **Plans & Cases** panel displays the selected session's cases, requirement/code links, action traces, verification evidence, and live results. Its **Report** tab contains `test_plan.md` and `test_report.md`; **Notes & Plans** retains case details and other notes. Autonomous `mobile_run_task` Flash/Pro runs retain their existing behavior.

Hybrid execution measures device action, post-action observation, persistence, and fixed-segment elapsed time separately. `handoff_wait_ms` includes agent reasoning, MCP transport, and any human delay; it is not a pure model-latency measurement. Compare the same APK, device, and network conditions before claiming an end-to-end speedup.

### 黑盒状态准备与固定计划执行

用例可声明 `environment`，无需在待测应用内增加测试接口。例如：

```json
{
  "package_name": "com.example.app",
  "reset": "clear_data",
  "checks": [
    {"kind": "setting_equals", "namespace": "global", "key": "auto_time", "value": "1"},
    {"kind": "device_time_between", "start": "2026-09-29T00:00:00+08:00", "end": "2026-09-30T00:00:00+08:00"}
  ]
}
```

`reset` 支持 `none`、`clear_data`、`reinstall`。重新安装时必须提供主机上的 `apk_path`；计划保存 APK 的 SHA-256，执行前校验文件未变化，并使用 Android SDK 的 `aapt` 核对包名，安装后清空应用数据。`set_settings` 可以临时修改受支持的时间与显示设置，并在下一用例开始或会话关闭时恢复原值。先检查安装包、系统设置和设备时间，再修改应用数据。检查失败时记录 `blocked` 和证据，跳过该用例。重复准备同一用例会清除旧验证结果，避免把新状态误判为上次的通过结果。清空数据只能重置本地状态；服务端配额、远程配置或账号数据仍需要独立的可控测试环境。

WebUI 首页 **Test Plans** 和 Workspace 的 **Task Queue** 展示待确认工作流，每项独立选择空闲设备并点击确认；Codex 等待到确认成功才租用设备执行。不同工作流选择不同设备可并行；同设备竞争由已有设备锁保护，失败的一项回到等待状态重新选择。确认时不预留设备，实际启动时再次检查。**Notes & Plans** 保留用例详情和普通笔记，**Report** 独立展示 `test_plan.md`、`test_report.md`。测试目标、源码关联、用例和执行计划由 Codex 通过 `mobile_test_context`、`mobile_test_cases` 和 `mobile_test_execution_plan` 生成。如需关联已保存的 APK 提交记录，先用 `mobile_test_intakes(intake_id=...)` 读取任务，再向 `mobile_test_context` 传入对应的 `intake_id`；原有 APK 与源码状态校验继续生效。

固定计划由 Codex 通过 `mobile_session`、`mobile_prepare_case` 和 `mobile_run_plan` 执行。后端仍保留 `POST /api/test_workflows/{workflow_id}/run` 执行接口，先通过 `POST /api/test_workflows/{workflow_id}/confirm_device` 确认该工作流的设备，再调用执行接口，按用例执行“准备 → 固定步骤 → 结果验证 → 报告”。遇到声明的 `model` 步骤、定位失败或断言未出现时，该接口启动的任务停在 `handoff`，保留设备锁和报告，不会将用例误记为失败或通过。Codex 调用 `mobile_webui_handoff(workflow_id, operation="status")` 获取当前用例和 `resume_token`，再用 `observe`、`act`、必要时 `verify` 处理动态页面，最后用 `resume` 继续自动步骤。新的交接会再次返回 `handoff`；全部结束后后台自动关闭会话。无法继续时调用 `close` 释放设备。

### 📱 Device Selection & Multi-Device Execution
ARTEMIS supports parallel execution across multiple connected Android devices and emulators. Source-linked workflows use their own confirmed device, independent of the global selector. Sessions without a workflow and autonomous tasks retain the following rules:
1. **Direct Device Specification**: Provide `device_serial` explicitly (e.g. `device_serial="63191FDKX00062"` or `device_serial="emulator-5554"`). Per-device execution locks allow distinct devices to run in parallel.
2. **Saved WebUI Selection**: When `device_serial` is omitted, ARTEMIS uses the device selected in the WebUI for the current ADB server. An unavailable selected device is rejected without falling back to another device. Without a saved selection, existing automatic selection rules apply.
* **User Choice Priority**: When multiple devices/emulators are connected and no selection is saved, AI agents should ask the user which device to run on.
* **Device Diagnosis**: Inspect connected hardware, authorization status, and serials anytime via `adb devices` or `adb devices -l`.

## 🚀 How to Run & Configure

WebUI 的 **Device** 列表显示在线设备及 Idle、Busy、Queued 状态。先选择空闲设备再启动任务；选择会按 ADB 服务保存，后续自主运行任务和未绑定 `workflow_id`、未显式传入 `device_serial` 的 Codex/ARTEMIS 会话使用该设备。绑定工作流的会话使用该工作流卡片中确认的设备。已选设备离线、占用或有排队任务时会拒绝启动，不会自动换机；显式传入的 `device_serial` 优先，正在执行的任务保持原设备。列表每 5 秒刷新，也可点击 **Refresh** 手动更新。System Setup 中的 **Diagnostic device** 仅切换诊断对象。

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
