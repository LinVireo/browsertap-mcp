---
name: browsertap-bridge-recovery
description: "诊断和恢复 browsertap-mcp (BTAP) 的连接、超时、旧 schema 或旧扩展问题。用于桥断、MCP 浏览器工具挂住、list_tabs 无结果、401 或 Unknown command: downloads。先区分 MCP 进程、bridge、扩展和页面层，不把页面拒绝当作桥故障。"
---

# BTAP 连接与恢复

本 skill 写给**正在调用 BTAP、需要恢复连接的 agent**。
正常任务执行和标签页清理见 [[browsertap-default]]；
安装与完整人工操作见[故障排查](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.zh-CN.md)。
修改产品源码的任务另读仓库 AGENTS.md / CONTRIBUTING.md。

## 先判定，再操作

1. 保存失败工具、目标 `session_id`、`error_code`、`delivery_state` 和
   `operation_id`。有未知执行先补查，不把恢复连接当成可以重放的证明。
2. 运行安装环境中的 `browsertap doctor`；MCP 仍响应时也可用 `get_setup_status`。
   使用实际安装的可执行文件路径，避免诊断到另一个 Python 环境。
3. 按 `action` 处理对应组件。没有明确结论时，再按“端口、HTTP、MCP”逐层检查。
4. 恢复后先验证 `list_tabs` 和目标身份，再恢复业务操作。记录未知结果与尚需人工完成的动作。

| `action` / 证据 | 对应处理 |
| --- | --- |
| `none` / `healthy` | 通道正常；检查原工具或页面层，不重启。 |
| `wait_for_extension` / `starting` | bridge 刚启动，给扩展重连时间，再做一次有界检查。 |
| `restart_mcp_session` / `stale_package` | 重启该 MCP 会话，使其加载新包与工具 schema；不是重启浏览器。 |
| `restart_bridge` / `stale_bridge` | 确认影响范围后用 `browsertap bridge --restart`。 |
| `reload_extension` / `stale_extension` | 请求用户在对应浏览器的扩展页手动 Reload BrowserTap Bridge。 |
| `check_config` / `initialization_failed` | 先纠正报告中的配置或解释器问题，不把它当运行中的桥宕机。 |

bridge 由多个 MCP 会话共享。重启前说明会影响这些会话，且不会取消已经在浏览器执行的 JS。
`browsertap bridge` 不带参数会前台常驻；日常恢复使用管理子参数，
只有明确需要前台调试时才运行裸命令。

## 构建身份

MCP server、常驻 bridge 和 MV3 扩展分别加载代码。更新包后要检查各自的运行身份，
不能只比较 `get_setup_status.package_version` 与扩展 manifest 版本。

| `extension_build_verdict` | 结论 |
| --- | --- |
| `matches_tree` | 运行 worker 与当前扩展源码一致；仅版本号差异不要求 Reload。 |
| `stale_worker` | 运行 worker 与源码不同，需要人工 Reload。 |
| `stamp_not_regenerated` | 源码与生成的 stamp 不一致，应交给源码维护者处理，不能靠 Reload 证明新旧。 |
| `unverifiable` | 未能比较，不是通过。 |

`extension_build_enforced=false` 同样表示没有执行比较。
源码维护者修改扩展后运行 `python -m scripts.extension_stamp --write`，
再由用户 Reload；调用方不要为消除诊断而自行改 stamp。
`matches_tree` 也不免除协议版本或必需 capability 的真实缺口。

`chrome.runtime.reload()` 不是项目认可的磁盘源码更新验证方法。
BTAP 不允许自动禁用自身来强制刷新；被禁用后它无法重新启用自己。

## 分层检查

默认 bridge 使用 WebSocket 18765、HTTP 18766；自定义地址/端口以 doctor 报告为准。
扩展 WebSocket 检查 origin，HTTP 使用共享 Bearer token，是两条不同通道。

| 检查 | 成功能证明什么 | 失败后看哪里 |
| --- | --- | --- |
| 对应端口在 LISTENING | 有进程监听，不代表一定是 BTAP | 核对 PID、进程命令行和配置端口。 |
| 认证的 `/link` 只读请求返回正确结构 | bridge 的 HTTP handler 可用 | HTTP 状态、token 配置、进程日志。 |
| `get_all_sessions` 返回预期标签页 | bridge 收到了扩展的页面注册 | 空列表时继续查浏览器/扩展，不先重装客户端。 |
| MCP `list_tabs` 返回 | MCP 客户端到 bridge 的路径可用 | 只有 MCP 失败时定位该客户端会话。 |

Windows 查看监听信息：

