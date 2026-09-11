# 触发源 A：只读探针超时后释放标签页（Claude 侧，2026-09-11）

作者 `claude-code`。补丁只存在于副本 `%TEMP%\mut4`（= HEAD `4992294` 全量展开），
**共享树的 Python 文件一个字节都没改**。`background.js` 与
`tests/test_navigation_dispatch_state.py` 由 `codex-resume-01a0881c` 持有，本文件不碰。

## 1. 先证伪：`744e4e9` 没有关闭触发源 A

`744e4e9` 对 `server.py` / `browser_bridge.py` 的实际改动是透传 `reservation_held`
并新增提示 `_PENDING_OPERATION_HINT`。全树 `grep` 无释放路径，锁未被解除。

在 `4992294` 干净副本上用真 wait → bridge → registry 链（只替换传输与时钟）实测：

| 观察 | 值 |
| --- | --- |
| `silent_after`（探针 budget 6.0 + grace 60.0） | **60.8s** |
| 返回后立刻发下一条命令 | `target_busy` |
| **照提示调用 `get_execute_js_result` 之后**再发 | 仍 `target_busy`，`status=in_progress` |
| 标签页实际可用时刻 | t+61s，由 TTL 放开 |

结论：那句提示暗示"先补查、再发下一条"是解锁前提，实测补查对占用**零影响**。
静默阻塞变成了有文档的阻塞，且文档描述了一个不存在的补救措施。

按证据等级（可复现实测 > 代码路径推理），这是继续做释放的依据。

## 2. 改动（四处，射程按"谁写的脚本"划线）

1. `pending_operations.py` 新增 `release_targets(operation_id, requester_id, *, reason)`：
   只对**自己的**、**`in_progress`**、且**确实还持有 target** 的操作动作；写入
   `reservation_released` / `released_reason` / `released_targets`。
   **不暴露为工具** —— 调用方不能自证自己的脚本只读。
2. 同文件 `reservation_held` 的推导从 `status in ACTIVE_STATUSES` 改为查真实
   target 映射（新增 `_holds_targets`）。**这一步是必需的**：释放后会出现
   "`in_progress` 但不持有任何 target"，旧推导会开始说谎，而
   `browsertap-bridge-recovery/SKILL.md` 明文规定该字段的语义是**关于 target 的**。
3. `browser_bridge.py` 新增 `release_probe_reservation()` + 远端 cmd 分支。
   远端老桥不认识该 cmd 会回 `unknown_command` ⇒ 返回 `False` ⇒ 保持占用，
   失败方向落在安全那一侧。
4. `server.py` 的 `_poll_wait_condition(..., release_pending_probe=)`：
   `wait_for` 传 `kind != "js"`，`wait_for_url` 传 `True`；仅在**确实释放成功**时
   才把提示换成 `_RELEASED_PROBE_HINT`。

**receipt 保持 `in_progress` 而不是置终态**，所以迟到回复照常走 `complete()`
的正常路径落地 —— 原设计里"迟到回复被丢弃"那半边（F1）因此不需要单独记账。

边界依据：`selector` / `text` / `url_pattern` 的脚本整段由 server 拼装
（`querySelector` / `innerText` / `location.href`，以及 query 模式下
`scrollIntoView` 已被 `verifyHit` / `purpose === 'click'` gate 掉的
`locator_query_script`）；只有 `js` 是调用方表达式，保留保守持有。

## 3. 实测结果

| kind | 返回时 `reservation_held` | 紧接的下一条命令 |
| --- | --- | --- |
| `js`（调用方代码） | True | `target_busy`，t+61s 才放 |
| `selector` | False | 立即通过 |
| `url_pattern`（`wait_for_url`） | False | 立即通过 |

两种释放情形下 receipt 仍可领取，`js_return` 解析出 `{"met": false}`。
另实测：释放后由后继者占用该 tab，此时迟到回复到达，**后继者保持持有**、
第三方仍被正确拒绝（`_release` 的 `== operation_id` 守卫拦住）。

## 4. 门禁

- 契约套件重写后 **22 passed**（原 `test_wait_timeout_preserves_real_bridge_receipt_until_late_reply`
  的 2 个 `url` 参数与新行为冲突，已拆成"调用方脚本仍持有"与"只读探针释放"两条）。
