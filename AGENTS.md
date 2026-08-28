# AGENTS.md

Notes for an AI agent working in this repository. Only things the code does not
show on its own -- the traps that cost someone an afternoon. General coding
style, the gate commands and the release flow live in
[CONTRIBUTING.md](CONTRIBUTING.md) ([简体中文](CONTRIBUTING.zh-CN.md)).

**What each tool does is not in this file.** The full parameter table for all 55
tools is the `## Tools` section of [README.md](README.md)
([简体中文](README.zh-CN.md)). That table is the single authoritative list; do
not copy it here, it will go stale.

## 1. Three processes, three different ways a change takes effect

This is the first thing to check when an edit appears to do nothing.

| Process | What it is | When your edit takes effect |
|---|---|---|
| MCP server | one per client session, short-lived | **immediately** on an editable install |
| bridge daemon | `pythonw -m browsertap_mcp.bridge`, long-lived, outlives every server | **only after a restart** |
| Chrome extension | unpacked MV3 extension loaded from `src/browsertap_mcp/chrome_extension` | **only after a manual reload** |

The bridge runs **its own** `BrowserBridge` and keeps **its own**
`default_session_id`. A request with `sessionId: None` is therefore routed by the
**bridge**, not by the server. After changing any session-resolution logic in
`browser_bridge.py`, restart the bridge before testing, or you will conclude the
change had no effect:

```bash
browsertap bridge --restart   # Chrome does not need restarting
```

The extension reconnects by itself within a few seconds; leave the browser
alone.

**Reloading the extension cannot be automated.** Do not spend time on it:

- `chrome.runtime.reload()` restarts the service worker but does **not** re-read
  `background.js` from disk. You reconnect to the same old build.
- `chrome.management.setEnabled(self, false)` does force a re-read, but the only
  thing that could re-enable the extension is the extension itself, so the
  bridge goes down permanently and needs manual recovery. The extension now
  **refuses** it (`self_disable_unsupported`), because a rule that lives only
  in this file is one an agent reaching for `set_extension_enabled` never
  reads -- and that tool is the exposed path to exactly this call. Do not
  remove the guard to "force a reload"; there is no automated reload.

Ask the human to press Reload once on `chrome://extensions`. To find out which
component is stale, run `browsertap doctor` and read `action`:
`reload_extension`, `restart_bridge` and `restart_mcp_session` each name the one
thing that will actually fix it -- the other two will not.

**Do not answer "is the extension stale?" from a version number.**
`chrome.runtime.getManifest().version` is parsed by Chrome at load time and
refreshed on its own schedule; `background.js` does not follow it. Measured here
in both directions: once the running worker reported 0.4.14 after a bump to
0.4.15 while every advertised capability was present and a Reload was genuinely
needed, and once the versions agreed and a Reload was *still* needed. So equality
does not prove fresh and inequality does not prove stale, and neither does the
capability list.

What does is `extension_build_stamp`: a hash of the extension sources compiled
into `background.js` as a literal, so the value the worker reports came out of
the JavaScript it is actually running -- there is no layer in between. Compare it
to a fresh hash of the directory and the answer is decisive. `get_setup_status`
and `browsertap doctor` do that and publish `extension_build_verdict`:

| verdict | what it means | whose move |
|---|---|---|
| `matches_tree` | the worker is running this tree | nobody's; stop asking for a Reload |
| `stale_worker` | it is not | the human's -- press Reload |
| `stamp_not_regenerated` | an extension file was edited and the stamp was not | yours -- see below |
| `unverifiable` | the extension predates the stamp, or the directory is unreadable | nobody's; it is unknown, not a pass |

`stamp_not_regenerated` is the one that catches *you*, and it is why the verdict
cannot simply compare and report: a stale stamp makes a **fresh** worker report
the old literal too, so the comparison is meaningless in both directions rather
than merely wrong. Editing anything under `chrome_extension/` therefore ends with

```bash
python -m scripts.extension_stamp --write
```

and `tests/test_extension_build.py` fails the offline suite if you forget --
which is the whole point, because the alternative is a stamp that confidently
describes a build nobody has. `extension_build_enforced` is the usual
`on_screen` / `input_quiet.enforced` shape: false means no comparison happened,
so a caller must treat the answer as unknown and not as a pass.