```powershell
netstat -ano | Select-String '18765|18766'
```

只有需要隔离 HTTP 层时才直接请求。下例读取 doctor 已解析的 token 路径，
适用于启用鉴权且 doctor 已能输出配置的场景；不打印 token，也不把它放进公开报告：

```powershell
$btapStatus = browsertap doctor | ConvertFrom-Json
$btapToken = (Get-Content -Raw -LiteralPath $btapStatus.state_paths.token_file).Trim()
$btapUri = "http://$($btapStatus.bridge_host):$($btapStatus.bridge_http_port)/link"
Invoke-RestMethod -Method Post -Uri $btapUri -TimeoutSec 6 -ContentType 'application/json' -Headers @{ Authorization = "Bearer $btapToken" } -Body '{"cmd":"get_all_sessions"}'
```

其他平台同样从 doctor 报告的路径读取 token，由 HTTP 客户端构造 Bearer 头。
循环地址应绕过系统代理；自定义地址时核对实际目标，不能把默认端口的失败推广成服务不可用。

`/link` 返回 `{"r":[]}` 只表示没有已注册页面，不能单凭它断言扩展损坏。
浏览器全关、仅有不可脚本化页面、扩展被禁用、注册失败或暂时重连都可能产生空列表。
扩展 service worker 已连接时，部分浏览器级工具仍可在零页面标签页状态下工作。

## 连接故障

### Bridge 无监听

新 MCP 实例通常按需启动 detached bridge，`BROWSERTAP_NO_SPAWN=1` 会禁用该行为。
仍未启动时检查 doctor 返回的状态目录下的 `bridge.log`，再决定是否运行
`browsertap bridge --restart`。不要通过启动多个裸 bridge 或重启所有客户端抢端口。

### 扩展或页面未注册

先检查浏览器是否开启、BrowserTap Bridge 是否加载并启用，以及任务目标是不是可访问的
正常页面。页面工具需要可脚本化目标；仅有 `about:blank` 不提供普通页面会话。
Chrome/Edge/Opera 各自的扩展安装独立，一个浏览器缺失只修对应那一边。

`ext_never_registered` 表示未见注册，`registering` 表示没有活动页面；
`sw_slept_or_dropped` 表示注册数据长时间未更新。结合扩展错误、连接状态与恢复结果判断，
不要仅凭 MV3 worker 被回收就断言产品无 bug 或必需 Reload。
普通重连先做有界等待；持续失败或明确旧构建时再按报告要求人工处理。
`Receiving end does not exist` 是消息接收端不可用的线索，不是单独的故障归因。

### HTTP 正常，只有 MCP 调用失败

确认是否为特定工具路径、旧 schema、客户端超时或失联的 MCP 会话。
取消宿主中挂住的调用不等于浏览器操作已取消；保留并补查原 `operation_id`。
必要时只重启受影响的 MCP 会话，避免终止其他 agent 的 remote client。

### 端口被其他进程占用

