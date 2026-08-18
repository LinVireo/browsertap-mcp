# ABM Usage Guide

English | [中文](USAGE.zh-CN.md)

This guide describes the least disruptive way to use `agent-browser-mcp` with
an existing Chrome, Edge, or Opera session. The full 55-tool contract and every
parameter remain in the root [README](../README.md); this document defines the
recommended workflows and operation boundaries.

## 1. Operation levels

ABM operations are divided into three levels:

| Mode | What it touches | Does it change the visible browser? |
|---|---|---|
| Background page work | A named tab through CDP or the extension | No. `switch_tab` only retargets later calls. |
| Foreground tab work | The selected tab and its browser window | Yes. Use `activate_tab` or `switch_tab(activate=true)` explicitly. |
| Desktop work | The OS screen, cursor, and keyboard | Yes. Physical input can affect whatever is on screen. |

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

## 4. Screenshot sources and model capabilities

ABM has two intentionally different screenshot tools:

- `capture_page_screenshot` captures a tab through CDP. It can capture a
  background tab, a full page, or an explicit clip without bringing that tab
  forward. The MCP result includes image content and optional metadata/base64.
- `capture_desktop_screenshot` captures the current visible OS virtual desktop
  across all displays, including negative monitor coordinates when present. It
  is useful for checking physical input, browser chrome, native dialogs, and
  file pickers. It is **not** a background-tab screenshot. If the browser is
  minimized or another window is visible, those are the pixels captured. The
  result includes `monitor_count`, `left`, `top`, and a `model_note` describing
  this boundary.

An image attachment being returned or a file being saved does not prove that
the current model or host can inspect the pixels. Use an image-capable,
multimodal model when visual interpretation matters. Otherwise use
`scan_page`, `execute_js`, a page data API, or OCR where available. For canvas,
WebGL, and terminal pages, look for structured data first; screenshots are a
last resort for understanding pixels.

## 5. Foreground activation and physical input

Try page-level/CDP tools first. They are the normal path for forms, buttons,
keyboard shortcuts inside a page, scrolling, and drag operations.

Foreground activation or OS-level input is justified for browser UI, extension
popups, native file choosers, OS dialogs, or a page that exposes no usable
protocol/DOM/API surface. The order is:

1. Explicitly activate the requested tab only when the user must see it or a
   desktop action truly needs it.
2. ABM checks the target window, ownership, and `on_screen` state.
3. The physical-input lock and quiet-input gate run before any cursor or key
   event. User activity cancels the action instead of competing with it.
4. If activation cannot be confirmed, ABM returns `activation_failed` and sends
   no input.

The default `lab` profile skips elicitation for continuous automation. Set
`AGENT_BROWSER_LAB_NO_ELICIT=0` or `false` to restore session-level lab prompts;
`safe` asks for each physical-input or site-allow action. Neither profile
disables the lock, quiet-input gate, ownership checks, or screen confirmation.

## 6. Dialogs, permissions, and challenges

- Choose `dismiss`, `accept`, or `manual` explicitly for JavaScript dialogs and
  `beforeunload` when the navigation outcome matters.
- Treat site permissions as short leases. `set_site_permission` records and
  restores the previous setting; use `reset_site_permissions` for immediate
  cleanup.
- A stalled Turnstile or similar challenge is returned as `challenge_stalled`.
  Continue in the same user-visible tab rather than opening a second browser.
- ABM does not fall back to Playwright, a headless browser, or a separate
  profile. This preserves the user's login state and makes the foreground
  boundary explicit.

## 7. Diagnostics and upgrades

Run:

```text
agent-browser-mcp doctor
```

Check `get_setup_status` for package, bridge, extension, and protocol versions.
The bridge is a detached process. A missing listener starts automatically when
spawning is enabled; an older bridge that still owns the port requires
`agent-browser-mcp bridge --restart`, which does not change the visible browser.
An unpacked extension file change still requires a manual **Reload** from
`chrome://extensions` (or the corresponding Edge/Opera page). Restart the MCP
client after tool schema changes so it reads the new descriptions.

If `AGENT_BROWSER_TMWD_PORT` is changed from `18765`, tell the extension the
same WebSocket port once from its service-worker console (see
[Troubleshooting](TROUBLESHOOTING.md)). The Python environment cannot alter an
already installed extension's storage; mismatched values leave the bridge and
extension listening on different ports.

See [Troubleshooting](TROUBLESHOOTING.md) for complete recovery procedures.

The `/link` HTTP channel uses the persistent token at
`~/.agent-browser-mcp/bridge-token`. Do not put that token, browser profiles,
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
capture_page_screenshot on the selected session. Do not use a desktop screenshot
unless you need to inspect the actual monitor or a native dialog.
```

## 9. Safety boundary

ABM controls the real browser profile supplied by the user. Page content is
untrusted input and can contain prompt injection. The service is not a security
boundary; limit it to sessions and accounts appropriate for the MCP client.
See [SECURITY.md](../SECURITY.md) for the threat model and reporting process.