The first row of that table is a rule the code has to follow, not advice for the
reader, and for three releases it did not. `reload_extension_required` OR-ed the
version comparison in beside the verdict, so `matches_tree` could only ever add a
reload demand and never withdraw one -- the strongest signal wired as a judge that
can convict but not acquit. Because `versioning bump` rewrites `manifest.json` and
Chrome parses it only at load time, **every release bump left the extension one
version behind for the life of the install**, and `tests/live_preflight.py` reads
that flag rather than the verdict and has no override. So a human click stood
between each bump and any live evidence at all, and the click changed exactly one
thing: the number the gate was complaining about. Measured at 0.4.18 -- bridge
restarted to the new version, stamps equal, protocol equal, every capability
present, `doctor` still exiting 1 with `action: reload_extension`.

The version number is now the fallback it always was: it decides whenever the
stamp cannot (`unverifiable`, `stamp_not_regenerated`), because a weak signal beats
none, and it yields when the stamp says `matches_tree`. Two things must survive an
edit there. **Only the version number yields.** A worker whose JavaScript hashes to
this tree while speaking a different protocol, or while missing a capability this
tree requires, is a contradiction rather than a release-number gap, and folding
those into the same yield would turn the strongest signal into a blanket excuse --
`test_a_matching_stamp_does_not_excuse_a_protocol_or_capability_gap` fails if you
do. And **the gap is still published**, with a note saying why no reload is needed:
`healthy` sitting next to two different version numbers is otherwise a dead end,
because nothing else in the payload says which one this process believed, and the
true answer -- neither, it compared the code -- is not derivable from the rest.

There is a **fourth** kind of code the table above does not cover, and it is the
one exception to "an edit needs a reload or a restart". The two files in
`src/browsertap_mcp/page_scripts/` -- `page_outline.js` and `list_groups.js` --
are **not** part of the extension. `simphtml.py` reads them off disk at import
(`_load_page_script`) and `get_html` / `get_main_block` inject them through
`execute_js` on **every call**, so an edit is live the moment the MCP server
restarts (immediate on an editable install) with **no reload and no bridge
restart**. That is the whole reason this layer was carved out of the extension:
it is what decides *what the model sees of a page* (visibility analysis, the main
content block), and iterating on it used to mean editing a `r'''...'''` literal
inside `simphtml.py`, which no JavaScript tool could read. Do not move a page
script into `chrome_extension/` to "keep the JS together"; you would trade a
zero-friction edit for a manual Reload per iteration and hand it `chrome.*` APIs
it must never call (a page script runs in the page, not the worker -- eslint's
second config block gives it `globals.browser` only, so a stray `chrome.` fails
`no-undef`).

The one rule that must survive an edit to either file: the file ends with a bare
reference to its entry point (`pageOutline;` / `listGroups;`), **never a call**.
The call site appends `return pageOutline(...);` to the fragment, so a trailing
`pageOutline()` would run the entire page analysis **twice per request** and
discard the first result -- a full-page clone plus visibility pass on every
`scan_page`, `get_html` and page screenshot. That defect shipped for months
because the line was the 358th line of a Python string literal; both files are
pinned now by `tests/test_page_scripts.py`, which counts `document.body` reads
under node (`..._runs_the_page_analysis_exactly_once` and
`..._cutlist_payload_also_runs_its_analysis_exactly_once` -- one test could only
ever vouch for one file). The bare identifier is also what keeps eslint's
`no-unused-vars` satisfied without an inline disable.

## 2. Changing a tool signature or a default touches four places

Miss one and some agent keeps calling the tool from outdated instructions:

1. the `## Tools` table in **both** `README.md` and `README.zh-CN.md`
2. the tool's own `description=` in `server.py` -- this is the text a calling
   agent actually receives
3. `src/browsertap_mcp/skills/browsertap-default/SKILL.md` -- the **caller's**
   rules of engagement (which tool to call first, when `session_id` is
   mandatory). This one decides whether other agents misuse the server.
4. `src/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md` -- what to do when
   the transport itself is down.