先确认端口所属进程，不能按端口号直接杀进程。
若选择非默认端口，需要协调 Python 的 `BROWSERTAP_BRIDGE_PORT` 与扩展存储设置；
准确操作见[自定义端口](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.md#bridge-port-conflict-or-custom-port)。
旧 `chrome_extension/config.js` 已移除，不要创建或编辑它来配置当前扩展。

### HTTP 401

拒绝正文是纯文本 `unauthorized: missing or bad bridge token`，不是 JSON。
这表明认证失败，不代表扩展坏了；同一 token 也保护 `/api/result` 与 `/api/longpoll`。

1. 比较 MCP 与 bridge 各自的 `state_paths`，尤其 `token_file`、
   `token_fingerprint`、`state_dir_kind`；`state_paths_disagreement` 会指出差异。
   指纹用于比较，不是可用凭据。
2. 路径不同先统一环境配置。`BROWSERTAP_STATE_DIR` 改状态目录，
   `BROWSERTAP_BRIDGE_TOKEN_FILE` 单独覆盖 token 文件；不要为每个编辑器另造 token。
3. 路径一致但 bridge 持有旧内容时，按
   `bridge_token_is_from_before_the_file_changed` 的诊断重启 bridge，再验证认证。
4. 首次使用会创建持久 token；关闭浏览器、卸载扩展或重装包不会轮换它。
   `BROWSERTAP_BRIDGE_TOKEN` 仅在文件不存在时导入，文件存在后不覆盖。
   全量数据清理不是常规恢复步骤，确需清理须先确认并停止相关 bridge。

关闭鉴权不是修复 token 不一致的默认方法。

## 结果未知与页面拒绝

有 `operation_id` 时，在**原 MCP 会话**调用 `get_execute_js_result`。
它接受 execute_js 和其他桥命令的句柄，查询不重发；完成结果可重复读取。
保留期和容量边界见 [[browsertap-default]]；`operation_unknown` 或过期不证明未执行。

只有 `delivery_state=undelivered` 才证明未投递。`sent_unconfirmed`、
`delivered_no_result`、`in_progress`、`operation_status=outcome_unknown`
都不能作为自动重放的理由。debugger detach 不证明页面 JS 停止；
`reservation_held` 缺失时也不能猜测目标已释放。

`wait_for` / `wait_for_url` 使用服务端短同步探测；超时返回句柄时同样补查。
`execute_js` 的策略、monitor、执行和清理共用总 deadline，
不会因为切换传输就获得一份新的调用预算。

| 页面/操作层状态 | 处理 |
| --- | --- |
| `blocked_by_dialog` / `blocked_by_beforeunload` | 原 MCP 会话依据任务意图用 `handle_dialog`；保留原执行句柄。 |
| `target_busy` / `busy` | 等待已有操作或交给其所有者处理；活 OS lock 不受元数据 TTL 抢占。 |
| `capture_busy` | 让原捕获会话停止或清空，不接管活 owner 的捕获。 |
| `obscured` / `outside_viewport` | 输入未派发；处理遮挡、滚动或重新定位，不是桥故障。 |
| `invalid_selector` / `ambiguous` / `not_interactable` | 检查当前 DOM 与 locator。 |
| `cross_origin_frame` / `unsupported_frame_transform` | 当前定位路径不受支持，不能用重连修复。 |
| `cdp_timeout` / `debugger_detached` | 核对原操作是否可能继续；先补查，不换通道重复输入。 |
| `debugger_conflict` | 由 DevTools/竞争 debugger 的使用者释放占用。 |
| `requires_user_action` / `input_activity_detected` / `activation_failed` | 按人工批准、用户活动或前台状态处理，不重启桥。 |
| `challenge_stalled` | 将同一 tab 交给用户，不另起独立浏览器。 |
| `unsupported` | 当前浏览器 API 无法提供所需能力或可恢复性，不改走物理输入。 |

多浏览器时显式传完整 `session_id`。有直接生命周期证据时结果才会换发
`rebound_from` / `replacement_session_id` / `tab_identity`；
没有证据的死句柄仍拒绝，不能按 URL/title 猜测。
`switched_session` / `switched_from` 只说明隐式默认目标变化，需重新核对目标。

`open_new_tab` 已返回 owned 身份但 `ready=false` 时，保留
`session_id + generation + owner_id`，不要以为创建没发生而再开一个。
下载 `directory_applied=false` 时先检查默认下载目录和下载状态，不重复下载；
`active_element` / `focus_confirmed` 是输入目标证据，不证明浏览器位于前台。

## 旧契约与直接 HTTP

- 客户端没有工具或拒绝新参数：检查安装包和 MCP schema，刷新对应会话。
- `Unknown command: downloads`：核对目标扩展的能力与构建，按
  `reload_extension_required` / `extension_build_verdict` 处理，
  不因这一条错误重启浏览器或使用页面 fetch 下载附件。
- `/link` 常用顶层命令包括 `get_all_sessions`、`get_clients`、`diagnose`、
  `find_session`、`resolve_session`、`execute_js`、`ext_cmd`、
  `get_execute_js_result`；完整分派见同版本 `browser_bridge.py`。
  低层 HTTP 不是完整 MCP 工作流的等价替代，尤其是跨调用目标锁和 owner 上下文。

低层 JSON 形状只在诊断该通道时使用：

```jsonl
{"cmd":"get_all_sessions"}
{"cmd":"find_session","url_pattern":"example.com"}
{"cmd":"execute_js","sessionId":"<current-session-id>","code":"return document.title","timeout":15}
{"cmd":"ext_cmd","clientId":"<current-client-id>","payload":{"cmd":"tabs","method":"list"}}
```

这些是四个独立请求，不是一个 JSON 文档。`timeout` 单位是秒；
`find_session` 返回匹配列表而非单个 id。读取 `r` 的真实结构后再解包，
只有值确实是 JSON 文本时才额外解析。扩展命令放在 ext_cmd 的 `payload` 中，
JS 放在 execute_js 的 `code` 中，不能把扩展 payload 直接发到顶层。

恢复完成的证据是对应组件状态正确、MCP 调用成功且目标身份已核对。
报告实际做过的恢复动作和仍未结案的操作，不把“端口监听”或“重启命令成功”
单独称作业务操作恢复。
