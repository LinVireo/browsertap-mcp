# Full-source review, md-recorded issue closure, three-perspective assessment

## Goal

Read every source module from scratch, verify each issue recorded in
BUGREPORT.md / BUGREPORT-2026-09-08.md / CODE_REVIEW_REPORT.md /
JSON_PARSE_TIGHTEN.md against the working tree, fix what is still open
(excluding ISSUE-1, owned by 09-10-zombie-js-after-cdp-timeout), and deliver a
three-perspective assessment (professional developer / enterprise / agent as
user).

## Status of the md-recorded issues (verified 2026-09-10 against the dirty tree)

| Source | Item | Verdict |
|---|---|---|
| CODE_REVIEW_REPORT §3.1-1 | screenshot atomic write | fixed (`server._atomic_write_bytes`) |
| CODE_REVIEW_REPORT §3.1-2 | 50 MB size cap | fixed |
| CODE_REVIEW_REPORT §3.2-3 | 7 OS-level tools | removed (49 tools registered) |
| CODE_REVIEW_REPORT §3.2-4 | ABM legacy in `paths.py` | intentionally kept (other installs); out of scope |
| JSON_PARSE_TIGHTEN | `JSON.parse(code)` hijack | fixed (cmd/code split, 82aabc9) -- but see R1: the fix broke four callers |
| BUGREPORT-09-08 ISSUE-1 | zombie JS after cdp_timeout | in progress in the other task |
| BUGREPORT-09-08 ISSUE-2 | `get_execute_js_result` in skill docs | present (SKILL.md 121-134) |
| BUGREPORT-09-08 ISSUE-3 | async IIFE regression gate | present (`tests/test_exec_return_shapes.py`) |
| BUGREPORT.md BUG-1..5 | all live fixed 2026-08-12 | not re-verified here |

## Requirements (new defects found by the from-scratch read, measured live)

- R1 (P0, CONFIRMED live): the extension's WS router no longer coerces a text
  JSON envelope in `code`, but `server._cdp`, `cdp_batch`, `upload_files`, and
  the page-input `exec_js` fallback still send `{"cmd": ...}` as *code*. Live
  result: `set_cookies`/`delete_cookies` silently degrade to `document.cookie`
  (HttpOnly dropped, `status: ok`), `cdp_batch` and `upload_files` fail with
  `SyntaxError: Unexpected token ':'`. Route these through `ext_cmd`.
- R2 (P0, CONFIRMED live): `page_type`'s batch guard command has no
  `"cmd": "cdp"`, so `handleBatch` answers `unknown cmd: undefined` and keeps
  going; the `batch_guard_failed` protection never fires.
- R3 (P1, CONFIRMED live): `press_commands` emits `keyDown` without `text`, so
  Chrome fires no `keypress`: `submit_key="Enter"` does not submit a form,
  Enter inserts no newline in a textarea, `page_press("a")` inserts nothing.
- R4 (P2): `ChallengeAttemptTracker` expires on age since first attempt while
  the server-side counter expires on idleness; the two can disagree
  (`attempts=3`, `stalled=False`). Anchor the tracker on the last attempt.
- R5 (P3): `cdp_batch` with non-object JSON raises `AttributeError`
  (`internal_error`) instead of `invalid_request`.
- R6 (P3, agent UX): two generic error strings say "keep this MCP server
  running via Hermes" regardless of the client.
- R7 (report only): fork review findings on `browser_bridge.py`
  (`research/browser_bridge-review.md`), notably the permanent tab reservation
  after a non-`killed` `cdp_timeout` -- overlaps the zombie task, so it is
  reported to that task rather than changed here.

## Constraints

- Another session edited `background.js` / `browser_bridge.py` /
  `tests/test_zombie_probe.py` until 03:20; do not touch those files unless the
  mtime check right before the edit shows no newer write.
- Server-side Python changes need a fresh MCP process to be observed; verify
  through pytest (offline + a live subset) rather than through this session's
  MCP tools.
- Keep the wire contract of `ext_cmd` (`{"cmd": ...}` objects on the `cmd`
  field) and the `_JS_WIRE_MARKER` invariant for caller scripts.

## Acceptance Criteria

- [ ] Offline suite passes (baseline 2595 passed).
- [ ] Live: `set_cookies` result carries `method: "cdp"` (not `document.cookie`);
      `cdp_batch` of `Runtime.evaluate 1+1` returns 2; `upload_files` sets a
      file on a real `<input type=file>`.
- [ ] Live: `page_type(submit_key="Enter")` on a single-input form triggers
      `submit`; `page_press("Enter")` in a textarea inserts a newline.
- [ ] Offline: a batch whose guard lacks `cmd` is rejected by the test; the
      live guard path yields `batch_guard_failed` when the guard is false.
- [ ] Assessment document delivered with file:line evidence and the three
      perspectives scored separately.
