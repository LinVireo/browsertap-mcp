---
name: browser-mcp-default
description: 浏览器自动化默认入口。任何打开网页、填表、点击、截图、抓取或复用已登录 Chrome/Edge 的任务，优先使用 agent-browser-mcp (ABM) MCP 工具。
---

# 浏览器任务只走 ABM（agent-browser-mcp）

**agent-browser-mcp (ABM)** 通过 CDP 桥连接**真实已登录**的 Chrome/Edge。使用本 skill 的浏览器任务应只使用 ABM，不另起 headless、Playwright 或其他独立浏览器 profile。

## 为什么只保留 ABM

- **复用真实登录态**：当前 Chrome/Edge 的 cookie、SSO、会话都在。
- 默认部署面向单操作员；多 Agent 通过显式 session 与 owner 契约隔离。
- 桥故障时走 **abm-bridge-recovery** / `agent-browser-mcp doctor`，不换别的 browser 产品。

## 固定套路（照抄执行，禁止临场发挥）

模型翻车基本都发生在"自己解释错误语义"上。下面两类表是**唯一正确响应**，见到即做，不推理、不换路、不重试第二次。

### 高频流程模板（链路固定，步数固定）

| 场景 | 固定链路 |
|---|---|
| 只读抓取 | `list_tabs` → 借 U（记 `original_url`）或 `open_new_tab` → `scan_page(session_id=...)` → 需要数据再 `execute_js(session_id=...)` → 借用的还原 URL；owned 的带 `owner_id` 关 |
| 表单交互 | `list_tabs` → `open_new_tab`（记 session+generation+owner）→ `wait_for_url` → `scan_page` → `page_type`/`page_click`（每次带 session_id）→ `wait_for_url` → `close_tabs(session_id, owner_id=...)` |
| 下载附件 | `download_file(url=..., session_id=...)`。禁 `?dl=1`、禁页面 fetch、禁裸 `Page.navigate` 猜目录 |
| 验证码页 | 同 tab `page_click` 一次 → `challenge_stalled` → **立刻交还用户，停手报告**。不另起浏览器、不跑长 `execute_js` |
| 长页读取 | `scan_page` → `scroll_page(to=...)` → 再 `scan_page`（±5000px 窗口外内容必须滚动后再扫） |

### 错误语义 → 唯一动作

| 见到 | 唯一动作 |
|---|---|
| `no_response` / `switched_session` | `list_tabs` → `switch_tab` 重定位 → 只重试**无副作用**操作；有副作用脚本必须先 `scan_page` 确认未落地 |
| `busy` | 等 10 秒重试**一次**；再 `busy` 就停手报告，不循环不删锁 |
| `blocked_by_dialog` | `handle_dialog(action="accept"|"dismiss")` 释放，然后继续 |
| `challenge_stalled` | 停，把 tab 交还用户 |
| `requires_user_action` / `input_activity_detected` | 改用 `page_*`，或停手 |
| `cdp_timeout` / `debugger_detached` | `list_tabs` 验桥活 → 重试一次无副作用调用 |
| `debugger_conflict` | 让用户关 DevTools/竞争 debugger，再原调用重试 |
| `401` / `unauthorized` | 转 [[abm-bridge-recovery]] 成因 6（token 文件不一致），不重启浏览器不重装扩展 |
| `Unknown command: downloads` | 目标浏览器扩展旧了：`chrome://extensions` 手动 Reload，不重启桥 |

### 硬禁止（以前能跑、现在必炸）

- `execute_js` 里 `setTimeout`/sleep 硬等 → 改 `wait_for` / `wait_for_url`
- 重放可能已执行的副作用脚本（`no_response` 后盲重试）
- 对 Turnstile/登录盾页跑长 `execute_js`
- 页面 `fetch` 下载附件

## 标准动手流程

