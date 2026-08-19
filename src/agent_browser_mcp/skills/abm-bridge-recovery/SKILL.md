---
name: abm-bridge-recovery
description: 恢复 agent-browser-mcp (ABM) 的 CDP 桥连。触发：桥断了 / MCP 浏览器工具挂住 / get_setup_status 转圈 / list_tabs 拿不到 tab / Unknown command: downloads。分层排错：netstat → /link curl → list_tabs，别把 MCP 层挂当成桥断。
---

# agent-browser-mcp (ABM) 桥连恢复

浏览器自动化走 **agent-browser-mcp**（调用方守则见 [[browser-mcp-default]]），通过 CDP 连真实
Chrome/Edge。桥连或"浏览器探测"中断时按此恢复。**调用语义、tab 归属、工具选择顺序不在这份文档里**
——那些属于守则；这里只管"连不上/挂住了怎么恢复"。

给人看的完整排错手册（本文件随包发布，读者手上可能没有仓库目录，所以给 URL）：
<https://github.com/0xlinn/agent-browser-mcp/blob/main/docs/TROUBLESHOOTING.md>
（中文 `docs/TROUBLESHOOTING.zh-CN.md`）。本文件只保留 agent 需要**自己动手**的那部分。

## 关键原则

**别把 MCP 层挂当成桥断。** MCP 工具挂 120s+ 时不要一直重试 MCP 层，先分层定位：

```
netstat → /link curl → list_tabs（MCP 层验通）
```

如果 status 类工具（`get_setup_status`）还挂，绕开它用能秒回的工具。物理工具返回
`requires_user_action`、`busy`、`input_activity_detected`、`activation_failed` 都**不是**桥故障。
物理租约不排队；OS ownership 覆盖整个动作，即使超过元数据默认 TTL 30 秒也不能抢占。TTL 仅在动作
结束或 owner 进程退出、OS lock 释放后用于 stale 元数据恢复。遇到 `busy` 停下稍后重试，别循环、
手删锁、杀进程或重启桥。若是 manual 对话框造成 `blocked_by_dialog`/`busy`，用 `handle_dialog` 释放。

## 组件与要求

- 扩展 **Agent Browser MCP Bridge**，源在仓库 `src/agent_browser_mcp/chrome_extension`
  （端口在 `config.js`）。扩展版本与包版本统一，`chrome://extensions` 上看到的号应与
  `get_setup_status.package_version` 一致；不一致就是没 Reload。
- 桥接端口：ws **18765** / http **18766**。
- 桥守护进程：`agent-browser-mcp bridge`（等价于 `python -m agent_browser_mcp.bridge`，**前台常驻**）。
  管理用 `--restart` / `--stop`，别用裸 `bridge` 去"顺手起一下"。
  **MCP 实例探测不到桥会自动 detached 拉起守护并走 remote 模式**（`AGENT_BROWSER_NO_SPAWN=1`
  可关），桥死了通常自愈；日志 `~/.agent-browser-mcp/bridge.log`（>5MB 轮转 `bridge.log.old`）。
- `remote_mode=false` → MCP 自托管 driver，**仅当自动拉起失败才会发生**；见到先查 `bridge.log`
  找 spawn 失败原因。
- **硬性要求**：① 扩展在 `chrome://extensions`（或 `edge://extensions`）已加载并启用（需开
  Developer Mode，dev-mode 扩展重启后常被自动禁用）；② 至少一个 **http(s)** 标签页开着
  （`about:blank` 不算）。
- **多浏览器（Chrome + Edge 并存）**：扩展要在两个浏览器各自的扩展页分别加载。桥按
  `client_id:tab_id` 命名 session（`client_id` 形如 `chrome_a1b2c3`/`edge_x9y8z7`，按 profile
  隔离持久化），两边小整数 tab id 不互撞、`tabs_update` 只清各自的 session。选目标：
  `switch_tab(browser="chrome"|"edge")` 或传组合 id `switch_tab(session_id="edge_x9y8z7:456")`；
  `list_tabs` 每 tab 带 `browser` 字段。详见「多浏览器」节。

