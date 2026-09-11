# browsertap-mcp

[English](https://github.com/LinVireo/browsertap-mcp/blob/main/README.md) | 中文文档

[![离线 CI](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE)

[使用指南](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.zh-CN.md) · [故障排查](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md) · [安全说明](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md) · [隐私政策](https://github.com/LinVireo/browsertap-mcp/blob/main/PRIVACY.md) · [贡献指南](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.zh-CN.md) · [变更记录](https://github.com/LinVireo/browsertap-mcp/blob/main/CHANGELOG.md)

**让 agent 操作你正在使用的 Chrome、Edge 或 Opera。**
BTAP 通过浏览器扩展连接 MCP 客户端，复用已打开的标签页和现有登录态。
agent 可以读取页面、填写表单、下载附件、检查网络活动；页面输入默认在指定后台标签页中完成，
不移动桌面光标。

BTAP 连接的是真实浏览器 profile，不是临时沙箱。只连接允许该 agent 访问的账号和数据。
首次安装扩展需要手动操作，见下面的[上手步骤](#60-秒上手)和[风险提示](#风险提示)。

## 60 秒上手

共三步，手动加载扩展可能需要超过一分钟。

1. **安装软件包**并定位扩展目录：

   ```bash
   python -m venv .venv
   ./.venv/bin/python -m pip install browsertap-mcp
   ./.venv/bin/browsertap extension-path
   ```

   Windows PowerShell 改用 `.\.venv\Scripts\python.exe` 和
   `.\.venv\Scripts\browsertap.exe`。普通页面和浏览器工具不需要桌面依赖；
   仅在需要受限物理兜底时，在同一环境运行 `pip install "browsertap-mcp[desktop]"`。

2. **手动加载扩展。** 打开 `chrome://extensions`，开启**开发者模式**，选择
   **加载已解压的扩展程序**，选中刚打印的目录。页面工具需要一个正常的 `http://` 或 `https://` 页面。

3. **连接 MCP 客户端**到已安装的可执行文件。以 Claude Code 为例：

   ```bash
   claude mcp add browsertap -- "$PWD/.venv/bin/browsertap"
   ```

   其他客户端及 Windows 路径见[快速开始](#快速开始)。使用明确的可执行文件路径时，
   不需要先激活虚拟环境。

首次可问 agent：**列出已打开的标签页，再总结我选定的页面，不要导航或关闭它。**
连接失败或没有预期标签页时，在安装环境中运行 `browsertap doctor`，按其 `action` 处理。
下文为简洁使用 `browsertap` 短命令；若未加入 `PATH`，请使用其完整路径。

## 按读者找文档

| 读者 | 从哪里开始 |
| --- | --- |
| 安装和使用 BTAP 的用户 | 本 README；具体工作流和边界见[使用指南](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.zh-CN.md)。 |
| 排查本地连接的用户 | 携带 `browsertap doctor` 结果阅读[故障排查](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md)。 |
| 调用 BTAP 工具的 agent | 客户端实际收到的工具 schema，以及可选的[调用方 skills](#agent-skill可选)。 |
| 修改 BTAP 的开发者或 agent | [贡献指南](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.zh-CN.md)；编码 agent 还需读 [AGENTS.md](https://github.com/LinVireo/browsertap-mcp/blob/main/AGENTS.md)。 |

下方[工具列表](#工具列表)保留本源码树完整的参数参考。使用已安装版本时，应对照同版本 tag
中的文档；开发分支可能包含尚未发布的修改。实际可调用能力以连接的服务端为准。

## 核心能力

- **读取和检查页面**：简化 HTML/文本、JavaScript、页面截图，以及有容量限制的网络和 console 捕获。
- **后台页面交互**：`page_click`、`page_type`、`page_press`、`page_drag` 派发受信任的 CDP 输入，不移动桌面光标。
- **复用现有 profile**：带登录态的下载、Cookies、storage、书签、扩展管理和临时站点权限。
- **处理中断**：显式对话框策略、条件等待，以及用于补查延迟结果的操作句柄，避免重复执行。
- **并行任务隔离**：显式浏览器/标签页目标和按所有权清理。不同标签页可并行，但同一 profile 的状态不隔离。
- **多浏览器共存**：同一 bridge 可连接 Chrome、Edge、Opera 和多个 profile；部分浏览器级操作不需要页面标签页。

## 能力分层

BTAP 把能力分成三层，让 agent 按任务选择最窄、最稳定的接口：

- **Page 能力**：页面 DOM、JavaScript、等待、滚动、截图，以及指定标签页内的
  `page_*` CDP 输入。普通网页工作默认走这一层，不使用操作系统鼠标或键盘。
- **Browser 能力**：真实 Chromium profile 及其浏览器原生能力，包括标签页、下载、Cookies、
  storage、站点权限、书签、扩展、service worker 消息和原生 CDP；很多操作不需要前台标签页。
- **Desktop 能力**：不是通用的公开能力。当前唯一的显式 opt-in 是仅限 lab、用于页面离开
  对话框的 `resolve_leave_dialog` 末尾 Enter 兜底；浏览器 chrome、原生文件选择器、扩展 UI、
  打印/保存对话框及其他页面层缺口仍然不支持。0.5.0 移除的七个全局 OS 键鼠/桌面截图工具不会
  作为普通网页失败后的兜底回来。

`resolve_leave_dialog` 仍是页面范围内、仅限 `lab` 的恢复流程；末尾的 Enter 兜底是受限例外，
不是通用桌面能力。实际注册表位于 `get_setup_status` 返回值的 `data.capability_registry`，客户端不必
根据包 extras 或文档猜测当前工具面。每个条目还会说明 target 是 none、optional 还是
required，操作是 read、write 还是 mixed，以及是否涉及 desktop opt-in。所有公开工具现在都在
不改工具名的前提下返回 `btap.result.v1` envelope：成功的操作数据放在 `data`，明确的旧版失败
payload 保留在 `legacy`，`error`/`error_code`、`retryable`、`target` 和 `diagnostics` 提供稳定的
机器可读状态。失败同时设置 MCP `isError=true`。数组、对象、HTML 和其他大段正文从 `data` 或
`legacy` 读取，顶层仅保留少量标量兼容字段。重试以 envelope 的裁定为准：明确的
`retry_safe=false` 或可能已经执行的证据优先于连接错误的默认重试提示。

## 什么时候该用别的

需要复用现有 Chromium profile、并默认在后台标签页工作的任务适合 BTAP。
本项目不是 headless 测试运行器，不支持 Firefox/WebKit，也不提供通用桌面自动化。

隔离浏览器测试或基于无障碍快照的流程，可对照
[Playwright MCP](https://github.com/microsoft/playwright-mcp)；
以 DevTools 调试和性能分析为主的任务，可对照
[Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)。
复用真实浏览器并非 BTAP 独有，应按任务和所需 API 选择。
BTAP 注册全部 49 个工具，需要缩小工具面时由客户端筛选。

## 环境要求

- Python 3.10+
- Chrome、Edge 或 Opera
- Linux、macOS 或 Windows。普通页面、浏览器和 CDP 工具不需要操作系统级输入；只有
  仅限 lab 的 `resolve_leave_dialog` 物理兜底需要可用的桌面会话
- 运行中的 Chromium 用户会话，而不是隔离的 headless 容器。**故意不提供 Docker 镜像**：
  服务接的是**你自己**已登录的 Chrome，扩展要人手动加载一次
- Claude Code 或其他 MCP 客户端

## 快速开始

### 1. 安装

创建虚拟环境并安装软件包。只有需要剩余的、仅限 lab 的物理兜底时，才需要可选的 `desktop`
extra：

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install browsertap-mcp
.\.venv\Scripts\browsertap.exe extension-path
```

**Linux 或 macOS**

```bash
python -m venv .venv
./.venv/bin/python -m pip install browsertap-mcp
./.venv/bin/browsertap extension-path
```

核心安装 `pip install browsertap-mcp` 已足够支持页面、浏览器和 CDP 工具。它不包含
`pyautogui`、`mss`、`pillow`，这些依赖仅用于 `resolve_leave_dialog` 的 lab Enter 兜底及其
屏幕/输入检查；只有需要这条兜底时才加装 `[desktop]`。

要改这个项目本身（而不只是用它），改成 editable 安装：extras 一样，扩展目录和 skill
直接从工作树里读。

```bash
git clone https://github.com/LinVireo/browsertap-mcp.git
cd browsertap-mcp
python -m venv .venv
./.venv/bin/python -m pip install -e ".[dev,desktop]"
./.venv/bin/browsertap extension-path
```

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
后台标签页、前台激活、剩余物理兜底的闸门，以及多模态模型的适用场景。

## 配置

### 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `BROWSERTAP_BRIDGE_HOST` | `127.0.0.1` | 桥的绑定地址 |
| `BROWSERTAP_BRIDGE_PORT` | `18765` | WebSocket 端口。HTTP 使用 `PORT+1`，`PORT+2` 为锁 socket，保证同时只有一个 bridge **持有**前两个端口（第二个 bridge 不会退出，会转为通过第一个工作）。它与状态目录下的 `spawn.lock` 文件是两回事：后者负责避免多个 MCP 会话同时拉起多个守护进程。使用自定义端口时，还需单独告知扩展一次，见 [docs/TROUBLESHOOTING.zh-CN.md](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md)。 |
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
`healthy`、`starting`、`ext_never_registered`、`sw_slept_or_dropped`、`registering` 或
`bridge_unreachable`；`advice` 提供对应恢复建议。`starting` 表示 bridge 刚启动，正在等待扩展
握手；此时 `action` 为 `wait_for_extension`，等待几秒后再次运行 `doctor`。`registering` 表示
扩展已连接，但尚无正常的 `http(s)` 内容标签页完成注册。

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

这两份文档写给使用 BTAP 的 agent，不是修改 BTAP 源码的 agent。编码约定与验证命令见
[AGENTS.md](https://github.com/LinVireo/browsertap-mcp/blob/main/AGENTS.md) 和
[贡献指南](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.zh-CN.md)。

请把客户端的 skill 管理器**指向这个目录**，不要复制文件。复制出来的副本在内容恰好一致期间
看不出问题，等你升级包之后就静默收不到更新了。如果确实保留了副本，可以用
`python -m scripts.check_tool_docs --check-installed-skills --skill-mirror DIR`
与随包原件比对，并指出是哪一份漂了。

### 升级

以下版本标记随源码维护，不代表开发工作树已经发布。使用新工具签名或 0.5.0 迁移说明前，
先核对安装包和对应 release tag。

当前版本:Python 包、bridge 与 Chrome unpacked 扩展统一为 **0.4.20**。

三个组件分别加载更新：

1. 用 `pip install -U browsertap-mcp` 更新安装包；使用 `[desktop]` 时保留该 extra，
   然后重启对应 MCP 会话。
2. 运行 `browsertap doctor`。若要求 `restart_bridge`，用 `browsertap bridge --restart`
   加载守护进程的新代码。
3. 若要求 `reload_extension`，打开 `chrome://extensions`，对 BrowserTap Bridge
   点**重新加载**。扩展源文件的更新需要这个手动步骤。

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
    且不创建可见窗口。`client_id` 标识一个已连接的浏览器/profile 实例，`session_id` 是其当前的
    `clientId:tabId` 复合标签页句柄，不是永久 tab 身份；连接条目还可能带有 `tab_identity`。
    多个浏览器和 profile 可以并存。
3. **MCP 服务**：将上述能力公开为 MCP 工具。每个 agent 的 MCP 进程分别维护默认目标和标签页所有权记录。

浏览器连接包含两条通道：按标签页的会话通道，以及直接连接扩展 service worker 的通道。第二条
通道使部分工具在普通标签页全部关闭时仍可用。

## 操作边界

**选定标签页不会自动激活前台。** `switch_tab` 默认 `activate=false`，仅修改后续调用目标。
只有调用 `activate_tab`、传入 `switch_tab(activate=true)` 或执行需要前台的物理输入时，浏览器可见
状态才会改变。页面读取、JavaScript 和 `page_*` 输入工具均可在后台标签页上运行。

**只有一种坐标，落在标签页内。** `page_click`/`page_drag` 使用指定标签页内的**视口**坐标，
通过 CDP 派发，不移动光标或聚焦窗口，响应包含 `foreground_changed: false`。已经没有会跟它混淆的
桌面坐标工具了——吃物理屏幕像素的那几个在 0.5.0 移除了。

**两种像素单位，而截图用的不是你点击用的那种。** 视口坐标是 **CSS 像素**（`getBoundingClientRect`
报告的空间）；页面截图回来的是**设备像素**，即 CSS × `devicePixelRatio`，所以在 125% 缩放下从图上
量到的点比 `page_click` 需要的大 25%。`capture_page_screenshot` 因此报出 `image_width`/`image_height`
和 `pixel_space: "device"`，把这个系数摆到明面上而不是让调用方猜。从图上量点是唯一没有命中判定的
路径——优先用 `scan_page` 给的 selector，那条路会在派发前先跟页面核对。

**自动化 profile。** 未设置 `BROWSERTAP_MODE` 时默认使用 `lab`，并按
`BROWSERTAP_LAB_NO_ELICIT=1` 处理。Lab 对站点 `allow` 和受限离开对话框兜底免 elicitation；
`safe` 对每次站点 `allow` 询问，并直接拒绝物理 Enter 兜底。两种 profile 都不是对所有浏览器
操作逐次确认。所有权和目标检查始终存在；物理路径还保留 OS lock、安静窗口和 `on_screen`
检查。`input_quiet.enforced` 说明是否获得了可比较的输入标记。

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
`original_url`，且不得关闭。只有本任务改过 URL、同一标签页生命周期仍存在，
并确认用户此后没有另行导航时，才恢复原 URL。

推荐顺序为：调用 `list_tabs`；只在只读或轻量操作中借用匹配标签页；搜索、筛选、排序、翻页、滚动、
展开/折叠、导航、表单和其他会改变页面视图或状态的操作使用 A；没有匹配标签页时创建 A；任务结束后仅关闭 A。
不得将初始标签页集合登记为 owned，
不得关闭 U/B、在状态变更时省略显式目标、复用旧原生标签页 ID、绕过 generation 检查或遗漏 A 的清理。

多 agent 并行时，**每个 agent 使用独立 A 标签页，每次调用显式传 `session_id`**。
同一 MCP 进程和独立 MCP 进程都可以并行操作不同标签页。每个进程有自己的默认标签页；共享进程的
agent 也共享这个默认值，因此 `switch_tab` 不代表 agent 身份。每次调用固定自己的目标，显式目标
不会改变其他调用的目标或进程默认值。

跨进程协作锁覆盖一次完整 MCP 调用及其内部浏览器往返，竞争同一标签页的调用返回 `target_busy`。
bridge 还会记录已派发命令的目标占用：`wait=false` 或响应超时后仍保留，直到收到确定的浏览器结果或确认
标签页生命周期结束。用发起操作的同一 MCP 会话调用 `get_execute_js_result` 领取 JS 结果，
不要重放仍在执行的脚本。直接 `/link` 和 Python driver 调用也受 bridge 的单命令占用保护，
但跨多次浏览器往返的完整调用锁需要 MCP command scope。

调试器超时或断开后，JavaScript 可能仍在运行。此时轮询返回 `status=in_progress`、
`operation_status=outcome_unknown` 和 `reservation_held=true`，不得重放脚本。
手动弹窗也保留占用，只有发起执行的 MCP 会话能调用 `handle_dialog`。处理完弹窗不代表脚本已结束；
扩展无法回传最终结果时仍保留 `outcome_unknown`。调用方可以用 `close_tabs(..., owner_id=...)`
关闭自己创建的标签页，结束该生命周期。

Console/network 捕获的变更权属于启动它的 MCP 会话。其他会话不能重新启动、停止或清空该捕获
（`capture_busy`）；普通页面操作和不清空缓冲的 console 读取仍可使用。所属 MCP 进程退出后，
其他会话可以回收捕获。进程退出本身不会取消页面 JS 或释放进行中的目标占用，标签页所有者可关闭
自己创建的标签页来恢复。共享一个 MCP 进程的
agent 也共享这份所有权。上述保护不保证多步工作流原子性，也不隔离同一 profile 共享的 Cookies/storage。

### 结构化状态与恢复字段

先读 `ok` 和 `error_code`；操作状态位于 `data`/`legacy`，短小状态值也会保留在顶层兼容字段中。
失败同时设置 MCP `isError=true`：

| `status` / `error_code` | 含义 |
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
| `target_busy` | 标签页被另一调用或仍在执行的浏览器命令占用。检查 `delivery_state` 和 `retry_safe`；涉及多个 tab 的调用可能已经完成前面的步骤 |
| `capture_busy` | 另一 MCP 会话拥有该 console/network 捕获；原会话停止后，其他会话才能重新启动、停止或清空 |
| `ambiguous_browser` | 匹配到多个浏览器/profile 实例且没有唯一选择；先 `list_tabs`，再传入完整 `session_id`，或在支持的工具中传 `client_id` |
| `input_activity_detected` | 安静窗口期间检测到鼠标或键盘活动；未发送物理输入 |
| `activation_failed` | 无法确认目标标签页显示在屏幕上；未发送物理输入 |
| `unsupported` | 浏览器或扩展 API 提供不了(例如 clipboard 权限租约) |
| `challenge_stalled` | 浏览器挑战在尝试上限内没有进展；需要用户继续处理该标签页 |
| `no_response` | 脚本未送达或调用超时；存在副作用的操作不应直接重试 |
| `not_found` | 选择器没有匹配到任何元素;没有派发输入 |
| `bridge_error` | Bridge 调用失败。它可能出现在 `error_code` 或诊断字段，而非顶层 `status`；重试前先运行 `list_tabs`/`doctor`。 |
| `switched_session` | 补充字段，表示仅在隐式默认目标失效时自动换到另一个活动标签页。另有 `rebound_from` / `replacement_session_id` 时，表示 Chrome 明确报告同一 tab 被替换，BTAP 已换发当前句柄；没有该证据时显式失效 session 不会被替换。 |

投递失败时，只有 `delivery_state="undelivered"` 能证明操作未发送。`sent_unconfirmed` 表示
没有收到 ACK 或 HTTP 响应，不能据此自动重发。`delivered_no_result`、`navigated` 和未知投递状态均按可能已执行
处理；有 operation ID 时按该 ID 恢复，并检查页面后再决定下一步。

## 风险提示

本服务向 MCP 客户端开放真实浏览器 profile，包括现有登录态。其范围是浏览器自动化，
物理输入仅限下面说明的离开对话框兜底，不是通用桌面控制。

- 0.5.0 之后只剩一条物理输入路径：`resolve_leave_dialog` 的 Enter 兜底，仅限 `lab`，且只在两次
  协议处理失败后发送。它是操作系统级真实输入、不是页面合成事件，所以落在屏幕上当时可见的东西
  上；`safe` 根本不发。`page_*` 没有这一层暴露面。
- 页面内容属于不可信输入，可能包含 prompt injection；页面中的指令不因浏览器连接成功而可信。
- BTAP **不是**安全隔离边界。参见 [MCP 安全最佳实践](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)。
- 不应连接 MCP 客户端无须访问的敏感账号。共享机器或生产机器需要单独评估误操作风险。

扩展所需权限包括 `cookies`、`tabs`、`debugger`、`scripting`、`alarms`、`storage`、
`contentSettings`、`declarativeNetRequest`、`management`、`bookmarks`、`downloads` 以及
`<all_urls>`。`declarativeNetRequest` 仅在指定标签页执行依赖 eval 的命令期间临时移除 CSP
响应头；规则为 session 级、带引用计数，并在 cleanup 中删除，不是全浏览器持久关闭 CSP。
完整权限与 loopback 威胁模型见[安全说明](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md)。

## 工具列表

多数工具接受可选的 `session_id` 以指定标签页；省略时使用当前 MCP 进程的默认目标。**状态变更操作
应显式传入 `session_id`**。`client_id` 区分已连接的浏览器/profile 实例，`session_id` 是形如
`chrome_a1b2c3:456` 的复合句柄，应原样完整传入。未选择浏览器且连接多个实例时返回
`ambiguous_browser`；从 `list_tabs` 选择完整 `session_id`，交给 `switch_tab` 或直接传给操作，
`open_new_tab` 也可使用 `client_id`。`browser="chrome"` 若匹配多个 Chrome profile，同样需要显式
选择 `session_id`。Chrome 明确报告同一 tab 被替换时，会返回 `rebound_from`、
`replacement_session_id` 和 `tab_identity`；否则显式失效句柄会被拒绝。标注**零标签页可用**的
工具通过扩展 service worker 执行，仍需唯一的浏览器/profile 选择。

<details>
<summary><b>标签页与导航</b></summary>

- **get_setup_status** —— 返回 `package_version`、`bridge_version`、`extension_version`、`protocol_version`、连接状态、端口、标签页与恢复动作。允许自动拉起时，未监听的 bridge 会自动启动；`restart_bridge_required=true` 表示仍在运行的 bridge 必须执行 `browsertap bridge --restart` 才能替换。`reload_extension_required=true` 表示 unpacked 扩展受平台限制，必须手动 Reload；**仅版本号不同已不再单独置位它**——Chrome 只在 load 时 parse `manifest.json`、不 Reload 就永远不重新 parse，否则每次涨版本都要人点一次、而那一次唯一改变的就是这个数字。`restart_mcp_session_required=true` 是反方向：某个组件**比运行中的服务更新**，过期的是当前进程，只有重启 MCP 会话或客户端才能消除；此时另外两个标志保持 false，因为重启 bridge 或重新加载扩展只会再报同一个不匹配。`extension_build_stamp` 是更强的信号，回答四个版本字段回答不了的问题：它是编译进 `background.js` 的扩展源码哈希，由**正在运行的** worker 报告，所以把它和 `expected_extension_build_stamp`（当前目录的新鲜哈希）相比，在两个方向上都是决定性的——而版本相等已经两次被实测判错。结论看 `extension_build_verdict`：`matches_tree`（worker 跑的就是这份代码）、`stale_worker`（不是，去 Reload）、`stamp_not_regenerated`（改了扩展文件但没跑 `python -m scripts.extension_stamp --write`，此时比较在两个方向上都不成立）、`unverifiable`（扩展早于该机制，或目录读不出来——见 `extension_build_error`）。`extension_build_enforced=false` 表示这次比较根本没发生，应当按未知处理，不要当成通过。另一个工具正在运行时它照样应答；`default_session_id` 是本次请求取得的 MCP 进程默认目标快照，其他调用的临时目标已隔离，因此 `default_session_settled=true`。无参数
- **get_automation_profile** —— 查看当前 MCP 进程使用 `lab` 还是 `safe` profile
- **set_automation_profile** —— 切换当前 MCP 进程的 `lab|safe` profile;覆盖值不会持久化或重载扩展
  - `mode`(string):`lab` 或 `safe`
- **list_tabs** —— 在 `data.tabs` 中列出已连接标签页的完整 session 句柄和 `browser` 字段。另一个工具正在运行时照样应答；`default_session_id` 是本次请求取得的 MCP 进程默认目标快照，`default_session_settled=true`。并行 agent 仍应显式指定目标。无参数
- **list_all_tabs** —— *(零标签页可用)* 列出全部标签页,含 `list_tabs` 隐藏的 `chrome-extension://` 页面。这类页面永远不会成为会话,所以没有 session id,要用 `cdp_command(tab_id=...)` 操作
  - `session_id`(string,可选):问哪个浏览器/profile
- **switch_tab** —— 指定当前 MCP 进程后续调用的**目标**标签页。`url_pattern` 必须只匹配一个标签页；若匹配多个，需传入完整 `session_id`。`browser` 匹配多个 profile 时也必须显式指定 `session_id`。默认 `activate=false`，不会激活标签页或聚焦浏览器；需要前台时传入 `activate=true` 或调用 `activate_tab`
  - `session_id`(string,可选)、`url_pattern`(string,可选):子串匹配、`browser`(string,可选):`chrome` / `edge` / `opera`、`activate`(boolean,可选):默认 `false`
- **activate_tab** —— 激活标签页并聚焦其窗口。这是显式改变浏览器前台状态且不发送物理输入的方式。在 Windows 上，BTAP 会先请求恢复最小化窗口；若响应仍为 `on_screen=false`，表示无法确认目标已显示，此时不得执行屏幕坐标输入
  - `session_id`(string,可选)
- **open_url** —— 当前标签页导航到 URL,并报告**实际落地**的地址。全局默认仍是 `dismiss`;lab 命中配置的 shell/IDE host 时自动 accept。协议 `navigate` 在重 SPA 失效时自动降级 `Page.navigate`。CDP 返回 `isDownload=true` 时改为返回 `{type:"download",status:"triggered"}`,不再只报 `navigation_failed`;此时附带的 `ERR_ABORTED` 是正常下载导航语义
  - `url`(string)、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`、`beforeunload`(string,可选):默认 `dismiss`、`intent_leave`(boolean,可选):`false` 强制保留页面
- **download_file** —— 通过 Chrome 原生下载管理器下载 HTTP(S) URL，并使用该浏览器 profile 的 Cookies 和登录态。默认等待完成，返回 `status="completed"` 和已验证的绝对 `path`；中断返回 `failed`，超时或 `wait=false` 返回带 `download_id` 的 `in_progress`。显式 `session_id` 必须仍然有效，失效时不会改用其他 profile。附件下载应使用本工具，不应在页面内调用 `fetch`
  - `url`(string)、`filename`(string,可选):相对下载名称、`directory`(string,可选):任意绝对目标目录并自动建父目录;要求 `wait=true`、`wait`(boolean,可选):默认 `true`、`timeout`(number,可选):默认 60 秒,最大 1800、`session_id`(string,可选):选择浏览器 profile、`overwrite`(boolean,可选):默认 `false`,最终目标已存在时拒绝,只有显式 `true` 才替换。带 `directory` 的调用若超时会返回 `directory_applied=false`:后续搬移不再受跟踪,Chrome 可能继续下载到浏览器默认目录
- **open_new_tab** —— 默认在当前 MCP 进程选中的浏览器/profile 后台创建标签页，也可用 `session_id` 或 `client_id` 指定其他实例。生成唯一 `operation_id`，并在限定时间内等待准确的 session/generation 注册；需要前台时传 `active=true`。返回 `{operation_id,tab_id,session_id,generation,ready,owned,opener,owner_id,load_status}`。扩展按 operation ID 去重；只有带准确 `client_id+tab_id+generation` 的 completed 记录才登记 ownership，即使 `ready=false`；`ready` 仅表示 session 工具能否立即使用。创建投递前 registry 不确定时返回 `status="unknown",may_have_created=false,retry_safe=true`；投递后不确定时为 `may_have_created=true,retry_safe=false`。若 `may_have_created=false,retry_safe=true`，先解决返回的失败原因，再省略 `operation_id` 重新调用。恢复 `retry_safe=false` 的已投递创建时，传回相同 `operation_id`、返回的 `client_id` 和 `owner_id`，只读取持久化记录，不重放 `tabs/create`；恢复探测失败仍保留不确定性和 owner 凭据。首次恢复探测查不到记录时，`reconciliation.resume_required=false` 指引调用 `list_tabs()` 检查对应浏览器，停止反复恢复同一记录。记录缺失、URL 相同或标签页数量不变均不能证明未创建或本任务所有权；缺少精确身份与任务归属证据时保留未知结果。保留 `owner_id`，仅按已登记的本任务 session/generation 清理。需要可靠开页时使用本工具；页面 `window.open()` 或锚点 click 可能因缺少用户手势被拦截
  - `url`(string)、`timeout`(number,可选):默认 `15`、`active`(boolean,可选):默认 `false`、`session_id`(string,可选):选择浏览器/profile、`owner_id`(string,可选):让同一任务的多个新 tab 共用一个 owner、`operation_id`(string,可选):恢复句柄、`client_id`(string,可选):创建或恢复时锁定浏览器/profile client
- **close_tabs** —— *(零标签页可用)* 接受原生数字 tab ID 或完整 `client:tabId` session ID，对 `chrome-extension://` 页面同样有效。默认 `only_if_agent_owned=true`，必须传入 `open_new_tab` 返回的 `owner_id`，并在关闭前核对当前 lifecycle generation；用户预存标签页、其他 Agent 的标签页和复用 ID 的新生命周期均会被拒绝。若用户已关闭 owned 标签页，清理返回 `status=already_gone, closed_by=user`，不会使用旧原生 ID 关闭其他标签页；实际关闭 owned 标签页时返回 `closed_by=agent`；显式关闭非 owned/U 标签页时返回 `closed_by=none`，且不计入本任务 owned 清理。若返回 `already_gone` 但浏览器里仍有同一工作页面，先 `list_all_tabs` 核对 URL/title，再决定是否按新的 session/generation 关闭；BTAP 不会按 URL 自动转移 ownership。只有 Chrome 明确报告 `tabs.onReplaced` 且稳定 tab 身份匹配时，才会安全换发当前 session 句柄并迁移 ownership。仅当用户明确要求关闭非 owned/U 标签页时，才可设置 `only_if_agent_owned=false`
  - `tab_id`(integer/string 或数组)、`session_id`(string,可选)、`owner_id`(string,安全默认下必填)、`only_if_agent_owned`(boolean,默认 `true`)
</details>

<details>
<summary><b>页面读取与执行</b></summary>

- **scan_page** —— 把页面读成简化 HTML 或纯文本。返回 `links`,把正文里每个 `#rN` 引用映射到绝对 URL;有内容留在视区外时返回 `offscreen` 和 `hint`。后台标签页可能报告 viewport 高度为 0；普通 DOM/文本/API 工作仍可继续，只有明确需要视觉/布局保真时才调用 `activate_tab`;页面可探测时还会返回 `render_state`/`content_ready`，区分真实正文与 loading、hydrating、shell-only 的 SPA；空壳结果应先重试或使用 `wait_for`。`cutlist`（默认开）会折叠重复的长列表，并为每个被折叠的容器返回一个由该容器自身结构推导出来的 CSS selector。本工具**不修改页面** —— 不写属性、不写 id、不写 `window` 全局变量，所以一次扫描对页面自己的脚本是不可见的
  - `session_id`(string,可选)、`text_only`(boolean,可选):默认 `false`、`cutlist`(boolean,可选):默认 `true`,把重复列表裁成少量样本、`maxchars`(integer,可选):默认 `35000`、`instruction`(string,可选)、`extra_js`(string,可选)、`timeout`(number,可选):默认 `15`
- **wait_for** —— 等待指定条件成立后返回。与轮询 `scan_page` 相比，该工具避免重复序列化完整 DOM。服务端在同一截止时间内调度短同步检查，避免后台页面定时器节流。四个条件必须且只能提供一个；`selector` 接受 CSS 字符串或“后台页面输入”一节所述的结构化 locator。超时仍带 `operation_id` 时，先用 `get_execute_js_result` 查询该检查；未完成期间不重放表达式
  - `selector`(string/object,可选):CSS 或结构化 locator、`text`(string,可选)、`url_pattern`(string,可选)、`js`(string,可选)、`gone`(boolean,可选):默认 `false`、`timeout`(number,可选):默认 `15`、`session_id`(string,可选)
- **wait_for_url** —— 等导航落定:阻塞到标签页 URL 匹配 `url_pattern`(正则,或纯子串,两种都试),并且在 `wait_ready=false` 之外还要求 `document.readyState` 为 `complete`,然后返回最终的 `url`、`title` 和 `ready_state`。在会触发跳转的点击或 `open_url` 之后用它;`wait_for(url_pattern=...)` 只查 URL,新文档还是空白的时候就可能返回。使用与 `wait_for` 相同的有界同步检查和未完成操作恢复流程
  - `url_pattern`(string):匹配 URL 的正则或子串、`timeout`(number,可选):默认 15、`wait_ready`(boolean,可选):要求 `readyState === 'complete'`,默认 `true`、`session_id`(string,可选)
- **scroll_page** —— 滚动并报告新位置,长页面可以分几屏读完
  - `to`(string,可选):默认 `bottom`,也可传 `top`、像素偏移或要滚到可见的 CSS 选择器、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **execute_js** —— 在页面中执行 JavaScript 并返回结果。`timeout` 是覆盖对话框策略设置、monitor 快照、投递/重试、导航检查和清理的单一总 deadline；显式 `session_id` 在这些浏览器往返中保持不变，不依赖进程默认目标。真正的长任务可设 `wait=false`：扩展确认收到后，BTAP 立即返回 `status="in_progress"` 和 `operation_id`，后续用 `get_execute_js_result` 领取结果，不得重放脚本；后台模式有意不支持 `dialog_policy="manual"`。脚本导致页面导航时返回 `status="navigated"` 和 `landed_url`，而不是 `success`，且脚本返回值不可用。`dialog_policy` 控制 `alert`/`confirm`/`prompt`：`dismiss`（默认）和 `accept` 直接应答并记录到 `dialogs`；`manual` 只用于同步调用，保持原生对话框打开、暂停脚本并返回 `blocked_by_dialog`，后续由 `handle_dialog` 处理。标签页已有 manual 执行暂停时立即返回 `busy`。等待页面状态应使用 `wait_for`/`wait_for_url`，不要在 `execute_js` 中嵌入延迟 `setTimeout` 或 sleep Promise。JSON 编码后的 `js_return` 超过 24 KiB UTF-8 内联上限时，BTAP 会把完整值写入私有临时 JSON 文件，并返回 `result_file`、`result_bytes`、`result_sha256` 和 `result_format`，不再返回会被截断的半截内容
  - 遇到 `Cannot access contents of the page` 先分流再重试：如果脚本尝试了 `window.open` 或导航，使用 `open_new_tab`（Chrome 没有用户手势时可能拦截）；如果是当前 tab 本身不允许注入，换可脚本化的普通 `http/https` tab 或使用支持的 CDP 路径。不要把这句错误直接理解成“当前页面读不到”。
  - `script`(string)、`session_id`(string,可选)、`no_monitor`(boolean,可选):默认 `false`、`timeout`(number,可选):默认 `15`、`dialog_policy`(string,可选):`dismiss`(默认)、`accept` 或 `manual`、`wait`(boolean,可选):默认 `true`
- **get_execute_js_result** —— 由发起操作的同一 MCP 会话按 `operation_id` 读取或短暂等待结果，接受 `execute_js` 以及其他超时桥命令返回的句柄。查询绝不重放操作；完成结果可重复读取，补查响应丢失后仍可再查。进行中、未知/过期和其他会话的句柄会返回明确状态或错误。占用到期后，首个通过校验的迟到终态回包保存在 `late_result`（`success` 和 `data`），`late_reply_age` 表示收到它后的秒数；原 `unknown` 收据和 `retry_safe=false` 保留，不恢复占用、不延长保留期。结果最多保留 10 分钟，最多保存 512 条已完成操作记录，容量压力可能使其提前淘汰；查不到结果不证明操作未执行。成功返回的大值沿用 `execute_js` 的无损 `result_file` 元数据；迟到大值的文件元数据位于 `late_result` 内，其 `data` 为 null
  - `operation_id`(string)、`timeout`(number,可选):默认 `0`,范围 `0`–`120`
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
左上角的**视口 CSS 像素**（`getBoundingClientRect` 报告的空间），既不是桌面像素，也不是
`capture_page_screenshot` 返回的设备像素。

应显式传入 `session_id`。调用在自己的上下文中固定目标，不改变当前 MCP 进程的默认目标。
失效句柄没有同一 tab 被替换的证据时会被拒绝。另一 MCP 调用正在使用该标签页时可能返回
`target_busy`；作用范围见上方并行任务说明。

`selector` 保持兼容 CSS 字符串,也可传结构化 locator 对象,主定位键必须且只能有一个:`css`、`role`(可带 `name`)、`text` 或 `label`;在 locator 对象内部,`selector` 是兼容旧调用的 CSS 别名。`exact` 控制 role/name 或 text 精确匹配;`frame` 逐层进入同源 iframe;`shadow` 逐层进入开放 Shadow DOM。仅点击支持 frame 内点位形状 `{"frame":[...],"x":20,"y":30}`,其中 x/y 是最终同源 iframe 视口内的 CSS 坐标,执行前会累加 frame 偏移。零匹配返回 `not_found`,多匹配返回 `ambiguous`,跨域 iframe/关闭 shadow root 会明确上报且不派发输入。selector 点击若穿过带非恒等 CSS transform 的 iframe 链,会返回 `unsupported_frame_transform` 并保持零派发;查询/输入路径不受影响。

- **page_click** —— 点 CSS/结构化 `selector` 或视口坐标。定位方式二选一。selector 模式中,未提供 offset 的轴取元素中心;显式提供的 `offset_x`/`offset_y` 则从元素左上角按对应轴计算。`{"frame":[...],"x":20,"y":30}` 是 frame 内点位模式:进入列出的同源 iframe,累加偏移后按顶层文档 CSS 坐标派发;它指向像素而非元素,因此不做命中判定。缺失、歧义、不可交互、跨域 iframe、关闭 shadow root 或带 CSS transform 的 iframe 都返回结构化状态且不派发;后者状态为 `unsupported_frame_transform`。selector 模式还会在派发前在页面里做一次命中判定:在折叠线以下就先滚动进视口(`scrolled_into_view`),那个像素属于别的元素时返回 `obscured` 并用 `occluded_by` 指出遮挡者,滚动后仍不在屏幕上返回 `outside_viewport` —— 这两种情况都不点,因为派发出去的点击会落在别的元素上并报成功。命中通过的点击带 `hit_verified: true`。坐标模式不做命中判定:坐标指的是像素,不是元素——而且从 `capture_page_screenshot` 上量到的像素是*设备*像素,得先除以 `devicePixelRatio`。验证码仍有 `challenge_detected`、`attempts` 与 `challenge_stalled` 上限
  - `selector`(string/object,可选)、`x`(number,可选)、`y`(number,可选)、`offset_x`(number,可选)、`offset_y`(number,可选)、`button`(string,可选):默认 `left`、`clicks`(integer,可选):默认 `1`、`session_id`(string,可选)、`timeout`(number,可选):默认 `15`
- **page_type** —— 往 CSS/结构化 locator 选中的字段输入;省略 `selector` 时使用当前焦点。Xterm.js 自动改投 helper textarea;缺失、歧义、只读或不可输入目标不会收到文本/按键;旧 CSS 字符串非法时返回 `status="invalid_selector"`,不再裸抛 `SyntaxError`。定位结果带脱敏的 `active_element` 身份,并在尝试聚焦时带 `focus_confirmed`,所以省略 selector 的输入也可审计。`clear=true` 先选中已有内容,`submit_key` 事后按键
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
- **save_pdf** —— 有界 `Page.printToPDF`,验证 PDF 后原子写文件;`save_path` 是**相对路径**,落在 `~/Downloads/browsertap` 下,绝对路径或 `..` 越界会抛 `ValueError`;超时会强制释放 debugger lease
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

- **capture_page_screenshot** —— 通过 CDP 截视口、`full_page` 或显式 `clip`;PNG/JPEG/WebP 可选,JPEG/WebP 支持 `quality`。返回元数据和 MCP 图片内容;`save_path` 只额外落盘,且是**相对路径**,落在 `~/Downloads/browsertap` 下,绝对路径和 `..` 越界被拒。元数据自报单位:`image_width`/`image_height` 从返回的字节里解析,`pixel_space: "device"`(CSS × `devicePixelRatio`),所以从图上量到的点不能直接喂给 `page_click`。头解析不出来时报 `null` 尺寸加一条 `dimensions_note`,不猜——`size` 是字节数,不是尺寸
  - `session_id`(string,可选)、`tab_id`(integer,可选)、`format`(string,可选):默认 `png`、`full_page`(boolean,可选):默认 `false`、`clip`(object,可选):`x`,`y`,`width`,`height`,可带 `scale`、`quality`(integer,可选):0–100、`save_path`(string,可选)、`return_base64`(boolean,可选):默认 `false`、`timeout`(number,可选):默认 `20`
</details>

<details>
<summary><b>0.5.0 已移除：操作系统级输入与桌面截图</b></summary>

`mouse_move`、`mouse_click`、`mouse_drag`、`type_text`、`hotkey`、`pointer_info` 和
`capture_desktop_screenshot` 已不存在。它们驱动的是整个桌面而不是一个标签页，所以作用对象是
屏幕上当时恰好显示的东西。改用：

| 已移除 | 改用 |
| --- | --- |
| `mouse_click` | `page_click` |
| `mouse_move` | 不需要——`page_click` 自己定位 |
| `mouse_drag` | `page_drag` |
| `type_text` | `page_type` |
| `hotkey` | `page_press` |
| `pointer_info` | 用 `execute_js` 读元素几何 |
| `capture_desktop_screenshot` | `capture_page_screenshot` |

`page_*` 调用失败是定位问题，不是去找屏幕坐标兜底的理由——用 `scan_page` 重读页面、修 locator。
浏览器界面、原生文件选择器、扩展弹窗和操作系统对话框本来就不在页面级协议事件能到的范围内，
按「不支持」处理，而不是由一条桌面路径顶上。

只剩一条物理路径：`resolve_leave_dialog` 在两次协议 accept 失败后发送 Enter，仅限 `lab`。`safe`
走 MCP elicitation 询问，被拒绝、取消或客户端不支持时返回 `requires_user_action`。两种情况下闸门
都不变——跨进程锁（已占用时立即返回 `busy`，不排队）、一段短暂安静窗口（检测到鼠标或键盘活动时
返回 `input_activity_detected`，不发送输入）、激活目标标签页、然后动作。这个窗口能检测到什么取决于
操作系统：只有 Windows 提供最后输入时间戳，指针位置在 Wayland、无头容器以及未授予辅助功能权限的
macOS 上都读不到。一个信号都拿不到时窗口照样等完，但没有任何东西可供比对，所以结果带一个
`input_quiet` 字段列出实际采样到的标记，一个都没有时 `enforced: false`——这种机器上通过只能当作
未经验证，不能当作桌面确实空闲。无法确认标签页显示在屏幕上时返回 `activation_failed` 且不发送输入，
所以最小化的窗口得到的是一个错误，而不是一个发错地方的 Enter。

</details>

## 故障排查

应先运行 `browsertap doctor`。连接、版本、对话框、权限和物理输入相关的恢复流程见
[故障排查指南](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md)。

## 许可证

MIT —— 见 [LICENSE](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE)，
它同时打进 wheel 与 sdist。Fork 或二次分发时请保留。

BTAP 由 `LinVireo` 维护，本发行版的权威公开仓库为 `LinVireo/browsertap-mcp`。
