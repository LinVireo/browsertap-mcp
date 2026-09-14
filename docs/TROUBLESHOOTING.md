# Troubleshooting

English | [中文](TROUBLESHOOTING.zh-CN.md)

This guide covers connection, version, dialog, permission, and physical-input
failures. For normal operating workflows, see the [usage guide](USAGE.md).

## Known limitations and recovery

The following cases were observed during live verification on 2026-09-12–14.
Confirmed fixes and remaining limits are distinguished below. Passing offline
tests or CI alone does not establish successful browser recovery.

### A native file chooser stays open

Live inspection of a real Chrome file dialog returned
`native_dialog_unverifiable` because the native owner belonged to another
process. Without a ticket, `cancel_native_file_dialog` could not reach its
successful cancellation path. Closing the originating tab also left the chooser
open. This is not a verified unattended recovery capability.

Use `upload_files` with the page's file input to avoid opening a chooser.
For an already-open dialog, inspection and cancellation remain limited to the
documented Windows layouts and ownership checks. If inspection refuses or
closure is uncertain, stop retrying and report that manual closure may be
needed. `handle_dialog` handles JavaScript dialogs, not OS file choosers.
Do not open another chooser just to test recovery. A user's manual closure or
a later observation that the window is gone does not prove BTAP cancelled it.

### JavaScript continues after `exec_timeout`

A response deadline does not terminate dispatched JavaScript. Before 0.5.3,
an `exec_timeout` could release the target while the original script continued,
allowing a second MCP process to modify the same page. Version 0.5.3 keeps
dispatched timeouts as `operation_status=outcome_unknown` with
`reservation_held=true` during the existing bounded recovery window. Commands
targeting that tab receive `target_busy`; unrelated tabs remain available.
After retention expires, `retry_safe=false` and the unknown receipt remain.
Expiry does not prove that the page is idle or cancel late effects.

Use `get_execute_js_result` from the originating MCP session to inspect an
available `operation_id`; polling does not replay or cancel the script.
Avoid replay and conflicting work in the affected tab while execution is
uncertain. Closing a tab owned by the current task with its `owner_id` ends
that document's lifecycle; do not close a user's tab for cleanup. A lifecycle
end does not undo requests or other effects already sent.

### Navigation fails during `Page.enable`

Before 0.5.4, navigation initialization could fail after two 2.5-second
`Page.enable` attempts even when the caller's timeout had time left. A fully
local page with a busy main thread reproduced this failure; external network
latency is not required. Version 0.5.4 bounds debugger attachment by the same
deadline and gives the single initialization retry the remaining call budget.
`Page.navigate` still dispatches at most once; initialization failures report
`dispatched=false`.

The original intermittent incident's trigger remains unconfirmed. This fix
does not diagnose unrelated JavaScript read or dialog-cleanup timeouts. After
an error, inspect the operation receipt and target state before retrying a
state-changing call; a later successful call alone does not establish a fix.

### Manual dialog recovery reports a CDP timeout

A live manual-dialog check returned `debugger_detached` after
`Runtime.releaseObjectGroup` exceeded its cleanup deadline. Separate checks on
fully loaded foreground and background pages handled the dialog successfully;
the intermittent failure has no confirmed root cause or targeted fix.

After creating or navigating a tab, use `wait_for_url` to verify that the
requested document is complete before opening a native dialog. If handling
times out, inspect with `handle_dialog(action="manual")` and query the original
operation receipt. The dismiss may already have occurred, and a closed dialog
does not recover a missing script result. Preserve `outcome_unknown` and
`retry_safe=false`; do not replay the script or infer success from a later retry.

### A sandboxed child frame blocks main-page execution

An iframe with `sandbox` but without `allow-scripts`, including
`sandbox="allow-same-origin"`, can cause default `execute_js` dialog-policy
setup to return `dialog_scope_setup_failed`. Preparation currently requires
acknowledgements from child frames, so a frame that forbids scripts can block
the call even when the main document is scriptable. This can also fail a
script readback after an earlier page input succeeded.

Classify this as the frame-preparation limitation rather than assuming the main
page forbids scripts or the preceding input failed. Inspect the existing state
before any retry; repeating the same default call does not fix the frame
restriction. Preserve the page's sandbox settings and report the blocked step.

