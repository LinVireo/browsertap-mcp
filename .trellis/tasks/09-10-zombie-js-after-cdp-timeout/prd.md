# PRD: Settle zombie page JS after cdp_timeout (BUGREPORT-2026-09-08 ISSUE-1)

## Problem

`execute_js` falls back to `Runtime.evaluate` via CDP. When that evaluate hits
`cdp_timeout`, the extension force-detaches the debugger and reports
`outcome_unknown` / `retry_safe:false`. Detaching does not stop the page script:
a synchronous loop keeps the main thread pinned, a pending `fetch` keeps running.
The caller learns nothing about which of those it is, so the next call on that
tab may collide with the zombie or time out again for the same reason.

## Design decision (operator 2026-09-10: "先把bug修复了吧")

`Runtime.terminateExecution` terminates the running script **or, if none is
running, the next one** (page's own handler included). It is therefore only
safe when a script is *provably* running. Proof is a sentinel evaluate:

1. Fresh attach (short timeout). Fails -> `unknown`.
2. Sentinel `Runtime.evaluate("1")`, `awaitPromise:false`, short timeout.
   Returns -> main thread free -> `not_blocking` (script finished or is idle
   awaiting async; nothing to kill safely). Times out -> thread pinned -> 3.
3. Re-attach, `Runtime.terminateExecution`, sentinel again.
   Returns -> `killed`. Times out -> `still_running`.
3b. Before terminating: if `currentProtocolDialog(tabId)` reports an open
   native dialog, verdict `blocked_by_dialog`, nothing terminated -- alert()
   blocks inside whichever script called it, possibly the page's own.
4. Detach. Whole probe bounded (`ZOMBIE_PROBE_BUDGET_MS`); over budget ->
   `unknown`.

`terminateExecution` is never issued on the `not_blocking` path -- that is the
whole point of the sentinel.

## Contract

Extension error payload on `cdp_timeout` gains `zombie` in
`{killed, not_blocking, still_running, blocked_by_dialog, unknown}` plus `zombie_detail` (string,
human diagnostic, no script body). `browser_bridge._classify_page_error`
forwards both into diagnostics; `server._result_diagnostics` whitelists them so
the MCP envelope carries them. `retry_safe` stays `false` in every case: even
`killed` does not undo side effects the script produced before the kill.

## Acceptance

- Node harness slice tests for the probe: each of the four verdicts reachable,
  `terminateExecution` sent only after a sentinel timeout, never on the
  `not_blocking` path (negative assertion), probe budget respected, no probe on
  non-timeout failures.
- Python: `zombie` / `zombie_detail` pass through bridge classification and the
  server whitelist; absent on success.
- Mutation check: removing the sentinel gate must fail the negative assertion.
- Offline suite green, ruff green, extension stamp regenerated.
- Live verification (operator, after extension reload): the BUGREPORT recipe --
  hang `execute_js` (sync loop) -> `cdp_timeout` with `zombie:"killed"` ->
  immediate `execute_js("41+1")` returns 42 in normal time.

## Status 2026-09-10 (fable-5.1)

- Implemented: `settleZombieAfterTimeout` (background.js ~2560-2670), passthrough in
  browser_bridge.py (~246) and server.py whitelist (~420).
- Tests: tests/test_zombie_probe.py 16 passed; sentinel-gate mutation kills 6.
  test_dialog_policy's file-wide `terminateExecution` ban scoped to the manual slice.
- Offline: 2595 passed / 55 deselected; ruff + eslint clean; stamp 17959947f0a7c422.
- Codex (gpt-6-astra) second-opinion review: BLOCKED twice on relay 429
  (api.zzzcoding.org). Not re-attempted. Review questions A-F answered by
  fable-5.1 from source; A produced the `blocked_by_dialog` gate.
- Pending: extension Reload + live recipe (operator); codex review when relay recovers.

## Status 2026-09-10 (live, fable-5.1) — PASS

- Recipe passed in real Chrome: sync loop -> cdp_timeout with zombie=killed,
  reservation_held=false -> execute_js("41+1") = 42 immediately. not_blocking
  branch also verified (async wait; nothing terminated; reservation kept).
- Three live-only findings folded in: probe on the live attachment via a
  beforeInvalidate hook (fresh attach cannot reach a pinned renderer);
  timeoutSettled await (native reply during the probe stripped the verdict);
  bridge releases the tab on killed (_execution_may_continue).
- Offline 2605 passed; stamp 3e2a82fe69ed629d; 4 extension reloads, 2 bridge restarts.
- Codex review still owed when the relay stops 429ing.
