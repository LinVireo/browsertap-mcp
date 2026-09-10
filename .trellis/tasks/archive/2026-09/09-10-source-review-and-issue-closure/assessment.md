# browsertap-mcp — 三视角评估（2026-09-10，工作区 HEAD `de0ed83` + 未提交批次）

评分基线：源码从头通读（`server.py` 全文、`bridge.py`、`cli.py`、`paths.py`、
`simphtml.py`、`page_input.py`、`extension_build.py` 全文；`browser_bridge.py` 与四个
守护进程模块由 fork 复审；`background.js` 读取路由 / 执行 / 批处理 / CSP / cookie 段），
加上离线 2629 passed、live 子集 14 passed，以及 6 条本轮实测过的真实浏览器行为。
"已验证" = 有 file:line 或实测输出；"推断" 单独标注。

## A. 专业开发者视角 —— 7.5 / 10

**强项（已验证）**
- 契约密度极高：每个边界都写清楚"为什么"，`_result_envelope`（`server.py:593`）把
  49 个工具统一成 `btap.result.v1`，`retry_safe` / `delivery_state` /
  `may_have_executed` 是一等公民，不是事后补的字段。
- 危险操作全部有"证据链"：`_TabOwnershipRegistry` 的 owner+generation 双重能力
  （`server.py:725`）、`open_new_tab` 的 exactly-once 协调（`server.py:3086`）、
  `spawn.lock` 的 O_EXCL + pid 活性（`server.py:1156`）、`bridge.pid` 的
  pid+creation_ticks+exe 身份（`bridge.py:306`）。
- 三平台进程身份、日志就地轮转（`bridge.py:46`）、Windows 句柄语义都处理到位。
- 测试是真门禁：2629 离线用例、live 套件读构建 stamp（`extension_build_verdict`）
  而不是版本号。

**缺陷（本轮新发现，已修）**
1. P0 `set_cookies` / `delete_cookies` / `cdp_batch` / `upload_files` 自 82aabc9 起
   坏了一周：命令信封走 `code` 字段被 eval（实测 `SyntaxError: Unexpected token ':'`），
   cookie 静默降级到 `document.cookie`（HttpOnly 丢失、`status: ok`）。
2. P0 `page_type` 的焦点守卫缺 `"cmd":"cdp"`（旧 `server.py:6240`），扩展记
   `unknown cmd: undefined` 后继续输入，`batch_guard_failed` 永远触发不了。
3. P1 `press_commands` 的 keyDown 无 `text`（`page_input.py:298`），Enter 不提交
   表单、textarea 不换行、`page_press("a")` 不出字符（实测
   `keys=[keydown:Enter, keyup:Enter], submitted=false`）。
4. P2 `ChallengeAttemptTracker` 按首次尝试计龄、server 端按空闲计龄，两边可以不一致。

**结构性代价（未修，推断）**
- `server.py` 7600+ 行、`background.js` 5300+ 行，单文件承载全部工具；测试按文本
  位置切 `background.js`（memory 里记过 88 处 `.index()` 锚点），拆分成本真实存在。
- `execute_js` 默认 `no_monitor=False`：每次调用多做两次全页 `get_html`
  （300k 字符上限 + BeautifulSoup 两次 + difflib），大页面上是秒级 CPU。默认值偏向
  "信息量"而不是"延迟"。
- `_externalize_execute_js_result` 写到系统临时目录且不回收（`server.py:4751`）。

**判据**：这三处 P0/P1 都属于"门有读者、但读者看错了字段"——live 断言读 `status`
不读 `method`，离线断言读 `method`/`assertTruthy` 不读 `cmd`，live 断言读 `keydown`
不读 `keypress`。修复时把断言改成读"路由"本身。

## B. 企业采用视角 —— 6.5 / 10

**可以接受的部分（已验证）**
- 本地端口 token 鉴权 + `hmac.compare_digest`、`/link` 拒绝时 drain body、
  WS Origin 只放扩展协议、client takeover 60 秒宽限（fork 复核：三条 HTTP 路由全部
  过 `check_link_token_drained`）。
- 写路径沙箱：`save_pdf` / `capture_page_screenshot` 只能写 `~/Downloads/browsertap`
  （`server.py:108`），原子写 + 50 MB 上限。
