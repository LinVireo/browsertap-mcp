# browser_bridge.py + daemon-side modules — read-only correctness review

Scope: `src/browsertap_mcp/browser_bridge.py`, `pending_operations.py`,
`capture_ownership.py`, `requester_liveness.py`, `command_scope.py`
(working tree 2026-09-10, ~02:40). No edits made.

Routes exposed by the daemon: `/api/longpoll`, `/api/result`, `/link`
(`browser_bridge.py:1233,1304,1324`). All three call
`check_link_token_drained` first; none lacks the token check. The
`before_request` hook (`:1211`) rejects web Origins on every route. The WS port
is Origin-prefix gated only (`:1492`), by design.

Findings, most severe first.

## 1. HIGH — a dispatched `cdp_timeout` whose zombie verdict is not `killed` reserves the tab forever

- `browser_bridge.py:492-520` (`_execution_may_continue`), `:1001`
  (`_complete_operation` → `complete(outcome_unknown=True)`),
  `pending_operations.py:393-395`, `:171-178` (`_abandon_silent_operations`
  skips anything not `in_progress`), `:256-286` (`reserve` raises
  `target_busy` for an incumbent unless the new op is a cleanup kind, and
  cleanup kinds only *borrow* via `_recoveries`, never clear `_targets`).
- Defect: an `outcome_unknown` operation holds `_targets[tab]` until
  `finish_target` (tab removed / generation changed). Nothing else releases
  it: the silence sweep exempts it, `handle_dialog`/`clear_dialog_policy`/close
  borrow around it, and `server.py:5213` skips the `clear_dialog_policy`
  cleanup precisely when `reservation_held` is true. The reservation is meant
  to hold a slot for a late reply, but no late reply can arrive: the extension
  answers each `id` once (`background.js:2546-2554` returns the timeout as the
  single `{ok:false}` reply) and its `reply` closure is bound to the receiving
  socket (`background.js:5158-5160`).
- Failure scenario: `execute_js(script=<slow fetch>, timeout=20)` → extension
  returns `cdp_timeout`, `dispatched:true`, `zombie:"not_blocking"` (or
  `unknown`, or no verdict from an older extension) → every later
  `execute_js` / `scan_page` / `page_click` on that tab, from any MCP session,
  gets `target_busy` with `operation_in_progress:true` until the tab is closed.
  Each such op also counts as active (`pending_operations.py:224-252`), so a
  long-lived daemon reaches `operation_capacity_exceeded` (256 per requester /
  1024 global) with no way for the caller to "collect" them, as the message
  advises. The in-progress zombie task fixed exactly this for `killed`
  (`test_zombie_probe.py:265`); `not_blocking` is documented there as
  "may still be awaiting async", but the awaited value has no channel back.
- Verdict: CONFIRMED (traced daemon + extension reply path). Overlaps task
  `09-10-zombie-js-after-cdp-timeout`; coordinate before touching.

## 2. MEDIUM — `_capture_commands` entries for uncertain capture ops are never pruned

- `browser_bridge.py:992-995`: on an uncertain result the entry is kept; the
  only other removal is `_finish_tab_operations` (`:1090-1092`) on tab
  lifecycle end. `PendingOperations._prune` does not know about this dict.
- Failure scenario: `network_capture start` on a tab times out at the CDP
  layer with a dispatched `cdp_timeout` → `_capture_commands[op]` stays for
  the daemon's lifetime while the tab lives; repeat on many tabs/hours → growth
  proportional to timed-out capture mutations. Small objects, but unbounded.
- Verdict: CONFIRMED (no other pop site).

## 3. MEDIUM — the silent-grace rationale names a reply path that does not exist

- `pending_operations.py:34-50` says the grace window "has to cover an MV3
  worker eviction plus its restart, reconnect and result delivery". After a
  reconnect the reply arrives on a new `JSExecutor` instance, and
  `accepts_reply` (`pending_operations.py:337-338`) requires `expected is
  owner` for WS transports, so such a reply is rejected
  (`browser_bridge.py:1039-1048`, `rejected_operation_replies++`). The
  extension side never attempts it anyway (`background.js:5159`,
  `:5306`: reply only if `sock === ws && OPEN`).
- Consequence: the 60 s grace holds a tab for a reply that is structurally
  impossible after a socket change; the hold is pure latency for the next
  caller. Not a crash; a design/doc inconsistency that will mislead the next
  change to the sweep.
- Verdict: CONFIRMED for the code paths; whether the grace should shrink is a
  design call.

## 4. LOW — the local `execute_js` waiter ignores `lifecycle_ended` until its deadline

- `browser_bridge.py:2296-2384`: the loop only checks `self.results`,
  `self.acks` and `session.is_active()` flips. When `_apply_extension_tabs`
  removes the tab or bumps its generation it calls `finish_target`
  (`:1729`, `:1778`), which sets `lifecycle_ended` and notifies activity — but
  the waiter has no branch for it, so it sleeps to `remaining() <= 0` and
  then reports `"Session … reloaded and new page is loading..."` (`:2345`)
  for a tab that was *removed*.
- Failure scenario: `execute_js(timeout=60)` on a tab the user closes at
  t=1 s → caller blocks ~59 s, then gets `navigated/closed` with a "reloaded"
  message. `get_execute_js_result` would have said `navigated` immediately.
- Verdict: CONFIRMED.

## 5. LOW — `ext_cmd` send failure drops the client socket but leaves its tab sessions live

- `browser_bridge.py:2642-2649`: `self.ext_clients.pop(client_id)` only.
  Sessions of type `ext_ws` whose `client` is that same dead socket stay
  `is_active()` until `handle_close` fires (`:1655`), which for a half-open
  socket may be much later.
