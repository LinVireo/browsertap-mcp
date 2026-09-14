# 故障排查

[English](TROUBLESHOOTING.md) | 中文

本文档说明连接、版本、对话框、权限和物理输入相关问题。常规操作流程见
[使用指南](USAGE.zh-CN.md)。

## 已知限制与恢复

以下情况来自 2026-09-12 至 14 日的真实浏览器验证，下文区分已确认修复和剩余限制。
仅离线测试或 CI 通过不能证明这些场景已能在真实浏览器中自动恢复。

### 原生文件框一直未关闭

实测真实 Chrome 文件框时，检查因 native owner 属于另一进程而返回
`native_dialog_unverifiable`。没有票据，`cancel_native_file_dialog` 无法完成成功取消路径；
关闭所属标签页后，该文件框仍然存在。因此尚未验证这一能力可无人值守恢复。

上传使用 `upload_files` 和页面文件输入控件，避免弹出文件框。已打开的文件框仍受既有 Windows
布局和所有权检查限制；检查拒绝或关闭结果不确定时，停止重试并说明可能需要手动关闭。
`handle_dialog` 处理的是 JavaScript 对话框，不能关闭操作系统文件框。
不要再弹一个文件框来验证恢复。用户手动关闭，或后来观察到窗口消失，都不能证明 BTAP 自动取消成功。

### `exec_timeout` 后 JavaScript 仍继续执行

响应 deadline 不会终止已派发的 JavaScript。0.5.3 之前，`exec_timeout` 可能在旧脚本
仍运行时提前释放目标，使第二个 MCP 进程能修改同页。0.5.3 将已派发的超时保留为
`operation_status=outcome_unknown`，在既有有限恢复窗口内保持 `reservation_held=true`。
同页命令返回 `target_busy`，其他标签页仍可使用。占用到期后仍保留未知回执和
`retry_safe=false`；到期不证明页面空闲，也不会取消迟到的副作用。

有 `operation_id` 时，用发起操作的同一 MCP 会话调用 `get_execute_js_result` 检查；
轮询不会重放，也不会取消脚本。执行状态不确定时，不重放脚本，也不在该页开始会与旧脚本冲突的工作。
可以携带 `owner_id` 关闭本任务创建的标签页以结束该 document 生命周期；不要为清理关闭用户标签页。
结束生命周期不能撤销已经发送的请求或其他副作用。

### 手动对话框恢复报告 CDP 超时

一次真实手动对话框验证在 `Runtime.releaseObjectGroup` 清理超时后返回
`debugger_detached`。独立验证中，完成加载的前台和后台页面都能成功处理对话框；
该偶发失败尚无确认根因或针对性修复。

创建或导航标签页后，先用 `wait_for_url` 确认目标文档完成加载，再触发原生对话框。
处理超时后，用 `handle_dialog(action="manual")` 检查状态，并查询原操作回执。
关闭动作可能已经执行，对话框消失也不能恢复丢失的脚本结果。保留
`outcome_unknown` 和 `retry_safe=false`，不重放脚本，也不把后续重试成功当成已修复。

### sandbox 子 frame 阻断主页面执行

带 `sandbox` 且未允许 `allow-scripts` 的 iframe（包括 `sandbox="allow-same-origin"`）
可能使默认 `execute_js` 对话框策略准备返回 `dialog_scope_setup_failed`。当前准备流程要求子
frame 确认，禁脚本的 frame 因而可能阻断整个调用，即使主页面本身允许执行脚本。
这也会造成前面的页面输入已成功，最后脚本读取结果却失败。

应将其识别为 frame 准备限制，不能据此判定主页面禁止脚本或前面的输入失败。
任何重试前先检查已有状态；重复相同的默认调用不能修复 frame 限制。
保留页面原有 sandbox 设置，并报告受阻步骤。

### 扩展卸载需要用户手势

实测中，`uninstall_extension` 在 `show_confirm_dialog=true` 和 `false` 两种设置下均返回
`chrome.management.uninstall requires a user gesture.`。切换确认参数不会提供 Chrome 要求的手势。
被拒绝时，需要用户在 `chrome://extensions` 或对应浏览器的扩展管理页移除选定扩展。
再用 `list_extensions` 核对；人工移除后扩展消失，不算卸载工具成功。
BTAP 也无法通过活动响应通道卸载自身。

### 可见按钮返回 `not_interactable` 或 `obscured`

按钮的 DOM 本体可以是 `0×0`，由 CSS `::after` 提供可见点击区域。真实验证中，
按 role/name 定位此类按钮返回 `not_interactable`；定位其可见文字则返回 `obscured`，
因为该点实际归按钮自身所有。selector 点击尚未兼容这种布局。