The two skills **cross-reference each other** (`[[...]]`); updating only one
points the reader at advice that no longer holds. Phrases that must survive an
edit are listed in `REQUIRED_SKILL_TEXT` in `scripts/check_tool_docs.py`. Never
hardcode an extension version in a skill: the extension and the package share
one version now, so the reader must compare
`get_setup_status.package_version` at runtime.

The gate for all of the above:

```bash
python -m scripts.check_tool_docs
```

The skills ship inside the wheel as package data.
`browsertap skill-path` prints the directory that holds them as
`<name>/SKILL.md`. Point your skill manager **at that directory** instead of
copying the files: a copy reads as correct for as long as the contents happen to
agree and then silently stops receiving updates. If you keep a copy anyway,
`python -m scripts.check_tool_docs --check-installed-skills --skill-mirror DIR`
compares the hashes and tells you which skill drifted. Asking for that check
without naming a directory fails on purpose rather than passing vacuously.

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

## 4. Screen-coordinate tools act on the *visible* tab

`mouse_click`, `type_text` and `capture_desktop_screenshot` drive the real mouse
and keyboard. They land on whatever is actually visible on screen, which is a
different thing from the "target tab" that `switch_tab` selected.

These tools once made "raise the window first" **opt-in**, so `switch_tab` +
`mouse_click` silently clicked the wrong tab: the coordinates were valid,
pyautogui reported success, and nothing anywhere reported a problem. Raising the
target is now the **default** and opting out is explicit
(`activate_session="none"`). Think that failure mode through before changing
these defaults.

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
raises `input_activity_detected` only when a marker present in **both** samples
changed, and the markers are a **Windows-only** last-input timestamp plus the
pointer position -- unavailable under Wayland, in a headless container, and on
macOS without the accessibility permission. All three missing means nothing to
compare, so the gate was a bare `sleep()` that passed unconditionally and said
nothing, on the machines least likely to be watched. It now returns a report and
`run_physical_action` attaches it to the result as `input_quiet`. Two things must
survive an edit: **a vacuous pass is reported, not refused** -- refusing would
take physical input away from machines where pyautogui works fine, which is why
this mirrors `on_screen` rather than `activation_failed`; and **`enforced` is
computed from what was comparable in both samples**, so hardcoding it True puts
the silence back. Never read a pass as "the desktop was idle" without checking
that field.

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

## 5. MV3 `chrome.alarms` has a 60-second floor

Passing a smaller value raises **no error**; Chrome silently rounds it up. That
has bitten this repository twice:

1. **keepalive** -- old code used `delayInMinutes: 0.4` with a comment claiming
   "~24s, under the 30s service-worker timeout". It never ran at 24s, so it
   could not keep the worker alive at all, only wake it *after* it was already
   collected. It is now two layers: `setInterval(20s)` keeps a live worker alive
   and an alarm probe (`scheduleProbe`) recovers after collection. **Both are
   required.**
2. **self-reload** -- `chrome.alarms.create(..., {when: Date.now() + 200})` was
   rounded up the same way and did not fire 200 ms later.

Anything with a sub-minute period must use `setInterval`. Alarms are only good
for surviving worker collection.

## 6. The HTTP port is token-authenticated

Port 18766 (`/link`, `/api/result`, `/api/longpoll`) requires a bearer token,
which closes the hole where any local process could execute JS in your browser.
The port-18765 WebSocket used by the extension is checked by origin instead and
is unaffected. Full operator detail is in
[SECURITY.md](SECURITY.md) and [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md);
the two facts that catch people writing code here:

- A rejected request returns **plain text** `unauthorized: missing or bad bridge
  token`, **not** JSON. A client that parses `{"error": ...}` reports a parse
  failure instead of the real cause.
- Every authenticated route must **drain the request body before** returning 401,
  via `check_link_token_drained` rather than `check_link_token`. On Windows,
  wsgiref resets the connection on a rejected request that still has an unread
  body -- guaranteed once the body exceeds the socket buffer -- so the caller
  sees a dropped connection instead of a 401. Copy that helper for any new
  authenticated route.

The WebSocket port is the asymmetric half, and the thing that makes it
survivable is not the origin check. `clientId` arrives in the message body and
the sender chooses it, so writing it straight into `ext_clients` handed the
namespace to whoever spoke last -- and every later `ext_cmd` with it.
`_claim_ext_client` binds an id to one socket instead, which needs no protocol
change because `handle_close` already unbinds a socket that goes away: an
ordinary reconnect finds no incumbent, so only a *still-open* one can be in the
way. Two halves of that must survive an edit:

