# Handoff from 09-10-source-review-and-issue-closure (2026-09-10)

Read-only finding that belongs to this task's area; not changed by the review task.

**Non-`killed` `cdp_timeout` reserves the tab for good.** `browser_bridge.py`
marks the operation `outcome_unknown`; `pending_operations.py` exempts
`outcome_unknown` from the silence sweep, every later caller on that tab gets
`target_busy`, cleanup kinds only borrow the reservation, and
`server.execute_js` skips `clear_dialog_policy` while `reservation_held`. No
late reply can ever land: the extension answers each id once and only on the
socket the request arrived on. Scenario: slow-`fetch` `execute_js` ->
`zombie: not_blocking` / `unknown` -> the tab is unusable by every agent until
it is closed, and it holds one of the 1024 / 256 capacity slots permanently.

Full write-up with line references:
`.trellis/tasks/09-10-source-review-and-issue-closure/research/browser_bridge-review.md`
(finding 1; findings 2-8 are lower severity and in the same file).

## Disposition update (2026-09-10)

The daemon-side follow-up is now implemented in the shared pending-operation
registry. A non-`killed` `cdp_timeout` still remains `outcome_unknown` during a
bounded per-operation grace period so a caller can inspect its retained timeout
receipt, but it no longer reserves the tab forever. When that deadline expires,
the record becomes `abandoned`, its `wire_result` and `js_return_lost` diagnosis
remain readable, and the target reservation is released. The result reader
returns `status: unknown` with `retry_safe: false`, preserving the rule that an
uncertain script must not be replayed automatically.

The same patch removes uncertain capture bookkeeping, wakes a local waiter when
the tab lifecycle ends, and makes ACK cleanup race-safe. `blocked_by_dialog`
continues to hold its target because it has an explicit manual recovery path.

Evidence: `tests/test_zombie_probe.py`,
`tests/test_pending_bridge_operations.py`,
`tests/test_silent_operation_expiry.py`, and
`tests/test_pending_execution_results.py` passed (110 focused tests); the full
offline suite passed 2630 tests with 61 live tests deselected. The original
finding is therefore closed for the permanent-reservation defect; the bounded
grace-window rationale remains a separate design question in the source-review
record.
