# BTAP Usage Guide

English | [中文](USAGE.zh-CN.md)

This guide describes the least disruptive way to use `browsertap-mcp` with
an existing Chrome, Edge, or Opera session. The full 49-tool contract and every
parameter remain in the root [README](../README.md); this document defines the
recommended workflows and operation boundaries.

## 1. Operation levels

BTAP operations are divided into three levels:

| Mode | What it touches | Does it change the visible browser? |
|---|---|---|
| Background page work | A named tab through CDP or the extension | No. `switch_tab` only retargets later calls. |
| Foreground tab work | The selected tab and its browser window | Yes. Use `activate_tab` or `switch_tab(activate=true)` explicitly. |
| Desktop work *(deprecated, removed in v0.6.0)* | The OS screen, cursor, and keyboard | Yes. Physical input can affect whatever is on screen — which is why it is going away; see §5. |

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

## 4. Screenshots and model capabilities

BTAP has one screenshot tool:

- `capture_page_screenshot` captures a tab through CDP. It can capture a
  background tab, a full page, or an explicit clip without bringing that tab
  forward. The MCP result includes image content and optional metadata/base64.
  The OS-level desktop capture was removed in 0.6.0: it photographed whatever
  window happened to be in front, which is a different question from "what does
  this tab show".

Both tools, and `save_pdf`, take a **relative** `save_path` that resolves under
`~/Downloads/browsertap`. An absolute path or a `..` escape is rejected with a
`ValueError`; the sandbox is not configurable by environment variable.

An image attachment being returned or a file being saved does not prove that
the current model or host can inspect the pixels. Use an image-capable,
multimodal model when visual interpretation matters. Otherwise use
`scan_page`, `execute_js`, a page data API, or OCR where available. For canvas,
WebGL, and terminal pages, look for structured data first; screenshots are a
last resort for understanding pixels.

## 5. Foreground activation, and the one remaining physical path

Page-level CDP tools are *the* path for forms, buttons, keyboard shortcuts
inside a page, scrolling, and drag operations. `page_click`, `page_type`,
`page_press`, and `page_drag` reach all of it without foreground activation.

**The seven OS-level tools were removed in 0.6.0**, so there is no
screen-coordinate surface to escalate to. A failing `page_click` is a targeting
problem: read `obscured` / `outside_viewport` / `not_found` off the result and
fix the target. Browser UI, extension popups, native file choosers, and OS
dialogs are outside what a page-level event can reach at all; report that as
unsupported.

Foreground activation on its own — `activate_tab`, or `switch_tab(activate=true)`
— sends no input; use it when the user must see a tab.

One physical path is left: `resolve_leave_dialog` sends Enter after two protocol
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

## 6. Dialogs, permissions, and challenges

- Choose `dismiss`, `accept`, or `manual` explicitly for JavaScript dialogs and
  `beforeunload` when the navigation outcome matters.
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
An unpacked extension file change still requires a manual **Reload** from
`chrome://extensions` (or the corresponding Edge/Opera page). Restart the MCP
client after tool schema changes so it reads the new descriptions.

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
See [SECURITY.md](../SECURITY.md) for the threat model and reporting process.
