# ARTEMIS 部署与使用指南（Codex 模式）

适用仓库：[kika-zhenpengfei/artemis](https://github.com/kika-zhenpengfei/artemis)，使用包含 `install_codex.sh` / `install_codex.bat` 的版本。

Codex 负责阅读源码、制定用例和判断结果，ARTEMIS 负责设备操作与证据保存，无需配置 ARTEMIS 模型凭据。使用流程为：

**一键安装 → 重启 Codex → 连接手机 → 在 Codex 发起测试（自动启动 WebUI）→ 页面选设备并确认 → 查看报告。**

## 1 一键安装

先安装 Git 和 Codex，并完成 Codex 登录。准备一台 Android 7.0 或更高版本的测试设备，以及可传输数据的 USB 线。Codex、ARTEMIS 和手机连接在同一台电脑上。

获取工程：

```bash
git clone git@github.com:kika-zhenpengfei/artemis.git
cd artemis
```

没有配置 GitHub SSH 时，可改用 `git clone https://github.com/kika-zhenpengfei/artemis.git`。

在工程根目录执行对应系统的一条命令：

| 系统 | 安装命令 |
| --- | --- |
| macOS / Linux | `bash install_codex.sh` |
| Windows PowerShell | `.\install_codex.bat` |

脚本会复用可用的 uv、兼容的 Node.js/npm 和 ADB；缺失工具下载到本工程的 `.artemis-tools/`，Python 依赖放在 `.venv/`，前端依赖和构建产物留在工程内。它不会调用系统包管理器、升级系统工具、切换全局 Node、修改 shell PATH 或自动启动 ADB 服务。依赖下载及缓存目录也限定在工程内。

工程之外会更新 Codex 配置中的 **ARTEMIS MCP 配置项**，供 Codex 在其他 Android 工程中调用；其他 MCP 配置保留，修改前备份。全局 `AGENTS.md` / `AGENTS.override.md` 不再自动写入。工程 `.env` 仍会创建或更新 executor 模式，已有文件先备份。ARTEMIS CLI 可能创建自身的用户配置、数据或日志目录，但不会改动其他开发工具的设置。

安装需要联网，无需管理员权限。scrcpy / FFmpeg 只复用已有版本，缺失时跳过，录像或投屏功能可能受限。Linux 使用 glibc 发行版；Linux ARM 需预先提供可用的 ADB。安装失败按提示修复后重跑；已下载完成的私有工具可复用。

**安装完成后，完全退出并重启 Codex。** 无需手动修改 PATH、编辑 MCP 配置或运行 `artemis init`。已有 shell、系统工具和旧版全局规则不会被自动删除或回滚。

安装脚本默认只完成安装与配置，之后按下一节直接在 Codex 中使用即可。若希望安装后立即打开 WebUI，也可加 `--launch`（Windows 为 `-Launch`），此时需保持该终端运行。

## 2 安装后的最简启动方式（推荐）

**日常直接在 Codex 中发起测试，无需先进入 ARTEMIS 目录或手动启动终端服务。**

1. 手机开启 USB 调试，连接电脑并允许调试授权；使用模拟器时，先启动模拟器。
2. 在 Codex 中打开待测 Android 工程，发送下面的请求，将 `【测试目标】` 换成实际需求。
3. 点击 Codex 返回的 WebUI 链接，为该工作流选择空闲设备并点击 **Confirm device**，Codex 随后执行并生成报告。

```text
请使用 ARTEMIS 测试当前工程的【测试目标】。
先检查环境、阅读源码并准备用例，打开等待设备确认的工作流会话，
返回 workflow_id 和 WebUI 链接，并保持等待。
等我在 WebUI 为该工作流选择空闲设备并确认后，再开始执行。
结束后关闭会话释放设备，并生成测试报告。
```

Codex 打开工作流会话、进入等待设备阶段时，ARTEMIS 会自动启动后台 WebUI；已有服务则直接复用。默认地址为 [ARTEMIS WebUI](http://localhost:8000)，默认不会自动弹出浏览器，点击返回的链接即可。仅打开 Codex 或准备测试用例时，页面服务尚不一定启动。

如果当前 Codex 对话不在待测工程中，在请求里补充源码绝对路径即可。安装或更新后需要重启 Codex，日常测试无需重复安装。首次操作设备时，ARTEMIS 默认可安装或升级 Helper，以采集界面信息。

如果曾手动关闭自动启动，或页面打开失败，让 Codex 先调用 `mobile_diagnose` 检查，再按第 5 节的备用命令处理。

## 3 在 Codex 中发起测试

下面是更完整的测试请求示例。将待测 Android 工程放到本机，并让 Codex 能读取它；包名等信息可由 Codex 从源码确认。测试新 APK 时再提供本机 APK 路径，以及是否允许安装、重装或清空数据；这些信息统一在 Codex 中提供。

下面的请求可直接复制，先将源码路径替换为本机实际路径：

```text
请使用 ARTEMIS 验证 Neon Love 普通主题详情页的展示流程。
源码目录：/absolute/path/to/ThemeTemplateV3
应用包名：com.ikeyboard.theme.neon.love
使用手机上已安装的应用，核对版本，保留应用数据和默认输入法。

先阅读源码，以 pro profile 准备用例和检查点：
1. 从主题列表进入普通主题详情，检查名称、预览和主操作状态。
2. 返回列表，再次进入同一主题，检查展示是否一致。

准备工作流后，打开等待设备确认的会话，给出 workflow_id 和 WebUI 链接，并保持等待。
必须等我在 WebUI 为该工作流选择空闲设备并点击 Confirm device，
且会话状态为 running 后，才开始设备操作。
不要代我确认设备，不要购买、下载、解锁、应用或收藏资源。
完成后返回列表；无论成功或失败，都关闭会话释放设备并生成报告。
```

随后按以下顺序操作：

1. Codex 准备用例并返回 `workflow_id`，进入 `awaiting_device` 等待状态。
2. 在 WebUI 首页 **Test Plans**，或 Workspace 的 **Task Queue** 中找到该工作流卡片。
3. 在 **Device for this workflow** 中选择 **Idle** 设备，点击 **Confirm device**。
4. Codex 收到 `running` 后开始测试，完成后关闭会话并生成报告。

设备选择绑定具体工作流；页面的全局 **Default device** 不能代替工作流确认。修改计划后需要重新确认；设备被占用或离线时会返回等待，不会自动换到其他手机。

**确认设备时，保持原 Codex 对话继续等待。** 按钮不会唤醒已经结束的对话。若对话已结束，在原对话发送：

```text
继续执行工作流 <workflow_id>，读取其 WebUI 设备确认，完成测试并生成报告。
```

`flash` 适合直接路径，`pro` 适合需要明确检查点的验收；它们不会切换 Codex 当前模型。只验证详情展示时通常无需重装应用；明确要求重装 APK 时，还需安装 Android SDK Build Tools，并确保 `aapt` 在 PATH 中可用。

## 4 查看报告与并行测试

在任务列表中打开对应会话：

| 页面 | 用途 |
| --- | --- |
| Task Queue | 查看任务状态、确认待启动工作流的设备 |
| Notes & Plans | 查看用例、验证证据和普通笔记 |
| Report | 查看 `test_plan.md` 和 `test_report.md` |

默认报告保存在 `traces/<workflow_id>/notes/`。首次部署应完成一次完整测试，确认：**设备确认前保持等待、确认后才操作、报告包含验证结果、结束后设备恢复 Idle**。

需要并行时，让多个活跃的 Codex 对话分别准备独立工作流，在 WebUI 为每个工作流确认不同的空闲设备。不同设备可并行，同一设备同一时间只能执行一个任务；每个对话使用自己的工作流和会话。若已配置全局并发限制，先检查 `ARTEMIS_CONCURRENCY_MODE`，按设备并行使用 `per_device`。

## 5 备用终端命令与更新

日常使用按第 2 节直接在 Codex 发起测试即可。只有自动启动失败、主动关闭过自动启动或需要维护服务时，才使用下面的命令。

以下命令在 ARTEMIS 工程根目录执行；Windows 将 `bash artemis.sh` 替换成 `.\artemis.bat`。此入口仅为 ARTEMIS 进程加载工具路径，无需修改全局 PATH。

手动启动 WebUI（保持终端运行）：

```bash
bash artemis.sh ui --host 127.0.0.1 --port 8000 --no-open
```

检查设备连接：

```bash
bash artemis.sh --adb devices -l
```

设备状态应为 `device`；`unauthorized` 表示尚未授权，`offline` 表示连接未就绪。需要改为后台运行 WebUI 时，先结束前台服务，并在没有测试任务的情况下执行：

```bash
bash artemis.sh restart --no-open
```

查看状态和停止服务：

```bash
bash artemis.sh status
bash artemis.sh stop
```

更新前，让 Codex 关闭测试会话，再停用 MCP 连接、停止 WebUI。前台 WebUI 可在其终端按 `Ctrl+C` 结束；后台服务使用上面的 `stop` 命令。检查并处理本地未提交修改后，在工程根目录执行：

```bash
git status --short --branch
git switch main
git pull --ff-only origin main
```

然后重新运行第 1 节的一键安装命令，它会同步依赖、重建 WebUI 并更新 MCP 配置。完成后重启 Codex，按第 2 节重新发起测试，WebUI 会随工作流会话自动启动。`pull --ff-only` 被拒绝时先处理分支差异，不强制覆盖。

**WebUI 重启与 Codex MCP 重载需要分别完成。** 测试运行期间不要重载 MCP；已启动会话不能仅凭旧 `session_id` 在新 MCP 进程中继续。

保留默认端口 8000 和工程下的 `traces/` 最方便。自定义端口时，保持 `ARTEMIS_UI_PORT`、`ARTEMIS_DAEMON_PORT` 与启动命令的 `--port` 一致；自定义数据目录时，MCP 与 WebUI 的 `ARTEMIS_TRACES_DIR` 使用同一个绝对路径。已有 MCP 环境配置优先于 `.env`。

迁移历史记录时，先关闭测试和服务，再复制完整的 `traces/`（或自定义数据目录）。新电脑重新运行安装脚本，不复制旧 `.artemis-tools`、`.venv`、`node_modules` 或 Codex 用户目录；移动现有工程后也要重跑安装，更新路径。安装完成后保留 `.artemis-tools/`，日常运行仍需要其中的工具与配置。

## 6 常见问题

遇到 ARTEMIS 或设备问题，先让 Codex 调用 `mobile_diagnose`，需要自动修复时调用 `mobile_diagnose(attempt_fix=true)`，并按报告处理。

| 现象 | 处理方法 |
| --- | --- |
| 安装脚本报下载或依赖错误 | 检查终端错误、网络和工程目录写入权限，修复后重新运行一键安装命令 |
| Codex 已连接，但 WebUI 页面打不开 | 仅加载 MCP 或准备测试用例不会启动页面；让 Codex 打开工作流会话并进入等待设备状态。仍失败时先诊断，再按第 5 节手动启动；自定义端口使用 Codex 返回的链接 |
| 终端中 `uv` 或 `adb` 找不到 | 私有安装不会加入全局 PATH；日常使用 `bash artemis.sh` / `.\artemis.bat`，ADB 参数前加 `--adb` |
| 提示 Local runtime is missing 或工程路径变化 | 在当前工程重新运行一键安装，不手工复制旧运行配置 |
| MCP 工具缺失或显示旧参数 | 重启 Codex；迁移过工程目录时重新运行安装脚本 |
| 设备为 `unauthorized` / `offline` | 解锁并授权手机，检查数据线；Windows 检查厂商 USB 驱动，Linux 检查 USB / udev 权限 |
| 页面缺少设备确认、Report，或出现 `Method Not Allowed` | 停止旧服务，重新运行安装脚本，启动当前 WebUI 后刷新浏览器 |
| 计划不可见或设备确认后一直等待 | 核对工作流 ID、Confirm device 和空闲状态，保持原 Codex 对话活跃，并检查 MCP 与 WebUI 是否使用相同工程和数据目录 |
| 设备一直 Busy | 让持有设备的原 Codex 会话关闭测试；原进程已退出时先诊断，再清理遗留状态 |
| Windows 更新提示 `os error 32` | 先关闭测试、MCP 和 WebUI，再运行安装脚本 |

MCP 完全无法启动时，在工程根目录运行以下命令诊断：

```bash
bash artemis.sh doctor
```

CLI doctor 可能仍提示模型凭据缺失；Codex 模式以 MCP `mobile_diagnose` 的 executor 判定为准，无需因此增加模型配置。
