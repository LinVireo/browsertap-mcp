# 故障排查

[English](TROUBLESHOOTING.md) | 中文

本文档说明连接、版本、对话框、权限和物理输入相关问题。常规操作流程见
[使用指南](USAGE.zh-CN.md)。

## 诊断顺序

1. 运行 `browsertap doctor`。
2. 查看 `get_setup_status`，核对 `package_version`、`bridge_version`、
   `extension_version` 和 `protocol_version`。
3. 仅执行状态结果给出的恢复操作。允许自动拉起时，未监听的 bridge 会自动启动；若
   `restart_bridge_required=true`，运行 `browsertap bridge --restart`，该操作不会改变
   浏览器前台状态。`reload_extension_required=true` 表示必须手动重新加载未打包扩展。
4. 再次运行 `doctor`，确认至少存在一个正常页面连接。

Bridge 日志位于 `~/.browsertap/bridge.log`，上限 5 MB，轮转时保留一份
`bridge.log.old`。URL 在写入日志时已做脱敏 —— 保留 scheme、host 与截断后的 path，
去掉 query 与 fragment —— 但日志仍能看出浏览器访问过哪些站点，也仍包含来自页面的错误文本，
两个文件对外提供前都要先检查内容。哪些内容允许出现、哪些不允许，见
[SECURITY.md](../SECURITY.md)。

## 连接问题

### 没有已连接的标签页

确认未打包扩展已启用，并至少打开一个正常的 `http` 或 `https` 页面。空白页和浏览器内部页面
不会建立普通页面会话。重新加载扩展后，应刷新页面或打开新 URL，再次运行 `doctor`。

### MCP 客户端无法启动服务

确认 Python 包已安装，且 `browsertap` 可通过 `PATH` 访问。使用虚拟环境安装时，应在
MCP 客户端配置中填写可执行文件的绝对路径。Windows 通常为
`<repo>\.venv\Scripts\browsertap.exe`，Linux/macOS 为
`<repo>/.venv/bin/browsertap`。

若操作系统级输入或桌面截图报告依赖缺失，应安装 desktop extra：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
```

### `/link` 返回 HTTP 401

Bridge 与 MCP 进程必须解析到同一个 token 文件，默认路径为
`~/.browsertap/bridge-token`，无需为不同编辑器分别设置 token。不要靠猜判断各进程读的是哪个
文件：`browsertap doctor` 会给出运行它的那个进程的 `state_paths`，两端不一致时另有
`state_paths_disagreement`，逐字段列出 `this_process` 与 `bridge` 的取值。token 之间按截断的
`sha256:` 指纹比对，因此这项诊断不会打印或复制 token 本身。出现
`bridge_token_is_from_before_the_file_changed` 说明守护进程在启动时把旧 token 锁进了内存：
重启 bridge 后重试。若差异在 `state_dir` 或 `token_file` 上，则是两个进程的环境不同，
在路径一致之前重启无效。
结果回传与轮询通道（`/api/result`、`/api/longpoll`）使用同一个 token，返回同样的 `401`，
响应正文是纯文本行 `unauthorized: missing or bad bridge token`，不是 JSON；按
`{"error": ...}` 解析所有错误的客户端只会报解析失败，看不到真实原因。

### 调用被拒绝并返回 `Session ... is not connected`

这是有意的拒绝。你明确指定了 `session_id`，而该标签页已经不存在，BTAP 不会把这次调用改到
别的标签页执行：让"点结账"或提交表单落在替代页面上，比报错严重得多。消息会列出仍然连接
着的标签页：

```text
Session chrome:123 is not connected. BTAP refused to execute on a different tab.
Active sessions: chrome:456, chrome:789. Select the intended target with
switch_tab and retry.
```

用 `switch_tab` 选定真正要操作的标签页，或原样传入列表中的 `session_id`，然后重试。
若消息很短且没有候选列表，说明当前没有任何标签页连接，参见上面的"没有已连接的标签页"。

### 返回结果里带 `switched_session`

你没有传 `session_id`，共享的默认目标已经失效，BTAP 为这次未指定目标的调用重新选了同一浏览器
里一个存活的标签页，而不是直接失败。调用**已经执行**，落在 `switched_session` 指向的标签页上，
`switched_from` 是失效的那个。重复任何有副作用的操作之前，先用 `list_tabs` 或 `scan_page`
确认它落在你预期的页面上。明确传入的 `session_id` 永远不会被静默替换，那种情况返回的是上面
那条拒绝。

### 调用返回 `no_response` 或 `bridge_error`

先运行 `list_tabs`，确认原始 `session_id` 仍然存在。只读调用可在页面重连后，使用该明确 session
重试一次。对于导航、输入、下载或其他有副作用的操作，应先检查页面或操作状态；请求可能已经
执行，只是响应在超时后丢失。多个标签页同时失败时运行 `doctor`。

### 命令超时

应区分 MCP 客户端启动超时与单个工具 deadline。MCP 进程无法启动时，将客户端连接超时设为至少
60 秒，并填写可执行文件绝对路径。单个浏览器工具超时时，继续使用明确的 `session_id`，仅在已知
操作本身较慢时增加该工具的 `timeout`，并检查 `~/.browsertap/bridge.log`。未确认副作用是否
已发生前，不得循环重试状态变更操作。

### Bridge 端口冲突或自定义端口

BTAP 连续使用三个端口：`BROWSERTAP_BRIDGE_PORT` 为 WebSocket，`PORT+1` 为 HTTP，`PORT+2`
为单 bridge 锁。只有前两个承载流量；第三个在某个 bridge 持有期间一直保持打开，因此抢锁失败的
第二个 bridge 不会退出，而是转为通过第一个工作。它与状态目录下的 `spawn.lock` 文件是两套机制：
后者负责避免多个 MCP 会话在同一时刻各拉起一个守护进程——所以"`PORT+2` 上只有一个监听者"本身
并不能证明只启动过一个守护进程。其他应用占用这些端口时，客户端可能误判为已连接到错误服务。
Windows 可先只读检查端口持有者：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 18765,18766,18767 |
  Select-Object LocalAddress,LocalPort,OwningProcess
Get-CimInstance Win32_Process |
  Where-Object ProcessId -In <comma-separated-owner-pids> |
  Select-Object ProcessId,ExecutablePath,CommandLine
```

