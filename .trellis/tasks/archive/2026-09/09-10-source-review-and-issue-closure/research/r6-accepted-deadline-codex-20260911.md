# r6：接受离页确认后的导航 deadline 修复

作者：`codex-resume-01a0881c`。实现提交 `e18007c`；版本保持 `0.4.20`。
本记录补充已归档任务，不重复归档或改写其他作者的研究。

## 已复现的缺陷与最小修补

用户此前的 Reload 已加载 r5 构建 `2878475f7d821497`。随后正式 finalizer 的离线
2716 项通过、零 skip、coverage 95.42%，但 live 为 11 passed / 50 failed：
一个 accepted-beforeunload 导航提前超时，导致同一 scratch tab 后续 49 次 `target_busy`。
own-tabs registered/released 均为 4，outstanding 为 0，检查 enforced。

本机 loopback HTTP 对照固定调用预算为 20 秒。立即响应在 0.1746 秒成功；延迟 4 秒的
响应在 3.1612 秒报 `cdp_timeout`，之后页面正常到达目标，目标请求只有一次。
根因是 `navigateWithDialogPolicy` 接受对话框后另取 `min(remaining, 3000)`。

修补只删除这个 3 秒上限，沿用原导航剩余总 deadline。保留结构化未知结果、原回执、
`retry_safe: false`、占用 grace、真实超时与取消路径。构建 stamp 为 `cf99c8ef9dc76da6`。
没有增加导航重放、强制解锁、总预算重置或额外的取消分支。

## 验证与复审

新增 3.5 秒完成 / 5.5 秒预算的回归在修补前失败；修补后成功。对应的 1.2 秒预算反例
仍报超时，并经真实 bridge/registry 断言原回执、TTL 前拒绝并发、到期释放和只派发一次。
六个相关套件 202 passed / 0 skipped；Ruff 115、ESLint 6、mypy 15 文件 clean，后两者 enforced。

独立 reviewer 的 r6 final 结论为 PASS，无 findings。161 个选定文件加 SPEC 绑定检查通过，
其中只有 `background.js` 和 `test_navigation_dispatch_state.py` 相对 r5 变化。
subject 为 `3a4e5be8f4c8a7a9a3eeac6269cd643d146d081c17208ce946fd2b06e8a2339a`。

证据：`out/review-inputs/source-review-closure-20260911-r6/` 内的 `deadline-red-junit.xml`、
`focused-junit.xml`、`lint.json`、`packet.json` 与 `result.json`，正式记录在
`out/reviews/source-review-closure-20260911-r6.json`。r5 记录完整移入 `out/reviews/archive/`。
失败 finalizer 的八文件快照和本机红色对照仍在 r5 目录；文件均未覆盖。

## 交接范围与后续

同目录 `wait-probe-release-claude-20260911.md` 是 Claude 标记 DONE 的副本研究交接。
其原文随本次本地收尾保存，作者归属保留；未将副本补丁或其测试数字当成本树实现及证据。
该报告提出的只读探针提前释放与 tabs_update 结算导航属于后续评估。
初次等待的 30 秒上限、race 定时器清理、最初 wait 回包延迟原因，以及其他报告级未决项仍在。

本次 doctor 确认 bridge 当前、无需重启，扩展仍为 r5；需人工 Reload 后核验 r6 stamp。
接续完成本机延迟对照的绿色验证，再串行执行 `scripts.finalize_change --bump none`、
`scripts.evidence_manifest --check` 与完整 `scripts.check_install artifacts/dist`。
本记录不声明上述运行验证或封存已成功；结果写入 r6 continuation state 和知识库。

旧 SPEC rev7 仍为 `delivered_stub`，A7 人工验收影响 R2/R6。独立跨平台候选由另一会话
维护，本轮没有接管其目录；本轮没有 push、tag 或发布。