1. **先定位目标 tab**：`list_tabs`（每个 tab 带 `browser` 字段 chrome/edge/opera + 一个 session id）。
2. **选中目标**：`switch_tab(browser="chrome")` 或 `switch_tab(url_pattern="linux.do")` 或 `switch_tab(session_id="chrome_xxx:123")`（session id 原样传，形如 `client_id:tab_id`，别拆）。**它只改后续调用的目标，不会把 tab 提到前台**（`activate` 默认 `false`）——改目标永远不打扰用户正在看的东西。真需要标签页到前面才传 `activate=true` 或调 `activate_tab`。
3. **读页面**：`scan_page`（简化 HTML/文本，保留登录态）。
4. **执行/交互**：普通点击/输入优先 `page_click` / `page_type` 的结构化 locator（`css` / `role+name` / `text` / `label`，可进同源 `frame` 和开放 `shadow`）；页面数据/API 才优先 `execute_js(script=...)`。`page_press` / `page_drag` 同样走后台 CDP，不抢用户鼠标；需要视觉核对时用 `capture_page_screenshot(session_id=...)`；开页 `open_url` / `open_new_tab`，其中 `open_new_tab` 默认 `active=false` 在后台创建；cookie `get_cookies`。
5. **物理输入是最后手段**（页面输入够不到的原生 UI/对话框）：`safe` 每次批准；默认 `lab` 按 `AGENT_BROWSER_LAB_NO_ELICIT=1` 免询问执行。跨进程锁、安静窗口、ownership 和前台确认始终生效。五个物理输入工具都接受 `session_id` 和 `activate_session`。
6. **等待与滚动**：`wait_for(selector=/text=/url_pattern=/js=)`（四个条件互斥，只传一个；`gone=True` 等元素消失）、**`wait_for_url(url_pattern=...)`**（等**导航落定**：URL 匹配 + `document.readyState === 'complete'`，正则或纯子串都试。点击跳转或 `open_url` 之后用它，别用 `wait_for(url_pattern=...)` —— 它只查 URL，新文档还是空白就可能返回）、`scroll_page(to="bottom"/"top"/像素数)`。别用 `execute_js` 里 sleep 硬等。
7. **上传文件**：`upload_files(selector="input[type=file]", paths=[...])`。
8. **下载附件**：直接 `download_file(url=..., session_id=...)`，让浏览器原生下载管理器带当前 profile 的 Cookie/登录态下载；默认等待完成并返回已验证的绝对 `path`。显式 `session_id` 必须存活，死 session 会报错而不会改投其它 profile。需要指定落点时给绝对 `directory`（会建父目录，默认拒绝覆盖同名目标；只有明确要替换时才传 `overwrite=true`，且必须保持 `wait=true`）；若超时返回 `directory_applied=false`，说明搬移未发生且不再跟踪，文件可能继续落入浏览器默认下载目录。附件不要用页面 `fetch`，也不要用裸 `Page.navigate` 猜下载目录。

## 工具选择优先级（按这个顺序，别一上来就动物理输入）

1. **页面读取/JS/页面 API**：`scan_page`、`execute_js`、`wait_for*`、`scroll_page`、`get_cookies`、`storage_get` —— 不打扰用户、不需要批准。
2. **后台页面输入**：`page_click`、`page_type`、`page_press`、`page_drag` —— 显式 `session_id` 下对指定 tab 派发 CDP 输入，不移动光标、不提前台、不需要批准。`page_click` / `page_type` / `wait_for` 的 `selector` 可传旧 CSS 或结构化 locator；歧义、不可交互、跨域 frame、关闭 shadow root 都拒绝派发。坐标是**视口**坐标（相对页面区域），不是桌面坐标。
3. **结构化中断**：对话框用 `handle_dialog`（配 `execute_js(dialog_policy="manual")` / `open_url(beforeunload="manual")`）；站点权限用 `set_site_permission` / `reset_site_permissions`（租约 60–600 秒，到期自动恢复）。
4. **物理输入**（最后手段）：`mouse_move` / `mouse_click` / `mouse_drag` / `type_text` / `hotkey` 这 5 个直接工具 —— 只用于浏览器 chrome、原生文件选择器、扩展弹窗、OS 对话框。`safe` 逐次批准；默认 `lab` 免询问，显式把 `AGENT_BROWSER_LAB_NO_ELICIT` 设为 false 才恢复会话级批准。`resolve_leave_dialog` 另有一条仅在两次协议处理失败后使用 Enter 的后备路径；`capture_desktop_screenshot` 只读、不需批准。