## 第一步：分层诊断

**首选：一条命令直接给判定 + 一句话建议。** 判定表做进了桥里，输出 `cause` + `advice`，
不用手动 netstat/curl/看 badge：

```bash
agent-browser-mcp doctor
```

只有排查 `/link` 本身时才手工请求。token 从共享文件读取，**不要输出它**：

```powershell
# Windows PowerShell
$abmTokenFile = if ($env:AGENT_BROWSER_BRIDGE_TOKEN_FILE) { $env:AGENT_BROWSER_BRIDGE_TOKEN_FILE } else { Join-Path $env:USERPROFILE '.agent-browser-mcp\bridge-token' }
$abmToken = (Get-Content -Raw -LiteralPath $abmTokenFile).Trim()
curl.exe -s --noproxy "*" --max-time 6 -X POST http://127.0.0.1:18766/link -H "Content-Type: application/json" -H "Authorization: Bearer $abmToken" -d '{"cmd":"diagnose"}'
```

```bash
# macOS / Linux
ABM_TOKEN_FILE="${AGENT_BROWSER_BRIDGE_TOKEN_FILE:-$HOME/.agent-browser-mcp/bridge-token}"
curl -s --noproxy '*' --max-time 6 -X POST http://127.0.0.1:18766/link \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $(tr -d '[:space:]' < "$ABM_TOKEN_FILE")" \
  -d '{"cmd":"diagnose"}'
```

> **`/link` 默认使用 Bearer token 鉴权**。所有 ABM 进程读取
> `~/.agent-browser-mcp/bridge-token`，编辑器无需配置 token。**401 ≠ 桥挂了**，
> 通常是升级前的旧 bridge 还持有不同 token，或双方显式覆盖了不同的 token 文件路径。
> 详见「成因 6」。被拒时正文是纯文本 `unauthorized: missing or bad bridge token`，
> **不是** JSON，别照 `{"error":...}` 解析。

`diagnose` 的 `cause` → 对应成因：

| cause | 含义 | 去哪节 |
|---|---|---|
| `healthy` | N 个 tab 已注册，桥+扩展都通 | 若 MCP 工具仍挂 → 成因 3 |
| `ext_never_registered` | 扩展从没连过桥 | → 成因 2a（红 Errors/storage）/ 2b（被禁用）/ 无 http 页 |
| `sw_slept_or_dropped` | 曾注册但 >90s 无心跳（含 2c/2d，不细分）| → 成因 2c/2d：等 5s 自愈，否则 reload |
| `registering` | 刚有心跳但 0 活跃 tab | 稍等 / 打开一个 http(s) 页面 |
| `bridge_unreachable` | 桥守护无响应 | → 成因 1（通常自动拉起）|
| `diagnose_failed` | 连 `diagnose` 本身都抛了 | 看返回里的 `error`，多半也是成因 1 |

**`doctor` 还有一层在 `cause` 之上**：先判"三个进程里谁在跑旧代码"，命中就只打这一条、不再打
cause 行。看到它就别按成因表排了，照它说的做：

| doctor 末行 | 含义 | 怎么办 |
|---|---|---|
| `[!!] stale_package` | 有组件比当前 MCP 进程新 | 重启 **MCP 会话/客户端**。重启桥或 Reload 扩展都修不了 |
| `[!!] stale_extension` | 扩展是旧构建 | 去 `chrome://extensions` Reload **一次** Agent Browser MCP Bridge |
| `[!!] stale_bridge` | 常驻桥在跑旧代码 | `agent-browser-mcp bridge --restart`；**不用**重启 Chrome |

> 改桥侧代码（`tmwebdriver.py` 等）后要**重启桥守护**才生效。老 daemon 收到 `diagnose`
> 返回裸 `ok`（没这分支），见到 `ok` 就是旧代码还在跑。

**Fallback：手动分层**（`diagnose` 不可用/想眼见为实）：

```powershell
netstat -an | Select-String '18765|18766'   # 应见 18765/18766 LISTENING
```

