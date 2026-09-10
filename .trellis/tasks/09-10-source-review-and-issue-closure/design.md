# Design

## R1 -- text command envelopes must travel on the `cmd` field

Since 82aabc9 the extension routes strictly by field: `data.cmd` (object) goes to
`handleExtMessage`, `data.code` (string) is page JavaScript. `BrowserBridge.ext_cmd`
already sends `{"id", "cmd": {...}}`; `BrowserBridge.execute_js` sends
`{"id", "code": "..."}`. Four server call sites still hand a JSON *text* to
`exec_js`, which therefore reaches `eval()` and dies with `SyntaxError:
Unexpected token ':'`.

Change (server.py only):

- `_cdp(method, params, session_id, tab_id, timeout)` keeps its signature (a test
  monkeypatches it) and resolves `(session_id, client_id, tab_id)` with a new
  `_resolve_cdp_target(session_id, tab_id)`; it then delegates to `_direct_cdp`,
  which already sends `{"cmd": "cdp", ...}` through `ext_cmd` and keeps the
  `exec_js` text fallback only for an explicit `unknown cmd` answer from an old
  router. Return value stays the unwrapped CDP result (`_direct_cdp` unwraps the
  `{ok, data}` envelope exactly like the WS `resultPayload` did).
- New `_extension_batch(payload, *, session_id, timeout)` for `cdp_batch` and
  `upload_files`: resolves the target the same way, adds `tabId`, sends through
  `ext_cmd`, and falls back to `exec_js(json.dumps(payload))` only on
  `_unknown_command_error` or when the driver has no `ext_cmd` (fake drivers in
  older tests). The reply shape (`{"data": <results list | {ok:false,...}>}`) is
  unchanged for both callers.
- `_run_page_input.dispatch` is already correct; the fallback there stays.

Tests: the upload/cdp_batch offline tests install a fake driver with `ext_cmd`
and assert the payload arrived on `ext_cmd` with `cmd == "batch"`; the live
cookie tests assert `results[0]["method"] == "cdp"` so the document.cookie
degradation can no longer pass as success.

## R2 -- the page_type guard is a `cdp` command

`server.page_type` builds the guard as `{"method": "Runtime.evaluate", ...,
"assertTruthy": True}`; `handleBatch` dispatches on `c.cmd`, so the guard is
recorded as `unknown cmd: undefined` and skipped. Add `"cmd": "cdp"` and make
the offline assertion cover every command in the batch (`all(c["cmd"] == "cdp")`).

## R3 -- key presses carry `text`

`Input.dispatchKeyEvent` produces a `keypress` (and the character / implicit
form submission / textarea newline) only when `keyDown` carries `text`.
`press_commands` gains: for a chord whose modifiers are none or Shift only, the
final key's down event is `keyDown` with `text` = the printable character, or
`"\r"` for Enter; any other modifier keeps `rawKeyDown` without text (Ctrl+A
must not insert "a"). `keyUp` is unchanged. `type_commands` inherits this
through `press_commands`, so `submit_key="Enter"` submits.

## R4 -- idle-anchored challenge window

`ChallengeAttemptTracker.record` refreshes `started_at` on every recorded
attempt so the window measures idleness, matching the server-side counter.

## R5 / R6 -- small contract fixes

- `cdp_batch`: reject a non-object JSON with `ValueError` (invalid_request).
- Replace the two "via Hermes" strings with client-neutral wording.

## Out of scope (reported, not changed)

- `browser_bridge.py` / `pending_operations.py` findings from the fork review
  (research/browser_bridge-review.md); the permanent reservation after a
  non-`killed` `cdp_timeout` belongs to the zombie task.
- `background.js`: not edited; the guard fix is server-side.