## 后台页面输入（page_*）—— 默认交互方式

`page_click` / `page_type` / `page_press` / `page_drag` 在指定 tab 内派发**受信任的 CDP 输入事件**（不是合成 JS 事件），但**不会**激活标签页、聚焦窗口或移动桌面光标。它们工作在后台 tab 上，是"点登录按钮/填表单/按回车"的默认选择，优先级高于物理输入。

- **必须显式传 `session_id`**：调用期间驱动绑定到该 tab，结束后把共享默认还原。指名死 tab 会被拒绝，不会偷偷换 tab 执行。
- **`execute_js` 全链路定向**：baseline/diff/transient monitor、无 ACK 安全重试、导航落点读取都继续使用同一个显式 `session_id`，不会在中间步骤掉回共享默认；`timeout` 是覆盖策略设置、执行、重试、monitor 与清理的单一总 deadline，不要再为各阶段额外叠加等待。
- **Xterm/ttyd 输入**：`page_type(selector=".xterm", ...)`、传 xterm 后代，或在页面只有一个 `.xterm-helper-textarea` 时省略 selector，都会自动聚焦 helper textarea 后派发受信任输入。要清当前 shell 行时先 `page_press("ctrl,u", session_id=...)`，不要把表单语义的 `clear=true` 当作终端清行。
- **坐标**：`page_click`/`page_drag` 的 `x`/`y` 是**视口**坐标。优先用 `selector`（点元素中心，可加 `offset_x`/`offset_y` 偏移）—— 跨域 iframe 里的 Cloudflare Turnstile 复选框可以点，不需要伸进 iframe 的 DOM。
- **验证码（Turnstile 等）留在用户的浏览器里**：在同一个已连接 tab 里用 `page_click` 处理，尝试次数有上限（回复带 `challenge_detected` 和 `attempts`）；验证码不再推进时结果是 `challenge_stalled`，**停下来把 tab 交还给用户自己处理**。绝不另起 Playwright / headless 浏览器 / 独立自动化 profile 兜底。
- **选择器没匹配**：返回 `not_found`，什么都没派发。

## 物理输入：profile 闸门后执行

`mouse_click` / `type_text` / `mouse_drag` / `mouse_move` / `hotkey` 走**真实鼠标键盘**，落在屏幕上**实际可见**的页面，跟"目标 session"是两件事。`resolve_leave_dialog` 在协议处理失败时也可能发送一次物理 Enter。

ABM 默认使用 `lab`（`AGENT_BROWSER_MODE` 未设也视为 lab），并按 `AGENT_BROWSER_LAB_NO_ELICIT=1` 语义免 elicitation；切到 `safe` 后每次调用都单独批准。用 `get_automation_profile` 查看，用 `set_automation_profile(mode="lab"|"safe")` 临时切换当前 MCP 进程；切换会清空批准缓存且不持久化。

拒绝、取消或客户端不支持批准时返回 `requires_user_action` 且不发输入。无论 profile 如何，跨进程锁、安静窗口、目标提前台和 `on_screen` 检查都不能跳过。

批准之后顺序固定：拿跨进程锁（**被占用 → 立即返回 `busy`，不排队**）→ 等安静窗口（用户碰了键鼠 → `input_activity_detected`，不发输入）→ 提前台 → 执行。OS lock 覆盖整个动作，超过元数据 TTL 30 秒仍不可抢占；TTL 只在动作结束或 owner 退出、OS lock 释放后回收 stale 元数据。`busy` 时停下稍后重试，别循环、删锁、杀进程或重启桥。

**五个直接物理输入工具都接受 `session_id` 和 `activate_session`**。正常浏览器输入应传完整 `session_id`（跟其它工具相同），工具会在安静窗口后激活并核验该标签页；不传则回落到全局共享的默认目标，而那个可能已被别的任务改掉：

```
别的任务 switch_tab(A)            → 全局默认 = A
你 scan_page(session_id=B)        → 在 B 上读，内部 restore 把默认还成 A
你 mouse_click(x, y)              → 提的是 A，点在 A 上 ✗ 目标错误
```

**正确写法**：