### Extension removal requires a user gesture

Chrome returned `chrome.management.uninstall requires a user gesture.` for
`uninstall_extension` with both `show_confirm_dialog=true` and `false` in live
checks. Changing the confirmation flag does not supply the required gesture.
If refused, the user must remove the selected extension through
`chrome://extensions` or the browser's equivalent management page. Recheck with
`list_extensions`; disappearance after manual removal is not tool success.
BTAP also cannot uninstall itself through its active response channel.

### A visible button returns `not_interactable` or `obscured`

A button can have a `0×0` DOM box while CSS `::after` supplies its visible
click area. In live verification, locating such a button by role/name returned
`not_interactable`; targeting its visible text returned `obscured` because
the button itself owned that point. Selector clicks do not yet account for
this layout.

Inspect the current layout and use `document.elementFromPoint(x, y)` to verify
that a point in the visible control belongs to the intended button. Then use
`page_click(x=x, y=y, session_id=session_id)` with top-document viewport CSS
coordinates. Coordinate mode does not perform the selector's hit test, so
verify the point before dispatch and inspect the resulting page state.
Reconnecting the bridge does not change this layout limitation.

## Diagnostic order

1. Run `browsertap doctor`.
2. Inspect `get_setup_status` and compare `package_version`, `bridge_version`,
   `extension_version`, and `protocol_version`.
3. Apply only the recovery action reported by the status result. A missing
   bridge listener is started automatically when spawning is enabled. If
   `restart_bridge_required=true`, run `browsertap bridge --restart`;
   this does not change the visible browser. `reload_extension_required=true`
   requires a manual reload of the unpacked extension.
4. Re-run `doctor` and confirm that at least one normal page is connected.

Bridge logs are stored at `~/.browsertap/bridge.log`, capped at 5 MB with one
previous generation kept as `bridge.log.old`. URLs are redacted where they are
logged -- scheme, host and a truncated path survive, query strings and fragments
do not. Bridge exception handlers omit payload text and tracebacks; arbitrary
protocol identifiers use hash references. The log still identifies sites and
contains local paths, socket addresses and timings, so review both files before
sharing them.
[SECURITY.md](../.github/SECURITY.md) states exactly what may and may not appear there.

## Connection problems

### No connected tabs

Confirm that the unpacked extension is enabled and that at least one normal
`http` or `https` page is open. Blank pages and browser-internal pages do not
create a normal page session. After reloading the extension, refresh the page
or open a new URL, then run `doctor` again.

The bridge pins the exact Origin of the extension from `browsertap extension-path`.
Its diagnosis includes `ws_origin_policy.default_origin` and `identity_source`
(`manifest_key`, `unpacked_path`, or `unavailable`). Compare the ID with the
installed extension on `chrome://extensions`. A copied extension directory has
a different ID when no manifest key is present: load the reported package path,
or explicitly trust the copy's complete `chrome-extension://<id>` through
`BROWSERTAP_WS_ALLOWED_ORIGINS` in the bridge environment. Restart the bridge
after correcting its configuration. An unavailable identity requires repairing
the package manifest JSON/key. A key accepts strict Base64 or Chromium-compatible
PEM; whitespace in raw Base64 is invalid. On Windows, loading via a path with
different directory casing or a junction can also change the unpacked ID;
compare the actual installed ID instead of assuming aliases are interchangeable.
A new `clientId` never grants trust to an unrelated ID.

### The MCP client cannot start the server

Confirm that the package is installed and that `browsertap` is available
on `PATH`. When the package is installed in a virtual environment, configure
the MCP client with the absolute executable path. On Windows this is typically
`<repo>\.venv\Scripts\browsertap.exe`; on Linux/macOS it is
`<repo>/.venv/bin/browsertap`.

If the remaining lab-only `resolve_leave_dialog` physical fallback reports a
missing dependency, reinstall with the desktop extra, for example:

```powershell
.\.venv\Scripts\python.exe -m pip install -U "browsertap-mcp[desktop]"
```

From a source checkout the equivalent is `-e ".[desktop]"` run in that checkout.