- **A refused socket is closed, and nothing else about it is recorded.** Not its
  tabs, and not `last_ext_seen` / `client_last_seen` -- refreshing those would
  let a stranger make a dead extension read as freshly checked in, which is the
  one field `doctor` uses to tell those apart. Closing is what makes it
  self-heal: the real extension reconnects and wins the moment the stale socket
  ages out, where a silently ignored socket would look connected forever.
- **`CLIENT_TAKEOVER_GRACE_SECONDS` is not padding.** A half-open socket (TCP
  ESTABLISHED, peer gone) is real here -- it is why the extension pings at all
  -- so refusing every newcomer unconditionally locks the bridge out until a
  human restarts it. The stamp it is measured against is refreshed by every
  message the owner sends, keepalive pings included, so an idle browser at 20 s
  per ping is never near the 60 s window.

Refusals are counted rather than swallowed (`rejected_client_takeovers` in
`diagnose`), because the failure this prevents is silent by construction.

Because this lives in the bridge, a change here needs a bridge restart
(section 1). Restarting your editor is not what does it.

## 7. Running the tests

```bash
# offline: no browser, no bridge, no network. Under two minutes.
python -m pytest tests/ -q

# live: needs the bridge running and the extension connected. Four minutes or so.
python -m pytest tests/ -q -m live
```

Three things decide whether the live run means anything, and skipping any of them
wastes the whole run:

- **The bridge and the extension have to be running this checkout.** Both are
  long-lived and keep whatever build they started with (section 1), so a green
  live run can certify code that is not in the tree -- and until the fixture
  checked it, nothing downstream recorded which build had answered, so a stale
  extension could be sealed as a release. The session fixture now asks
  `get_setup_status()` before it samples anything and fails the run naming every
  stale component with its one fix. There is no override for this one, and it is
  a failure rather than a skip on purpose:
  `tests/live_preflight.stale_component_reason` says why.
- **Leave the browser alone if you can.** With someone opening and closing tabs,
  failover can pick a tab that is still loading and the CDP fallback loses its
  debugger mid-command. Measured: browser in use → 8 minutes and one failure;
  idle browser → 4 m 15 s, 54 passed. This is advice about noise, not a
  precondition: the preflight records what the browser did and never refuses or
  skips over it, because the user's tabs are the user's. A busy browser used to
  skip the whole live layer, which read someone else's tabs to decide whether the
  suite may run and left machines whose browser is never idle with no live layer
  at all.
- **Do not work on the machine during the coverage round.** Instrumentation
  roughly doubles the wall clock. The tests that used to fail as a group there
  now assert the budget the code hands downstream instead of the elapsed time,
  so the class is mostly gone -- but a remaining wall-clock assertion is a loose
  backstop, not a performance target. Re-run on an idle machine before treating
  a timing failure as a defect.

The live suite **touches the human's foreground tab**. Note the active tab before
you start and put it back afterwards:

```python
[t for t in S.list_all_tabs()["data"] if t["active"]]
```

Do not remember the id you get back -- resolve it again with
`tests/live_preflight.resolve_remembered_tab`, for the reason in section 3. It
returning `None` means the human closed that tab, which is theirs to do and fails
nothing.

The `scratch_session` fixture opens one temporary tab that every live test
shares and closes it at the end. Do not change it to one tab per test; that
makes a mess of a real person's browser.

**The one thing the live layer fails a run over is a tab it opened and did not
close.** That verdict comes from `server._TAB_OWNERSHIP.outstanding()`, not from
a diff of the browser, and the difference is the whole point: a diff cannot say
who opened a tab, so the check it replaced fired identically for a human opening
a tab, a human closing one, and the suite leaking its own scratch tab. Three
things must survive an edit here:

- **Never widen it back to the whole browser.** The user may open, close and
  navigate their own tabs mid-run; that is recorded as `tab_activity` and judged
  not at all.