```
mouse_click(x=..., y=..., session_id=B)
type_text(text="...", session_id=B)
mouse_move(x=..., y=..., session_id=B)
mouse_drag(x1=..., y1=..., x2=..., y2=..., session_id=B)
hotkey(keys_csv="ctrl,c", session_id=B)
```

- 真要操作浏览器外的桌面（原生对话框、任务栏），且已确认当前可见焦点就是目标 → 显式传 `activate_session="none"`。
- 返回里的 **`on_screen`** 要看：`false` 表示窗口没能提到屏幕上（Windows 上最小化的 Chrome 不一定提得起来），这一击**不会命中**，别当成功——结果是 `activation_failed` 也不会发输入。`null` 表示扩展是旧构建、报不了，让用户去 `chrome://extensions` 点刷新。
- 返回里的 `activated.activated_session_id` 就是它实际提起来的 tab，跟你要的对不上就别继续点。

## 对话框与权限（结构化中断）

- **对话框**：全局默认仍是 `dismiss`（保住页面），不能静默把所有 beforeunload 改成 accept。`handle_dialog` 最多 3 秒，应答不到就明确 `no_dialog`/error，不允许挂 15 秒。
- **想离开 shell / ttyd / code-server / jupyter**：优先 `open_url(..., session_id=..., beforeunload="accept")`。默认 lab 对 `AGENT_BROWSER_AUTO_BEFOREUNLOAD_HOSTS`（默认 `shell.,ttyd,code-server,jupyter,vscode-web`）匹配的当前 host 会自动 accept；明确想留在当前页时传 `intent_leave=false`。
- **框已经弹出**：调 `resolve_leave_dialog(session_id=...)`。它先做两次协议 accept；只有 lab 允许物理输入时才用 Enter 兜底。要检查而不选择仍用 `handle_dialog(action="manual")`。
- **SPA/ttyd 内部命令失效**：`execute_js` 的 `set_dialog_policy` 明确 Unknown 时自动走 `Runtime.evaluate`；`open_url` 的 `navigate` 明确 Unknown 时自动走 `Page.navigate`。超时代表结果未知，有副作用的命令不得换路重放；先核对页面实际状态。不得停在误导性的 `Unknown cmd` 让用户手工操作。
- **站点权限**：租约 60–600 秒并自动恢复。`safe` 的每次 `allow` 都要批准；lab 会话级复用或按 no-elicit 配置执行。不可恢复的能力返回 `unsupported` / `requires_user_action`。

## 持续捕获与 debugger 生命周期

- Network：`network_capture_start(session_id=...)` → 执行页面动作 → 在清理路径中**始终** `network_capture_stop(session_id=..., url_pattern=..., resource_type=..., status_min=..., status_max=..., include_response_bodies=...)`。默认 500 条环形缓冲、单 body 256 KiB；`url_pattern` 按浏览器 JavaScript `RegExp` 编译，非法表达式会结构化报错并保持捕获运行，修正后重试 stop；有效过滤只影响返回结果，stop 始终释放 lease。
- Console：`console_capture_start` → `get_console_messages(offset=..., max_items=..., filter='user')` → **始终** `console_capture_stop`。诊断页面自身日志时用 `filter='user'` 排除 isolated extension/content-script context；要完整 buffer 用空值/`all`。
- capture、截图、PDF 或任意 CDP 返回 `cdp_timeout` 时，ABM 已强制 invalidate/detach；先 `list_tabs` 验证桥仍活，再最多重试一次无副作用调用。`debugger_conflict` 表示 DevTools/其他 debugger 占用，关闭竞争者后再试。
- 不要并发驱动同一个 tab 的多条一次性 CDP 操作；Network/Console capture 本身会共享 ABM 的引用计数 attachment。

### 页面内容读不出（分享页/SPA 前端不渲染）→ 抓包找 API 复用 token 直调

前端不渲染 ≠ 数据不存在。分享页/SPA 常因 Turnstile/登录态/风控故意不把正文渲染进 DOM，但服务器 API 数据完整（实测 DeepSeek 分享页：`/api/v0/share/content?share_id=...` 返回 200/659KB，前端 main 区域为空）。固定链路：