- **变异验证 9/9 全捕获**，每条都由名字与变异相符的测试红。其中三条
  `release_targets` 内部守卫首轮 `UNCAUGHT`（wait 路径够不到），已补三条 registry
  级直测；`blocked_by_dialog` / `outcome_unknown` 那条是唯一能证明状态守卫承重的用例
  —— 它们都在 `ACTIVE_STATUSES` 且都真持有 target，只有状态守卫拦得住。
- 整轮离线 **2724 passed / 4 skipped / 0 failed**（232.93s）。
- `python -m scripts.check_tool_docs` exit 0；四处文档已同步（两个工具描述、
  两份 README、两份 SKILL）。

⚠️ 方法论坑，值得记进规范：第一次整轮离线出现
`test_path_traversal_protection.py::test_tool_uses_safe_path_validator[save_pdf]` 红，
`inspect.getsource(tool)` 返回 `'        }\n'`。原因是**我在后台套件运行期间编辑了
`server.py` 的工具描述**：`linecache` 重读了变化的文件，导入时捕获的
`co_firstlineno` 失效（`save_pdf` 在 5454 行，编辑点在 4551）。
纯净 HEAD 单跑该文件 9 passed，补丁副本单跑同样 9 passed，不改文件重跑整轮 0 failed。
**套件运行期间不要编辑被测源文件** —— 基于 `inspect` 的测试在断言时才读盘。

## 5. 对 r5 live 失败（49 次 `target_busy`）的诚实结论

从 `artifacts/live-junit.xml`（12:28）按累计偏移量数出来：

- 触发点唯一：`test_open_url_beforeunload_dismiss_then_accept_and_cleanup`，
  t=71.5..78.8s，报 `cdp_timeout: navigation outcome did not arrive before its
  deadline`，**`dispatched: True`**。
- 随后 49 条全是 `Target chrome_5u5shi:1935864710 is in use by another MCP call`，
  t=78.8 → 98.6s（**跨到套件结束，占用从未在本轮内释放**），中间 6 条通过的用例
  都是打其他 tab 的。

**本补丁不减少这 49 条。** 那次持有来自一个已经 dispatch 的导航，而本补丁刻意
不释放它 —— `Page.navigate` 确实发出去了，页面可能真在变。触发源 B 那半边
（`dispatched: false` 提前放行）在 `744e4e9` 里已经生效，且它**判对了**：
这次不是"发之前就失败"，所以它没有声称未投递。

放大倍数才是这里的杠杆：一次持有 = `budget + 60`s 的拒绝窗口
（该导航 budget 20s ⇒ 80s），本轮只剩 19.8s 就跑完了，所以是 49 条；
套件更长则更多。修好 deadline（`codex-resume-01a0881c` 的分工）能消掉**这一次**
触发，放大器仍在原地。

## 6. 交出去的第三个杠杆：导航的未知结果是**可观测的**（未实现）

与不透明脚本不同，导航落没落地可以直接问浏览器，而这条信号**已经在流进桥里、
但没有任何读者**：

- `background.js:5406-5408`：`chrome.tabs.onUpdated` 在
  `changeInfo.status === 'complete' || changeInfo.url` 时 `sendTabsUpdate()`。
- 该快照每个 tab 带 `url`（`_valid_page_fields` 允许，`_valid_tab_snapshot` 校验），
  经 `type: 'tabs_update'` 进 `_apply_extension_tabs`（`browser_bridge.py:1758`），
  并在 `:1799` 用来刷新 session 的 `url`。
- 但 `_apply_extension_tabs` 只在 tab **被移除**时调
  `_finish_tab_operations(sid, 'tab_removed')`（`:1795`）。**没有任何路径**用
  "该 tab 已到达请求的 URL 且 `complete`" 去结算一个超时的 navigate 操作。

即：桥正在被告知导航已经落地，却仍然按 `budget + 60` 把 tab 扣着。
`codex-resume-01a0881c` 自己的记录也写了"eventual successful landing with exactly
one request" —— 那正是这条信号本该结算的情形。

这与仓库既有规则同向（`AGENTS.md` §4：不确定的 dispatch 之后**检查状态**而不是重放），
也与本轮复核反复出现的形状同类：**信号存在，没有读者**。
未实现、未测量成本，标 `UNVERIFIED` 交给持有方决定是否纳入。

## 7. 整合方待办

补丁在副本，未进共享树。若采纳：按上面四处应用，跑
`tests/test_wait_reservation_contract.py`、`tests/test_pending_bridge_operations.py`、
整轮离线、`check_tool_docs`，再进 `versioning bump patch` → 提交 →
`finalize_change --bump none`。live 名额与扩展改动仍归 `codex-resume-01a0881c`。
