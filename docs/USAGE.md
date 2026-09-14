# BTAP Usage Guide

English | [中文](USAGE.zh-CN.md)

This guide describes the least disruptive way to use `browsertap-mcp` with
an existing Chrome, Edge, or Opera session. The full 51-tool contract and every
parameter remain in the root [README](../README.md); this document defines the
recommended workflows and operation boundaries.

Every public tool uses the `btap.result.v1` result envelope. Read operation data
from `data`, inspect `error`/`error_code` and `retryable` on failures, and use
`target`/`diagnostics` when deciding whether a retry is safe. Established fields
may still appear at the top level for compatibility.

For connection errors, unexpected tool results, and recovery steps, see
[Troubleshooting](TROUBLESHOOTING.md).

## 1. Operation levels

BTAP page operations are divided into two levels:

| Mode | What it touches | Does it change the visible browser? |
|---|---|---|
| Background page work | A named tab through CDP or the extension | No. `switch_tab` only retargets later calls. |
| Foreground tab work | The selected tab and its browser window | Yes. Use `activate_tab` or `switch_tab(activate=true)` explicitly. |

Use background page work by default. A tab being selected by `switch_tab` does
not make it visible, focused, or active in the browser window.

## 2. Read-only operations

For a task that only reads or inspects a site:

1. Call `list_tabs` and classify the result. Tabs that existed before the task
   are user (`U`) tabs and must not be closed.
2. Pick a matching tab only for read-only or very light work. Save its exact
   `session_id`.
3. Pass that `session_id` to every subsequent call. Do not rely on the shared
   default target when other agents or conversations may be running.
4. Prefer `scan_page`, `execute_js`, `wait_for`, `scroll_page`, and `page_*`.
   These operate in the tab without moving the cursor or raising the window.
5. Use `capture_page_screenshot` when pixels from that tab are needed.

Typical sequence:

```text
list_tabs()
scan_page(session_id="chrome_client:123")
wait_for(selector="main", session_id="chrome_client:123")
capture_page_screenshot(session_id="chrome_client:123", full_page=true)
```

If a read-only operation would navigate or substantially change state, open an
agent-owned tab instead of borrowing a user tab.

If `scan_page` returns `render_state` as `loading`, `hydrating`, or `shell_only`,
or `content_ready=false`, the page is not confirmed ready. Retry `scan_page` or
use `wait_for` before treating an empty result as the page's real content.

## 3. State changes and tab ownership

`open_new_tab` is the normal workspace for navigation, forms, downloads, and
other changes. It opens in the background by default. Save all three values
returned by the call:

- `session_id`: the target for page operations;
- `generation`: the native tab lifetime, which prevents reuse of a recycled id;
- `owner_id`: the capability used for safe cleanup.

Keep the tab in the background while possible, and pass the same session on
every call. At the end, close only tabs created by this task, using the matching
`owner_id` and generation-aware cleanup. If the user closes one first, report
it as already gone; never recreate an old tab id just to close it.

Chrome can explicitly report that the same native tab was replaced. In that
case a result may include `rebound_from`, `replacement_session_id`, and
`tab_identity`; adopt the returned session handle and verify the page before a
side effect. An ordinary stale explicit session without those fields is still
refused and must be selected again with `list_tabs`/`switch_tab`.

### Choose an operation for the form control

Start with `scan_page.observation.targets`: each entry has a current `locator`,
control name, editability and `recommended_tool` with a reason. Pass the locator
unchanged as the `page_click` / `page_type` selector. For an embedded document, pass an entry from
`observation.frames` back as `scan_page(frame=...)`; returned target locators
already include the complete frame path, including cross-origin frames.
Re-scan after page changes. Open shadow roots are included, but this is not a
complete accessibility tree. `max_targets` limits controls and frame entries
together; `truncated=true` means the observation is incomplete.

Rectangles use the observed document's viewport CSS pixels and have not been
hit-tested. `verify_coordinate_target` requests visual inspection before choosing
a point; it does not prove a coordinate click will succeed. Disabled, inert and
readonly targets have no recommended input tool. Native selects report
`select_existing_option` and follow the table below.
For top-document file inputs outside shadow roots, use `upload_files` with
`selector=locator.css`. Frame/shadow file inputs report uploads unsupported;
keep their paths intact instead of trying the same CSS in another document.