1. 打开页面 → `scan_page` 确认「有壳无正文」（记录可见字符数做证据，别判定 ABM 坏）。
2. `network_capture_start(include_bodies=true, max_body_bytes=2000000)` → `open_url` 同 URL 重载。
3. `network_capture_stop(url_pattern=/api/)` 找候选：返回 200 且 body_size 最大的 JSON，路径常含 share/content/detail/history/messages。
4. 从该请求提取完整 headers：`authorization` Bearer、`x-client-*` 自定义头、Referer、UA。
5. PowerShell `Invoke-RestMethod` 原样复制 URL + headers 直调 → 递归找 messages/content 数组 → 按 role（USER/ASSISTANT）重组 → 落盘。
6. 失败回退：403/401 → 重载页面重抓新 token；参数加密 → `execute_js` hook fetch/XHR 找生成逻辑；跨域 → `execute_js` 页面内 fetch 同源 API（自动带 cookie）。

硬规则：内容读不出先走这条链，不许另起 headless/Playwright 兜底；token 现抓现用，不写进任何文件当长期凭证。

## 截图不等于模型看见了页面

`capture_page_screenshot` 会返回文本元数据和 MCP `ImageContent`;可用 `full_page=true`、`clip={x,y,width,height,scale}`，JPEG/WebP 可带 `quality`;传 `save_path` 只会额外落盘,
不会抑制图片附件。但工具报告"已保存"或"已附加"只证明截图成功,**不证明当前模型能看见像素**。

- 当前模型/宿主能直接消费图片内容时,才可根据截图判断视觉状态。
- 当前模型不支持图片,或工具结果里只显示文件路径/元数据时,不得说"截图显示……""我看到……"。结构化网页先用 `scan_page`;需要页面内部状态时用 `execute_js`。
- canvas、WebGL、终端模拟器等 DOM 文本很少的页面,优先找页面自身的数据 API。Xterm.js 可从 `window.term.buffer.active` 的 line/cell 读取字符;只有页面没有可读 API 且环境提供 OCR 时才走"截图 + OCR"。
- 用户只是要求保存截图时,非视觉模型可以报告 `saved_to` 和大小,但不要替截图内容下结论。

## ⚠️ 硬规则：复用已有 tab + 别顶掉别人的 tab

这两条是最容易犯、最惹用户烦的错，务必遵守：

- **动手前必 `list_tabs`，然后按 U/A/B 归属决定——别把"已有匹配 URL"等同于"可以随意改这个 tab"**：
  - **目标站已经开着**（`list_tabs` 里有 `url` 匹配的 U）→ 仅读/轻操作可借用 B；会导航、重表单或明显改变页面时优先 `open_new_tab` 开 A。借用前记 `original_url`，结束仍存活时恢复，绝不 close。
  - **目标站没开过**（`list_tabs` 里没有匹配的 tab）→ **直接 `open_new_tab("https://目标站")` 在后台开一个，这是正常且正确的操作，不要犹豫、不要卡在"确认 session"、更不要反过来问用户"要不要我打开"**。用户让你操作某个站，就默认授权你去开它。ABM 开新 tab 默认 `active=false`，不抢前台；只有确实需要用户看见页面或使用物理键鼠时才显式传 `active=true`。
  - `open_new_tab` 返回的 `generation` 是该原生 tab 生命周期标识；create ACK 有 `tab_id+generation` 时即返回 `owned=true` 和随机 `owner_id`，即使 session 尚未注册、`ready=false`。调用方必须保存 `session_id + generation + owner_id`；`ready=false` 只表示暂不能用 session 工具，不影响用 owner capability 安全清理。同一任务需开多个 A 时，把第一个 `owner_id` 传给后续 `open_new_tab(owner_id=...)`，便于统一安全收尾。
  - 一句话：**只读可借 U；会改状态优先开 A；A 用完必带 owner_id 关闭。**
