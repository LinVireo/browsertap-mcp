# 原目录覆盖测试整合结果 — 2026-09-12

Active task: `.trellis/tasks/09-12-coverage-handoff`

实现者：根会话委派的 `trellis-implement`；文件认领仍属于
`codex-coverage-01a09080`。本实现者未修改认领、任务状态或他人的研究记录。

## 结果与改动范围

已在原目录应用候选补丁，随后只适配一个落后于当前合同的断言。
最终变更为 18 个测试文件、2426 行增加、32 行删除；未改产品源码。
最终 18 文件同轮离线测试为 **1222 passed，0 failed / error / skipped**。
Ruff tests、`git diff --check` 和 18 文件 LF 检查均通过。

| 文件 | 增加 | 删除 |
| --- | ---: | ---: |
| `tests/test_bridge_coverage.py` | 96 | 0 |
| `tests/test_browser_bridge_coverage.py` | 544 | 7 |
| `tests/test_cli_coverage.py` | 64 | 0 |
| `tests/test_command_scope.py` | 61 | 0 |
| `tests/test_dialog_policy.py` | 82 | 0 |
| `tests/test_downloads.py` | 73 | 2 |
| `tests/test_execution_failure_contract.py` | 150 | 0 |
| `tests/test_file_write_safety.py` | 53 | 0 |
| `tests/test_input_boundary_contract.py` | 35 | 2 |
| `tests/test_mcp_tab_concurrency.py` | 92 | 3 |
| `tests/test_pending_bridge_operations.py` | 105 | 4 |
| `tests/test_physical_input.py` | 42 | 0 |
| `tests/test_requester_liveness.py` | 25 | 0 |
| `tests/test_server_coverage.py` | 694 | 10 |
| `tests/test_silent_operation_expiry.py` | 28 | 0 |
| `tests/test_simphtml_coverage.py` | 166 | 0 |
| `tests/test_site_permissions.py` | 65 | 1 |
| `tests/test_tab_create_recovery_contract.py` | 51 | 3 |

`test_command_scope.py` 与导入候选逐字节一致，保留了非 loopback 命名空间、
POSIX 非阻塞 flock、存储错误关闭描述符这三个回归用例。

## 唯一适配

首轮 18 文件同跑为 1221 passed / 1 failed。失败位置是
`test_waiting_for_a_pending_probe_keeps_its_receipt_without_replaying[failed]`。
候选断言 `pending == {}` 已不符合原目录的等待修复：
`server.py` 的 `_poll_wait_condition` 在收集到 failed/lost/expired 结果时
保留原回执并增加 `operation_status`，不能把失败结果当作未满足条件而重放。
该行为也由 `.trellis/spec/backend/error-handling.md` 的等待回执合同约束。

适配后的断言精确要求六个字段：

```python
{
    "operation_id": "wait-operation",
    "delivery_state": "sent_unconfirmed",
    "reservation_held": True,
    "operation_status": "failed",
    "retry_safe": False,
    "poll_with": "get_execute_js_result",
}
```

原有错误消息、单次 dispatch、单次 poll 断言全部保留。
另外 17 个文件与导入补丁后的哈希一致；没有修改 fixtures、生产状态、
覆盖率排除或其他断言来消除失败。

## 应用与身份核对

- 解释器：`D:/venvs/agent-browser-mcp/Scripts/python.exe`。
- 实际导入：`D:/browsertap-mcp/src/browsertap_mcp/__init__.py`。
- HEAD：`db11d228880d6c254581746c1c0812b499664183`。
- 补丁：`out/coverage-handoff-20260912/coverage-candidate.patch`，123338 bytes。
- 补丁 SHA-256：`d55146ccc554e64f9f486a32a2d8498140d8a16b1d3a096bc71cd720a3d2a000`。
- 实际应用开始：`2026-09-11T16:58:43.129765+00:00`。
- 应用前 18 路径的 Git status 为空，原文件已备份并核对 SHA。
- `git apply --check` 与 `git apply` 均为 exit 0；未暂存文件。
- 应用前后完整源码集合只改变了这 18 个测试路径。
- 对方 11 个冻结文件的 SHA 与 mtime 在应用前后、最终测试前后均保持一致。

完整 source identity：

| 边界 | content_sha256 | 文件数 |
| --- | --- | ---: |
| 应用前 | `177372c382074bb7979c88adfa9902d1d113a20ead195c99668ebd3ac4d8286e` | 379 |
| 应用后 | `4056f3c84da10abbe3a8ed5702643479abcdd338d423a76cf8bd1e2734315aa4` | 379 |
| 最终测试前后 | `476f820af3988b5b7545d4887dac6c3aadc780b01418e3048d9473e4ab818d42` | 380 |

Trellis 研究记录也计入完整 identity，所以写入本报告后完整哈希还会变化。
源码和测试是否变化须同时比较逐文件记录，不能仅凭完整哈希判断。
本报告写入后的身份见 `integration/source-at-implementation-handoff.json`。

## 验证与产物

所有路径以下述目录为前缀：
`out/coverage-handoff-20260912/integration/`。

- `preflight.json`、`original-tests/tests/`：18 原文件身份、备份和 11 冻结文件身份。
- `patch-check.txt`、`patch-apply.txt`、`patch-application.json`：应用命令、时间和退出码。
- `source-before-apply.json`、`source-after-apply.json`、`apply-comparison.json`：应用边界快照。
- `affected-tests-initial.*`：初次失败的完整日志、JUnit 与命令记录。
- `adaptation-before.json`：唯一适配的改前 SHA/mtime 与原因。
- `affected-tests-final.json`：最终精确参数、UTC 时间、源码身份和 JUnit 汇总。
- `affected-tests-final.log`、`affected-tests-final.junit.xml`：1222 passed 的原始证据。
- `source-before-final-tests.json`、`source-after-final-tests.json`：最终测试边界快照。
- `checks-final.json`、`ruff-tests-final.log`、`diff-check-final.log`、
  `changed-tests-lf-final.json`：最终静态检查证据。
- `tests-final.json`、`frozen-final.json`：18 测试和 11 冻结文件的最终 SHA/mtime。
- `integrated-tests-final.patch`、`integrated-tests-final.numstat.txt`：可独立审查的最终差异。
- `verification.json`：整合结果、边界检查与证据哈希的结构化汇总。

最终测试命令由 `affected-tests-final.json` 的 `command` 完整记录：
指定这 18 文件运行 `python -m pytest ... -q`，JUnit、cache 和 basetemp
均位于自己的 integration 目录。默认 `not live` 标记保持生效。

最终测试运行时间为 `2026-09-11T17:06:41.964285+00:00` 至
`2026-09-11T17:07:43.672451+00:00`，pytest 报告 60.16 秒。
静态检查实际运行 `python -m ruff check tests --no-cache` 和
`git diff --check`，均 exit 0；LF 检查读取 18 文件的原始字节，CRLF 数全为 0。

## 待根代理完成

实现者已停止测试写入，交给根代理独立 Trellis check。没有未修复测试失败。
共同工作树的全量测试、覆盖率、lint 和打包安装检查由诊断 owner 在 ready 回执后执行。
本批次没有运行全量 coverage/build、live、桥重启、扩展 Reload、finalizer、
产品 commit、push 或发布，也没有采用候选 99.53% 作为原目录覆盖率。
