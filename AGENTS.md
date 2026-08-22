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
  bridge goes down permanently and needs manual recovery. **Never call it.**

Ask the human to press Reload once on `chrome://extensions`. To find out which
component is stale, run `browsertap doctor` and read `action`:
`reload_extension`, `restart_bridge` and `restart_mcp_session` each name the one
thing that will actually fix it -- the other two will not.

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

Because this lives in the bridge, a change here needs a bridge restart
(section 1). Restarting your editor is not what does it.

## 7. Running the tests

```bash
# offline: no browser, no bridge, no network. Under two minutes.
python -m pytest tests/ -q

# live: needs the bridge running and the extension connected. Four minutes or so.
python -m pytest tests/ -q -m live
```

Two things decide whether the live run means anything, and skipping either wastes
the whole run:

- **Leave the browser alone.** With someone opening and closing tabs, failover
  can pick a tab that is still loading and the CDP fallback loses its debugger
  mid-command. Measured: browser in use → 8 minutes and one failure; idle
  browser → 4 m 15 s, 54 passed.
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

The `scratch_session` fixture opens one temporary tab that every live test
shares and closes it at the end. Do not change it to one tab per test; that
makes a mess of a real person's browser.

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

## 9. Machine-specific notes

Anything that only makes sense on one machine -- absolute paths, which browser
profile is in use, how a particular skill manager is wired -- belongs in an
untracked `AGENTS.local.md`, not here. This file must stay valid for someone who
just cloned the repository, and `python -m pytest tests/test_documentation_contract.py`
fails if an absolute path leaks into a published document.