- **`own_tabs.enforced` is computed from the ownership counters**, so "nothing
  leaked" can be told apart from "this process opened no tabs, so nothing was
  measured" -- the same field the quiet-input gate needed for the same reason.
- **Both causes stay in the message.** A forgotten `close_tabs(..., owner_id=)`
  and a close refused with `lifecycle generation changed` (Chrome discarded the
  suite's own tab and `close_tabs` correctly would not close its replacement)
  leave identical registry records and need different fixes.

To assert that a tab really came to the front, use the `active` field from
`list_all_tabs()`. Do **not** use `document.visibilityState`: it also reports
`hidden` when the window is merely minimised, and a test cannot control that, so
the assertion turns flaky.

## 8. Odds and ends

- **`pythonw.exe` showing up twice is normal**, not a duplicate daemon. The
  `Scripts\pythonw.exe` in a virtualenv is a forwarding stub that launches the
  real interpreter as a **child** with the same command line. The child is the
  one **holding the port**. Identify it with `netstat -ano` on 1876x rather than
  by counting processes; killing the parent takes the child with it.
- Concurrently starting MCP instances race to spawn the bridge: "port closed →
  spawn" is a check-then-act that is not atomic across processes. A lock file
  (`O_EXCL` create + 30-second expiry) guards it. Note that the lock is
  **not released on success** and relies on expiry: an earlier version released
  it immediately, later arrivals were still failing the port check (binding takes
  time), took the lock again and spawned another -- 12 concurrent instances
  produced 8 daemons. Do not "fix" that back.
- The bridge log rotates at 5 MB, in **two** places that must keep the same cap
  (`bridge.LOG_MAX_BYTES`). `server._bridge_log_path` renames the file at spawn
  time, which is the only moment it can: the handle it opens becomes the daemon's
  stdout and stderr and is then held for the process's whole life. So that half
  never fires on a bridge that stays up for weeks -- which is the case the cap
  exists for. `bridge.rotate_own_log` covers it from inside the daemon, every
  `LOG_CHECK_SECONDS`, by copying to `bridge.log.old` and calling `os.ftruncate`
  on its own fd. It must stay a copy-and-truncate: Windows refuses to rename a
  file that has an open handle unless the opener asked for `FILE_SHARE_DELETE`,
  and Python does not. The daemon is detached and created with
  `CREATE_NO_WINDOW`, so a crash leaves a trace **only** in that file.
- What may be written into that log is a **policy**, not a formatting choice:
  `redact_url` at every logging call site, `redact_pattern` for a caller's search
  string, and never the token in any form. `SECURITY.md` states it to operators
  and `tests/test_log_redaction.py` scans the module's source for a `logger.*`
  line carrying a raw `.url` / `['url']` / `url_pattern`, so a new log line that
  writes a URL straight through fails the offline suite rather than shipping.
- If a manual `execute_js` debugging session left a CDP debugger attached, the
  whole live suite fails afterwards with "debugger already attached". Suspect
  your own leftovers before blaming a code change; re-verify in a clean browser.
- **`Error: No SW` from a `chrome.*` call is the worker being collected**, not a
  failure. Chromium's `ExtensionFunctionDispatcher::DispatchForServiceWorker`
  answers a call whose service worker has already stopped with that bare string
  (`No RPH` when the render process host went first). The trap is that it arrives
  *before* the WebSocket close is processed, so `ws.readyState` still reads
  `OPEN`, and a benign/real split keyed on the socket alone files a routine
  eviction as `console.error` -- a red entry on `chrome://extensions` for an
  install that is working, which is the first thing a new user is told to check.
  `isWorkerGoneError` is that check and the log sites go through it. Do **not**
  fold it into the `dead` flag in `ws.onmessage`: that flag also decides whether
  to send the error reply, and the socket here is still open, so the bridge can
  be answered instead of waiting out its full timeout.
- **The extension JavaScript is linted now**, which it was not for most of this
  repository's life -- `npm ci` once, then the ordinary `python -m
  scripts.lint_report` covers it (the gate itself is CONTRIBUTING.md's section).
  Two eslint rules are *tuned* rather than obeyed, because the idiom they would
  flag is load-bearing: `catch (_) {}` around a `chrome.*` call is how the
  routine `No SW` above gets swallowed, so `allowEmptyCatch` is on and an unused
  catch binding named exactly `_` is permitted. Write `catch (_)` when you are
  deliberately ignoring the error and name the binding only when you read it --
  `catch (e) {}` with `e` unused fails the gate. Do not reach for an inline
  `eslint-disable` instead; there is not one in the tree, and the first one makes
  every later reader wonder which rules still mean something.
- **A rule count sampled from one lint target cannot vouch for another.** eslint
  has a third vacuous-pass shape that ruff does not: a flat config whose `files:`
  pattern stops matching still walks the directory, exits 0 and reports every
  file clean while applying **zero** rules -- `files_scanned` cannot see it, and
  only `eslint --print-config` can. `_rules_applied` in `scripts/lint_report.py`
  asks that question **once per target** and reports the **minimum**, so
  `rules_applied > 0` means every target is covered rather than at least one.
  Sampling the first target was sound while `JS_LINT_TARGETS` held one entry and
  became a vacuous pass the moment `page_scripts` was added: the new directory
  would have inherited the extension's rule count and read as enforced while
  enforcing nothing. Adding a third target means adding a config block for it in
  `eslint.config.mjs` in the same change -- the floor is what turns forgetting
  that into a red gate instead of a silent one.

- **Line endings are a correctness property here, not a formatting one.** The
  release seal hashes the *raw bytes* of every tracked file
  (`evidence_manifest.source_identity`), so a CRLF working copy gives
  `content_sha256` a value nobody else can reproduce -- not even from a clone
  of the commit it names -- and nothing in the sealed record mentions line
  endings, so the first reader to notice is a third party who cannot verify
  it. Two tests in `tests/test_evidence_manifest.py` hold that down, and they
  are not the same check. One asks **git** whether an `eol` attribute governs
  every tracked path: `.gitattributes` is a single `* text=auto eol=lf` rule
  because the suffix list it replaced had lost three times (`*.mjs`, `*.yml`
  and `*.toml` appended late, `.yaml` never covered) and no suffix can cover
  `LICENSE` or `.gitignore` at all -- measured at 0.4.15, five tracked paths
  were ungoverned and a fresh clone of the sealed commit differed from the
  worktree in three of them. The other fails if any tracked text file in the
  worktree holds CRLF, because the attribute governs a *checkout* while a
  local tool can undo it afterwards: `Path.write_text` translates on Windows,
  which is how `check_derived_notices.py --write` did it to its own source and
  how a `ruff format` with `line-ending = auto` did it to 21 files at once.
  Neither test can be satisfied by the other, so do not fold them together.

- **Editing a derived file is a two-step operation now.** Eight files here are
  derived from GenericAgent, and `THIRD-PARTY-NOTICES.md` states line-for-line how
  much of each upstream file survives. Those figures can only be measured against
  an upstream checkout, which is not in this tree, so for three releases the only
  automated question asked about them was whether `identical / total` matched the
  stated percent. That is self-consistency, not truth, and it stayed green while
  the table drifted: `7efc604` edited `simphtml.py` and `popup.js`, nothing
  re-measured, and the notice shipped claiming `780 of 873` for a file that by
  then matched 757. Editing one of the eight therefore means:

  ```bash
  git clone https://github.com/lsdefine/GenericAgent <dir>
  python -m scripts.check_derived_notices --upstream <dir> --check   # what drifted
  # correct the table from its output -- the rows are generated, never typed
  python -m scripts.check_derived_notices --upstream <dir> --write   # record the hashes
  ```

  `--upstream` is mandatory rather than defaulted, for the same reason
  `--check-installed-skills` refuses to run without a directory: a measurement
  against a path that happens not to exist is worse than no measurement. What
  makes the offline suite able to catch this at all is `MEASURED_AGAINST`, the
  sha256 of each derived file as of the last measurement -- comparing that to the
  tree needs nothing but the tree.

  Two things about that hash are load-bearing, and both exist to stop the gate
  from crying wolf, because a gate that goes red on noise is one people learn to
  route around. It works on lines rather than bytes, so line endings cannot move
  it: every tracked path is pinned to LF by `.gitattributes` -- one
  `* text=auto eol=lf` rule, since the suffix list it replaced could not cover an
  extensionless file -- but a tree that arrives another way is not, and a script
  that round-trips a file through Python's text mode on Windows produces CRLF,
  which is how 21 tracked files in this repository came to hold it at once. Note
  which half does that work before simplifying either: reading in text mode
  already collapses CRLF on the way in, and `splitlines()` drops it too, so the
  property survives losing one of them and **no single-line change here can make
  the CRLF test go red**. Measured with the same pair in
  `tests/test_extension_build.py`.

  The second is that it is **blind to exactly two generated lines**, and to no
  others. `GENERATED_LINES` is that list, and both entries earn their place the
  same way -- the line is written by a script here, and it does not exist upstream
  at all, so it cannot be part of an `identical` count in either direction:

  * `manifest.json`'s version string, which `versioning bump` rewrites on every
    release. Upstream declares `"version": "2.0"` and every value this package can
    hold is a `MAJOR.MINOR.PATCH` triple, so the line fails to match before the
    bump and after it -- measured at 0.4.14 and 0.4.15, `29 of 40` both times.
  * `background.js`'s `BTAP_BUILD` stamp, which `scripts.extension_stamp --write`
    regenerates whenever *any* extension file changes. Without it, editing
    `content.js` would demand a re-measure of `background.js` as well -- a file
    nobody touched. The pattern is imported from
    `browsertap_mcp.extension_build` rather than copied, because two copies would
    drift and the symptom would be this gate going red on every regeneration.

  Either exemption missing meant a release demanded a re-measure that needs an
  upstream clone and could only return the same number. Nothing else may be
  excluded, and that is asserted rather than trusted:
  `test_the_notice_fingerprint_ignores_only_the_two_generated_lines` fails if
  normalising touches any other line of any derived file, and it reads the set from
  `GENERATED_LINES` rather than a list typed in the test, so adding a third
  exemption is red rather than quiet. Each entry also has a partner test that fails
  if a *real* edit to that same file leaves the hash alone.
- **A reverse gate can only ask about the file set it was told about.** The check
  that every derived file is credited enumerated `chrome_extension/`, which was
  the whole derived set until the T0 refactor carved two files out of
  `simphtml.py`'s string literals into `page_scripts/`. Those two held a higher
  share of upstream's lines than anything except `popup.html` -- 76% and 85% of
  their own content -- and shipped uncredited for three releases, invisible to the
  one gate built to catch exactly that, because they arrived by a *refactor*
  rather than a fork. Both were replaced outright in 0.4.15 and the files there
  now are original, but the hole they went through was the enumeration, so that is
  what changed: anything under `page_scripts/` is treated as derived unless it is
  named in `ORIGINAL_PAGE_SCRIPTS`, which makes the next extraction a red gate
  instead of a silent one. Adding a derived file outside those two directories
  still means adding its directory to `_expected_derived_paths()` and its pair to
  `DERIVED_PAIRS`.

  The same directory caught a **second** gate the same way in the same release,
  which is why this is stated as a shape and not as one file's history.
  `test_manifest_declares_the_chrome_floor_its_own_api_use_forces` derives
  `minimum_chrome_version` from the APIs the browser-side code actually calls, and
  it read `chrome_extension/*.js` -- the whole of that code until the page analysis
  moved. So the highest floor in the tree (Chrome 121, for the
  `Element.checkVisibility()` option names) sat in a directory the gate could not
  see, and a manifest saying 111 read as verified. Both gates now assert their own
  file set, so losing a directory is red rather than quiet. When you add
  browser-side code in a third place, those two asserts are what will tell you.

  One consequence of that replacement is worth knowing before you read the notice:
  every upstream file has exactly one heir here again, so the table's rows are
  independent. While three of them shared `simphtml.py` as an ancestor **their
  numerators overlapped and could not be summed**; `family_total()` walks upstream
  line *indices* so each counts once, and it is still called because a future
  split would recreate the overlap.

## 9. Machine-specific notes

Anything that only makes sense on one machine -- absolute paths, which browser
profile is in use, how a particular skill manager is wired -- belongs in an
untracked `AGENTS.local.md`, not here. This file must stay valid for someone who
just cloned the repository, and `python -m pytest tests/test_documentation_contract.py`
fails if an absolute path leaks into a published document.
