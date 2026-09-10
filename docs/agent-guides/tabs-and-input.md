# Tabs and input

Read before changing target selection, lifecycle generations, input dispatch,
focus emulation, hit testing, quiet-input checks, or debugger ownership.

Start with [AGENTS.md](../../AGENTS.md) for the task entry point. Numbered
sections retain the original guide's references; other section numbers refer
to that root guide. Commands and release gates remain in
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## 3. Tab ids are not stable -- never remember one

A tab id dies when the tab is closed, the browser restarts, or the extension is
reloaded. The worst bug this repository has had came from exactly that: the
driver pinned `default_session_id` to the first tab that registered, that tab was
closed, and from then on **every call that did not name a tab** failed with "not
connected, pick a target with switch_tab" -- so an agent had to run `list_tabs` →
`switch_tab` before every single action on a tab it had never chosen.

The current rule, which must survive any change here:

- Caller did **not** name a tab (`session_id=None`) → if the default is dead,
  **silently re-pick a live one** (`BrowserBridge._live_default_session_id`). The
  caller expressed no preference, so re-picking violates nothing.
- Caller **did** name a tab and it is dead → **still refuse**. Never substitute
  another tab. A "click checkout" landing on the wrong page is far worse than an
  error.

Both automatic picks (that one and `_pick_failover_session`) also skip tabs
Chrome will never let the extension script -- `is_scriptable_url`. The trap is
that the Web Store is an ordinary `https://` page, so a content script registers
there and the tab joins the pool like any other, while every injection comes back
`The extensions gallery cannot be scripted.` with nothing dispatched. Both
filters are preferences with a fallback: a browser showing only such pages
behaves exactly as it did before rather than starting to refuse, and a tab the
caller named explicitly still gets the real error.

The other half of "never remember a tab id" is that the *generation* paired with
one has to outlive the service worker, and that is not automatic. The snapshot
lives in `chrome.storage.session` and is written **whole**, because that write is
also what prunes generations for tabs that are gone -- so a worker that starts,
fails to read the store and then writes its own map deletes every generation it
does not happen to contain, which for a fresh worker is all of them. Neither read
is exotic: `No SW` from a `chrome.*` call during worker startup is routine
(section 8). Measured once on a real browser: an eviction mid-live-run re-minted
all 15 tabs inside a single millisecond, and `close_tabs` then refused two tabs
the suite owned with `lifecycle generation changed` until a human closed them.

`loadTabGenerations` therefore treats an unreadable store as a different fact
from an empty one. Three things must survive an edit there:

- **`tabGenerationsLoaded` gates every write.** A map that was never read is a
  local guess, and publishing a guess is the deletion described above.
- **A failed read is not memoised.** It used to be, so one transient `No SW` left
  the worker guessing until it was collected again.
- **The durable value wins.** A generation minted while the store was unreadable
  is provisional and loses to the stored one on the retry; one minted for a tab
  the store never knew is real, and is kept and published instead.

Do not simplify that back into a single `catch` that yields `{}`. Both halves
then read as "the store is empty", and the symptom shows up minutes later as a
refused close. `tests/test_phase0_recovery.py` runs the real function under node
with a faked `chrome.storage.session`, so the eviction behaviour is checked
offline.

In tests, never hardcode a real id. The sentinel is
`chrome_nonexistent:999999`.

## 4. Physical input acts on the *visible* tab

The seven OS-level tools that drove the real mouse and keyboard were removed in
0.5.0. One physical path is left: `resolve_leave_dialog`'s lab-only Enter
fallback, sent only after a protocol accept has actually failed. It lands on
whatever is visible on screen, which is a different thing from the "target tab"
that `switch_tab` selected.

The removed tools once made "raise the window first" **opt-in**, so a
`switch_tab` + click pair silently acted on the wrong tab: the coordinates were
valid, pyautogui reported success, and nothing anywhere reported a problem.
Raising the target is the **default** for every physical dispatch and opting out
is explicit (`activate_session="none"`). Think that failure mode through before
changing these defaults.

A Windows-specific trap on top of it: when the window is **minimised**,
`chrome.tabs.update({active: true})` succeeds and
`windows.update({focused: true})` reports success, yet the page is **not on
screen at all**. `_activate()` therefore tries to un-minimise first and then
reports `on_screen` honestly. Treat `on_screen: false` as "this input will miss",
never as success.

The CDP tools (`page_click`, `page_type`, `page_press`, `page_drag`) do not touch
the mouse and do not raise the tab, but they have the same failure shape one
layer down. Chrome discards `Input.*` events aimed at a tab that never received
focus, and `Emulation.setFocusEmulationEnabled` **ACKs before the renderer has
applied it** -- so an input dispatched right after it is dropped, every CDP
command still returns success, and nothing anywhere reports a problem. Measured
on a freshly opened tab: roughly one silent miss in eight; `document.hasFocus()`
sampled just before the dispatch predicted it exactly.