- **每次页面操作都显式带 `session_id`，不要吃全局默认**。`list_tabs` 拿到目标 tab 的 session id 后，`scan_page(session_id=...)` / `execute_js(session_id=..., script=...)` / `capture_page_screenshot(session_id=...)` / `page_click(session_id=...)` 全都把它带上。原因：全局默认 session 是所有任务共享的单例，另一个任务一 `switch_tab`/`open_url` 就会把它改掉；如果你依赖默认，下一步就可能打到别人刚切过去的 tab 上，把正在用的 tab"顶掉"。**显式 session_id = 你的操作永远落在你锁定的那个 tab，不受其它任务干扰。**（`scan_page`/`execute_js` 内部已做 save/restore，带 session_id 调用后会把全局默认还原，不再永久污染；但你自己每步都带上才最稳。）
- **一句话**：先看有没有现成 tab（有则复用），锁定它的 session_id，之后每一步都带着这个 id。

## ⚠️ 多对话/多 agent 并行：Tab 归属（硬规则）

用户经常**同时开多个对话**用 ABM。冲突的本质是**抢同一个物理 tab**。必须区分三类：

| 类型 | 是什么 | 你必须怎么做 |
|------|--------|----------------|
| **U 用户 tab** | 用户自己开的、任务开始前就在 `list_tabs` 里的 | **默认不管、禁止 close**；不要当临时工作区 |
| **A 你自有 tab** | 本任务 `open_new_tab` 得到且返回 `owned=true` 的 | 记下 `session_id`+`generation`+`owner_id`，全程只带该 session；**任务结束（成功或失败）必须用 owner_id close** |
| **B 借用 U** | 目标站已经开着，你临时用 | 可借用；结束时 **恢复 original_url**（需要的话）；**绝不 close** |

### 决策序

1. `list_tabs`。
2. 目标站**已开** → 只读/轻操作才借 U；会改 URL/重表单则优先 `open_new_tab` 开 **A**。必须借 U 时先保存 `original_url`。
3. 目标站**未开** → `open_new_tab` → 保存 `session_id + generation + owner_id` → 只操作返回的 A → **finally 调 `close_tabs(session_id, owner_id=...)`**。
4. 并行对话：各开各的 A，或 `browser=chrome|edge` 分工；**禁止**多个对话挤同一个用户热门页硬抢。
5. 发现内容被别人改、`switched_session`、莫名导航 → 让出，换自己的 A。

### 禁止

- 关闭用户预存 tab（TG、用户 shell、用户业务页等）
- **把任务开始时 `list_tabs` 的整表登记成「可关闭」**（会把用户已开站一起关掉——灾难）
- 只有 **`open_new_tab` 返回的** session 才准 close；借用的 tab **只还原 URL，绝不 close**
- `close_tabs` 默认 `only_if_agent_owned=true`：不带正确 `owner_id` 会拒绝；不得为省事把它关掉。只有用户明确说要关闭某个 U/非 owned tab 时，才可设 `only_if_agent_owned=false`
- 批量 close 前所有目标必须属于同一个 `owner_id`；混入 U 或别的 Agent A 时整批不应执行
- 用户若已手动关掉你开的 tab（含**误点到你的 tab 再关掉**）：当作 `already_gone` / `closed_by=user`，清掉该 owned；**不要**用旧 tab_id 补关，**不要**默认连环重开；任务还需要该站时再 `open_new_tab` 拿**新的** A（或先问用户）
- **用户自己开、且从未被你 `open_new_tab` 进 owned 的站，用户自己关了**：只是 list 少了一项——**不要当成你关的**，不要写进「本任务已关闭」、不要因此去 close/补枪
- 只有你对 **owned** tab **主动调用了 close/release 并且成功**，才能说是你关的
- 借用后不还原 URL（tab 还在时）
- 依赖全局 default_session、不传 `session_id`
- `open_new_tab` 后任务结束不关（泄漏）
- 把 U 当成「用完就关」

### 与「同对话内复用」的关系

- **同一对话**：目标站已开且只读/轻操作 → 可借用；会导航或改重状态 → 开自己的 A。
- **多对话并行**：更稳的是**各开各的 A**；只有明确可共享只读时才共用一个 U。

## 关键约定