若端口属于其他应用，应选择一组三个连续空闲端口，并在 MCP/bridge 进程中将基础 WebSocket 端口
写入 `BROWSERTAP_BRIDGE_PORT`。扩展读不到环境变量，需要单独告知它同一个基础端口：打开
`chrome://extensions`，点击 **BrowserTap Bridge** 下的 **Service Worker**，在弹出的
控制台执行：

```js
chrome.storage.local.set({ btap_port: 19765 })   // 换成你的基础端口
```

扩展会立即改连新端口。随后再运行 `browsertap bridge --restart`。Python 环境变量无法
自动修改扩展 storage。不得仅凭进程名终止未知端口持有者。

## 版本与重新加载问题

### 工具拒绝文档中存在的参数

MCP 客户端会在会话期间缓存工具 schema。升级服务后，应重新启动 MCP 会话或客户端。
若 `get_setup_status` 报告 `reload_extension_required`，应在 `chrome://extensions` 或 Edge、
Opera 对应页面中手动重新加载未打包扩展。

`chrome.runtime.reload()` 只重启扩展 service worker，无法可靠地从磁盘重新读取源文件。

### Bridge 版本仍为旧版

Bridge 是独立后台进程，其生命周期可能长于 MCP 会话。允许自动拉起时，包括
`get_setup_status` 在内的普通工具会在端口无人监听时自动启动 bridge，但不会替换仍占用端口的
旧 bridge。若 `restart_bridge_required=true`，应执行返回的重启动作；通常无需重启编辑器或浏览器。

对于由 0.3.4 或更高版本启动的 bridge，可直接在后台管理生命周期，不会聚焦或重启浏览器：

```powershell
browsertap bridge --restart
browsertap bridge --stop
```

BTAP 在 `~/.browsertap/bridge.pid` 中记录受管进程，终止前同时核验 PID、创建身份和可执行
文件，不会仅因进程名为 `pythonw.exe` 就将其终止。

旧版本 bridge 可能早于 PID 记录机制。首次迁移时，命令会返回 `unmanaged_running`，不会误报
成功或终止未知进程。在 Windows 上，应先找出 bridge 端口的持有者，再核对其命令行，最后只
终止经过核验的 PID：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 18765,18766,18767 |
  Select-Object LocalPort,OwningProcess
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*browsertap_mcp.bridge*' } |
  Select-Object ProcessId,ParentProcessId,ExecutablePath,CreationDate,CommandLine