- Failure scenario: laptop sleep → first `list_extensions` fails with
  `extension_not_connected`; the next `execute_js` still resolves the default
  tab to the dead socket, pays a second failure at `:2277` before marking it.
  One wasted call, not a wrong result.
- Verdict: CONFIRMED.

## 6. LOW — lock-file directories grow without bound

- `requester_liveness.py:64-69`: one new random id + `requester-locks/<sha>.lock`
  per MCP process start, never deleted (the dead-probe at `:104-131` leaves
  the file). `command_scope.py:53`: one `command-locks/<sha>.lock` per
  distinct `namespace/target`; tab ids churn, so the set grows for the life of
  the install.
- Failure scenario: months of editor restarts → thousands of 1-byte files in
  `~/.browsertap/`. Cosmetic on NTFS; no functional impact found.
- Verdict: PLAUSIBLE (growth traced; no deletion site found).

## 7. LOW — `connect_at` / `connected_at` are reset by routine traffic

- `browser_bridge.py:871-876`: `Session.reconnect` stamps `connect_at` on
  every HTTP long-poll (`:1268`), so an HTTP session never satisfies
  `FAILOVER_SETTLE_SECONDS` (`:2024-2027`) and always falls to the
  `settled or usable` fallback. `:1732-1735` rewrites `info['connected_at']`
  on every tabs snapshot for ext_ws sessions, so that field means "last
  snapshot", not "connected".
- Failure scenario: mixed WS + HTTP pool during failover picks by the
  fallback order rather than settledness. Rare configuration.
- Verdict: CONFIRMED for the resets; PLAUSIBLE for user-visible effect.

## 8. LOW — check-then-pop on `self.acks` in the success path

- `browser_bridge.py:2394`: `if exec_id in self.acks: self.acks.pop(exec_id)`
  while `clean_sessions` (`:1543-1546`) and `_sync_pending_operations`
  (`:1015-1018`) pop the same dict from other threads. The 600 s / retained-id
  guards make the window practically unreachable, but a hit would raise
  `KeyError` *after* the script succeeded and turn a completed result into an
  internal error for the caller (the result stays retained).
- Verdict: PLAUSIBLE. Everywhere else uses `.pop(key, None)`.

## 9. INFO — `requesterId` is client-asserted

- `browser_bridge.py:1392-1395`, `:1432`: the daemon adopts whatever
  `requesterId` a `/link` caller sends, so any process holding the bridge
  token can read or consume another MCP session's retained results
  (`operation_owner_mismatch` is advisory, not authenticated). Same trust
  domain as the token itself (a token holder can already run arbitrary JS),
  so this is a boundary note, not a bypass.

## Not defects (checked)

- Lock ordering: `_DRIVER_STATE_LOCK` → `PendingOperations._lock` →
  `CaptureOwnershipRegistry._lock` / `_IDENTITY_LOCK`; no path takes them in
  the reverse order. Both RLocks re-enter safely from `_complete_operation`
  under `_record_operation_reply`.
- Duplicate completion from reply thread + waiter is idempotent
  (`pending_operations.py:387-390`, `capture_ownership.py:117`).
- `drain_request_body` / `json_object_body` cover the 401, 403 and malformed
  body paths on all three routes.
- `_claim_ext_client` takeover + `_unregister_client` leave no stale socket
  reference: `_apply_extension_tabs` rebinds `sess.client` on every snapshot.
- Windows `msvcrt.locking` is issued at offset 0 in both lock helpers
  (`command_scope.py:53-62` position is 0 after open/fstat/ftruncate;
  `requester_liveness.py:35` seeks explicitly).

## Follow-up disposition (2026-09-10)

The findings above were recorded before the daemon-side lifecycle patch. The
original traces remain useful as the audit record; their current disposition is:

- **Finding 1 (HIGH): fixed with a bounded release.**
  `PendingOperations._abandon_silent_operations` now expires both a silent
  `in_progress` operation and an already-replied `outcome_unknown` operation
  after its per-operation `silent_after` deadline. The latter keeps its
  `wire_result` and timeout diagnosis, marks the record `abandoned`, and
  releases the tab reservation with `abandoned_reason:
  unknown_outcome_reservation_ttl`. `blocked_by_dialog` remains reserved for
  its explicit human recovery path. `get_execute_js_result` reports the
  released unknown outcome as `status: unknown`, `retry_safe: false`, with the
  retained diagnosis, so callers are not invited to replay an uncertain script.
- **Finding 2 (MEDIUM): fixed.** `_complete_operation` finishes capture
  ownership and removes the `_capture_commands` entry for every terminal
  extension result, including uncertain CDP timeouts. Lifecycle cleanup still
  removes entries for a tab that ends.
- **Finding 3 (MEDIUM): remains open as a design/documentation question.** The
  grace window is now bounded per caller budget, but the comment's reconnect
  delivery rationale still needs an explicit product decision before it is
  shortened. No behavior change was made for this item.
- **Finding 4 (LOW): fixed.** The local `execute_js` waiter observes a
  `lifecycle_ended` operation while waiting and returns the navigated/closed
  result immediately. The success-path ACK cleanup also uses a race-safe
  `pop(..., None)`.
- **Finding 8 (LOW): fixed.** The remaining success-path ACK removal is now
  race-safe for concurrent cleanup.
- **Findings 5, 6, 7, and 9:** unchanged and still open at the severity and
  confidence stated above.

Validation after the patch: the focused lifecycle set passed 110 tests; the
broader operation/bridge set passed 213 tests with one live test deselected;
the offline suite passed 2630 tests with 61 live tests deselected; and Python,
JavaScript, and mypy lint reported zero violations.
