# Runtime lifecycle

Read before changing process startup, extension build checks, page-script loading,
worker keepalive, daemon spawning, or log rotation.

Start with [AGENTS.md](../../AGENTS.md) for the task entry point. Numbered
sections retain the original guide's references; other section numbers refer
to that root guide. Commands and release gates remain in
[CONTRIBUTING.md](../../CONTRIBUTING.md).

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

`matches_tree` is enforced in code, not advice. For three releases it was not:
`reload_extension_required` OR-ed the version comparison in beside the verdict, so
`matches_tree` could add a reload demand but never withdraw one. Every `versioning bump`
rewrites `manifest.json` and Chrome only parses that at load time, so each bump left the
extension one version behind for the life of the install -- and `tests/live_preflight.py`
read that flag rather than the verdict, with no override, so each bump demanded a human
Reload that changed only the number the gate complained about (measured at 0.4.18: bridge
and stamps current, `doctor` still exiting 1 with `action: reload_extension`).

The version number is now only a fallback: it decides when the stamp cannot
(`unverifiable`, `stamp_not_regenerated`) and yields when the stamp says `matches_tree`.
Two things must survive an edit there. **Only the version number yields** -- a worker
whose JavaScript matches this tree but speaks a different protocol, or misses a required
capability, is a contradiction rather than a version gap; folding it in would turn the
strongest signal into a blanket excuse, and
`test_a_matching_stamp_does_not_excuse_a_protocol_or_capability_gap` fails if you do.
**The version gap is still published**, with a note saying why no reload is needed --
`healthy` beside two different version numbers is otherwise unexplained, and the true
answer (the code was compared, not the numbers) is not derivable from the payload.

There is a **fourth** kind of code the table above does not cover, and it is the
one exception to "an edit needs a reload or a restart": the two files in
`src/browsertap_mcp/page_scripts/` -- `page_outline.js` and `list_groups.js` -- are
**not** part of the extension. `simphtml.py` reads them off disk at import
(`_load_page_script`) and `get_html` / `get_main_block` inject them through `execute_js`
on **every call**, so an edit is live the moment the MCP server restarts: no reload, no
bridge restart. That is why the layer exists at all -- it decides what the model sees of
a page (visibility analysis, main content block), and iterating on it used to mean
editing an `an r-string literal` literal inside `simphtml.py`. Do not move a page script into
`chrome_extension/`: you trade a zero-friction edit for a manual Reload per iteration,
and the page context must never call `chrome.*` (eslint's second config block gives it
`globals.browser` only, so a stray `chrome.` fails `no-undef`).

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