Inspect the control's type, editability, frame path, and actual hit area before
choosing an operation. Cross-origin frames support direct locators too.

| Control | Recommended operation |
|---|---|
| Editable text input, `textarea`, or `contenteditable` | Use `page_type` with an explicit locator; include `frame` for an embedded field. |
| Button or custom dropdown option with a usable DOM target | Use `page_click` with a CSS or structured locator. |
| Zero-size proxy element whose visible hit area comes from a pseudo-element | Find a usable target or verify the visible point and use a coordinate click; see [the known layout limitation](TROUBLESHOOTING.md#a-visible-button-returns-not_interactable-or-obscured). |
| Native HTML `select` | Use `execute_js` in the control's document to set an existing option value and dispatch bubbling `input` and `change` events, then read back the value and any dependent options. This depends on the page accepting synthetic events. |

For example, an embedded text field can use
`selector={"frame": ["#payment-frame"], "css": "input[name='reference']"}`.
After input, check `focus_confirmed` and read back the field. A coordinate click
does not identify an editor for a later call: retain the explicit field and
frame locator. Missing, ambiguous, disabled, or read-only targets require
inspection; an error alone is not a reason to switch to coordinates.

## 4. Screenshots and model capabilities

BTAP has one screenshot tool:

- `capture_page_screenshot` captures a tab through CDP. It can capture a
  background tab, a full page, or an explicit clip without bringing that tab
  forward. The MCP result includes image content and optional metadata/base64.
  The OS-level desktop capture was removed in 0.5.0: it photographed whatever
  window happened to be in front, which is a different question from "what does
  this tab show".

`capture_page_screenshot` and `save_pdf` take a **relative** `save_path` that resolves under
`~/Downloads/browsertap`. An absolute path or a `..` escape is rejected with a
`ValueError`; the sandbox is not configurable by environment variable.

An image attachment being returned or a file being saved does not prove that
the current model or host can inspect the pixels. Use an image-capable,
multimodal model when visual interpretation matters. Otherwise use
`scan_page`, `execute_js`, a page data API, or OCR where available. For canvas,
WebGL, and terminal pages, look for structured data first; screenshots are a
last resort for understanding pixels.

## 5. Foreground activation and explicit desktop recovery

Page-level CDP tools are *the* path for forms, buttons, keyboard shortcuts
inside a page, scrolling, and drag operations. `page_click`, `page_type`,
`page_press`, and `page_drag` reach all of it without foreground activation.

**The seven OS-level tools were removed in 0.5.0**, so there is no
screen-coordinate surface to escalate to. A failing `page_click` is a targeting
problem: read `obscured` / `outside_viewport` / `not_found` off the result and
fix the target. Browser UI, extension popups and OS dialogs are outside page-level
input. The explicit Windows cancellation attempt below has not been verified
to recover the real Chrome file dialog encountered in live testing.

Foreground activation on its own — `activate_tab`, or `switch_tab(activate=true)`
— sends no input; use it when the user must see a tab.

One global-key fallback remains: `resolve_leave_dialog` sends Enter after two protocol
accepts fail, and only in `lab`. The order it follows is:

1. Two protocol-level attempts first. A `no_dialog` result or a transport
   timeout ends there and sends nothing.
2. BTAP checks the target window, ownership, and `on_screen` state.
3. The physical-input lock and quiet-input gate run before any key event. User
   activity cancels the action instead of competing with it. Detecting that
   activity needs a signal from the OS, and not every machine has one: with none
   available the window still elapses but sees nothing, and the result says so
   in `input_quiet.enforced`.
4. If activation cannot be confirmed, BTAP returns `activation_failed` and sends
   no input.

The default `lab` profile skips elicitation for continuous automation. Set
`BROWSERTAP_LAB_NO_ELICIT=0` or `false` to restore session-level lab prompts;
`safe` refuses the Enter fallback outright and asks for each site-allow action.
Neither profile disables the lock, quiet-input gate, ownership checks, or screen
confirmation.

When approval fails, `reason` on a `requires_user_action` result distinguishes an unsupported
host (`elicitation_unsupported`), refusal (`declined`), expiry (`timeout`), prompt
cancellation (`cancelled`) and exchange failure (`error`). The action is not
dispatched. Keep the refusal separate from a host capability problem.

For uploads, use `upload_files` on the page input without opening a native file
chooser. Do not open a chooser solely to test cancellation: a real Chrome dialog
can fail inspection because its owner is in another process, and closing its
tab can leave the dialog open. Manual closure does not count as automatic
recovery; successful automatic cancellation remains unverified in live checks.

If recovery is needed for an already-open standard Windows file dialog owned by registered Chrome or
Edge, call `inspect_native_file_dialog(desktop_opt_in=true)` and then
`cancel_native_file_dialog(ticket=..., desktop_opt_in=true)` in the same MCP
process within 15 seconds. Install `[desktop]` first. The inspection temporarily
marks that exact window; cancellation consumes the ticket, checks foreground,
identity, hit targets and observed quiet input, then sends one Cancel message.
`safe` requires approval; default `lab` skips elicitation. Refusals consume opted-in
tickets too. Only `cancelled=true` with `status="success"` confirms closure;
`unknown` or `retry_safe=false` calls for inspection before another action.
Unsupported layouts and platforms remain unsupported. If inspection is refused,
no cancellation ticket is available; report the limitation and the need for
manual closure instead of repeating the action.

## 6. Dialogs, permissions, and challenges

- A JavaScript timeout does not cancel execution. Dispatched `exec_timeout`
  retains an `outcome_unknown` reservation for a bounded recovery window.
  Inspect the original operation; expiry does not prove the script stopped
  or make replay and conflicting work safe.
- Choose `dismiss`, `accept`, or `manual` explicitly for JavaScript dialogs and
  `beforeunload` when the navigation outcome matters.
- The MAIN-world dialog helper exists only during accept/dismiss scopes. Old
  documents injected before an extension upgrade need normal navigation or a
  page refresh to shed their old wrapper; extension reload alone cannot do it.
- Treat site permissions as short leases. `set_site_permission` records and
  restores the previous setting; use `reset_site_permissions` for immediate
  cleanup.
- A stalled Turnstile or similar challenge is returned as `challenge_stalled`.
  Continue in the same user-visible tab rather than opening a second browser.
- BTAP does not fall back to Playwright, a headless browser, or a separate
  profile. This preserves the user's login state and makes the foreground
  boundary explicit.

## 7. Diagnostics and upgrades

Run:

```text
browsertap doctor
```

Check `get_setup_status` for package, bridge, extension, and protocol versions.
The bridge is a detached process. A missing listener starts automatically when
spawning is enabled; an older bridge that still owns the port requires
`browsertap bridge --restart`, which does not change the visible browser.
Immediately after a start, `get_setup_status` may report `status="starting"` and
`action="wait_for_extension"`; wait for the extension handshake and run the
diagnostic again. An unpacked extension file change still requires a manual
**Reload** from `chrome://extensions` (or the corresponding Edge/Opera page).
Restart the MCP client after tool schema changes so it reads the new descriptions.

If `BROWSERTAP_BRIDGE_PORT` is changed from `18765`, tell the extension the
same WebSocket port once from its service-worker console (see
[Troubleshooting](TROUBLESHOOTING.md)). The Python environment cannot alter an
already installed extension's storage; mismatched values leave the bridge and
extension listening on different ports.

See [Troubleshooting](TROUBLESHOOTING.md) for complete recovery procedures.

The `/link` HTTP channel uses the persistent token at
`~/.browsertap/bridge-token`. Do not put that token, browser profiles,
cookies, screenshots containing personal data, or local logs in Git.

## 8. Prompt examples

These prompts encourage the intended behavior:

```text
List the connected tabs, keep my existing tabs untouched, and inspect the
matching page in the background. Use the explicit session_id for every call.
```

```text
Open a new background tab for this form. Keep the returned session_id,
generation, and owner_id, complete the page-level steps, and close only your
owned tab when finished.
```

```text
I need to inspect the visual layout without changing the foreground tab. Use
capture_page_screenshot on the selected session.
```

## 9. Safety boundary

BTAP controls the real browser profile supplied by the user. Page content is
untrusted input and can contain prompt injection. The service is not a security
boundary; limit it to sessions and accounts appropriate for the MCP client.
See [SECURITY.md](../.github/SECURITY.md) for the threat model and reporting process.