- **多浏览器并存**：动手前先 `list_tabs` 按 `browser` 字段锁定 chrome 还是 edge，别跨浏览器误操作。可用 `AGENT_BROWSER_PREFERRED_BROWSER=chrome|edge` 设盲选默认。
- **并发**：不同 tab 可并行；**同一个 tab 内的操作要串行**（CDP fallback 的 attach/detach 会打架）；并发时**显式传 session_id**，别吃默认 session（failover 会改写默认）。
- **返回里的状态字段**：
  - `status="no_response"` —— 看 `delivery_state`、`retry_safe` 与 `abm_retried`：`undelivered/true` 表示脚本压根没离开桥（可放心重试）；`sent_unconfirmed/true` 表示已写进扩展 socket 但没等到 ACK，扩展是「先 ACK 再执行」，所以基本没跑，但**不是证明**——不可逆的动作（下单、删除、转账）看到这个状态要先用 `scan_page` 核实再决定；`delivered_no_result/false` 表示脚本可能仍在运行，禁止重放副作用。`abm_retried=true` 表示 ABM 已在原 deadline 内自己重试过一次，你这次重试是第二次。显式 session 不会改投其它 tab。见到先 `list_tabs` 重新确认目标；若脚本只是用 `setTimeout`/sleep 等待，改用 `wait_for`/`wait_for_url`，不要继续把等待嵌进 `execute_js`。
  - `blocked_by_dialog` / `blocked_by_beforeunload` —— 有原生对话框开着，用 `handle_dialog` 应答（导航被拦就带 `beforeunload="accept"` 重发）。
  - `cdp_timeout` / `debugger_detached` —— 当前调用失败但 lease 已回收；`list_tabs` 应仍可用。`debugger_conflict` 要先关闭 DevTools/竞争 debugger。
  - `busy` —— 另一个 ABM 进程持物理输入锁，或该 tab 已有挂起的 manual 执行；**立即返回、绝不排队**，等那件事结束再重试，别循环。
  - `requires_user_action` —— 物理输入或 `set_site_permission(allow)` 的批准被拒绝/取消/不可用，什么都没做。
  - `input_activity_detected` / `activation_failed` —— 物理输入批准后因用户动了鼠标键盘、或 tab 无法确认在屏上而**没有发出**。
  - `challenge_stalled` —— 验证码在尝试上限内没进展，把 tab 交还给用户。
  - `redirected` / `navigated` —— 导航落点与请求不同 / `execute_js` 把页面导航走了（返回值确实丢了，看 `landed_url`）。
  - `type="download", status="triggered"` —— `open_url` 被浏览器下载取代；只有同时有 `isDownload=true` 时 `ERR_ABORTED` 才是正常下载语义。要完成/失败和最终路径，改用 `download_file`。
  - `closed_by="agent"|"user"` —— 只有 `agent` 才能计入本任务主动关闭；`status="already_gone", closed_by="user"` 表示 owned tab 在收尾前已被用户关闭，禁止补关旧 id。

## 工具全表（55 个）

**没有任何工具把 `session_id` 设成必填** —— 冷启动直接 `scan_page` 就能读当前 tab，
不必先 `list_tabs` + `switch_tab`。默认目标死了（tab 关了、浏览器重启、扩展 reload）
会自动重选活的；但**你明确指名的死 tab 会被拒绝**，不会偷偷换 tab 执行。
多任务并行时仍然按上面的规则显式带 id。