检查当前布局，用 `document.elementFromPoint(x, y)` 确认可见控件内的点实际命中目标按钮，
再以顶层文档视口的 CSS 坐标调用 `page_click(x=x, y=y, session_id=session_id)`。
坐标模式不执行 selector 的命中检查，因此调用方应在派发前核对点位，并检查操作后的页面状态。
重连桥不会改变这一布局限制。

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
去掉 query 与 fragment。桥的异常处理不再写入 payload 文本或 traceback；任意协议标识符使用
哈希引用。日志仍能识别访问过的站点，并含本地路径、socket 地址和时间信息，两个文件对外提供前
都要先检查内容。哪些内容允许出现、哪些不允许，见
[SECURITY.md](../.github/SECURITY.md)。

## 连接问题

### 没有已连接的标签页

确认未打包扩展已启用，并至少打开一个正常的 `http` 或 `https` 页面。空白页和浏览器内部页面
不会建立普通页面会话。重新加载扩展后，应刷新页面或打开新 URL，再次运行 `doctor`。

桥默认只允许 `browsertap extension-path` 对应扩展的精确 Origin。诊断中的
`ws_origin_policy.default_origin` 和 `identity_source`（`manifest_key`、`unpacked_path`
或 `unavailable`）说明来源。与 `chrome://extensions` 的已安装 ID 对照；没有 manifest key
时，复制到另一个目录会产生不同 ID。加载报告的包路径，或在桥环境的
`BROWSERTAP_WS_ALLOWED_ORIGINS` 中显式添加该副本完整的 `chrome-extension://<id>`，
修正配置后重启桥。`unavailable` 需先修复包内 manifest JSON/key；key 支持严格 Base64 或
Chromium 兼容的 PEM，原始 Base64 中的空白无效。Windows 下通过目录大小写别名或 junction
加载也可能产生不同 ID，应核对实际安装的 ID。另报 `clientId` 不能让陌生 ID 获得信任。

### MCP 客户端无法启动服务

确认 Python 包已安装，且 `browsertap` 可通过 `PATH` 访问。使用虚拟环境安装时，应在
MCP 客户端配置中填写可执行文件的绝对路径。Windows 通常为
`<repo>\.venv\Scripts\browsertap.exe`，Linux/macOS 为
`<repo>/.venv/bin/browsertap`。

若仅限 lab 的 `resolve_leave_dialog` 物理兜底报告依赖缺失，应安装 desktop extra：

```powershell
.\.venv\Scripts\python.exe -m pip install -U "browsertap-mcp[desktop]"
```

源码检出里对应的写法是在该检出目录下跑 `-e ".[desktop]"`。

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

先查看 `state_paths.token_file_status`：`missing`、`empty`、`ready`、`unreadable` 与
`invalid_encoding` 是不同状态。只有 ready 内容有指纹；`token_file_error` 给出不含凭据
字节的原因。元数据不可读时，`token_file_exists` 和 `state_dir_exists` 可以为 null。
默认目录状态未知时保留 canonical 路径，不转向 legacy 目录。
修正权限或编码问题，保留已有 token 文件。显式相对 `BROWSERTAP_STATE_DIR` 和
`BROWSERTAP_BRIDGE_TOKEN_FILE` 按发起进程的工作目录解析，daemon 收到对应绝对路径。

`error_code: malformed_diagnosis` 表示 bridge 返回了不可用的报告，仍按
`bridge_unreachable` 给出重启动作，不能据此认定版本陈旧。后续 DNS/socket 端口探测失败时，
`doctor` 保留已取得的诊断、增加 `port_probe_errors`，并以非零退出码结束。

### 调用被拒绝并返回 `Session ... is not connected`

这是有意的拒绝。你明确指定了 `session_id`，但 BTAP 无法验证同一个标签页是否有存活的
session，因此没有派发脚本。消息会列出仍然连接的 session，供你检查真正要操作的目标：

```text
Session chrome:123 is not connected. BTAP refused to execute because no live session could be verified for the same tab.
No script was dispatched. Active sessions: chrome:456, chrome:789. Run list_tabs,
verify the intended target, then select its live session_id with switch_tab and retry.
```

用 `switch_tab` 选定真正要操作的标签页，或原样传入列表中的 `session_id`，然后重试。
若消息很短且没有候选列表，说明当前没有任何标签页连接，参见上面的"没有已连接的标签页"。

只有一个有证据支持的例外：Chrome 明确报告同一个原生标签页被替换时，BTAP 可能返回
`rebound_from`、`replacement_session_id` 和 `tab_identity`，而不是拒绝。此时采用返回的新
session id，并在重复状态变更操作前确认页面；没有这些字段时，显式过期 session 永远不会被替换。

### 返回结果里带 `switched_session`

你没有传 `session_id`，共享的默认目标已经失效，BTAP 为这次未指定目标的调用重新选了同一浏览器
里一个存活的标签页，而不是直接失败。调用**已经执行**，落在 `switched_session` 指向的标签页上，
`switched_from` 是失效的那个。重复任何有副作用的操作之前，先用 `list_tabs` 或 `scan_page`
确认它落在你预期的页面上。显式指定的 session 只有在结果同时带有有证据的
`rebound_from` / `replacement_session_id` / `tab_identity` 时才会重绑定；否则仍返回上面的拒绝。