# 仅在端口持有者和命令行都确认属于 BTAP bridge 后执行：
Stop-Process -Id <verified-port-owner-pid> -Force
browsertap bridge --restart
```

这一迁移不需要重启 Chrome；扩展会在后台重新连接新 bridge。

### 状态反复要求重启或重新加载，但做了没有变化

比较 `package_version` 与 `bridge_version`、`extension_version`。若某个组件**比
`package_version` 更新**，说明过期的是 MCP 服务进程本身，此时 `get_setup_status` 报
`status: stale_package`、`action: restart_mcp_session`。这是"MCP 会话仍在运行时升级了包"
的正常结果：磁盘上的文件已经是新版，而运行中的进程仍持有启动时导入的版本。

应重启 MCP 会话或客户端。重启 bridge 或重新加载扩展都无法消除该状态 —— 两者都会重新读取
同一批新文件并再次报告同一个不匹配，因此这种状态下 `restart_bridge_required` 与
`reload_extension_required` 均为 false。

## 浏览器交互问题

### 标签页持续返回 `blocked_by_dialog` 或 `busy`

使用 `dialog_policy="manual"` 的调用可能保留了原生对话框，并暂停对应执行。应使用相同的
`session_id` 调用 `handle_dialog(action="accept")` 或
`handle_dialog(action="dismiss")`。该标签页阻塞期间，其他标签页仍可正常使用。

### 物理输入返回 `requires_user_action`

MCP 客户端可能未实现 elicitation，或用户拒绝了该操作。应优先使用 `page_click`、
`page_type`、`page_press` 和 `page_drag`，这些工具不需要桌面级输入。`safe` 模式下，
`setting="allow"` 的站点权限同样需要 elicitation。

### 物理输入返回 `busy`

另一个 BTAP 进程持有非排队物理输入锁。应在当前操作结束后重试。不得循环重试、删除锁文件、
终止无关进程，或仅为清除该状态而重启 bridge；owner 进程退出后，过期锁元数据会自动回收。

### 物理输入返回 `input_activity_detected`

安静窗口期间检测到鼠标或键盘活动，因此 BTAP 未发送物理输入。仅在桌面空闲时重试，或改用
不接触桌面的页面级工具。

### 结果里 `input_quiet.enforced` 为 `false`

安静窗口确实等了，但这台机器没有任何 BTAP 能采样的输入信号，所以它无法得知当时是不是
有人在用鼠标或键盘。只有 Windows 提供最后输入时间戳；指针位置在 Wayland、无头容器、
以及未授予辅助功能权限的 macOS 上读不到。本次操作**没有**被拦——直接拒绝会让这些
本来能用的机器彻底用不了物理输入——但这种通过只能当作未经验证，不能当作已确认桌面
空闲；有人可能在键盘前时优先用页面级工具。`input_quiet.observed` 列出确实应答了的标记，
所以部分可观测的机器仍能看出它被盯的是哪几项。

### 物理输入返回 `activation_failed`

BTAP 无法确认目标已显示在屏幕上，因此未发送输入。仅当任务确实需要桌面输入时，才恢复浏览器
窗口并显式激活目标标签页。

### macOS 上的物理输入不生效

应为终端或 MCP 客户端授予辅助功能权限。桌面截图还需要屏幕录制权限。

### 物理输入报告桌面会话无法初始化

`desktop` extra 已安装，但这台机器没有可用桌面：无头服务器、没有 X11 display 的 SSH 会话，
或已锁屏、无人值守的控制台。`pyautogui` 在 import 阶段绑定 display，`mss` 在 `mss.mss()`
内部绑定，因此错误信息会先说明这是桌面问题，再附上后端的真实原因 —— `KeyError: 'DISPLAY'`、
某个 Xlib 错误，或 `mss` 的 `ScreenShotError`。重装 extra 不能解决。请改用
`page_click`、`page_type`、`page_press`、`page_drag` 与 `capture_page_screenshot`：
它们通过 CDP 驱动标签页，不需要桌面；或者把 BTAP 放到有真实桌面会话的机器上运行。

## 权限清理

站点权限租约到期后会恢复原设置。`reset_site_permissions()` 可立即恢复活动租约；不传参数时，
恢复所选浏览器的全部租约。恢复失败的租约会保留并重试，同时记录到 bridge 日志。