### `/link` returns HTTP 401

The bridge and the MCP process must resolve the same token file. The default is
`~/.browsertap/bridge-token`; separate editor-specific token values are
not required. Do not guess which file each process read: `browsertap doctor`
reports `state_paths` for the process that ran it and, when the two disagree,
`state_paths_disagreement` naming each differing field for `this_process` and
for `bridge`. Tokens are compared as a truncated `sha256:` fingerprint, so the
comparison never prints or copies the token itself.
`bridge_token_is_from_before_the_file_changed` means the daemon locked an older
token into memory at start-up: restart the bridge and retry. Differing
`state_dir` or `token_file` values mean the two processes have different
environments, and a restart will not help until the paths agree. The result and long-poll
channels (`/api/result`, `/api/longpoll`) use the same token and answer the same
`401`. The response body is the plain-text line
`unauthorized: missing or bad bridge token`, not JSON, so a client that parses
every error as `{"error": ...}` will report a parse failure instead of the real
cause.

Read `state_paths.token_file_status` before attempting recovery: `missing`,
`empty`, `ready`, `unreadable` and `invalid_encoding` are distinct. Only ready
content has a fingerprint; `token_file_error` describes failures without token
bytes. `token_file_exists` and `state_dir_exists` can be null when metadata is
unreadable. Unknown default-directory metadata keeps the canonical path rather
than selecting the legacy directory. Correct access or encoding problems
instead of replacing an existing token file. Explicit relative `BROWSERTAP_STATE_DIR` and
`BROWSERTAP_BRIDGE_TOKEN_FILE` values use the launching process's working
directory; spawned daemons receive the corresponding absolute paths.

`error_code: malformed_diagnosis` means the bridge returned an unusable report.
The result remains `bridge_unreachable` with the restart action; it does not
prove a stale version. If a later DNS/socket probe fails, `doctor` preserves the
setup report, adds `port_probe_errors`, and exits nonzero.

### A call is refused with `Session ... is not connected`

This refusal is deliberate. You named an explicit `session_id`, and BTAP could
not verify a live session for that same tab. No script was dispatched. The
message names the sessions that are still connected so you can inspect the
intended target:

```text
Session chrome:123 is not connected. BTAP refused to execute because no live session could be verified for the same tab.
No script was dispatched. Active sessions: chrome:456, chrome:789. Run list_tabs,
verify the intended target, then select its live session_id with switch_tab and retry.
```

Select the tab you meant with `switch_tab`, or pass its `session_id` verbatim,
then retry. A short message with no candidate list means nothing is connected at
all; see "No connected tabs" above.

There is one evidence-backed exception: when Chrome reports that the same native
tab was replaced, BTAP may return `rebound_from`, `replacement_session_id`, and
`tab_identity` instead of refusing. Adopt the returned session id and verify the
page before repeating a state-changing action. Without those fields, an explicit
stale session is never substituted.

### A result carries `switched_session`

You passed no `session_id`, the shared default target had died, and BTAP re-picked
a live tab in the same browser rather than failing a call that never named one.
The call did execute, on the tab reported in `switched_session`; `switched_from`
is the tab that went away. Confirm with `list_tabs` or `scan_page` that it landed
where you intended before repeating anything with side effects. An explicitly
named session is only rebound when the result also carries the evidence-backed
`rebound_from` / `replacement_session_id` / `tab_identity` fields; otherwise it
produces the refusal above.

### A call returns `no_response` or `bridge_error`

Run `list_tabs` and verify that the exact `session_id` still exists. For a
read-only call, retry once against that explicit session after the page has
reconnected. For navigation, typing, downloads, or any other side effect, first
inspect the page or operation state; a timed-out request may have completed even
though its reply was lost. Use `doctor` when multiple tabs fail together.

### A command times out