把上面 curl 的 `{"cmd":"diagnose"}` 换成 `{"cmd":"get_all_sessions"}`，三种结果对应三种病：

| /link 返回 | 判定 | 去哪节 |
|---|---|---|
| `{"r":[...真实 tab...]}` | 桥+扩展都通，问题在 MCP 实例层 | → 成因 3 |
| `{"r":[]}` | 桥活着但扩展没注册 tab | → 成因 2 |
| 连不上 18766（拒绝/超时） | 桥守护死了 | → 成因 1 |
| `401` | 桥启了 token 鉴权、请求没带/带错 | → 成因 6 |

**`{"r":[]}` 但 18765 有 ESTABLISHED**（TCP 连着实则 0 session）："扩展注册崩了"或"半开僵尸
连接"，不是桥的问题，按成因 2 排。

## 六类成因与恢复

### 成因 1：端口无 LISTENING / 连不上 18766 → 桥守护死了

`netstat` 里 18765/18766 完全无 LISTENING，MCP 工具立即报连接被拒（不是超时，是连不上 18766）。
**通常自愈**——任何新 MCP 实例的工具调用发现桥不在会自动 detached 拉起守护。仍需手动时：

```bash
agent-browser-mcp bridge --restart
```

它后台拉起并打印 `{"status": "restarted", ...}`；`--stop` 只停不起。
**不要用不带参数的 `agent-browser-mcp bridge`**：那是**前台常驻**，会一直占住当前终端
（agent 调它等于把自己挂住），只适合人在终端里盯日志。

起来的标志是日志出现 `Bridge started: ws=18765 http=18766 pid=...`（stderr 与
`~/.agent-browser-mcp/bridge.log` 都有）；若打的是 `Bridge already running at ...; exiting`
说明桥本来就活着，问题不在这一节。netstat 见 LISTENING 后扩展**秒级自动重连**。

### 成因 2：`/link` 空数组 → 扩展没注册 tab

桥活着但扩展没在给桥注册 tab。**先分清是下面哪一种**——四个成因症状相似（`/link` 返回 `[]`），
最快的分流看两个信号：**扩展有没有红色 Errors** + **netstat 18765 有没有 ESTABLISHED**。

- 有红色 Errors → 2a（代码崩，看是不是 storage 权限）
- 扩展被禁用/开关灰 → 2b
- 18765 **有** ESTABLISHED 但 log 无注册 → 2c 半开僵尸（通常自愈）
- 18765 **无** ESTABLISHED + 报 `Receiving end does not exist` → 2d SW 睡死（直接 reload，最常见）

**2a. 扩展注册代码崩了（`chrome://extensions` 看"错误"列表）** ⚠️ 最隐蔽、最易误判

扩展启用且 TCP ESTABLISHED，但 `ext_ready`/`tabs_update` 抛异常 → 桥收 0 session。已知根因：
manifest 缺 `"storage"` 权限，`getClientId()` 访问 `chrome.storage.local` 时注册崩溃。

- 判定：扩展卡片下有红色"错误"，点开是 `ext_ready/tabs_update failed TypeError ... reading 'local'`
  （或其它 `chrome.*` 未定义）。
- 已修：manifest 补 `"storage"`，`getClientId()` 加 try/catch 回退内存 id。运行的版本早于该修复
  就升级后 Reload 扩展。
- **别误当"扩展被禁用"**——它启用着，是代码崩了。

**2b. dev-mode 扩展被自动禁用**

浏览器重启/更新后 dev-mode 扩展常被自动禁用。修复：

- 进 `chrome://extensions`（Edge 用 `edge://extensions`），开 Developer Mode，重新启用 / Reload
  **Agent Browser MCP Bridge**；
- 或前台化浏览器 / 打开一个新 http(s) 页面触发扩展重连。
- 多浏览器时若只有一边空：只 Reload 缺 tab 的那个浏览器的扩展即可，另一边不受影响（session 按
  `client_id` 隔离），`list_tabs` 按 `browser` 字段确认哪边缺。

