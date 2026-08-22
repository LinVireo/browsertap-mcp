# browsertap-mcp

[English](https://github.com/LinVireo/browsertap-mcp/blob/main/README.md) | 中文文档

[![离线 CI](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE)

[使用指南](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.zh-CN.md) · [故障排查](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md) · [安全说明](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md) · [贡献指南](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.zh-CN.md) · [变更记录](https://github.com/LinVireo/browsertap-mcp/blob/main/CHANGELOG.md)

`browsertap-mcp` 是一个通过 Chrome 扩展和 CDP 操作**当前真实浏览器会话**的 MCP 服务。
Agent 可直接使用现有登录态、Cookies 和已打开的标签页，无需另行启动沙盒浏览器或重复登录。

当前版本:Python 包、bridge 与 Chrome unpacked 扩展统一为 **0.4.1**。

当页面级输入无法完成操作时，BTAP 还提供五个直接发送操作系统级鼠标和键盘输入的工具。
`resolve_leave_dialog` 是额外一条受限路径，仅在两次协议处理失败后才可能发送 Enter。`safe`
profile 对物理输入进行询问；默认 `lab` profile 免询问执行，也可通过配置恢复会话级询问。
两种 profile 均保留输入锁、安静窗口、目标激活和屏幕确认。

## 60 秒上手

三步。每一步的完整说明（包括 Windows PowerShell 路径和各个客户端的配置）
见下方**快速开始**一节。

```bash
# 1. 源码安装。目前还没发到 PyPI。
git clone https://github.com/LinVireo/browsertap-mcp.git && cd browsertap-mcp
python -m venv .venv && ./.venv/bin/python -m pip install -e ".[desktop]"
./.venv/bin/browsertap extension-path   # 打印第 2 步要用的目录

# 3. 把 MCP 客户端指向同一个可执行文件（以 Claude Code 为例）。
claude mcp add browsertap -- "$PWD/.venv/bin/browsertap"
```

Windows 上同样三步，只是换成 `.\.venv\Scripts\python.exe` 和
`.\.venv\Scripts\browsertap.exe`。

**第 2 步是手工的，也是最耗时的一步。** 目前没有上 Chrome 应用商店，所以扩展需要
手动加载：打开 `chrome://extensions`，开启**开发者模式**，点**加载已解压的扩展程序**，
选 `extension-path` 刚打印的目录。然后打开一个普通的 `http://` 或 `https://` 页面 ——
`about:blank` 上跑不了内容脚本，不会建立任何会话。

接着直接问 agent：*我现在开了哪些标签页？* 如果返回为空，跑
`browsertap doctor`：它会给出一个 `cause` 和对应的一句 `advice`。

## 核心能力

- **真实浏览器与现有会话**：连接正在运行的 Chrome、Edge 或 Opera，并保留登录态、Cookies 和页面上下文。
- **默认后台操作**：`switch_tab` 仅修改后续调用目标，不激活标签页；页面操作可在指定后台标签页中完成。
- **页面读取**：将页面转换为长度可控的简化 HTML 或纯文本。长链接以 `#r1` 等短引用表示，并同时返回真实 URL。
- **JavaScript 执行**：在指定页面上下文中执行 JavaScript。
- **后台页面输入**：`page_click`、`page_type`、`page_press` 和 `page_drag` 在指定标签页内按视口坐标派发受信任的 CDP 输入事件，不移动桌面光标或改变可见标签页。
- **等待与滚动**：等待选择器、文本、URL 或 JavaScript 条件；滚动后可继续读取长页面。`scan_page` 会报告未包含在结果中的视区外内容。
- **显式对话框策略**：`alert`、`confirm`、`prompt` 和 `beforeunload` 支持 `dismiss`、`accept`、`manual` 策略；保留的对话框由 `handle_dialog` 处理。
- **临时站点权限**：为单个 origin 设置 60–600 秒的 notifications、geolocation、camera 或 microphone 权限，并在租约结束后恢复原设置。
- **原生 CDP 接口**：支持单条和批量命令，可按标签页、扩展 ID 或 target ID 寻址。
- **使用现有登录态的原生下载**：通过 Chrome 下载管理器和当前浏览器 profile 的 Cookies 下载附件，并返回已验证的本地路径。
- **零标签页操作**：扩展管理、CDP 目标列表、标签页列表和关闭操作通过扩展 service worker 通道执行，在没有普通标签页时仍可使用。
- **页面与桌面截图**：CDP 页面截图作为 MCP 图片内容返回，也可保存到文件；桌面截图仅用于核对实际屏幕和物理输入。不支持图片输入的模型应改用 `scan_page`、页面 API 或 OCR。
- **受保护的物理输入**：系统级鼠标、键盘和热键仅作为页面级操作无法完成时的后备方案。`lab` 可免 elicitation 执行，`safe` 对每次调用进行询问；两种 profile 均保留跨进程锁、安静窗口、所有权检查、目标激活和屏幕确认。
- **多浏览器共存**：Chrome、Edge 和 Opera 可同时连接同一个 bridge，各会话相互隔离。

## 环境要求

- Python 3.10+
- Chrome、Edge 或 Opera
- Linux、macOS 或 Windows；Linux 的操作系统级输入需要 X11 桌面
- Claude Code 或其他 MCP 客户端

## 快速开始

### 1. 安装

克隆仓库、创建虚拟环境，并安装推荐的桌面能力依赖。

**Windows PowerShell**

```powershell
git clone https://github.com/LinVireo/browsertap-mcp.git
Set-Location browsertap-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
.\.venv\Scripts\browsertap.exe extension-path
```

**Linux 或 macOS**

```bash
git clone https://github.com/LinVireo/browsertap-mcp.git
cd browsertap-mcp
python -m venv .venv
./.venv/bin/python -m pip install -e ".[desktop]"
./.venv/bin/browsertap extension-path
```

核心安装 `pip install -e .` 不包含操作系统级鼠标、键盘和桌面截图依赖，仅适用于明确不使用
这些工具的环境。首次发布到 PyPI 后，可改用
`pip install "browsertap-mcp[desktop]"`；发布前以以上源码安装方式为准。

### 2. 加载 Chrome 扩展

项目包含一个未打包扩展，首次使用时需手动加载。

```bash
browsertap extension-path
```

打开 `chrome://extensions`，启用**开发者模式**，选择**加载已解压的扩展程序**，然后选择上述命令输出的目录。
加载后，扩展名称显示为 **BrowserTap Bridge**。

Edge 或 Opera 可在 `edge://extensions` 或 `opera://extensions` 中加载同一目录；bridge 会自动区分不同浏览器。

随后打开一个正常的 `http://` 或 `https://` 页面。`about:blank` 无法运行内容脚本，因此不会建立页面会话。

#### 连接状态角标

扩展可能会在页面上显示小型 `BTAP：检测中`、`BTAP：已连接` 或
`BTAP：桥未连接` 角标。角标仅用于展示连接状态，不会显示页面内容、Cookie、token 或 URL。
打开扩展弹窗并取消勾选**在页面上显示连接状态**即可隐藏角标；隐藏角标不会停止
bridge、keepalive 或自动重连。

### 3. 在客户端里添加这个服务

以下通用配置适用于大多数 MCP 客户端：

```json
{
  "mcpServers": {
    "browsertap": {
      "type": "stdio",
      "command": "browsertap"
    }
  }
}
```

使用虚拟环境安装时，建议为 `command` 填写可执行文件的绝对路径，以避免客户端无法通过 `PATH` 定位服务。

<details>
<summary>Claude Code</summary>

```bash
claude mcp add browsertap -- browsertap
```

添加 `--scope user` 可在所有项目中启用该服务。虚拟环境安装示例：

```bash
claude mcp add browsertap -- /absolute/path/to/.venv/bin/browsertap
```

Windows PowerShell 应填写 `.venv\Scripts\browsertap.exe` 的绝对路径。

使用 `/mcp` 确认连接状态。
</details>

<details>
<summary>Claude Desktop</summary>

按照 MCP 官方[安装指引](https://modelcontextprotocol.io/quickstart/user)添加上述通用配置。示例文件：`examples/claude-desktop-config.json`。
</details>

<details>
<summary>Cursor</summary>

将通用配置写入 `.cursor/mcp.json`（单个项目）或 `~/.cursor/mcp.json`（全局）。示例文件：`examples/cursor-mcp.json`。
</details>

<details>
<summary>VS Code</summary>

```bash
code --add-mcp '{"name":"browsertap-mcp","command":"browsertap"}'
```

也可将配置写入 `.vscode/mcp.json`。VS Code 使用的配置键为 `servers`，不是 `mcpServers`。
</details>

<details>
<summary>Hermes</summary>

将以下内容添加到 `~/.hermes/config.yaml`：

```yaml
mcp_servers:
  browsertap:
    command: browsertap
    timeout: 120
    connect_timeout: 60
```

`browsertap print-hermes-config` 可输出该配置。示例文件：`examples/hermes-config.yaml`。使用 `hermes mcp list` 验证连接。
</details>

<details>
<summary>其他客户端</summary>

任何支持 stdio 的 MCP 客户端均可使用本服务。应按照对应客户端的安装说明添加上述通用配置。
</details>

### 首次调用示例

扩展已加载且存在正常页面后，可使用以下提示词验证连接：

> 我现在开了哪些标签页?读一下当前页面并总结。

若标签页列表为空，运行 `browsertap doctor`。

需要减少对用户操作的影响时，请先阅读 [`docs/USAGE.zh-CN.md`](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.zh-CN.md)。其中说明
后台标签页、前台页面和桌面截图的区别，以及多模态模型的适用场景。

## 配置

### 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `BROWSERTAP_BRIDGE_HOST` | `127.0.0.1` | 桥的绑定地址 |
| `BROWSERTAP_BRIDGE_PORT` | `18765` | WebSocket 端口。HTTP 使用 `PORT+1`，`PORT+2` 为单 bridge 锁 socket。使用自定义端口时，还需单独告知扩展一次，见 [docs/TROUBLESHOOTING.zh-CN.md](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md)。 |
| `BROWSERTAP_NO_SPAWN` | 未设置 | 设为 `1` 后 MCP 服务不自动启动 bridge，适用于由运维流程单独管理 bridge 的环境 |
| `BROWSERTAP_BRIDGE_AUTH` | 启用 | 仅在明确可信的本机兼容环境中设为 `off`。默认 BTAP 使用持久用户 token 保护 `/link`。 |
| `BROWSERTAP_BRIDGE_TOKEN_FILE` | `~/.browsertap/bridge-token` | 覆盖共享 token 文件位置。各编辑器不需要分别配置 token。 |
| `BROWSERTAP_BRIDGE_TOKEN` | 未设置 | 旧安装的一次性迁移来源。token 文件不存在时导入一次,此后始终以文件为准。 |
| `BROWSERTAP_PREFERRED_BROWSER` | 未设置 | `chrome` / `edge` / `opera`。多个浏览器都连上、又没指定标签页时,默认落在哪个浏览器 |
| `BROWSERTAP_MODE` | `lab` | `lab` 默认免询问连续自动化;`safe` 对每次物理输入/站点 allow 单独询问。也可用 `set_automation_profile` 只改当前 MCP 进程 |
| `BROWSERTAP_LAB_NO_ELICIT` | 启用 | `lab` 默认按 `1` 处理。只有明确设为 `0`/`false` 才恢复会话级询问;跨进程锁、安静窗口、前台确认和 ownership 始终生效 |
| `BROWSERTAP_AUTO_BEFOREUNLOAD_HOSTS` | `shell.,ttyd,code-server,jupyter,vscode-web` | `lab` 下匹配当前 host 时,普通 `open_url` 自动接受 beforeunload;显式 `intent_leave=false` 可强制保留页面 |
| `BROWSERTAP_WS_ALLOWED_ORIGINS` | 未设置 | 允许连接 bridge WebSocket 的额外 origin，以英文逗号分隔并精确匹配。扩展 origin 自动允许；不要加入宽泛或不可信 origin。 |
| `BROWSERTAP_WS_ALLOW_NO_ORIGIN` | 未设置 | 仅在可信的非浏览器本机 WebSocket 客户端无法发送 `Origin` 时设为 `1`。默认拒绝无 origin 客户端。 |

### 命令行

```bash
browsertap                      # 运行 MCP 服务(stdio)
browsertap extension-path       # 打印未打包扩展的目录
browsertap skill-path           # 打印随包发布的 agent skill 所在目录
browsertap doctor               # 诊断本地环境,输出 JSON
browsertap bridge               # 在前台运行桥
browsertap print-hermes-config  # 打印 Hermes 配置片段
```

`doctor` 会报告扩展路径、端口状态和已连接标签页数量，并返回结构化判定。`cause` 的取值为
`healthy`、`ext_never_registered`、`sw_slept_or_dropped`、`registering` 或
`bridge_unreachable`；`advice` 提供对应恢复建议。`registering` 表示扩展已连接，但尚无正常的
`http(s)` 内容标签页完成注册。

BTAP 首次使用时创建 `~/.browsertap/bridge-token`，bridge 和所有 MCP 进程均读取该文件。
关闭浏览器或编辑器不会轮换 token。卸载扩展或重装 Python 包时会保留该文件，因此重装后可以
继续使用。若需彻底清除用户数据，应先停止所有 BTAP bridge 进程，再删除整个
`~/.browsertap` 目录；下次启动时会生成新 token。

### Agent skill（可选）

BTAP 随包发布两份 skill，用来告诉调用方的 agent 该怎么驱动它。它们就是普通 Markdown，
**完全可选** —— 不装也不影响任何工具。它们补的是工具描述装不下的那部分判断：先调哪个工具、
什么时候必须带 `session_id`、哪些标签页属于用户因此不能碰。

```bash
browsertap skill-path           # 例如 .../site-packages/browsertap_mcp/skills
```

该目录下有：

| Skill | 作用 |
|---|---|
| `browsertap-default/SKILL.md` | 调用契约：动手前先选定目标；要改动页面就自己开标签页，收尾时关掉；遇到 `no_response` / `switched_session` / `bridge_error` 怎么处理。 |
| `browsertap-bridge-recovery/SKILL.md` | 传输层本身出问题时的恢复流程：三个组件里到底哪个是旧的，以及对应的那一次重启或重新加载。 |

请把客户端的 skill 管理器**指向这个目录**，不要复制文件。复制出来的副本在内容恰好一致期间
看不出问题，等你升级包之后就静默收不到更新了。如果确实保留了副本，可以用
`python -m scripts.check_tool_docs --check-installed-skills --skill-mirror DIR`
与随包原件比对，并指出是哪一份漂了。

### 升级

升级要做三件事，不是一件：三个部分不会同时变成新版，而第 3 步漏掉不会有任何报错。

1. 更新包 —— 发布到 PyPI 后用 `pip install -U browsertap-mcp`，源码安装则 `git pull`。
   新建的 MCP 会话立即生效。
2. `browsertap bridge --restart`。守护进程常驻、活得比每个 MCP 会话都长，不重启就
   一直用旧代码。
3. 打开 `chrome://extensions`，在扩展上点一次**重新加载**。磁盘上的文件已经换了，但 Chrome
   仍在跑先前加载的构建，且没有任何命令能让它重读。

`browsertap doctor` 会指出哪一部分是旧的，并给出唯一有效的动作：
`reload_extension`、`restart_bridge` 或 `restart_mcp_session`。另外两个不起作用，所以照这个
字段做，别三件一起做。

### 卸载

1. 运行 `browsertap bridge --stop` 停止托管的 bridge 守护进程。
2. 打开 `chrome://extensions`（Edge/Opera 使用对应扩展管理页），移除以未打包方式加载的
   **BrowserTap Bridge** 扩展。
3. 从每个 MCP 客户端配置中移除 `browsertap` 条目。
4. 在安装时使用的环境中运行 `pip uninstall browsertap-mcp`。若使用专用虚拟环境，退出该
   环境后再移除其明确目录。
5. 可选彻底清理：确认所有 BTAP bridge 均已停止后，移除 `~/.browsertap`。这会删除持久
   bridge token 和日志；默认保留这些数据，以便重装后无需重新配置即可继续使用。

## 工作原理

系统由三层组成：

1. **Chrome 扩展**（MV3）：注入真实页面，并通过 Chrome API 访问 `tabs`、`cookies`、`debugger` 和 `management`。
2. **BrowserBridge**：本地守护进程，监听 `127.0.0.1:18765`（WebSocket）和 `:18766`（HTTP），
   负责维护扩展连接、会话状态和结果转发。该进程独立于 MCP 实例运行，缺失时由 MCP 服务按需启动，
   且不创建可见窗口。会话以 `clientId:tabId` 标识，支持多个浏览器和 profile 并存。
3. **MCP 服务**：将上述能力公开为 MCP 工具。

浏览器连接包含两条通道：按标签页的会话通道，以及直接连接扩展 service worker 的通道。第二条
通道使部分工具在普通标签页全部关闭时仍可用。

## 操作边界

**选定标签页不会自动激活前台。** `switch_tab` 默认 `activate=false`，仅修改后续调用目标。
只有调用 `activate_tab`、传入 `switch_tab(activate=true)` 或执行需要前台的物理输入时，浏览器可见
状态才会改变。页面读取、JavaScript 和 `page_*` 输入工具均可在后台标签页上运行。

**页面坐标与桌面坐标相互独立。** `page_click`/`page_drag` 使用指定标签页内的**视口**坐标，
通过 CDP 派发，不移动光标或聚焦窗口，响应包含 `foreground_changed: false`。
`mouse_move`/`mouse_click`/`mouse_drag` 使用**桌面屏幕**坐标并驱动真实光标；两种坐标不可互换。

**自动化 profile。** 未设置 `BROWSERTAP_MODE` 时默认使用 `lab`，并按
`BROWSERTAP_LAB_NO_ELICIT=1` 处理，物理输入和站点 `allow` 不进行 elicitation。
`safe` profile 对每次操作进行询问。两种 profile 均保留跨进程锁、安静窗口、目标激活、所有权
和 `on_screen` 检查。

**对话框策略必须显式理解。** `execute_js(dialog_policy=...)`、`open_url(beforeunload=...)` 和
`handle_dialog(action=...)` 均支持 `dismiss`（默认）、`accept` 和 `manual`。全局默认优先保留页面；
仅在显式选择 `accept` 或 lab 的 host 规则匹配时自动离开。`handle_dialog` 会在三秒内应答，
否则返回 `no_dialog` 或结构化错误；`resolve_leave_dialog` 仅在协议方式失败且 lab 允许时使用物理 Enter。

**站点权限是短期租约。** `set_site_permission` 针对单个 origin 生效 60–600 秒，记录原设置，并在
到期、显式 `reset_site_permissions` 或 service worker 重启后恢复。`safe` profile 的每次 `allow` 都需
批准；浏览器 API 无法恢复的能力（例如 clipboard、企业托管设置和 OS 级权限对话框）返回
`unsupported` 或 `requires_user_action`。

**挑战页继续使用原浏览器会话。** Cloudflare Turnstile 等控件在同一个已连接标签页中由
`page_click` 处理，并有尝试次数上限。无进展时返回 `challenge_stalled`，后续处理应在同一标签页
中完成。BTAP 不会启动 Playwright、无头浏览器或独立自动化 profile 作为后备路径。

**工具契约变更需要重新加载。** MCP 客户端在启动会话时读取工具 schema 和描述；升级服务后应重启
MCP 会话或客户端。扩展源文件变更需要在 `chrome://extensions` 手动 **Reload**；
`chrome.runtime.reload()` 只重启 service worker，不能可靠地从磁盘重新读取源文件。

### 并行任务中的标签页所有权

使用标签页前应先确定其归属。**U（用户标签页）**是任务首次调用 `list_tabs` 时已经存在的页面，
默认不得关闭或导航。**A（Agent 标签页）**由本任务调用 `open_new_tab` 创建；应保存其
`session_id`、`generation` 和 `owner_id`，后续操作均显式使用该会话，并在清理阶段调用
`close_tabs(..., owner_id=...)`。**B（借用标签页）**是临时使用的 U；使用前记录
`original_url`，任务结束时在标签页仍存在的情况下恢复 URL，且不得关闭。

推荐顺序为：调用 `list_tabs`；只在只读或轻量操作中借用匹配标签页；导航、表单和其他明显状态
变更使用 A；没有匹配标签页时创建 A；任务结束后仅关闭 A。不得将初始标签页集合登记为 owned，
不得关闭 U/B、依赖共享默认 session、复用旧原生标签页 ID、绕过 generation 检查或遗漏 A 的清理。
并行任务应分别使用独立的 A。

### 结构化状态与恢复字段

预期内的中断通过 `status` 字段返回，而不是抛出异常：

| `status` | 含义 |
|---|---|
| `ok` / `success` | 完成并在协议允许的范围内验证过 |
| `redirected` | 导航落在与请求不同的 URL(登录墙、SSO、规范化重写) |
| `navigated` | `execute_js` 导致页面导航，原返回值不可用；`landed_url` 表示最终地址 |
| `blocked_by_dialog` | JavaScript 对话框保持打开，等待 `handle_dialog` 处理 |
| `blocked_by_beforeunload` | 导航已取消以保留页面；需要离开时使用 `beforeunload="accept"` 重新调用 |
| `dialog_handle_failed` | 已检测到对话框，但应答失败；标签页可能仍处于阻塞状态 |
| `navigation_failed` / `navigation_timeout` | `open_url` 超时未完成,或浏览器报错 |
| `triggered` 且 `type="download"` | `open_url` 被浏览器下载取代。只有 CDP 同时报告 `isDownload=true` 时 `ERR_ABORTED` 才可能是正常下载语义;要完成状态和本地路径请用 `download_file` |
| `requires_user_action` | 批准被拒绝、取消或不可用；未执行操作 |
| `busy` | 另一个 BTAP 进程持有物理输入锁，或标签页已有挂起的 manual 执行；调用立即返回且不排队 |
| `input_activity_detected` | 安静窗口期间检测到鼠标或键盘活动；未发送物理输入 |
| `activation_failed` | 无法确认目标标签页显示在屏幕上；未发送物理输入 |
| `unsupported` | 浏览器或扩展 API 提供不了(例如 clipboard 权限租约) |
| `challenge_stalled` | 浏览器挑战在尝试上限内没有进展；需要用户继续处理该标签页 |
| `no_response` | 脚本未送达或调用超时；存在副作用的操作不应直接重试 |
| `not_found` | 选择器没有匹配到任何元素;没有派发输入 |
| `bridge_error` | Bridge 调用失败。它可能出现在 `error_code` 或诊断字段，而非顶层 `status`；重试前先运行 `list_tabs`/`doctor`。 |
| `switched_session` | 补充字段，表示仅在隐式默认目标失效时自动换到另一个活动标签页。继续前应核对新目标；显式指定的失效 session 不会被替换。 |

## 风险提示

本服务操作真实浏览器会话，并可在授权后操作真实桌面。其权限范围包含所连接 profile 中的现有登录态。

- 鼠标移动、点击、键盘输入和热键均为操作系统级真实输入，不是页面合成事件。`safe` 逐次询问，
  `lab` 默认免询问或按配置恢复会话级询问；操作获准后将直接影响真实桌面。
- 页面内容属于不可信输入，可能包含 prompt injection；页面中的指令不因浏览器连接成功而可信。
- BTAP **不是**安全隔离边界。参见 [MCP 安全最佳实践](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)。
- 不应连接 MCP 客户端无须访问的敏感账号。共享机器或生产机器需要单独评估误操作风险。

扩展所需权限包括 `cookies`、`tabs`、`debugger`、`scripting`、`alarms`、`storage`、
`contentSettings`、`declarativeNetRequest`、`management`、`bookmarks`、`downloads` 以及
`<all_urls>`。`declarativeNetRequest` 仅在指定标签页执行依赖 eval 的命令期间临时移除 CSP
响应头；规则为 session 级、带引用计数，并在 cleanup 中删除，不是全浏览器持久关闭 CSP。
完整权限与 loopback 威胁模型见[安全说明](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md)。

## 工具列表

多数工具接受可选的 `session_id` 以指定标签页；省略时使用当前默认目标。**状态变更操作应显式传入
`session_id`**。默认目标由 bridge 上的任务共享，可能被其他任务调用 `switch_tab` 修改。会话 ID
形如 `chrome_a1b2c3:456`，应完整传入，不得拆分。标注**零标签页可用**的工具通过扩展 service
worker 通道执行，在普通标签页全部关闭时仍可使用。

<details>
<summary><b>标签页与导航</b></summary>

- **get_setup_status** —— 返回 `package_version`、`bridge_version`、`extension_version`、`protocol_version`、连接状态、端口、标签页与恢复动作。允许自动拉起时，未监听的 bridge 会自动启动；`restart_bridge_required=true` 表示仍在运行的 bridge 必须执行 `browsertap bridge --restart` 才能替换。`reload_extension_required=true` 表示 unpacked 扩展受平台限制，必须手动 Reload。`restart_mcp_session_required=true` 是反方向：某个组件**比运行中的服务更新**，过期的是当前进程，只有重启 MCP 会话或客户端才能消除；此时另外两个标志保持 false，因为重启 bridge 或重新加载扩展只会再报同一个不匹配。无参数
- **get_automation_profile** —— 查看当前 MCP 进程使用 `lab` 还是 `safe` profile
- **set_automation_profile** —— 切换当前 MCP 进程的 `lab|safe` profile;覆盖值不会持久化或重载扩展
  - `mode`(string):`lab` 或 `safe`
- **list_tabs** —— 列出已连接的标签页,每项带 `browser` 字段。无参数
- **list_all_tabs** —— *(零标签页可用)* 列出全部标签页,含 `list_tabs` 隐藏的 `chrome-extension://` 页面。这类页面永远不会成为会话,所以没有 session id,要用 `cdp_command(tab_id=...)` 操作
  - `session_id`(string,可选):问哪个浏览器
- **switch_tab** —— 指定后续调用的**目标**标签页。`url_pattern` 必须只匹配一个标签页；若匹配多个，需传入目标的完整 `session_id`。默认 `activate=false`，不会激活标签页或聚焦浏览器。仅在确需改变前台状态时传入 `activate=true`，或调用 `activate_tab`
  - `session_id`(string,可选)、`url_pattern`(string,可选):子串匹配、`browser`(string,可选):`chrome` / `edge` / `opera`、`activate`(boolean,可选):默认 `false`
- **activate_tab** —— 激活标签页并聚焦其窗口。这是显式改变浏览器前台状态且不发送物理输入的方式。在 Windows 上，BTAP 会先请求恢复最小化窗口；若响应仍为 `on_screen=false`，表示无法确认目标已显示，此时不得执行屏幕坐标输入
  - `session_id`(string,可选)
- **open_url** —— 当前标签页导航到 URL,并报告**实际落地**的地址。全局默认仍是 `dismiss`;lab 命中配置的 shell/IDE host 时自动 accept。协议 `navigate` 在重 SPA 失效时自动降级 `Page.navigate`。CDP 返回 `isDownload=true` 时改为返回 `{type:"download",status:"triggered"}`,不再只报 `navigation_failed`;此时附带的 `ERR_ABORTED` 是正常下载导航语义
  - `url`(string)、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`、`beforeunload`(string,可选):默认 `dismiss`、`intent_leave`(boolean,可选):`false` 强制保留页面
- **download_file** —— 通过 Chrome 原生下载管理器下载 HTTP(S) URL，并使用该浏览器 profile 的 Cookies 和登录态。默认等待完成，返回 `status="completed"` 和已验证的绝对 `path`；中断返回 `failed`，超时或 `wait=false` 返回带 `download_id` 的 `in_progress`。显式 `session_id` 必须仍然有效，失效时不会改用其他 profile。附件下载应使用本工具，不应在页面内调用 `fetch`
  - `url`(string)、`filename`(string,可选):相对下载名称、`directory`(string,可选):任意绝对目标目录并自动建父目录;要求 `wait=true`、`wait`(boolean,可选):默认 `true`、`timeout`(number,可选):默认 60 秒,最大 1800、`session_id`(string,可选):选择浏览器 profile、`overwrite`(boolean,可选):默认 `false`,最终目标已存在时拒绝,只有显式 `true` 才替换。带 `directory` 的调用若超时会返回 `directory_applied=false`:后续搬移不再受跟踪,Chrome 可能继续下载到浏览器默认目录
- **open_new_tab** —— 默认在后台创建标签页，为本次创建生成唯一 `operation_id`，并在限定时间内等待准确的 session/generation 注册；仅在确需前台操作时传入 `active=true`。返回 `{operation_id,tab_id,session_id,generation,ready,owned,opener,owner_id,load_status}`。扩展会对相同 operation ID 去重；只有包含准确 `client_id+tab_id+generation` 的 completed 记录才登记 ownership，即使 `ready=false`；`ready` 仅表示 session 工具是否可立即使用。创建投递前 registry 状态不确定时返回 `status="unknown",may_have_created=false,retry_safe=true`；创建已投递但 ACK/对账仍不确定时返回 `status="unknown",may_have_created=true,retry_safe=false`。随机 `owner_id` 仅用于该任务清理。对于已投递但结果仍不确定的创建，不得为同一请求再次调用 `open_new_tab`；应保留 `operation_id` 作为诊断信息
  - `url`(string)、`timeout`(number,可选):默认 `15`、`active`(boolean,可选):默认 `false`、`session_id`(string,可选):选择浏览器/profile、`owner_id`(string,可选):让同一任务的多个新 tab 共用一个 owner
- **close_tabs** —— *(零标签页可用)* 接受原生数字 tab ID 或完整 `client:tabId` session ID，对 `chrome-extension://` 页面同样有效。默认 `only_if_agent_owned=true`，必须传入 `open_new_tab` 返回的 `owner_id`，并在关闭前核对当前 lifecycle generation；用户预存标签页、其他 Agent 的标签页和复用 ID 的新生命周期均会被拒绝。若用户已关闭 owned 标签页，清理返回 `status=already_gone, closed_by=user`，不会使用旧原生 ID 关闭其他标签页；实际关闭 owned 标签页时返回 `closed_by=agent`；显式关闭非 owned/U 标签页时返回 `closed_by=none`，且不计入本任务 owned 清理。仅当用户明确要求关闭非 owned/U 标签页时，才可设置 `only_if_agent_owned=false`
  - `tab_id`(integer/string 或数组)、`session_id`(string,可选)、`owner_id`(string,安全默认下必填)、`only_if_agent_owned`(boolean,默认 `true`)
</details>

<details>
<summary><b>页面读取与执行</b></summary>

- **scan_page** —— 把页面读成简化 HTML 或纯文本。返回 `links`,把正文里每个 `#rN` 引用映射到绝对 URL;有内容留在视区外时返回 `offscreen` 和 `hint`
  - `session_id`(string,可选)、`text_only`(boolean,可选):默认 `false`、`cutlist`(boolean,可选):默认 `true`,把重复列表裁成少量样本、`maxchars`(integer,可选):默认 `35000`、`instruction`(string,可选)、`extra_js`(string,可选)、`timeout`(number,可选):默认 `15`
- **wait_for** —— 等待指定条件成立后返回。与轮询 `scan_page` 相比，该工具避免重复序列化完整 DOM。四个条件必须且只能提供一个；`selector` 接受 CSS 字符串或“后台页面输入”一节所述的结构化 locator
  - `selector`(string/object,可选):CSS 或结构化 locator、`text`(string,可选)、`url_pattern`(string,可选)、`js`(string,可选)、`gone`(boolean,可选):默认 `false`、`timeout`(number,可选):默认 `15`、`session_id`(string,可选)
- **wait_for_url** —— 等导航落定:阻塞到标签页 URL 匹配 `url_pattern`(正则,或纯子串,两种都试),并且在 `wait_ready=false` 之外还要求 `document.readyState` 为 `complete`,然后返回最终的 `url`、`title` 和 `ready_state`。在会触发跳转的点击或 `open_url` 之后用它;`wait_for(url_pattern=...)` 只查 URL,新文档还是空白的时候就可能返回。轮询发生在页面内且跨导航分块,长等待仍然很便宜
  - `url_pattern`(string):匹配 URL 的正则或子串、`timeout`(number,可选):默认 15、`wait_ready`(boolean,可选):要求 `readyState === 'complete'`,默认 `true`、`session_id`(string,可选)
- **scroll_page** —— 滚动并报告新位置,长页面可以分几屏读完
  - `to`(string,可选):默认 `bottom`,也可传 `top`、像素偏移或要滚到可见的 CSS 选择器、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **execute_js** —— 在页面中执行 JavaScript 并返回结果。`timeout` 是覆盖对话框策略设置、monitor 快照、投递/重试、导航检查和清理的单一总 deadline；显式 `session_id` 在这些浏览器往返中保持不变，不依赖共享默认目标。脚本导致页面导航时返回 `status="navigated"` 和 `landed_url`，而不是 `success`，且脚本返回值不可用。`dialog_policy` 控制 `alert`/`confirm`/`prompt`：`dismiss`（默认）和 `accept` 直接应答并记录到 `dialogs`；`manual` 保持原生对话框打开、暂停脚本并返回 `blocked_by_dialog`，后续由 `handle_dialog` 处理。标签页已有 manual 执行暂停时立即返回 `busy`。等待页面状态应使用 `wait_for`/`wait_for_url`，不要在 `execute_js` 中嵌入延迟 `setTimeout` 或 sleep Promise；`no_response` 会返回 `delivery_state` 与 `retry_safe`，已 ACK、可能执行过副作用的脚本不会被自动重放
  - `script`(string)、`session_id`(string,可选)、`no_monitor`(boolean,可选):默认 `false`、`timeout`(number,可选):默认 `15`、`dialog_policy`(string,可选):`dismiss`(默认)、`accept` 或 `manual`
- **handle_dialog** —— 检查或应答某个标签页上留着的对话框。`action="manual"` 只上报不选择(`blocked_by_dialog`,没有对话框则是 `no_dialog`);`accept`/`dismiss` 应答并释放被暂停的 `execute_js` 或 `open_url`。`prompt_text` 给被 accept 的 `prompt` 提供文本
  - `action`(string):`dismiss`、`accept` 或 `manual`、`prompt_text`(string,可选)、`session_id`(string,可选)、`timeout`(number,可选):默认 `3`,上限 3 秒
- **resolve_leave_dialog** —— 用于处理 shell、ttyd 或 IDE 页面离开时已出现的对话框：先执行两次协议级 accept；仅在 lab 允许物理输入时使用 Enter 作为最后后备方案
  - `session_id`(string,可选)
- **upload_files** —— 给文件输入框设置文件,这是 JS 做不到的(`input.files` 只读)。整个序列走一个 CDP batch,保证 DOM nodeId 中途不失效
  - `selector`(string):`<input type=file>`、`paths`(string 或 string 数组):本地绝对路径、`session_id`(string,可选)、`timeout`(number,可选):默认 `30`
- **get_cookies** —— 读取页面 Cookies
  - `session_id`(string,可选)、`tab_id`(integer,可选)
- **set_cookies** —— 把 Cookie 写进真实浏览器 profile。接受单个 Cookie 对象或列表(JSON 文本也行):`name` 必填,其余可选 `value`/`url`/`domain`/`path`/`expires`(Unix 秒)/`httpOnly`/`secure`/`sameSite`。走 CDP `Network.setCookie`,所以 HttpOnly 和跨路径 Cookie 都能写;仅当 CDP 不可用时才退回 `document.cookie`,并如实报告哪些 Cookie 没能带上 HttpOnly。既没给 `url` 也没给 `domain` 的 Cookie 作用域限定在当前页面
  - `cookies`(string 或 list 或 dict)、`session_id`(string,可选)、`tab_id`(integer,可选)、`timeout`(number,可选):默认 `20`
- **delete_cookies** —— 按名字删除 Cookie。先走 CDP `Network.deleteCookies`,失败退回 `document.cookie` 过期法。用 `domain`/`path` 限定作用域,或给 `url` 只删一个站点
  - `name`(string)、`domain`(string,可选)、`path`(string,可选)、`url`(string,可选)、`session_id`(string,可选)、`tab_id`(integer,可选)、`timeout`(number,可选):默认 `20`
- **storage_get** —— 读 localStorage 或 sessionStorage。给 `key` 取单个值;不给则用 `offset`/`max_items`/`max_bytes` 分页,返回 `next_offset` 和 `truncated`;默认超时 30 秒且失败不会关闭 MCP 会话
  - `key`、`area`、`session_id`、`offset`、`max_items`、`max_bytes`、`timeout`(均可选);`timeout` 默认 `30`
- **storage_set** —— 写一个 localStorage/sessionStorage 值(非字符串值先 JSON 编码)。写完立刻回读验证,配额满或隐私模式下的失败会被如实报告,不会静默丢失
  - `key`(string)、`value`(string)、`area`(string,可选):`local`(默认)或 `session`、`session_id`(string,可选)、`timeout`(number,可选):默认 `30`
</details>

<details>
<summary><b>后台页面输入</b></summary>

向指定标签页派发受信任的 CDP 输入事件。这些工具不会激活标签页、聚焦窗口或移动桌面光标；
每次响应均包含 `foreground_changed: false` 和 `input_mode: "cdp"`。所有坐标均为相对页面区域
左上角的**视口**坐标，而非桌面坐标。

应显式传入 `session_id`。调用期间，驱动绑定到该标签页，并在结束后恢复共享默认目标，因此定向调用
不会改变其他任务的目标。显式指定已失效的标签页时，调用会被拒绝，不会自动改用其他标签页。

`selector` 保持兼容 CSS 字符串,也可传结构化 locator 对象,主定位键必须且只能有一个:`css`、`role`(可带 `name`)、`text` 或 `label`;`exact` 控制 role/name 或 text 精确匹配;`frame` 逐层进入同源 iframe;`shadow` 逐层进入开放 Shadow DOM。零匹配返回 `not_found`,多匹配返回 `ambiguous`,跨域 iframe/关闭 shadow root 会明确上报且不派发输入。

- **page_click** —— 点 CSS/结构化 `selector` 或视口坐标。定位方式二选一。selector 模式中,未提供 offset 的轴取元素中心;显式提供的 `offset_x`/`offset_y` 则从元素左上角按对应轴计算。缺失、歧义、不可交互、跨域 iframe 或关闭 shadow root 都返回结构化状态且不派发。selector 模式还会在派发前在页面里做一次命中判定:在折叠线以下就先滚动进视口(`scrolled_into_view`),那个像素属于别的元素时返回 `obscured` 并用 `occluded_by` 指出遮挡者,滚动后仍不在屏幕上返回 `outside_viewport` —— 这两种情况都不点,因为派发出去的点击会落在别的元素上并报成功。命中通过的点击带 `hit_verified: true`。坐标模式不做命中判定:坐标指的是像素,不是元素。验证码仍有 `challenge_detected`、`attempts` 与 `challenge_stalled` 上限
  - `selector`(string/object,可选)、`x`(number,可选)、`y`(number,可选)、`offset_x`(number,可选)、`offset_y`(number,可选)、`button`(string,可选):默认 `left`、`clicks`(integer,可选):默认 `1`、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **page_type** —— 往 CSS/结构化 locator 选中的字段输入;省略 `selector` 时使用当前焦点。Xterm.js 自动改投 helper textarea;缺失、歧义、只读或不可输入目标不会收到文本/按键。`clear=true` 先选中已有内容,`submit_key` 事后按键
  - `text`(string)、`selector`(string/object,可选)、`clear`(boolean,可选):默认 `false`、`submit_key`(string,可选)、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **page_press** —— 在标签页里按一个键或逗号分隔的修饰键组合,如 `enter` 或 `ctrl,shift,k`
  - `keys_csv`(string)、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **page_drag** —— 在视口两点之间拖拽,作为一次不中断的事件序列
  - `x1`(number)、`y1`(number)、`x2`(number)、`y2`(number)、`duration`(number,可选):默认 `0.3`、`button`(string,可选):默认 `left`、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
</details>

<details>
<summary><b>站点权限</b></summary>

由 `chrome.contentSettings` 支撑的临时、origin 作用域权限租约。每条租约都记录原设置并在到期、显式 reset、service worker 重启或浏览器重启后恢复。

- **set_site_permission** —— 给一个 origin 设置一种权限,60–600 秒。`safe` 下每次 `allow` 都要批准;默认 `lab` 按 `BROWSERTAP_LAB_NO_ELICIT=1` 直接执行。拒绝返回 `requires_user_action` 且不改变任何东西;不可恢复能力返回 `unsupported`
  - `permission`(string)、`setting`(string):`allow`、`block` 或 `ask`、`origin`(string,可选):默认取标签页 origin、`duration_seconds`(integer,可选):60–600,默认 `300`、`session_id`(string,可选)
- **reset_site_permissions** —— 不等到期,现在就把匹配的租约恢复。`origin` 和 `permission` 都不给就恢复那个浏览器上的全部租约
  - `origin`(string,可选)、`permission`(string,可选)、`session_id`(string,可选)
</details>

<details>
<summary><b>CDP</b></summary>

- **cdp_command** —— 发送单条 CDP 命令
  - `method`(string):如 `Page.navigate`、`params_json`(string,可选):JSON 对象的文本形式、`session_id`(string,可选)、`tab_id`(integer/string,可选)、`extension_id`(string,可选)、`target_id`(string,可选)、`timeout`(number,可选):默认 `20`
- **cdp_batch** —— 批量发送,`batch_json` 必须是带 `cmd: "batch"` 的 JSON 对象
  - `batch_json`(string)、`session_id`(string,可选)
- **debugger_targets** —— *(零标签页可用)* 列出所有可 attach 的 CDP 目标,包括 service worker 和扩展背景页 —— 这些在 `list_tabs` 里永远看不到
  - `session_id`(string,可选)
- **save_pdf** —— 有界 `Page.printToPDF`,验证 PDF 后原子写文件;超时会强制释放 debugger lease
  - `save_path`(string)、`timeout`(number,可选):默认 `30`、`session_id`、`landscape`、`print_background`、`prefer_css_page_size`、`scale`、`page_ranges`(其余可选);`landscape` 默认 `false`,`print_background` 和 `prefer_css_page_size` 默认 `true`,`scale` 默认 `1.0`

> **操作其他扩展的限制**：Chrome 默认在 attach 阶段拒绝跨扩展调试，`tab_id`、`extension_id`
> 和 `target_id` 三种寻址方式均受该限制，除非 Chrome 使用
> `--silent-debugger-extension-api` 启动。这些参数主要用于操作 BTAP 扩展自身目标和执行故障诊断。
</details>

<details>
<summary><b>扩展管理</b></summary>

- **extension_path** —— 未打包扩展的绝对路径,用于手动安装。无参数
- **list_extensions** —— *(零标签页可用)* 已安装扩展的 id、名称、启用状态、类型、版本
  - `session_id`(string,可选)
- **set_extension_enabled** —— *(零标签页可用)* 启用或禁用已安装的扩展。Chrome 没有任何 API 可以*安装*扩展,所以这里只能开关已存在的
  - `extension_id`(string)、`enabled`(boolean)、`session_id`(string,可选)
- **uninstall_extension** —— *(零标签页可用)* 卸载其他扩展；默认显示 Chrome 确认框。仅对明确选定的测试扩展设置 `show_confirm_dialog=false`；活动通道无法卸载 BTAP 自身
  - `extension_id`(string)、`show_confirm_dialog`(boolean,可选):默认 `true`、`session_id`(string,可选)
- **get_bookmarks** —— *(零标签页可用)* 读取书签树
  - `session_id`(string,可选)
- **create_bookmark** —— *(零标签页可用)* 创建书签或文件夹
  - `title`(string)、`url`(string,可选):省略则创建文件夹、`parent_id`(string,可选)、`session_id`(string,可选)
- **remove_bookmark** —— *(零标签页可用)* 删除书签或递归删除文件夹
  - `bookmark_id`(string)、`recursive`(boolean,可选):默认 `false`、`session_id`(string,可选)
- **call_extension** —— *(零标签页可用)* 向另一个扩展发送 JSON;目标必须启用并通过 `externally_connectable` 允许 BTAP
  - `extension_id`(string)、`message_json`(string):JSON 文本、`session_id`(string,可选)
</details>

<details>
<summary><b>Network 与 Console 捕获</b></summary>

- **network_capture_start** —— 在指定 tab 上开始收集请求/响应和可选 body;默认 500 条环形缓冲、单 body 256 KiB
  - `session_id`(string,可选)、`include_bodies`(boolean,可选):默认 `true`、`max_entries`(integer,可选):默认 `500`,范围 10–2000、`max_body_bytes`(integer,可选):默认 `262144`,范围 1024–2097152、`body_timeout`(number,可选):默认 `5`,范围 0.1–10 秒、`timeout`(number,可选):默认 `10`
- **network_capture_stop** —— 返回当前 Network 捕获并释放 debugger lease;可只过滤返回结果,不改变捕获上限和 cleanup。`url_pattern` 由浏览器按 JavaScript `RegExp` 编译;非法表达式返回结构化错误,捕获仍保持运行以便修正后重试
  - `session_id`(string,可选)、`url_pattern`(string,可选):JavaScript `RegExp`、`resource_type`(string,可选)、`status_min`/`status_max`(integer,可选):100–599、`include_response_bodies`(boolean,可选):默认 `true`、`timeout`(number,可选):默认 `10`
- **console_capture_start** —— 开始收集 `console.*` 与未捕获异常
  - `session_id`(string,可选)、`max_entries`(integer,可选):默认 `500`,范围 10–5000、`timeout`(number,可选):默认 `10`
- **get_console_messages** —— 用 `offset`/`max_items` 分页读取或 `clear`;`filter='user'` 只保留页面 MAIN/default context 输出,空值/`all` 保留完整 buffer
  - `session_id`(string,可选)、`offset`(integer,可选):默认 `0`、`max_items`(integer,可选):默认 `200`、`clear`(boolean,可选):默认 `false`、`filter`(string,可选):`user` 或 `all`、`timeout`(number,可选):默认 `10`
- **console_capture_stop** —— 返回剩余 console 消息并释放 debugger lease
  - `session_id`(string,可选)、`timeout`(number,可选):默认 `10`
</details>

<details>
<summary><b>截图</b></summary>

- **capture_page_screenshot** —— 通过 CDP 截视口、`full_page` 或显式 `clip`;PNG/JPEG/WebP 可选,JPEG/WebP 支持 `quality`。返回元数据和 MCP 图片内容;`save_path` 只额外落盘
  - `session_id`(string,可选)、`tab_id`(integer,可选)、`format`(string,可选):默认 `png`、`full_page`(boolean,可选):默认 `false`、`clip`(object,可选):`x`,`y`,`width`,`height`,可带 `scale`、`quality`(integer,可选):0–100、`save_path`(string,可选)、`return_base64`(boolean,可选):默认 `false`、`timeout`(number,可选):默认 `20`
- **capture_desktop_screenshot** —— 捕获当前可见的操作系统虚拟桌面（全部显示器），返回元数据和 MCP 图片内容。它不是指定/后台标签页截图，可能包含其他应用；`save_path` 只额外落盘
  - `save_path`(string,可选)、`return_base64`(boolean,可选):默认 `false`
</details>

<details>
<summary><b>物理输入</b></summary>

这些工具按**桌面屏幕**坐标发送真实操作系统级输入，会移动实际光标或向当前焦点对象发送按键。
应优先使用可在后台标签页中运行的 `page_*` 工具。仅在页面级输入无法完成操作时使用桌面工具，
例如浏览器界面、原生文件选择器、扩展弹窗和操作系统对话框。

`safe` 模式下这五个直接工具逐次 elicitation;默认 `lab` 按 `BROWSERTAP_LAB_NO_ELICIT=1` 免询问执行,显式设为 false 才恢复会话级批准。拒绝、取消或不支持 elicitation 时返回 `requires_user_action`;无论哪种模式,锁/安静窗口/ownership/目标提前台与屏幕确认都不会跳过。`resolve_leave_dialog` 是第六条物理输入路径，只能在两次协议处理失败后用 Enter 兜底，并经过相同闸门。

物理输入按固定顺序执行：获取跨进程锁（已占用时立即返回 `busy`，不排队）；等待短暂安静窗口
（检测到鼠标或键盘活动时返回 `input_activity_detected`，不发送输入）；激活目标标签页；发送输入。
五个直接工具都接受与其他工具相同的 `session_id`。省略时使用全局共享默认目标，该目标可能已被
其他任务修改。仅在有意操作当前可见桌面或原生 UI 时使用 `activate_session="none"`。无法确认标签页显示
在屏幕上时返回 `activation_failed`，且不发送输入。

- **mouse_move** —— `x`(integer)、`y`(integer)、`duration`(number,可选):移动耗时秒数,默认 `0`(直接跳到目标点)、`session_id`(string,可选):要提前台的标签页、`activate_session`(string,可选):默认 `current`(先把目标标签页提前台),也可传 session id 或 `none`
- **mouse_click** —— `x`(integer,可选)、`y`(integer,可选):都省略时点当前指针位置、`button`(string,可选):默认 `left`,也接受 `right`/`middle`、`clicks`(integer,可选):默认 `1`、`interval`(number,可选):多次点击的间隔秒数,默认 `0.1`、`session_id`(string,可选):要提前台的标签页,正常情况就传这个、`activate_session`(string,可选):默认 `current`,也可传 session id 或 `none`
- **mouse_drag** —— `x1`(integer)、`y1`(integer)、`x2`(integer)、`y2`(integer)、`duration`(number,可选):按住按键移动的秒数,默认 `0.3`、`button`(string,可选):默认 `left`、`session_id`(string,可选):要提前台的标签页、`activate_session`(string,可选):默认 `current`,也可传 session id 或 `none`
- **type_text** —— `text`(string)、`interval`(number,可选):每个字符的间隔秒数,默认 `0.01`、`click_x`(integer,可选)、`click_y`(integer,可选):先点这里让输入框获得焦点、`session_id`(string,可选):要提前台的标签页,正常情况就传这个、`activate_session`(string,可选):默认 `current`,也可传 session id 或 `none`
- **hotkey** —— `keys_csv`(string):逗号分隔,如 `ctrl,c`、`session_id`(string,可选):要提前台的标签页、`activate_session`(string,可选):默认 `current`,也可传 session id 或 `none`
- **pointer_info** —— 当前指针坐标和屏幕尺寸。只读,不需要批准。无参数
</details>

## 故障排查

应先运行 `browsertap doctor`。连接、版本、对话框、权限和物理输入相关的恢复流程见
[故障排查指南](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md)。

## 致谢

BTAP 由 `LinVireo` 维护。[LICENSE](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE)
中的 MIT 版权声明按原样保留（`zhea`）；维护者与版权归属是两个不同角色。本发行版的权威公开仓库为
`LinVireo/browsertap-mcp`。

这里的浏览器层有一小部分来自 [GenericAgent](https://github.com/lsdefine/GenericAgent)，
感谢该项目及其作者提供的原始实现。下列文件源出于此，且此后均已大幅重写；本发行版的其余部分——
MCP 工具面、bridge 及其 token 鉴权、Chrome 扩展、发布证据链、测试套件与两份 README——均在此编写。

源出 GenericAgent 的部分：
- `TMWebDriver.py`（现由 `browser_bridge.py` 维护）
- `simphtml.py`
- `tmwd_cdp_bridge` Chrome 扩展资源

Fork 或二次分发时应保留上述致谢。

## 许可证

MIT