`_run_page_input` therefore sends `Emulation.setFocusEmulationEnabled`, then a
`Runtime.evaluate("document.hasFocus()")`, then the input -- all in **one**
batch. That middle command is not a debug leftover: it is the renderer round trip
the flag needs in order to be in effect by the time the input goes out, and its
answer is the proof. Two rules survive any change here:

- **Do not split it into two batches.** Each batch attaches and detaches its own
  debugger, and a detach immediately followed by a re-attach on the same tab
  wedges the service worker: measured as the whole batch timing out at 15 s
  instead of the ~0.16 s it takes now.
- **Do not "fix" a false reading by re-sending the input.** By then the events
  are already out; a repeat could double the click. Report that it may not have
  landed and let the caller check the page, exactly as the timeout path does.

The quiet-input gate in front of these tools has the same shape a third time. It
raises `input_activity_detected` only when a marker present in **both** samples changed,
and the markers are **Windows-only** -- last-input timestamp plus pointer position,
unavailable under Wayland, in a headless container, and on macOS without the
accessibility permission. When nothing can be compared the gate used to be a bare
`sleep()` that passed silently on exactly the machines nobody watches; it now returns a
report, attached to the result as `input_quiet`. Two things must survive an edit: **a
vacuous pass is reported, not refused** -- refusing would take physical input away from
machines where pyautogui works fine, which is why this mirrors `on_screen` rather than
`activation_failed`; and **`enforced` is computed from what was comparable in both
samples** -- hardcoding it True puts the silence back.

The same shape has a second half: a resolved element can be *found* and still not
be the thing at that pixel. A cookie banner, a modal backdrop or a sticky header
over the target left `Input.dispatchMouseEvent` reporting success while the click
went to the overlay, and a target below the fold got a click at negative
viewport coordinates -- both indistinguishable from a real click in the result.
`page_click` therefore hit-tests the point with `document.elementFromPoint`
(`_HIT_TEST_JS`, the browser's own answer, same as `simphtml` uses for z-index)
inside the **same** resolver batch, before any dispatch: below the fold it
`scrollIntoView`s once and re-probes, and a point owned by anything else refuses
with `obscured` + `occluded_by` or `outside_viewport` having dispatched nothing.
Three things must survive an edit here:

- **A hit is not identity.** `hit === el`, `el.contains(hit)` and
  `hit.contains(el)` are all hits (a label's text node, an icon inside a button),
  and the shadow-host chain is climbed because `elementFromPoint` stops at the
  host. Tighten that to equality and ordinary buttons start refusing.
- **Only selector mode.** Coordinates name a pixel, not an element, so there is
  nothing to compare them against; `verify_hit` stays False for `page_drag` and
  for the coordinate path.
- **Do not scroll inside a frame.** The frame offsets are measured before the
  probe, and scrolling can move an ancestor frame too, which invalidates them.
  Framed targets refuse instead, which is why the scroll is guarded by `!framed`.

Two wire-shape rules, each learned from a tool that reported success while doing
nothing (measured live, 2026-09-10):

- **Every batch command carries `"cmd": "cdp"`, and every command envelope goes
  through `ext_cmd`, never through `exec_js` as text.** `handleBatch` dispatches
  on `c.cmd`; the `page_type` guard was sent without it, recorded as
  `unknown cmd: undefined`, and the batch typed anyway -- `batch_guard_failed`
  could not fire. Since the cmd/code split the extension evaluates whatever
  arrives in `code` as page JavaScript, so `set_cookies`, `delete_cookies`,
  `cdp_batch` and `upload_files` sent as `json.dumps(payload)` died with
  `SyntaxError: Unexpected token ':'` -- the cookie tools silently through the
  `document.cookie` fallback. Route with `_cdp` / `_extension_batch`; the text
  route is only for a router that explicitly answers `unknown cmd`. Tests that
  fake only `exec_js` reach the *real* bridge once the route moves; fake
  `ext_cmd` and make `execute_js` raise.
- **A keyDown without `text` is a rawKeyDown in all but name.** Chrome fires
  `keypress` -- the character, a textarea newline, implicit form submission on
  Enter -- only when `text` is present. `press_commands` attaches it for
  presses without Ctrl/Alt/Meta (`"\r"` for Enter, the character otherwise) and
  deliberately not for modifier chords, so Ctrl+A selects instead of inserting
  an "a". Live assertions must read `keypress` / `submit`, not `keydown`: the
  suite was green on `keydown` while `submit_key="Enter"` submitted nothing.

## 8. Odds and ends

- If a manual `execute_js` debugging session left a CDP debugger attached, the
  whole live suite fails afterwards with "debugger already attached". Suspect
  your own leftovers before blaming a code change; re-verify in a clean browser.