| 类别 | 工具 |
|---|---|
| 探测/诊断 | `get_setup_status`、`get_automation_profile`、`set_automation_profile`、`extension_path`、`pointer_info`（只读，不需批准） |
| 标签页 | `list_tabs`、`list_all_tabs`、`switch_tab`、`activate_tab`、`open_url`、`open_new_tab`、`close_tabs` |
| 读页面 | `scan_page`（简化 HTML/文本，长链接压成 `#r1` 短引用，真实 URL 一并返回）、`capture_page_screenshot` |
| 执行 | `execute_js`（带 `dialog_policy`）、`cdp_command`、`cdp_batch`、`debugger_targets`、`save_pdf` |
| 等待/滚动 | `wait_for`、`wait_for_url`（等导航落定：URL 匹配 + readyState complete）、`scroll_page` |
| 后台页面输入 | `page_click`、`page_type`、`page_press`、`page_drag`（视口坐标，不需批准） |
| 对话框 | `handle_dialog`、`resolve_leave_dialog`（配合 `execute_js(dialog_policy="manual")`、`open_url(beforeunload="manual"|"accept")`） |
| 站点权限 | `set_site_permission`（租约 60–600s；`safe` 每次批准，`lab` 会话复用/可免询问）、`reset_site_permissions` |
| 表单/文件 | `upload_files`、`download_file`（原生登录态下载；默认等完成并返回最终绝对路径） |
| Cookie/存储 | `get_cookies`、`set_cookies`（CDP 写，HttpOnly 能写；无 url/domain 时限定当前页）、`delete_cookies`、`storage_get`、`storage_set`（写后回读验证） |
| 持续捕获 | `network_capture_start`、`network_capture_stop`、`console_capture_start`、`get_console_messages`、`console_capture_stop` |
| 物理输入 | `mouse_move`、`mouse_click`、`mouse_drag`、`type_text`、`hotkey`（safe 逐次批准；默认 lab 免询问；均接受 `session_id`/`activate_session`）；`resolve_leave_dialog` 仅在协议失败后可能发送 Enter；`capture_desktop_screenshot` 只读并返回 MCP 图片 |
| 扩展/书签 | `list_extensions`、`set_extension_enabled`、`uninstall_extension`、`call_extension`、`get_bookmarks`、`create_bookmark`、`remove_bookmark` |

## 出问题了怎么办

- 工具挂住 / `list_tabs` 拿不到 tab / `get_setup_status` 转圈 / 报连不上桥 —— **不是本 skill 的事，转 [[abm-bridge-recovery]]** 分层排错（一条 `agent-browser-mcp doctor` 直接判定是哪种故障 + 怎么修）。别在这里反复重试。
- **升级后先看版本诊断**：`get_setup_status` 返回 package/bridge/extension/protocol 版本。允许自动拉起时，未监听的 bridge 会自动启动；`restart_bridge_required=true` 表示仍占端口的旧 bridge 必须执行 `agent-browser-mcp bridge --restart`。只有 `reload_extension_required=true` 才需要用户在扩展页手动 Reload unpacked extension。若拿到 `status: stale_package` / `action: restart_mcp_session`（某个组件比运行中的服务**更新**），过期的是 MCP 进程自己：让用户重启 MCP 会话，**别叫他重启 bridge 或重载扩展**，那两步只会再报同一个不匹配。
- **报 401 / unauthorized**：bridge 与 MCP 没有读到同一个持久 token。默认唯一真源是 `~/.agent-browser-mcp/bridge-token`，各编辑器无需配置；文件已存在时残留的 `AGENT_BROWSER_BRIDGE_TOKEN` 也不能覆盖它。先按 [[abm-bridge-recovery]] 的「成因 6」确认 token 文件路径；默认路径一致时重启升级前的旧 bridge 一次，不要逐个适配或重启编辑器。
- **tab 卡住，每次调用都返回 `blocked_by_dialog` / `busy`**：`manual` 对话框策略留下了一个开着的原生对话框 + 后面暂停的执行。在那个 `session_id` 上调 `handle_dialog(action="accept")` 或 `"dismiss"` 释放。期间其它 tab 正常工作。
- **物理输入返回 `requires_user_action` 且从不弹批准**：客户端不支持 elicitation。优先改用 `page_*`；私有 lab 明确接受风险时可设置 `AGENT_BROWSER_LAB_NO_ELICIT=1` 并重启 MCP 进程。
- **验证码 `challenge_stalled`**：ABM 已停止尝试，把那个 tab 交给用户手动过盾；过完在同一 tab 继续。别另起浏览器。
- **客户端没有 `download_file`**：MCP 工具 schema 还是旧的，重启 MCP 会话/客户端。
- **`download_file` 返回 `Unknown command: downloads`**：MCP 已更新但目标浏览器仍加载旧扩展。先看 `get_setup_status.reload_extension_required`；为 true 时去该浏览器的 `chrome://extensions` / `edge://extensions` 手动 Reload Agent Browser MCP Bridge（版本应与 `get_setup_status.package_version` 一致）。不要据此重启浏览器，也不要退回页面 fetch。