**2c. 半开僵尸连接（TCP ESTABLISHED 但从没注册）**

桥重启后扩展的 WS 仍 readyState=OPEN、TCP ESTABLISHED，但没成功发过 `ext_ready`；`ws.send()`
在半开死连接上不报错（要等 OS 重传超时数分钟）。判定：`bridge.log` 里该 daemon 启动后**没有一条**
`Received tabs update` / `New tab connected`，但 netstat 有 ESTABLISHED。

- 已有自愈：桥收 `ping` 回 `pong`；扩展记 `lastPongAt`，~55s 无 pong 强制 `ws.close()`+重连。
  **复发通常 55s 内自愈，不必手动干预。** 想立刻恢复就 Reload 扩展（`onInstalled`→`ext_ready`）。

**2d. Service Worker idle 睡死（MV3 通病，最常见的复发）** ⭐ 认准这个报错直接 Reload

MV3 的 background service worker 会被浏览器在 idle 后回收。**独特判定信号**：

- content script / badge 点击报 **`Could not establish connection. Receiving end does not exist.`**
  （"SW 没有响应端"，区别于孤儿页面的 `context invalidated`）；
- `/link` 返回 `[]`；
- **netstat 里 18765 一条 ESTABLISHED 都没有**（与 2c 的关键区别：2c 有，2d 没有）；
- 扩展**启用着、无红色 Errors**、有"检查视图: Service Worker"行（浏览器认为 SW 存在但 inactive）。
- **恢复**：`chrome://extensions` 点 ↻ Reload → SW 重新实例化跑 `ensureConnected('onInstalled')`
  → **秒级全部 tab 重注册**。
- **已有自愈**：content script 每 5s 重开 `tmwd_keepalive` 长连，唤醒 SW 并自动连桥。开着正常网页
  时通常 <5s 自愈；页面全关/全是 `chrome://` 才需手动 Reload。
- **不是代码 bug**：正常态会复发，别改代码，直接 Reload。刷新网页也能唤醒，但 Reload 扩展最干脆。

修好 storage 权限并 Reload 后，`list_tabs` 会立即拿到全部真实 tab。

### 成因 3：桥通但某个 MCP 工具挂 120s+ → MCP 实例层

`/link` 秒回真实 tab，说明桥+扩展都通；是某个宿主每 call/session spawn 新 MCP 实例且不回收，
其中一条工具路径卡住。

- 取消挂住的那次调用，**绕开它**用能秒回的工具（`list_tabs` 等往往还能秒回）。别一直重试 MCP 层。
- 已有防护：远程命令有界超时报错；`get_setup_status`/`list_tabs` 在桥半死时约 5s 返回
  `bridge_error`；`execute_js(timeout=N)` 的策略设置、monitor、投递、无 ACK 重试和清理共享**一个**
  总 deadline，不会把多个 N 秒阶段串起来。

### 成因 4：浏览器全关 / 只剩 about:blank

打开一个正常 http(s) 网页（扩展会自动重连，`about:blank` 不行）。

### 成因 5：端口 18765/18766 被占

杀占用进程，或改扩展 `config.js` 里的端口。

### 成因 6：`/link` 返回 401（token 鉴权）

ABM 默认从 `~/.agent-browser-mcp/bridge-token` 取持久 token。**401 是客户端和常驻 bridge 使用的
token 不一致，不是扩展坏了**：

- **症状**：curl `/link` 直接 401；MCP 工具报 unauthorized；扩展那边一切正常（WS 18765 不走
  token，按 origin 校验）。
- **正常生命周期**：首次启动自动创建文件；关闭浏览器/编辑器不会轮换。卸载扩展或重装 Python 包也
  保留该文件，所以旧 token 是可复用状态，不会阻碍重装。
- **旧 env 迁移**：只有文件不存在时，`AGENT_BROWSER_BRIDGE_TOKEN` 才导入一次；文件存在后它不能
  覆盖文件。不要给不同编辑器分别配置 token。
