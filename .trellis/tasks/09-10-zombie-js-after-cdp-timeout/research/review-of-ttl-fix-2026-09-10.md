# Review of the bounded-TTL fix (2026-09-10)

Reviewer note written next to `from-source-review-2026-09-10.md` rather than
inside it, so the implementing session's own record is never overwritten.

Scope reviewed: `pending_operations.py`, `browser_bridge.py`,
`tests/test_silent_operation_expiry.py` as of the batch timestamped 12:11:26.

## Confirmed sound

- `reserve()` prunes before it raises `TargetBusyError` (`_prune()` at 229, the
  raise at 291), so the release has a reader on the path that reported the
  defect.
- `read()` reports `reservation_held: False` for `abandoned` while keeping
  `wire_result` and `abandoned_reason`, so the receipt survives the release.
- `caller_budget` is supplied from both the `execute_js` and the `ext_cmd`
  wait sites and both have tests, so the per-operation deadline is real rather
  than decorative.
- `waiters` and `blocked_by_dialog` guards are intact.
- Independent full offline run by the reviewing session: 2630 passed,
  61 deselected, exit 0.

## Open items

1. **The release rests on a single test whose name asserts the opposite of its
   body.** Replacing the `outcome_unknown` branch with `continue` fails exactly
   one test, `test_a_reply_already_in_hand_is_not_abandoned`, whose body asserts
   `status == "abandoned"`. A reader scanning the file concludes there is a test
   protecting `outcome_unknown` *retention*. Rename it and add a second
   assertion layer so the mutation kills more than one case.

2. **The justifying premise is false for one of three producers.**
   `outcome_unknown` is produced at `browser_bridge.py:2423`, `:2728`, and
   `pending_operations.py:441` (`observe_manual_execution`). "The extension has
   already answered the command with its final timeout frame" holds for the
   first two only; the dialog-observation path never received a timeout frame.

3. **On that third path the bounded grace window has zero length.** Measured:
   reserve with `caller_budget=10.0` and a pending execution, advance to 71s
   (past 10 + 60), then call `observe_manual_execution`. The status becomes
   `outcome_unknown` but `started_at` is not reset, so the operation is already
   past `silent_after` and the next `read()` returns `abandoned` /
   `unknown_outcome_reservation_ttl` / `reservation_held: False`. The
   disposition note's claim that a caller can inspect the retained receipt
   during a bounded grace period is therefore accurate for the `complete()`
   producers and inaccurate for this one.
   `test_unknown_outcome_is_still_readable_before_its_reservation_expires`
   covers only the `complete()` path with a fresh budget.
Suggested fix: reset `started_at` when the status transitions at `:441`, or
record an explicit `unknown_since` and measure the grace period from it.

## Disposition (2026-09-10)

The suggested explicit anchor is implemented as `_Operation.unknown_since` in
`src/browsertap_mcp/pending_operations.py`. Both `complete(...,
outcome_unknown=True)` and `observe_manual_execution()` set it only when the
operation first enters `outcome_unknown`; repeated observations do not extend
the window. `_abandon_silent_operations()` measures that status from
`unknown_since`, while ordinary `in_progress` operations still age from
`started_at`. This gives the manual-observation path the same bounded grace
window as a timeout reply even when the original caller budget has already
elapsed.

Regression coverage is in
`tests/test_silent_operation_expiry.py::test_manual_unknown_outcome_gets_a_fresh_grace_window`.
The focused TTL suite passed (`30 passed`), the zombie probe passed (`24
passed`), and the live manual-unknown-outcome concurrency case passed (`1
passed in 86.19s`).

The three open items above are therefore closed for the current implementation.
The unrelated xterm newline assertion remains a separate live-fixture issue;
it is not evidence against the TTL behavior.

## Unrelated live red, with the fork already decidable

`tests/test_live_browser.py::TestBackgroundPageInput::test_page_drag_events_reach_scratch_without_raising_it`
fails with `expected "printf 'btap-xterm'"` / `actual "printf 'btap-xterm'\n"`.
This is a consequence of the earlier keypress fix
(`page_input.py:255`, `_KEY_TEXT = {"Enter": "\r"}`), not of the TTL batch.

The fixture at `test_live_browser.py:741-743` is a bare
`<textarea class="xterm-helper-textarea">` with no xterm.js `preventDefault`, so
the retained newline is a property of the fixture rather than product
behaviour, and the same assertion block already checks that `keydown Enter`
reached the helper (`:827-828`). The stale value assertion at `:823` is what
should change; altering the input implementation would reintroduce the missing
`keypress` that the earlier fix removed.
