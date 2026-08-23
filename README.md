<!-- mcp-name: io.github.LinVireo/browsertap-mcp -->

# browsertap-mcp

English | [中文文档](https://github.com/LinVireo/browsertap-mcp/blob/main/README.zh-CN.md)

[![Offline CI](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE)

[Usage guide](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.md) · [Troubleshooting](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.md) · [Security](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md) · [Contributing](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.md) · [Changelog](https://github.com/LinVireo/browsertap-mcp/blob/main/CHANGELOG.md)

A Model Context Protocol (MCP) server that drives **the real Chrome you are already using**, through a Chrome extension and the Chrome DevTools Protocol. Your agent works inside your existing browser session, so logins, cookies, and open tabs are all already there — no separate sandbox browser to authenticate again.

Current release: unified Python package, bridge, and unpacked Chrome extension **0.4.6**.

It also reaches past the page: five direct tools provide real mouse and keyboard input at the OS level when page-level input is not enough. `resolve_leave_dialog` is one additional, narrowly scoped path that can send Enter after two protocol attempts fail. `safe` asks before physical input, while the default `lab` profile runs without elicitation and still enforces the cross-process lock, quiet-input gate, target activation, and on-screen confirmation.

## Start in 60 seconds

Three steps. Each one is spelled out in full under **Getting started** below,
with the Windows PowerShell paths and the config for every supported client.

```bash
# 1. Install from source. There is no PyPI release yet.
git clone https://github.com/LinVireo/browsertap-mcp.git && cd browsertap-mcp
python -m venv .venv && ./.venv/bin/python -m pip install -e ".[desktop]"
./.venv/bin/browsertap extension-path   # prints the directory step 2 needs

# 3. Point your MCP client at that same executable (Claude Code shown).
claude mcp add browsertap -- "$PWD/.venv/bin/browsertap"
```

On Windows the same three commands use `.\.venv\Scripts\python.exe` and
`.\.venv\Scripts\browsertap.exe`.

**Step 2 is manual, and it is the slow one.** There is no Chrome Web Store
listing yet, so the extension is loaded by hand: open `chrome://extensions`, turn
on **Developer mode**, click **Load unpacked**, and pick the directory
`extension-path` printed. Then open an ordinary `http://` or `https://` page --
`about:blank` runs no content script, so no session is established.

Then ask your agent *what tabs do I have open?* If the list comes back empty, run
`browsertap doctor`: it names one `cause` and the one matching `advice`.

## Key features

- **Real browser, real session** — attaches to your running Chrome/Edge/Opera. Logged-in sites, cookies, and page context are preserved.
- **Background by default** — a *selected* tab is not a *foreground* tab. `switch_tab` retargets without raising anything, and page work runs in the tab you named while you keep using the screen.
- **Page reading** — scan any page into simplified HTML or text, sized for a model's context. Long links are shortened to `#r1` refs and the real URLs come back alongside, so a results page stays both small and navigable.
- **JavaScript execution** — run arbitrary JS in the page.
- **Background page input** — `page_click`, `page_type`, `page_press`, and `page_drag` dispatch trusted CDP input events at *viewport* coordinates inside one named tab, without moving your cursor or changing which tab is visible.
- **Waiting and scrolling** — wait for a selector, text, URL, or JS condition; scroll and re-scan long pages. `scan_page` reports how much it left outside the viewport instead of dropping it silently.
- **Explicit dialog policies** — `alert`, `confirm`, `prompt`, and `beforeunload` each get a per-call `dismiss`/`accept`/`manual` policy and are reported truthfully; `handle_dialog` resolves one that is left open.
- **Temporary site permissions** — grant notifications, geolocation, camera, or microphone to one origin for 60–600 seconds; the prior setting is restored automatically.
- **Native CDP access** — single commands or batches. Addressable by tab, extension id, or target id.
- **Authenticated native downloads** — download attachments through Chrome's download manager with the active browser profile's cookies, wait for completion, and receive the verified local path.
- **Tab-less operation** — extension management, CDP target listing, and tab listing/closing go straight to the extension's service worker, so they work even with zero tabs open.
- Page **screenshots** — page capture via CDP is returned as MCP image content and can also be saved to disk; full desktop capture is available for physical-input checks. A model without image support must use `scan_page`, page APIs, or OCR to inspect content.
- **Guarded real physical input** — OS-level mouse move/click/drag, typing, and hotkeys are the last-resort path. `lab` can run without elicitation; `safe` prompts per call. Both profiles keep the lock, quiet-input gate, ownership checks, target activation, and on-screen confirmation.
- **Multi-browser** — Chrome, Edge, and Opera can all connect to one bridge at the same time without clobbering each other's sessions.

## Requirements

- Python 3.10+
- Chrome, Edge, or Opera
- Linux, macOS, or Windows. OS-level input on Linux requires an X11 desktop.
- Claude Code, or any other MCP client

## Getting started

### 1. Install

Clone the repository, create a virtual environment, and install the recommended
desktop feature set:

**Windows PowerShell**

```powershell
git clone https://github.com/LinVireo/browsertap-mcp.git
Set-Location browsertap-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
.\.venv\Scripts\browsertap.exe extension-path
```

**Linux or macOS**

```bash
git clone https://github.com/LinVireo/browsertap-mcp.git
cd browsertap-mcp
python -m venv .venv
./.venv/bin/python -m pip install -e ".[desktop]"
./.venv/bin/browsertap extension-path
```

The core install (`pip install -e .`) omits OS-level mouse/keyboard and desktop
capture dependencies. Use it only when those tools are intentionally disabled.
After the first PyPI release, `pip install "browsertap-mcp[desktop]"` will be
the non-editable install path; until then, the source install above is the
supported path.

### 2. Load the Chrome extension

This project ships an unpacked extension that has to be loaded once by hand.

```bash
browsertap extension-path
```

Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick the directory that command printed.
The loaded extension is listed as **BrowserTap Bridge**.

If you also use Edge or Opera, repeat the same steps at `edge://extensions` or `opera://extensions` with the same directory. The bridge tells the browsers apart automatically.

Then open a normal `http://` or `https://` page. A blank tab is not enough — content scripts cannot run on `about:blank`, so no session is established.

#### Connection status badge

The extension may show a small `BTAP: checking`, `BTAP: connected`, or
`BTAP: disconnected` badge on pages. The badge is presentation-only: it reports
the bridge connection state and does not display page content, cookies, tokens,
or URLs. Open the extension popup and clear **Show connection status on pages**
to hide it. Hiding the badge does not stop the bridge, keepalive, or automatic
reconnect behavior.

### 3. Add the server to your client

**Standard config** works in most tools:

```json
{
  "mcpServers": {
    "browsertap": {
      "type": "stdio",
      "command": "browsertap"
    }
  }
}
```

If you installed into a virtualenv, point `command` at the executable's absolute path instead — relying on `PATH` is the most common reason a client fails to start the server.

<details>
<summary>Claude Code</summary>

```bash
claude mcp add browsertap -- browsertap
```

Add `--scope user` to make it available across all projects. For a virtualenv install:

```bash
claude mcp add browsertap -- /absolute/path/to/.venv/bin/browsertap
```

On Windows PowerShell, use the absolute path to
`.venv\Scripts\browsertap.exe` instead.

Verify with `/mcp`.
</details>

<details>
<summary>Claude Desktop</summary>

Follow the MCP install [guide](https://modelcontextprotocol.io/quickstart/user) and use the standard config above. An example file is included at `examples/claude-desktop-config.json`.
</details>

<details>
<summary>Cursor</summary>

Put the standard config in `.cursor/mcp.json` for one project, or `~/.cursor/mcp.json` globally. An example file is included at `examples/cursor-mcp.json`.
</details>

<details>
<summary>VS Code</summary>

```bash
code --add-mcp '{"name":"browsertap-mcp","command":"browsertap"}'
```

Or write it into `.vscode/mcp.json` by hand — note that VS Code's key is `servers`, not `mcpServers`.
</details>

<details>
<summary>Hermes</summary>

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  browsertap:
    command: browsertap
    timeout: 120
    connect_timeout: 60
```

`browsertap print-hermes-config` prints this snippet. An example file is included at `examples/hermes-config.yaml`. Verify with `hermes mcp list`.
</details>

<details>
<summary>Other clients</summary>

Any MCP client that speaks stdio will work. Follow its own install guide and use the standard config above.
</details>

### Your first prompt

Once the extension is loaded and a normal page is open, try:

> What tabs do I have open? Read the current page and summarise it.

If tabs come back empty, run `browsertap doctor`.

For the least disruptive workflow, start with [`docs/USAGE.md`](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.md): it explains which operations stay in a background tab, when a desktop screenshot really means the monitor, and when an image-capable model is useful.

## Configuration

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `BROWSERTAP_BRIDGE_HOST` | `127.0.0.1` | Bridge bind address. |
| `BROWSERTAP_BRIDGE_PORT` | `18765` | WebSocket port. HTTP uses `PORT+1`, and `PORT+2` is a lock socket that keeps exactly one bridge *hosting* those two (a second bridge stays up and works through the first). Not to be confused with the separate `spawn.lock` file, which is what stops several MCP sessions from starting several daemons at once. For a custom port, also tell the extension once — see [docs/TROUBLESHOOTING.md](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.md). |
| `BROWSERTAP_NO_SPAWN` | unset | Set to `1` to stop the MCP server from auto-starting the bridge. Use it when you run the bridge yourself. |
| `BROWSERTAP_BRIDGE_AUTH` | enabled | Set to `off` only for an explicitly trusted local compatibility setup. By default BTAP authenticates `/link` with a persistent per-user token. |
| `BROWSERTAP_BRIDGE_TOKEN_FILE` | `~/.browsertap/bridge-token` | Override the shared token file location. Editors do not need individual token configuration. |
| `BROWSERTAP_BRIDGE_TOKEN` | unset | Legacy one-time migration source. If the token file does not exist, BTAP imports this value once; the file wins thereafter. |
| `BROWSERTAP_PREFERRED_BROWSER` | unset | `chrome`, `edge`, or `opera`. Which browser wins when several are connected and no tab is specified. |
| `BROWSERTAP_MODE` | `lab` | `lab` prioritizes uninterrupted automation and skips physical-input/site-allow elicitation; `safe` prompts for every such action. `set_automation_profile` changes only the current MCP process. |
| `BROWSERTAP_LAB_NO_ELICIT` | enabled | Lab skips elicitation by default. Set this to `0`/`false` only when you want session-level lab approval prompts; the cross-process lock, quiet-input gate, foreground confirmation, and ownership checks always apply. |
| `BROWSERTAP_AUTO_BEFOREUNLOAD_HOSTS` | `shell.,ttyd,code-server,jupyter,vscode-web` | In lab, ordinary `open_url` accepts beforeunload on matching current hosts. `intent_leave=false` always preserves the page. |
| `BROWSERTAP_WS_ALLOWED_ORIGINS` | unset | Comma-separated exact extra origins allowed to open the bridge WebSocket. Extension origins are allowed automatically; do not add broad or untrusted origins. |
| `BROWSERTAP_WS_ALLOW_NO_ORIGIN` | unset | Set to `1` only for a trusted non-browser local WebSocket client that cannot send `Origin`. The default rejects origin-less clients. |

### CLI

```bash
browsertap                      # run the MCP server (stdio)
browsertap extension-path       # print the unpacked extension directory
browsertap skill-path           # print the directory holding the shipped agent skills
browsertap doctor               # diagnose the local setup, as JSON
browsertap bridge               # run the bridge in the foreground
browsertap print-hermes-config  # print a Hermes config snippet
```

`doctor` reports the extension path, port state, and connected tab count. It also
returns a structured verdict: `cause` is one of `healthy`,
`ext_never_registered`, `sw_slept_or_dropped`, `registering`, or
`bridge_unreachable`, and `advice` is the matching one-line fix. `registering`
means the extension is connected but no normal `http(s)` content tab is ready.

BTAP creates `~/.browsertap/bridge-token` on first use and every bridge/MCP
process reads that same file. Closing browsers or editors does not rotate it. Removing
the browser extension or reinstalling the Python package deliberately leaves the token
file in place, so a reinstall continues to work. A full user-data purge may delete the
whole `~/.browsertap` directory only after all BTAP bridge processes have stopped;
the next start then creates a new token.

### Agent skills (optional)

BTAP ships two skills that tell a calling agent how to drive it. They are ordinary
Markdown and completely optional — every tool works without them. What they add is
the judgement the tool descriptions cannot carry: which tool to reach for first,
when `session_id` is mandatory, and which tabs belong to you and must be left
alone.

```bash
browsertap skill-path           # e.g. .../site-packages/browsertap_mcp/skills
```

That directory contains:

| Skill | What it is for |
|---|---|
| `browsertap-default/SKILL.md` | The calling contract: pick a target before acting, open your own tab for anything that mutates a page, close it in cleanup, and how to react to `no_response` / `switched_session` / `bridge_error`. |
| `browsertap-bridge-recovery/SKILL.md` | Recovery when the transport itself is down: which of the three components is stale, and the one restart or reload that fixes it. |

Point your client's skill manager **at that directory** rather than copying the
files. A copy looks correct for as long as the contents happen to agree, then
silently stops receiving updates when you upgrade the package. If you keep copies
anyway, `python -m scripts.check_tool_docs --check-installed-skills --skill-mirror
DIR` compares them against the shipped originals and names whichever one drifted.

### Upgrade

An upgrade is three steps, not one: the three parts do not become current at the
same moment, and step 3 fails silently if you skip it.

1. Update the package — `pip install -U browsertap-mcp` once it is on PyPI, or
   `git pull` in a source checkout. A new MCP session picks this up immediately.
2. `browsertap bridge --restart`. The daemon is long-lived and outlives
   every MCP session, so until it restarts it keeps serving the old code.
3. Open `chrome://extensions` and press **Reload** on the extension. Its files
   were replaced on disk, but Chrome keeps running the build it already loaded,
   and no command can make it re-read them.

`browsertap doctor` reports which part is stale and names the one action
that fixes it: `reload_extension`, `restart_bridge`, or `restart_mcp_session`.
The other two will not help, so read the field rather than doing all three.

### Uninstall

1. Stop the managed daemon with `browsertap bridge --stop`.
2. Open `chrome://extensions` (or the equivalent page in Edge/Opera) and remove the
   unpacked **BrowserTap Bridge** extension.
3. Remove the `browsertap` entry from each MCP client's configuration.
4. Run `pip uninstall browsertap-mcp` in the environment where it was installed. If
   you created a dedicated virtual environment, remove that specific environment after
   deactivating it.
5. Optional full cleanup: after confirming every BTAP bridge is stopped, remove
   `~/.browsertap`. This deletes the persistent bridge token and logs; the data is
   retained by default so reinstalling continues to work without reconfiguration.

## How it works

Three layers:

1. **Chrome extension** (MV3) — injected into real pages, reaches `tabs`, `cookies`, `debugger`, and `management` through Chrome APIs.
2. **BrowserBridge** — a local daemon on `127.0.0.1:18765` (WebSocket) and `:18766` (HTTP). It owns the extension connections, tracks sessions, and relays results. It runs detached from any MCP instance, and the MCP server starts it on demand with no console window. Sessions are keyed `clientId:tabId`, so several browsers and profiles coexist.
3. **MCP server** — exposes the whole thing as MCP tools.

Two channels reach the browser: a per-tab session channel, and a direct channel to the extension's service worker. The second one is why some tools keep working when every tab is closed.

## Behaviour you should know before driving it

**Selecting a tab does not raise it.** `switch_tab` defaults to `activate=false`: it only changes which tab later calls target. Nothing moves on screen until you call `activate_tab`, pass `switch_tab(activate=true)`, or approve a physical-input action. Page reading, JS, and the `page_*` input tools all work on a background tab.

**Two kinds of coordinates, two kinds of authority.** `page_click`/`page_drag` take **viewport** coordinates inside one tab and are dispatched through CDP — no cursor movement, no window focus, `foreground_changed: false` in the reply. `mouse_move`/`mouse_click`/`mouse_drag` take **desktop screen** coordinates and drive your real cursor. The two are not interchangeable, and a viewport coordinate pasted into `mouse_click` will land somewhere else entirely.

**Automation profiles.** With `BROWSERTAP_MODE` unset, BTAP defaults to `lab` with `BROWSERTAP_LAB_NO_ELICIT=1` semantics: physical input and site `allow` proceed without elicitation. `safe` prompts for every action. Both profiles keep the cross-process lock, quiet-input gate, target activation, ownership protection, and `on_screen` check, so higher authority never means stale or misdirected input. The quiet gate's reach is bounded by what the OS exposes rather than by the profile; `input_quiet.enforced` in the result says whether it could observe this machine at all.

**Dialogs are explicit.** `execute_js(dialog_policy=...)`, `open_url(beforeunload=...)`, and `handle_dialog(action=...)` take `dismiss` (default), `accept`, or `manual`. The global default still preserves the page; only an explicit accept or lab's configured shell/IDE host heuristic leaves automatically. `handle_dialog` answers within three seconds or reports `no_dialog`/an explicit error. `resolve_leave_dialog` tries protocol accept twice and uses physical Enter only as a final, lab-approved fallback.

**Permissions are leases, not grants.** `set_site_permission` covers one origin for 60–600 seconds, records the prior setting, and restores it on expiry/reset/service-worker restart. `safe` prompts for every `allow`; default `lab` applies it without elicitation. Browser capabilities that cannot be restored return `unsupported` or `requires_user_action`.

**Challenges stay in your browser.** A Cloudflare Turnstile or similar widget is handled in the same connected tab, by `page_click`, with a bounded number of attempts. When the challenge has not moved, the result is `challenge_stalled` and BTAP stops so you can finish it yourself in that same tab. BTAP never launches Playwright, a headless browser, or a separate automation profile as a fallback — the whole point is your real, logged-in session.

**Changed tools need a reload.** Tool schemas and descriptions are read once when your client starts the MCP server; after upgrading, restart the MCP session or your client, or you will keep calling the old signatures. Extension changes need a manual reload at `chrome://extensions` — `chrome.runtime.reload()` restarts the service worker without re-reading the files from disk.

### Tab ownership in concurrent tasks

Classify every tab before using it. A **U (user) tab** existed in the first `list_tabs` snapshot; do not close it or navigate it by default. An **A (agent) tab** is created by this task's `open_new_tab`; save its `session_id`, `generation`, and `owner_id`, pass that explicit session to every operation, and call `close_tabs(..., owner_id=...)` in cleanup. A **B (borrowed) tab** is a temporarily used U tab; record its `original_url`, restore that URL when the tab still exists, and never close it.

Decision order: run `list_tabs`; borrow an existing match only for read-only/light work; open an A tab for navigation, forms, or other state changes; open an A tab when no match exists; finally close only A tabs. Never register the initial tab snapshot as owned, close a U/B tab, depend on the shared default session, reuse an old native tab id, omit generation-aware cleanup, or leak an A tab. Separate concurrent tasks should use separate A tabs instead of competing for the same U tab.

### Structured statuses and recovery fields

Expected interruptions come back as a `status` field, not an exception:

| `status` | Meaning |
|---|---|
| `ok` / `success` | Completed and verified as far as the protocol allows. |
| `redirected` | Navigation landed on a different URL than requested (login wall, SSO, canonical rewrite). |
| `navigated` | An `execute_js` script navigated the page, so its return value is genuinely gone; `landed_url` says where it went. |
| `blocked_by_dialog` | A JavaScript dialog is open and waiting for `handle_dialog`. |
| `blocked_by_beforeunload` | Navigation was cancelled to keep the page; re-issue with `beforeunload="accept"` to leave. |
| `dialog_handle_failed` | A dialog was seen but answering it failed; the tab may still be blocked. |
| `navigation_failed` / `navigation_timeout` | `open_url` did not complete within its timeout, or the browser reported an error. |
| `triggered` with `type="download"` | `open_url` was replaced by a browser download. `ERR_ABORTED` can be normal only when CDP also reports `isDownload=true`; use `download_file` for completion and the local path. |
| `requires_user_action` | Approval was declined, cancelled, or unavailable — nothing was done. |
| `busy` | Another BTAP process holds the physical-input lock, or the tab already has a pending manual execution. Returned immediately, never queued. |
| `input_activity_detected` | You used the mouse or keyboard during the post-approval quiet window, so no physical input was sent. |
| `activation_failed` | The target tab could not be confirmed on screen, so no physical input was sent. |
| `unsupported` | The browser or extension API cannot provide this (e.g. clipboard permission leases). |
| `challenge_stalled` | A browser challenge made no progress within the attempt bound; hand the tab back to the user. |
| `no_response` | The script did not reach the tab or timed out — do not blindly retry anything with side effects. |
| `not_found` | The selector matched nothing; no input was dispatched. |
| `bridge_error` | A bridge call failed. It may appear as `error_code` or a diagnostic field rather than the top-level status; run `list_tabs`/`doctor` before retrying. |
| `switched_session` | Supplemental field indicating that only an implicit dead default was replaced with another live tab. Verify the new target before continuing; explicitly directed dead sessions are never substituted. |

## Disclaimers

This server drives your real browser and your real desktop. Anything it can do, you can do — and it inherits every session you are logged into.

- Mouse moves, clicks, typing, and hotkeys are real OS-level input, not synthetic page events. `safe` prompts per call; `lab` can reuse or disable prompts. Once allowed, it drives your actual desktop.
- Page content is untrusted input. A page your agent reads can attempt prompt injection, and the tools available make that consequential.
- This is **not** a security boundary. See [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices).
- Avoid pointing it at sensitive accounts you would not want an MCP client to see, and prefer not to run it on shared or production machines.

The extension requests broad permissions because the feature set requires them:
`cookies`, `tabs`, `debugger`, `scripting`, `alarms`, `storage`,
`contentSettings`, `declarativeNetRequest`, `management`, `bookmarks`,
`downloads`, and `<all_urls>`. `declarativeNetRequest` temporarily removes CSP
response headers only from the tab executing an eval-based command. The rule is
session-scoped, reference-counted, and removed in cleanup; it is not a
browser-wide persistent CSP override. See [Security](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md) for the full
permission and loopback threat model.

## Tools

Most tools accept an optional `session_id` to target one specific tab; omitting it uses the current target. Pass it explicitly for anything that changes state — the shared default is a single value every task on this bridge sees, and another task retargeting it is exactly how a click lands on the wrong page. Session ids look like `chrome_a1b2c3:456`; pass them verbatim and never split them. Tools marked **no tab needed** talk to the extension's service worker and work with zero tabs open.

<details>
<summary><b>Tabs and navigation</b></summary>

- **get_setup_status** — report `package_version`, `bridge_version`, `extension_version`, `protocol_version`, connection state, ports, tabs, and the required recovery action. A missing bridge listener is started automatically when spawning is enabled; `restart_bridge_required=true` means a bridge that is still running must be replaced with `browsertap bridge --restart`. `reload_extension_required=true` identifies the unpacked-extension platform limit and requires a manual Reload. `restart_mcp_session_required=true` is the opposite direction: a component is *newer* than the running server, so the stale build is this process and only restarting the MCP session or client clears it — the other two flags stay false, because a restart or reload would report the same mismatch again. No parameters.
- **get_automation_profile** — inspect whether the current MCP process uses `lab` or `safe`.
- **set_automation_profile** — switch the current MCP process between `lab|safe`; the override is not persisted and does not reload the extension.
  - `mode` (string): `lab` or `safe`
- **list_tabs** — list connected tabs. Each carries a `browser` field. No parameters.
- **list_all_tabs** — *(no tab needed)* list every open tab, including `chrome-extension://` pages that `list_tabs` hides. Those never become sessions, so they have no session id; drive them with `cdp_command(tab_id=...)`.
  - `session_id` (string, optional): which browser to ask.
- **switch_tab** — set the *target* tab for later calls. A `url_pattern` must match exactly one tab; if several match, select one with its full `session_id`. It does **not** raise the tab or focus the browser: `activate` defaults to `false`, so retargeting never disturbs what you are looking at. Pass `activate=true`, or call `activate_tab`, when you actually need the tab in front.
  - `session_id` (string, optional), `url_pattern` (string, optional): substring match, `browser` (string, optional): `chrome`, `edge`, or `opera`, `activate` (boolean, optional): default `false`.
- **activate_tab** — bring a tab to the foreground and focus its window. This is the explicit way to raise a tab, and the only one that does not involve approving physical input. Check `on_screen` in the reply: BTAP first asks Windows to restore a minimised browser, but `on_screen=false` means visibility still could not be confirmed and screen-coordinate input must not be sent.
  - `session_id` (string, optional)
- **open_url** — navigate the current tab. Global behavior remains `dismiss`; lab automatically accepts beforeunload on configured shell/IDE hosts. If the extension's `navigate` route is unavailable on a heavy SPA, BTAP falls back to `Page.navigate`. A CDP result with `isDownload=true` returns `{type:"download",status:"triggered"}` instead of only `navigation_failed`; the accompanying `ERR_ABORTED` is normal for that download navigation.
  - `url` (string), `session_id` (string, optional), `timeout` (number, optional): default `15`, `beforeunload` (string, optional): default `dismiss`, `intent_leave` (boolean, optional): `false` forces page preservation
- **download_file** — download an HTTP(S) URL through Chrome's native download manager, using that browser profile's cookies and authenticated session. It waits by default and returns `status="completed"` plus a verified absolute `path`; interrupted downloads return `failed`, while a timeout or `wait=false` returns `in_progress` with `download_id`. An explicit `session_id` must still be live and is never replaced with another profile. Use this for attachments instead of page `fetch`.
  - `url` (string), `filename` (string, optional): relative download name, `directory` (string, optional): arbitrary absolute destination directory; creates parents, `wait` (boolean, optional): default `true`; `directory` requires `true`, `timeout` (number, optional): default 60 seconds, maximum 1800, `session_id` (string, optional): selects the browser profile, `overwrite` (boolean, optional): default `false`; an existing final destination raises an error unless explicitly `true`. If a directory download times out, `directory_applied=false`: the move is no longer tracked and Chrome may finish into its default download directory.
- **open_new_tab** — open a background tab by default with a unique `operation_id` and wait a bounded time for exact session/generation registration; pass `active=true` only when foreground work is genuinely required. Returns `{operation_id,tab_id,session_id,generation,ready,owned,opener,owner_id,load_status}`. The extension deduplicates repeated requests with the same operation id. Ownership is registered only from a completed record containing the exact `client_id+tab_id+generation`, even when `ready=false`; `ready` only says whether session-scoped tools can be used immediately. A pre-create registry uncertainty returns `status="unknown",may_have_created=false,retry_safe=true`; after create dispatch, an unresolved ACK/reconciliation returns `status="unknown",may_have_created=true,retry_safe=false`. Keep its random `owner_id` capability and use it only for that task's cleanup. For an unresolved dispatched create, do not call `open_new_tab` again for the same request; retain `operation_id` as diagnostic/support evidence.
  - `url` (string), `timeout` (number, optional): default `15`, `active` (boolean, optional): default `false`, `session_id` (optional browser/profile selector), `owner_id` (optional capability to group several tabs under one task owner)
- **close_tabs** — *(no tab needed)* accept native numeric tab ids or full `client:tabId` session ids, including `chrome-extension://` tabs. The default `only_if_agent_owned=true` requires the `owner_id` returned by `open_new_tab` and verifies the current lifecycle generation before closing, so pre-existing user tabs and another agent's tabs are refused. If the user already closed an owned tab, cleanup returns `status=already_gone, closed_by=user` without reusing its native id. An actual owned close returns `closed_by=agent`; an explicit unowned/operator override returns `closed_by=none` so it is not counted as task-owned cleanup. Set `only_if_agent_owned=false` only when the operator explicitly asked to close an unowned/user tab.
  - `tab_id`, `session_id` (optional browser constraint), `owner_id` (required by the safe default), `only_if_agent_owned` (boolean, default `true`)
</details>

<details>
<summary><b>Page reading and execution</b></summary>

- **scan_page** — read the page as simplified HTML or text. Returns `links` mapping each `#rN` ref in the content to its absolute URL, and `offscreen` + `hint` when content was left outside the viewport.
  - `session_id` (string, optional), `text_only` (boolean, optional): default `false`, `cutlist` (boolean, optional): default `true`; collapse repetitive lists, `maxchars` (integer, optional): default `35000`, `instruction` (string, optional), `extra_js` (string, optional), `timeout` (number, optional): default `15`
- **wait_for** — wait until a condition holds, then return. Use this instead of polling `scan_page`, which re-serializes the whole DOM each time. Polling happens inside the page, so a 30s wait still costs one bridge roundtrip. Exactly one condition is required. `selector` accepts legacy CSS or the structured locator object described under background page input.
  - `selector` (string/object, optional): CSS or structured locator, `text` (string, optional): substring of body text, `url_pattern` (string, optional): regex on the URL, `js` (string, optional): expression to become truthy, `gone` (boolean, optional): wait for the condition to stop holding; default `false`, `timeout` (number, optional): default `15`, `session_id` (string, optional)
- **wait_for_url** — wait for navigation to settle: blocks until the tab URL matches `url_pattern` (regex, or plain substring — both are tried) and, unless `wait_ready=false`, `document.readyState` is `complete`; then returns final `url`, `title` and `ready_state`. Use after a click or `open_url` that navigates; `wait_for(url_pattern=...)` only checks the URL and can return while the new document is still blank. Polls in-page across navigation chunks, so a long wait is still cheap.
  - `url_pattern` (string): regex or substring to match against the URL, `timeout` (number, optional): default 15, `wait_ready` (boolean, optional): require `readyState === 'complete'`, default `true`, `session_id` (string, optional)
- **scroll_page** — scroll and report the new position, so a long page can be read in passes.
  - `to` (string, optional): default `bottom`; also accepts `top`, a pixel offset, or a CSS selector to bring into view, `session_id` (string, optional), `timeout` (number, optional): default `15`
- **execute_js** — run JavaScript in the page and return the result. `timeout` is one end-to-end deadline covering dialog-policy setup, monitor snapshots, delivery/retry, navigation inspection, and cleanup; an explicit `session_id` is forwarded through every one of those roundtrips instead of relying on the shared default. When a script navigates the page, `status` is `navigated` (not `success`) with `landed_url`; the script's return value is genuinely lost in that case and is reported as such rather than substituted. `dialog_policy` decides what happens if the script opens `alert`/`confirm`/`prompt`: `dismiss` (default) and `accept` answer it and report it under `dialogs`, while `manual` pauses the script with the native dialog still open and returns `blocked_by_dialog` — call `handle_dialog` to release it. A tab already holding a manual pause returns `busy` immediately. Use `wait_for`/`wait_for_url` instead of delayed `setTimeout` or sleep Promises; `no_response` reports `delivery_state` and `retry_safe`, and BTAP never replays an acknowledged script whose side effects may already have run.
  - `script` (string), `session_id` (string, optional), `no_monitor` (boolean, optional): default `false`, `timeout` (number, optional): default `15`, `dialog_policy` (string, optional): `dismiss` (default), `accept`, or `manual`
- **handle_dialog** — inspect or answer a dialog left open on a tab. `action="manual"` reports it without choosing (`blocked_by_dialog`, or `no_dialog` if nothing is open); `accept`/`dismiss` answer it and release any paused `execute_js` or `open_url`. `prompt_text` supplies the text for an accepted `prompt`.
  - `action` (string), `prompt_text` (string, optional), `session_id` (string, optional), `timeout` (number, optional): default `3`, capped at three seconds
- **resolve_leave_dialog** — for an already-open shell/ttyd/IDE leave prompt: two protocol accepts, then physical Enter only when lab permits it.
  - `session_id` (string, optional)
- **upload_files** — set files on a file input, which JavaScript cannot do (`input.files` is read-only). Runs as one CDP batch so the DOM node ids stay valid across the sequence.
  - `selector` (string): the `<input type=file>`, `paths` (string or array of strings): absolute local paths, `session_id` (string, optional), `timeout` (number, optional): default `30`
- **get_cookies** — read cookies for a page.
  - `session_id` (string, optional), `tab_id` (integer, optional)
- **set_cookies** — write cookies into the real browser profile. Takes one cookie object or a list (JSON text is accepted): `name` is required, plus optional `value`/`url`/`domain`/`path`/`expires` (Unix seconds)/`httpOnly`/`secure`/`sameSite`. Uses CDP `Network.setCookie`, so HttpOnly and cross-path cookies work; falls back to `document.cookie` only when CDP is unavailable, and then reports which cookies could not carry HttpOnly. Cookies with neither `url` nor `domain` are scoped to the current page.
  - `cookies` (string or list or dict), `session_id` (string, optional), `tab_id` (integer, optional), `timeout` (number, optional): default `20`
- **delete_cookies** — delete a cookie by name. Uses CDP `Network.deleteCookies`, falling back to expiring it via `document.cookie`. Scope with `domain`/`path`, or `url` to target one site.
  - `name` (string), `domain` (string, optional), `path` (string, optional), `url` (string, optional), `session_id` (string, optional), `tab_id` (integer, optional), `timeout` (number, optional): default `20`
- **storage_get** — read localStorage or sessionStorage. Omit `key` to page with `offset`/`max_items`/`max_bytes`; returns `next_offset` and `truncated`. The default timeout is 30s and a failed call does not close the MCP session.
  - `key` (string, optional), `area` (string, optional): `local` (default) or `session`, `session_id` (string, optional), `timeout` (number, optional): default `30`, `offset` (integer, optional), `max_items` (integer, optional), `max_bytes` (integer, optional)
- **storage_set** — write one localStorage/sessionStorage value (non-string values are JSON-encoded first). Verifies by read-back, so a quota-full or privacy-mode failure is reported instead of silently lost.
  - `key` (string), `value` (string), `area` (string, optional): `local` (default) or `session`, `session_id` (string, optional), `timeout` (number, optional): default `30`
</details>

<details>
<summary><b>Background page input</b></summary>

Trusted CDP input events delivered to one named tab. They do **not** activate the tab, focus its window, or move the desktop cursor — every reply carries `foreground_changed: false` and `input_mode: "cdp"`. All coordinates are **viewport** coordinates (relative to the top-left of the page area), never desktop coordinates.

Pass `session_id` explicitly: the call binds the driver to that tab for its duration and restores the shared default afterwards, so a directed call cannot leave another task's target moved. A `session_id` naming a dead tab is refused rather than redirected to a live one.

`selector` remains backward-compatible with CSS strings and also accepts a locator object with exactly one primary key: `css`, `role` (optional `name`), `text`, or `label`. `exact` applies to role/name or text matching; `frame` walks one or more same-origin iframe locators; `shadow` walks open Shadow DOM hosts. Zero matches return `not_found`, multiple matches return `ambiguous`, and cross-origin or closed roots are reported without dispatching input.

- **page_click** — click a CSS/structured `selector` or viewport coordinates. Exactly one targeting mode: either `selector`, or both `x` and `y`. With a selector, each omitted offset axis uses the element centre; a supplied `offset_x` or `offset_y` is measured from the element's top-left corner on that axis. Missing, ambiguous, non-interactable, cross-origin-frame, and closed-shadow targets return structured status without input dispatch. In selector mode the point is also hit-tested in the page before dispatch: a target below the fold is scrolled into view (`scrolled_into_view`), one whose pixel belongs to something else returns `obscured` with `occluded_by` naming the overlay, and one still off screen returns `outside_viewport` — in both cases nothing is clicked, because a dispatched click would have landed on the other element and reported success. A verified click carries `hit_verified: true`. Coordinate mode is not hit-tested: coordinates name a pixel, not an element. Challenge replies keep the bounded `challenge_detected`/`attempts`/`challenge_stalled` behavior.
  - `selector` (string/object, optional), `x` (number, optional), `y` (number, optional), `offset_x` (number, optional), `offset_y` (number, optional), `button` (string, optional): default `left`, `clicks` (integer, optional): default `1`, `session_id` (string, optional), `timeout` (number, optional): default `15`
- **page_type** — insert text into a CSS/structured-locator field, or into whatever already has focus when `selector` is omitted. Xterm.js containers/descendants retarget to `.xterm-helper-textarea`. Missing, ambiguous, read-only, or otherwise unusable targets return a structured status without dispatching text or keys. `clear=true` selects the existing value first; `submit_key` sends one key afterwards.
  - `text` (string), `selector` (string/object, optional), `clear` (boolean, optional): default `false`, `submit_key` (string, optional), `session_id` (string, optional), `timeout` (number, optional): default `15`
- **page_press** — press a key or a comma-separated modifier chord in the tab, e.g. `enter` or `ctrl,shift,k`.
  - `keys_csv` (string), `session_id` (string, optional), `timeout` (number, optional): default `15`
- **page_drag** — drag between two viewport points as one uninterrupted event sequence.
  - `x1` (number), `y1` (number), `x2` (number), `y2` (number), `duration` (number, optional): default `0.3`, `button` (string, optional): default `left`, `session_id` (string, optional), `timeout` (number, optional): default `15`
</details>

<details>
<summary><b>Site permissions</b></summary>

Temporary, origin-scoped permission leases backed by `chrome.contentSettings`. Every lease records the prior setting and restores it — on expiry, on explicit reset, and after a service-worker restart or browser restart.

- **set_site_permission** — set one permission for one origin, for 60–600 seconds. Supported: `notifications`, `geolocation` (or `location`), `camera`, `microphone`. `setting` is `allow`, `block`, or `ask`. In `safe`, every `allow` requires approval; default `lab` applies it without elicitation (`BROWSERTAP_LAB_NO_ELICIT=1` semantics). Declining returns `requires_user_action` and changes nothing. `clipboard` returns `unsupported`, because its exact prior state cannot be restored. Omit `origin` to use the target tab's current origin; only `http`/`https` origins are accepted.
  - `permission` (string), `setting` (string): `allow`, `block`, or `ask`, `origin` (string, optional): defaults to the tab's origin, `duration_seconds` (integer, optional): 60–600, default `300`, `session_id` (string, optional)
- **reset_site_permissions** — restore matching leases now instead of waiting for expiry. Omit both `origin` and `permission` to restore every lease on that browser.
  - `origin` (string, optional), `permission` (string, optional), `session_id` (string, optional)
</details>

<details>
<summary><b>CDP</b></summary>

- **cdp_command** — send one CDP command.
  - `method` (string): e.g. `Page.navigate`, `params_json` (string, optional): JSON object as text, `session_id` (string, optional), `tab_id` (integer/string, optional), `extension_id` (string, optional), `target_id` (string, optional), `timeout` (number, optional): default `20`
- **cdp_batch** — send a batch; `batch_json` must be a JSON object with `cmd: "batch"`.
  - `batch_json` (string), `session_id` (string, optional)
- **debugger_targets** — *(no tab needed)* list every CDP-attachable target, including service workers and extension background pages that `list_tabs` never shows.
  - `session_id` (string, optional)
- **save_pdf** — bounded `Page.printToPDF`; validates PDF bytes and atomically writes `save_path`. A timeout forcibly releases its debugger lease.
  - `save_path` (string), `session_id` (string, optional), `landscape` (boolean, optional): default `false`, `print_background` (boolean, optional): default `true`, `prefer_css_page_size` (boolean, optional): default `true`, `scale` (number, optional): default `1.0`, range `0.1`–`2.0`, `page_ranges` (string, optional), `timeout` (number, optional): default `30`

> **On driving *other* extensions:** Chrome refuses cross-extension debugging at attach time, and all three addressing forms (`tab_id`, `extension_id`, `target_id`) are rejected alike unless Chrome was started with `--silent-debugger-extension-api`. These parameters are for this extension's own targets and for diagnosis.
</details>

<details>
<summary><b>Extension management</b></summary>

- **extension_path** — absolute path of the unpacked extension, for manual install. No parameters.
- **list_extensions** — *(no tab needed)* installed extensions with id, name, enabled state, type, and version.
  - `session_id` (string, optional)
- **set_extension_enabled** — *(no tab needed)* enable or disable an installed extension. Chrome exposes no API to *install* one, so this only toggles what is already there.
  - `extension_id` (string), `enabled` (boolean), `session_id` (string, optional)
- **uninstall_extension** — *(no tab needed)* uninstall another extension. Confirmation defaults on; set it off only for an explicitly selected disposable/test extension. BTAP cannot uninstall itself through its active response channel.
  - `extension_id` (string), `show_confirm_dialog` (boolean, optional): default `true`, `session_id` (string, optional)
- **get_bookmarks** — *(no tab needed)* read the bookmark tree.
  - `session_id` (string, optional)
- **create_bookmark** — *(no tab needed)* create a bookmark or folder.
  - `title` (string), `url` (string, optional): omit to create a folder, `parent_id` (string, optional), `session_id` (string, optional)
- **remove_bookmark** — *(no tab needed)* remove a bookmark or folder subtree.
  - `bookmark_id` (string), `recursive` (boolean, optional): default `false`, `session_id` (string, optional)
- **call_extension** — *(no tab needed)* send JSON to another enabled extension; the target must allow BTAP via `externally_connectable`.
  - `extension_id` (string), `message_json` (string): JSON payload as text, `session_id` (string, optional)
</details>

<details>
<summary><b>Network and console capture</b></summary>

- **network_capture_start** — start collecting bounded request/response records and optional bodies. Defaults: 500-entry ring and 256 KiB per body.
  - `session_id` (string, optional), `include_bodies` (boolean, optional): default `true`, `max_entries` (integer, optional): default `500`, range 10–2000, `max_body_bytes` (integer, optional): default `262144`, range 1024–2097152, `body_timeout` (number, optional): default `5`, range 0.1–10 seconds, `timeout` (number, optional): default `10`
- **network_capture_stop** — return the current capture and release its debugger lease; always call it in cleanup. Returned records can be filtered without changing capture bounds or cleanup. `url_pattern` is compiled by the browser as a JavaScript `RegExp`; invalid patterns return a structured error and leave the capture running for retry.
  - `session_id` (string, optional), `url_pattern` (string, optional): JavaScript `RegExp`, `resource_type` (string, optional), `status_min`/`status_max` (integer, optional): 100–599, `include_response_bodies` (boolean, optional): default `true`, `timeout` (number, optional): default `10`
- **console_capture_start** — start collecting `console.*` and uncaught exceptions.
  - `session_id` (string, optional), `max_entries` (integer, optional): default `500`, range 10–5000, `timeout` (number, optional): default `10`
- **get_console_messages** — page through or clear the current console buffer. `filter='user'` retains page MAIN/default-context output and excludes isolated extension/content-script contexts; empty/`all` preserves the complete buffer.
  - `session_id` (string, optional), `offset` (integer, optional): default `0`, `max_items` (integer, optional): default `200`, `clear` (boolean, optional): default `false`, `filter` (string, optional): `user` or `all`, `timeout` (number, optional): default `10`
- **console_capture_stop** — return the remaining console messages and release its debugger lease.
  - `session_id` (string, optional), `timeout` (number, optional): default `10`
</details>

<details>
<summary><b>Screenshots</b></summary>

- **capture_page_screenshot** — page capture via CDP with viewport, `full_page`, or explicit `clip` modes. PNG, JPEG, and WebP are supported; `quality` is valid only for JPEG/WebP. Returns text metadata plus attached MCP image content; `save_path` only adds a disk copy. Base64 is omitted unless explicitly requested.
  - `session_id` (string, optional), `tab_id` (integer, optional), `format` (string, optional): default `png`, `full_page` (boolean, optional): default `false`, `clip` (object, optional): `x`,`y`,`width`,`height`, optional `scale`, `quality` (integer, optional): 0–100 for JPEG/WebP, `save_path` (string, optional), `return_base64` (boolean, optional): default `false`, `timeout` (number, optional): default `20`
- **capture_desktop_screenshot** — captures the currently visible OS virtual desktop across all displays and returns metadata plus MCP image content. This is not a selected/background-tab capture; it may include other applications. `save_path` only adds a disk copy.
  - `save_path` (string, optional), `return_base64` (boolean, optional): default `false`
</details>

<details>
<summary><b>Physical input</b></summary>

Real OS-level input at **desktop screen** coordinates. It moves your actual cursor and types into whatever has focus. Prefer the `page_*` tools: they are precise, do not interrupt you, and work on a background tab. Reach for these only when page input genuinely cannot work — browser chrome, native file pickers, extension popups, OS dialogs.

In `safe`, each of these five direct tools asks through MCP elicitation. Default `lab` uses `BROWSERTAP_LAB_NO_ELICIT=1` semantics and does not prompt; setting it false restores session-level lab approval. Decline, cancel, or unavailable elicitation returns `requires_user_action`; every profile still enforces the lock, quiet window, ownership, activation, and foreground check. `resolve_leave_dialog` is a sixth physical-input path, limited to a final Enter fallback after two protocol-level attempts and subject to the same gate.

After approval the sequence is fixed: take the cross-process lock (contended → `busy`, returned immediately, never queued), wait out a short quiet window (you touched the mouse or keyboard → `input_activity_detected`, nothing sent), then raise the target tab, then act. What that window can actually detect depends on the OS: only Windows exposes a last-input timestamp, and the pointer position is unavailable under Wayland, in a headless container, and on macOS without the accessibility permission. With no signal at all the window still elapses but has nothing to compare, so every result carries an `input_quiet` block naming the markers it sampled, with `enforced: false` when there were none — on such a machine read a pass as unverified rather than as an idle desktop. All five direct tools take `session_id` — the same one you pass every other tool — and raise that tab; without one they fall back to the shared global target, which another task may have changed. Use `activate_session="none"` only for intentional input to the already-visible desktop or native UI. If the tab cannot be confirmed on screen the result is `activation_failed` and no input is sent, so a minimised window produces an error rather than a click into the wrong place.

- **mouse_move** — `x` (integer), `y` (integer), `duration` (number, optional): glide time in seconds, default `0` (jumps straight to the point), `session_id` (string, optional): tab to raise, `activate_session` (string, optional): default `current` (raise the target tab first), a session id to raise a different tab, or `none`
- **mouse_click** — `x` (integer, optional), `y` (integer, optional): omit both to click wherever the cursor already is, `button` (string, optional): default `left`, also `right` or `middle`, `clicks` (integer, optional): default `1`, `interval` (number, optional): seconds between clicks, default `0.1`, `session_id` (string, optional): the tab to raise, and what you should normally pass, `activate_session` (string, optional): default `current`, a session id, or `none`
- **mouse_drag** — `x1` (integer), `y1` (integer), `x2` (integer), `y2` (integer), `duration` (number, optional): seconds spent moving with the button held, default `0.3`, `button` (string, optional): default `left`, `session_id` (string, optional): tab to raise, `activate_session` (string, optional): default `current`, a session id, or `none`
- **type_text** — `text` (string), `interval` (number, optional): seconds per keystroke, default `0.01`, `click_x` (integer, optional), `click_y` (integer, optional): click there first to focus the field, `session_id` (string, optional): the tab to raise, and what you should normally pass, `activate_session` (string, optional): default `current`, a session id, or `none`
- **hotkey** — `keys_csv` (string): comma-separated, e.g. `ctrl,c`, `session_id` (string, optional): tab to raise, `activate_session` (string, optional): default `current`, a session id, or `none`
- **pointer_info** — current cursor position and screen size. Read-only, no approval needed. No parameters.
</details>

## Troubleshooting

Run `browsertap doctor` first. For connection, version, dialog,
permission, and physical-input recovery procedures, see the dedicated
[troubleshooting guide](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.md).

## Credits

BTAP is maintained by `LinVireo`. The MIT copyright notice in
[LICENSE](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE) is retained
unchanged (`zhea`); maintenance and copyright attribution are distinct roles. The
canonical public repository for this distribution is `LinVireo/browsertap-mcp`.

A small part of the browser layer here originates in
[GenericAgent](https://github.com/lsdefine/GenericAgent). Thanks to that project and
its author for the original implementation. The files listed below started there and
have each been substantially rewritten since; everything else in this distribution --
the MCP tool surface, the bridge and its token authentication, the Chrome extension,
the release evidence pipeline, the test suite, and both READMEs -- was written here.

Originally from GenericAgent:
- `TMWebDriver.py` (now maintained as `browser_bridge.py`)
- `simphtml.py`
- the `tmwd_cdp_bridge` Chrome extension resources

If you fork or redistribute this, please keep the attribution.

## License

MIT