- **最常见根因**：① 升级前的旧 bridge 仍在内存中；② bridge 与 MCP 显式设置了不同的
  `AGENT_BROWSER_BRIDGE_TOKEN_FILE`；③ bridge 运行时手工删除或改写了 token 文件。
- **恢复顺序**：确认双方使用同一 token 文件路径；默认路径一致时只重启 bridge 一次，再重试 MCP
  工具。不要重装扩展，也不要轮流重启所有编辑器。
- **完整清理**：先停止所有 ABM bridge，再删除整个 `~/.agent-browser-mcp`，然后启动任意一个 MCP
  会话生成新 token。若先删文件但旧 bridge 仍运行，新客户端会生成新 token 并暂时 401。
- **同一个 token 也守 `/api/result` 和 `/api/longpoll`**，被拒时同样是纯文本 401。
- **兼容开关**：仅在明确可信的隔离环境中用 `AGENT_BROWSER_BRIDGE_AUTH=off` 关闭鉴权；这不是普通
  恢复步骤。

## 多浏览器（Chrome + Edge 同时连）

桥支持 Chrome / Edge / 多 profile 同时接入。要点：

- **session id 是 `client_id:tab_id`**（如 `edge_a1b2c3:456`），不是裸 tab 整数。`client_id` 按
  浏览器/profile 隔离持久化，两边永不撞号、互不清对方 tab（断开扫描按 `client_id` 隔离）。
- **按浏览器名选目标**：`switch_tab(browser="edge")` / `switch_tab(browser="chrome")`，可叠加
  `url_pattern` 缩小；`list_tabs` 每 tab 带 `browser` 字段（`chrome`/`edge`/`opera`）看归属。
- 报 `No connected tab for browser 'edge'`：Edge 侧扩展没连上——去 `edge://extensions` Reload 并
  开一个 http(s) 页面（见成因 2）。报错会列出当前实际连上的浏览器名。
- **改完扩展两边都要 Reload 一次**（MV3 缓存旧 SW）；桥侧代码改动还要重启桥守护。
- **默认落点**：MCP 实例环境变量 `AGENT_BROWSER_PREFERRED_BROWSER=chrome|edge` 指定"没指名时默认
  用哪个浏览器"（只影响盲选默认，不限制显式 `switch_tab`）。
- **断流/空响应语义**：`execute_js` 不假成功返回空数据——`status="no_response"` 表示脚本未送达
  （已自动重试 1 次）或已送达但超时（勿盲目重试有副作用脚本，先 `scan_page` 核实）；
  `switched_session`/`switched_from` 表示原会话断了、已自动切到**同浏览器**的活会话（不会静默跳
  浏览器）。见到这些字段：`list_tabs` → `switch_tab` → 只重试无副作用操作。
- **显式 session / 新 tab 语义**：显式 `session_id` 会贯穿 `execute_js` 的
  baseline/diff/transient/重试/落点读取，期间共享默认不会被停在目标 tab；新 tab 同时按
  `client:tabId + generation` 等待注册，`ready=true` 不会命中同数字 id 的旧生命周期。若
  `open_new_tab` 返回无 `generation`，说明扩展是旧构建，去手动 Reload；若 generation 已有但桥的
  `list_tabs` 不上报，说明 daemon 未重启。
- **xterm 输入不是桥故障**：`page_type` 会把 `.xterm` 容器/后代自动改投 `.xterm-helper-textarea`。
  终端无输入先确认扩展已 Reload 到当前版本，再显式传该页的 `session_id`；清当前 shell 行用
  `page_press("ctrl,u")`，不要因 `clear=true` 不符合终端行编辑语义就重启桥。
- **对话框/验证码/权限不是桥故障**：`blocked_by_dialog`/`blocked_by_beforeunload` →
  `handle_dialog`；`challenge_stalled` → 把同一 tab 交还用户；`busy` → 稍后重试，别循环。
  `set_site_permission` 返回 `unsupported` 表示浏览器无法提供可精确恢复的 API（如 clipboard/托管/
  OS 权限），不要查桥，也不要退化成物理点击。