- 隐私政策与 manifest 权限有契约测试对齐（CHANGELOG Unreleased）。
- 日志脱敏（`redact_url`），token 只记指纹。

**企业会卡住的点**
1. **读/写边界不对称（设计，未修）**：`upload_files` 可把本机任意可读文件送上任意站点
   （`server.py:6361`），`download_file(directory=...)` 可落到任意绝对目录
   （`server.py:3739`）。写有沙箱、读没有；prompt injection 场景下这是数据外泄通道。
   建议：上传根目录白名单（环境变量）+ 下载目录默认沙箱同 `save_path`。
2. **默认高权限**：`BROWSERTAP_MODE=lab` + `LAB_NO_ELICIT` 默认 true，站点权限租约
   和 leave 对话框物理回退都不弹确认（`server.py:915-946`）。企业部署必须显式
   `safe`，但文档把 lab 写成"本地操作员默认"。
3. **信任域是"同一 Windows 用户"**：`requesterId` 由客户端自报（fork 发现 #9），任何
   持 token 的本机进程可读另一个会话的保留结果。对多 agent 共享一台机器的团队要说清楚。
4. **CSP 剥离**：`withCspOff` 按 tab、按调用剥 CSP 响应头（`background.js:4441`），
   有 refcount 和启动清扫；已在 PRIVACY.md 披露。安全团队会追问"剥掉期间页面自身脚本
   是否也被放宽"——答案是"是"，需要在 SECURITY.md 明确。
5. **可运维性**：`browsertap doctor` 在无桥时会自己拉起桥再报 `ext_never_registered`
   （host-notes 记录）；企业监控会误报。
6. **供应链**：`node_modules` / eslint 进入 lint 门；PyPI 已上传、registry 已上架；
   Chrome Web Store 仍未发布 ⇒ 企业无法用策略推送扩展，只能开发者模式 unpacked 安装。

## C. Agent 作为使用者视角 —— 7 / 10（修复前 5.5）

**好用的地方（已验证）**
- 每个失败都带 `next_action` / `hint` / `poll_with`，并且 `retry_safe` 明确告诉我能不能
  重发；`SessionTargetNotFoundError` 直接列出替代 session。
- `open_new_tab` 返回 `owner_id`，`close_tabs` 只关自己开的；不会误伤用户 tab。
- `scan_page` 报 `render_state`（`shell_only` / `hydrating`），SPA 空壳不会被我当成
  空页面。
- `get_execute_js_result` 让超时的脚本可以领回结果而不重放。

**让 agent 走弯路的地方**
1. 修复前：`page_type(submit_key="Enter")` 返回 `status: success` 但表单没提交——
   agent 会误以为提交成功；`set_cookies` 返回 `ok` 但 HttpOnly 丢失；`upload_files`
   直接报 `page_execution_failed`，agent 只会猜选择器错了。三者都是"成功壳子里的失败"。
2. 修复前错误文案"keep this MCP server running via Hermes"：非 Hermes 客户端的 agent
   会去找 Hermes。
3. `cdp_timeout` 后 tab 永久 `target_busy`（fork #1，属另一 task）：agent 只能换 tab
   或让用户关掉，没有释放手段。
4. `execute_js` 默认监控让简单读取也慢；skill 文档建议 `no_monitor=true` 但默认值
   反过来。
5. 49 个工具零分组、描述长（`open_new_tab` 描述 900+ 字符）——对上下文预算是负担；
   memory 里 Q5（`--caps` 分组）仍未做。

## 综合 —— 7 / 10

架构与契约意识远高于同类项目；本轮暴露的问题不在设计，而在**门禁读错字段**：一次
安全修复（cmd/code 拆分）让四个工具坏了一周，离线 2595 用例与 live 54 用例都没抓到。
修完后离线 2629 passed、live 子集 14 passed，六条新 live 用例读的是路由与 `keypress`，
同类回归不会再静默通过。企业采用前需先决定读路径沙箱与默认 `safe` 模式；agent 侧
的最大剩余痛点是 `cdp_timeout` 后的永久保留（已移交 zombie task）。