Separate client startup timeouts from tool deadlines. If the MCP process does
not start, set the client's connect timeout to at least 60 seconds and use the
absolute executable path. If one browser tool times out, keep its explicit
`session_id`, increase that tool's `timeout` only when the operation is known to
be slow, and inspect `~/.browsertap/bridge.log`. Do not repeatedly retry
a state-changing operation without first verifying whether it landed.
For JavaScript, also see [continued execution after timeout](#javascript-continues-after-exec_timeout):
a released reservation does not prove the script stopped.

### Bridge port conflict or custom port

BTAP uses three consecutive ports: `BROWSERTAP_BRIDGE_PORT` for WebSocket,
`PORT+1` for HTTP, and `PORT+2` for the singleton lock. Only the first two carry
traffic; the third is held open for as long as a bridge is hosting, so a second
bridge that loses the race keeps running and works through the first one instead
of exiting. It is a different mechanism from the `spawn.lock` file in the state
directory, which is what keeps several MCP sessions starting at the same moment
from each launching a daemon -- so seeing exactly one listener on `PORT+2` is not
by itself evidence that only one daemon was started. A listener owned by
another application can make the client appear to be connected to the wrong
service.

The base must be an integer from `1` through `65533`. Invalid values are rejected
before network or spawn actions, while imports, help/version and package-path
commands remain available. Python probes/listeners and remote HTTP URLs support
IPv6 address families; the browser extension still connects to IPv4 loopback.

On Windows, inspect the owners without stopping anything:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 18765,18766,18767 |
  Select-Object LocalAddress,LocalPort,OwningProcess
Get-CimInstance Win32_Process |
  Where-Object ProcessId -In <comma-separated-owner-pids> |
  Select-Object ProcessId,ExecutablePath,CommandLine
```

If another application owns the range, choose a free three-port range and set
the base WebSocket port in `BROWSERTAP_BRIDGE_PORT` for the MCP/bridge process.
The extension cannot read environment variables, so tell it the same base port
once: open `chrome://extensions`, click **service worker** under **BrowserTap
Bridge**, and run this in the console that opens:

```js
chrome.storage.local.set({ btap_port: 19765 })   // use your base port
```

The extension reconnects to the new port immediately. Then run
`browsertap bridge --restart`. The Python environment cannot update
extension storage automatically. Never terminate an unknown port owner solely
by process name.

## Version and reload problems

### A tool rejects documented arguments

MCP clients cache tool schemas for the life of a session. Restart the MCP
session or client after a server upgrade. If `get_setup_status` reports
`reload_extension_required`, reload the unpacked extension from
`chrome://extensions` or the corresponding Edge or Opera page.

`chrome.runtime.reload()` restarts the extension service worker but does not
reliably reload source files from disk.

### The bridge version is stale

The bridge is a detached process and may outlive an MCP session. Normal tools,
including `get_setup_status`, automatically start it when no listener exists
and spawning is enabled. They do not replace an older bridge that still owns
the port. If `restart_bridge_required=true`, run the reported restart action;
restarting an editor or browser is not normally required.

For a bridge started by version 0.3.4 or later, lifecycle management is fully
backgrounded and does not focus or restart the browser:

```powershell
browsertap bridge --restart
browsertap bridge --stop
```

BTAP records the managed process in `~/.browsertap/bridge.pid` and checks
its PID, creation identity, and executable before termination. It never kills a
process merely because it is named `pythonw.exe`.

An older bridge may predate the PID record. In that one-time migration case the
command returns `unmanaged_running` instead of claiming success or terminating
an unknown process. On Windows, identify the process that owns the bridge ports,
then verify its command line before stopping that exact PID:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 18765,18766,18767 |
  Select-Object LocalPort,OwningProcess
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*browsertap_mcp.bridge*' } |
  Select-Object ProcessId,ParentProcessId,ExecutablePath,CreationDate,CommandLine
# Only after the port owner and command line match the BTAP bridge:
Stop-Process -Id <verified-port-owner-pid> -Force
browsertap bridge --restart
```

This migration does not require restarting Chrome. The extension reconnects to
the new bridge in the background.

### The status keeps asking for a restart or reload that changes nothing

Compare `package_version` against `bridge_version` and `extension_version`. If a
component is *newer* than `package_version`, the stale build is the MCP server
process itself, and `get_setup_status` reports `status: stale_package` with
`action: restart_mcp_session`. This is the normal outcome of upgrading the
package while an MCP session is live: the files on disk are new, but the running
process still holds the version it imported at startup.

Restart the MCP session or client. Restarting the bridge or reloading the
extension cannot clear it — both re-read the same new files and report the same
mismatch, which is why `restart_bridge_required` and
`reload_extension_required` are both false in this state.

## Browser interaction problems

### A tab remains `blocked_by_dialog` or `busy`

A call using `dialog_policy="manual"` may have left a JavaScript dialog open and the
corresponding execution paused. Call `handle_dialog(action="accept")` or
`handle_dialog(action="dismiss")` with the same `session_id`. Other tabs remain
available while that tab is blocked.

### Physical input returns `requires_user_action`

When approval was not granted, inspect `reason` in the result (also retained in
`legacy` and `diagnostics`). No approved action is dispatched for these reasons:

| `reason` | Meaning |
| --- | --- |
| `elicitation_unsupported` | The MCP host cannot present an approval request. |
| `declined` | The user declined the request. |
| `timeout` | The approval deadline elapsed. |
| `cancelled` | The approval prompt was cancelled. |
| `error` | The approval exchange failed. |

Prefer `page_click`, `page_type`, `page_press`, and `page_drag` when the task can
use page input. In `safe` mode, a site permission with `setting="allow"` also
requires elicitation. A refusal is not permission to change profile or replay
the action. Cancellation of the entire MCP task still propagates normally.

### Dialog helper globals remain after an extension upgrade

The extension no longer installs a MAIN-world dialog helper on every document.
An operation using `accept`/`dismiss` installs it for that scope, then restores
the page descriptors and removes its temporary controller when the final scope
ends; a bounded expiry handles abandoned scopes. Existing documents injected by
an older extension retain the old wrapper even after an extension reload. Normal
navigation or a page refresh creates a clean document. BTAP does not refresh
unrelated tabs to remove historical injection.

### Physical input returns `busy`

Another BTAP process owns the non-queued physical-input lock. Retry after the
active operation finishes. Do not loop, delete lock files, terminate unrelated
processes, or restart the bridge solely to clear this status; stale lock
metadata is reclaimed automatically after its owner exits.

### Physical input returns `input_activity_detected`

Mouse or keyboard activity occurred during the quiet-input window, so BTAP sent
no physical input. Retry only when the desktop is idle, or use a page-level tool
that does not interact with the desktop.

### A result reports `input_quiet.enforced: false`

The quiet-input window ran, but this machine exposes no input signal BTAP can
sample, so it could not tell whether someone was using the keyboard or mouse.
Only Windows exposes a last-input timestamp; the pointer position is unavailable
under Wayland, in a headless container, and on macOS without the accessibility
permission. The action was **not** blocked -- refusing would take physical input
away from machines where it otherwise works -- but treat the pass as unverified
rather than as a confirmed idle desktop, and prefer the page-level tools when
someone may be at the keyboard. `input_quiet.observed` lists the markers that did
answer, so a partly observable machine still shows what it was watched for.

### Physical input returns `activation_failed`

BTAP could not verify that the requested target was visible on screen and sent
no input. Restore the browser window and explicitly activate the intended tab
only when desktop input is required.

### Physical input does not work on macOS

Grant Accessibility permission to the terminal or MCP client if the remaining
`resolve_leave_dialog` Enter fallback is required. `capture_page_screenshot` is
CDP page capture and does not need macOS Screen Recording permission.

### The remaining physical fallback reports that the desktop session could not be initialised

The desktop extra is installed but this machine has no usable desktop: a
headless server, an SSH session with no X11 display, or a locked or unattended
console. `pyautogui` binds a display while importing and `mss` binds inside
`mss.mss()`, so the failure names the backend cause — `KeyError: 'DISPLAY'`, an
Xlib error, or an `mss` `ScreenShotError` — after the sentence identifying it as
a desktop problem. Reinstalling the extra does not help. Use `page_click`,
`page_type`, `page_press`, `page_drag`, and `capture_page_screenshot`, which
drive the tab through CDP and need no desktop, or run BTAP where a real desktop
session is available.

## Permission cleanup

Site-permission leases restore their previous values when they expire.
`reset_site_permissions()` can restore active leases immediately; without
arguments it applies to all leases for the selected browser. A failed restore
remains pending for retry and is recorded in the bridge log.