- **先分清旧 schema 还是旧扩展**：客户端没有某个工具、或拒绝新参数/描述 → 重启 MCP 会话/客户端；
  `Unknown command: downloads`、`on_screen:null` 或 `extension predates...` → 去目标浏览器扩展页
  手动 Reload。下载命令 Unknown 不是桥断，不要重启桥，也不要退回页面 `fetch`。
  `chrome.runtime.reload()` **不会**从磁盘重读扩展源码。
- **双浏览器开工**：先按 `browser` 锁定一边并全程传完整 session id；出现
  `no_response`/`switched_session`/`bridge_error` 先 `list_tabs` 重定位，有副作用的操作重试前先
  `scan_page` 确认。

## 旁路兜底

- **桌面物理输入仍可用**：桥断时 `capture_desktop_screenshot`/`mouse_*` 走本机截屏与
  pyautogui，不经 18766，可用来诊断或临时兜底。批准与否取决于 profile：默认 `lab` 免询问，
  `safe` 每次批准；客户端不支持批准时返回 `requires_user_action`，那是客户端能力问题，不是桥问题。
  两种 profile 下跨进程锁、安静窗口、目标提前台和 `on_screen` 检查都照常生效。
- **不用 MCP 工具直接驱动已登录浏览器**：POST `/link` 时从共享 token 文件构造 Bearer 头；不要
  依赖编辑器 env，也不要打印 token。**只有这几个顶层 cmd 合法**：`get_all_sessions` /
  `diagnose` / `find_session` / `ext_cmd` / `execute_js`。其它一律返回
  `{"r":{"error":"unknown cmd ..."}}`——**不是静默 ok**；扩展命令（tabs/management/cookies 等）
  必须包成 `{"cmd":"ext_cmd","payload":{...}}`，把扩展 payload 当顶层 cmd 发是协议错误，会被
  明确拒绝。
  - `{"cmd":"find_session","url_pattern":"example.com"}` → sessionId（`client_id:tab_id` 组合串，
    原样回传即可，别拆）
  - `{"cmd":"execute_js","sessionId":<id>,"code":"await fetch('/api/x').then(r=>r.text())","timeout":20000}`
    → `{"r":{"data":<string>}}`（data 已是字符串，别二次 `json.loads`；同源 fetch 带登录 cookie）
  - `{"cmd":"ext_cmd","payload":{"cmd":"tabs","method":"create","url":"https://..."}}` → 原生
    `chrome.tabs.create`（零 tab 也能开）
  - 多浏览器时 `get_all_sessions` 返回的每条带 `browser` 字段，可据此挑对应浏览器的 sessionId。

## 已知坑

- **别乱杀 MCP 实例**：它们是瘦 remote client，父进程（编辑器/宿主会话）活着就不是孤儿，杀了会
  报错或立即 respawn。要停桥用 `agent-browser-mcp bridge --restart`。
- 带 Turnstile 的登录盾页曾把 `execute_js` 挂死 >120s。现在监控往返有界、单次调用不再叠到
  120s+，超时以 `status="no_response"` 明确返回。验证码应改用**有界 `page_click`** 在同一个已连接
  tab 里处理（没进展返回 `challenge_stalled`，交还用户手动过盾），不要再对盾页跑长 `execute_js`。
- **附件下载**：首选 `download_file(url=..., session_id=...)`，由浏览器原生下载管理器带当前
  profile 的 Cookie/登录态下载，默认等待完成并返回最终绝对 `path`；显式死 session 会被拒绝，
  不会换到别的浏览器 profile。带 `directory` 时若超时并返回 `directory_applied=false`，目标搬移
  未发生且不再跟踪，文件可能继续落入浏览器默认下载目录。页面 `fetch` 可能被站点拒绝
  （`XHR not allowed` / `Failed to fetch`），不要拿它下载附件。`open_url` 仅在需要观察导航语义时
  使用：`isDownload:true` 会结构化返回 `type="download", status="triggered"`，此时 `ERR_ABORTED`
  正常，但它不承诺完成路径。无需 `Page.setDownloadBehavior`/`Browser.setDownloadBehavior`。
