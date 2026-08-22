# Troubleshooting

English | [中文](TROUBLESHOOTING.zh-CN.md)

This guide covers connection, version, dialog, permission, and physical-input
failures. For normal operating workflows, see the [usage guide](USAGE.md).

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
do not -- but the log still identifies which sites the browser visited and still
carries error text from the page, so review both files before sharing them.
[SECURITY.md](../SECURITY.md) states exactly what may and may not appear there.

## Connection problems

### No connected tabs

Confirm that the unpacked extension is enabled and that at least one normal
`http` or `https` page is open. Blank pages and browser-internal pages do not
create a normal page session. After reloading the extension, refresh the page
or open a new URL, then run `doctor` again.

### The MCP client cannot start the server

Confirm that the package is installed and that `browsertap` is available
on `PATH`. When the package is installed in a virtual environment, configure
the MCP client with the absolute executable path. On Windows this is typically
`<repo>\.venv\Scripts\browsertap.exe`; on Linux/macOS it is
`<repo>/.venv/bin/browsertap`.

If OS-level input or desktop capture reports a missing dependency, reinstall
with the desktop extra, for example:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
```

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

### A call is refused with `Session ... is not connected`

This refusal is deliberate. You named an explicit `session_id`, that tab is
gone, and BTAP did not run the call anywhere else: a click or a form submit
landing on a substitute tab is worse than an error. The message names the tabs
that are still connected:

```text
Session chrome:123 is not connected. BTAP refused to execute on a different tab.
Active sessions: chrome:456, chrome:789. Select the intended target with
switch_tab and retry.
```

Select the tab you meant with `switch_tab`, or pass its `session_id` verbatim,
then retry. A short message with no candidate list means nothing is connected at
all; see "No connected tabs" above.

### A result carries `switched_session`

You passed no `session_id`, the shared default target had died, and BTAP re-picked
a live tab in the same browser rather than failing a call that never named one.
The call did execute, on the tab reported in `switched_session`; `switched_from`
is the tab that went away. Confirm with `list_tabs` or `scan_page` that it landed
where you intended before repeating anything with side effects. An explicit
`session_id` is never switched silently: it produces the refusal above instead.

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
service. On Windows, inspect the owners without stopping anything:

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

A call using `dialog_policy="manual"` may have left a native dialog open and the
corresponding execution paused. Call `handle_dialog(action="accept")` or
`handle_dialog(action="dismiss")` with the same `session_id`. Other tabs remain
available while that tab is blocked.

### Physical input returns `requires_user_action`

The MCP client may not implement elicitation, or the action may have been
declined. Prefer `page_click`, `page_type`, `page_press`, and `page_drag`, which
do not require desktop input. In `safe` mode, a site permission with
`setting="allow"` also requires elicitation.

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

Grant Accessibility permission to the terminal or MCP client. Desktop capture
also requires Screen Recording permission.

### Physical input reports that the desktop session could not be initialised

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