### 调用返回 `no_response` 或 `bridge_error`

先运行 `list_tabs`，确认原始 `session_id` 仍然存在。只读调用可在页面重连后，使用该明确 session
重试一次。对于导航、输入、下载或其他有副作用的操作，应先检查页面或操作状态；请求可能已经
执行，只是响应在超时后丢失。多个标签页同时失败时运行 `doctor`。

### 命令超时

应区分 MCP 客户端启动超时与单个工具 deadline。MCP 进程无法启动时，将客户端连接超时设为至少
60 秒，并填写可执行文件绝对路径。单个浏览器工具超时时，继续使用明确的 `session_id`，仅在已知
操作本身较慢时增加该工具的 `timeout`，并检查 `~/.browsertap/bridge.log`。未确认副作用是否
已发生前，不得循环重试状态变更操作。
JavaScript 还存在[超时后继续执行](#exec_timeout-后-javascript-仍继续执行)的问题；
占用释放不能证明脚本已停止。

### Bridge 端口冲突或自定义端口

BTAP 连续使用三个端口：`BROWSERTAP_BRIDGE_PORT` 为 WebSocket，`PORT+1` 为 HTTP，`PORT+2`
为单 bridge 锁。只有前两个承载流量；第三个在某个 bridge 持有期间一直保持打开，因此抢锁失败的
第二个 bridge 不会退出，而是转为通过第一个工作。它与状态目录下的 `spawn.lock` 文件是两套机制：
后者负责避免多个 MCP 会话在同一时刻各拉起一个守护进程——所以"`PORT+2` 上只有一个监听者"本身
并不能证明只启动过一个守护进程。其他应用占用这些端口时，客户端可能误判为已连接到错误服务。
基础端口必须为 `1` 到 `65533` 的整数。无效值在网络或 spawn 操作前拒绝，import、
help/version 和包路径命令仍可使用。Python 探测、监听器和远程 HTTP URL 支持 IPv6
地址族；浏览器扩展仍连接 IPv4 回环地址。

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

使用 `dialog_policy="manual"` 的调用可能保留了 JavaScript 对话框，并暂停对应执行。应使用相同的
`session_id` 调用 `handle_dialog(action="accept")` 或
`handle_dialog(action="dismiss")`。该标签页阻塞期间，其他标签页仍可正常使用。

### 物理输入返回 `requires_user_action`

批准未通过时读取结果的 `reason`，同一字段也保留在 `legacy` 和 `diagnostics`。这些结果均不会
派发需要批准的操作：

| `reason` | 含义 |
| --- | --- |
| `elicitation_unsupported` | MCP 宿主不支持批准请求。 |
| `declined` | 用户拒绝。 |
| `timeout` | 等待批准超时。 |
| `cancelled` | 批准提示被取消。 |
| `error` | 批准交互失败。 |

任务可使用页面输入时优先选择 `page_click`、`page_type`、`page_press` 和 `page_drag`。
`safe` 模式下，`setting="allow"` 的站点权限同样需要 elicitation。拒绝不能作为切换 profile
或重新派发操作的依据；整个 MCP 任务被取消时仍正常传播取消。

### 扩展升级后仍看到旧的弹窗 helper 全局变量

扩展不再向每个 document 常驻注入 MAIN world 弹窗 helper。需要 `accept`/`dismiss` 的操作会在
本次范围内安装 helper；最后一个范围结束时恢复页面属性描述符并移除临时 controller，超时回收
处理被遗弃的范围。旧扩展已注入的 document 在扩展 Reload 后仍保留旧 wrapper；正常导航或刷新
页面后才会产生干净的新 document。BTAP 不会为此刷新无关标签页。

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

### macOS 上的剩余物理兜底不生效

若确实需要 `resolve_leave_dialog` 的 Enter 兜底，应为终端或 MCP 客户端授予辅助功能权限。
`capture_page_screenshot` 通过 CDP 截取页面，不需要 macOS 屏幕录制权限。

### 剩余物理兜底报告桌面会话无法初始化

`desktop` extra 已安装，但这台机器没有可用桌面：无头服务器、没有 X11 display 的 SSH 会话，
或已锁屏、无人值守的控制台。`pyautogui` 在 import 阶段绑定 display，`mss` 在 `mss.mss()`
内部绑定，因此错误信息会先说明这是桌面问题，再附上后端的真实原因 —— `KeyError: 'DISPLAY'`、
某个 Xlib 错误，或 `mss` 的 `ScreenShotError`。重装 extra 不能解决。请改用
`page_click`、`page_type`、`page_press`、`page_drag` 与 `capture_page_screenshot`：
它们通过 CDP 驱动标签页，不需要桌面；或者把 BTAP 放到有真实桌面会话的机器上运行。

## 权限清理

站点权限租约到期后会恢复原设置。`reset_site_permissions()` 可立即恢复活动租约；不传参数时，
恢复所选浏览器的全部租约。恢复失败的租约会保留并重试，同时记录到 bridge 日志。
