<!-- mcp-name: io.github.LinVireo/browsertap-mcp -->

# browsertap-mcp

English | [中文文档](https://github.com/LinVireo/browsertap-mcp/blob/main/README.zh-CN.md)

[![Offline CI](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/LinVireo/browsertap-mcp/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE)

[Usage guide](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.md) · [Troubleshooting](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.md) · [Security](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md) · [Privacy](https://github.com/LinVireo/browsertap-mcp/blob/main/PRIVACY.md) · [Contributing](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.md) · [Changelog](https://github.com/LinVireo/browsertap-mcp/blob/main/CHANGELOG.md)

**Browser automation for the Chrome, Edge, or Opera you already use.**
BTAP connects your MCP client to a browser extension, reusing your open tabs and
logged-in profile. Your agent can read pages, fill forms, download attachments,
and inspect network activity. Page input runs in the selected tab in the
background by default, without moving your desktop cursor.

BTAP controls a real browser profile, not a disposable sandbox. Give it access
only to accounts and data that the connected agent may use. First-time extension
installation is manual; [setup](#start-in-60-seconds) and [security](#disclaimers)
are below.

## Start in 60 seconds

Three setup steps; the manual browser step may take longer than a minute.

1. **Install** in a virtual environment and locate the extension:

   ```bash
   python -m venv .venv
   ./.venv/bin/python -m pip install browsertap-mcp
   ./.venv/bin/browsertap extension-path
   ```

   On Windows PowerShell, use `.\.venv\Scripts\python.exe` and
   `.\.venv\Scripts\browsertap.exe`. The optional desktop fallback requires
   `pip install "browsertap-mcp[desktop]"` in that same environment; ordinary
   page and browser tools do not need it.

2. **Load the extension manually.** Open `chrome://extensions`, enable
   **Developer mode**, choose **Load unpacked**, and select the printed
   directory. Open a normal `http://` or `https://` page for page tools.

3. **Connect your MCP client** to the installed executable. For Claude Code:

   ```bash
   claude mcp add browsertap -- "$PWD/.venv/bin/browsertap"
   ```

   Other clients and Windows paths are covered in
   [Getting started](#getting-started). Virtual-environment activation is not
   required when using explicit executable paths.

Ask the agent: **"List my open tabs, then summarize the page I select without
navigating or closing it."** If the connection fails or no expected tab appears,
run `browsertap doctor` from the installed environment and follow its `action`.
Later commands use the short name `browsertap`; use its full path when it is not
on your `PATH`.

## Documentation by audience

| Reader | Start here |
| --- | --- |
| Installing or using BTAP | This README, then the [usage guide](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.md) for workflows and boundaries. |
| Diagnosing a local setup | [Troubleshooting](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.md), with the output of `browsertap doctor`. |
| An agent calling BTAP tools | The client's live tool schemas and the optional [caller skills](#agent-skills-optional). |
| A human or agent changing BTAP | [Contributing](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.md); coding agents also read [AGENTS.md](https://github.com/LinVireo/browsertap-mcp/blob/main/AGENTS.md). |

The [Tools](#tools) section is the complete parameter reference for this source
tree. For an installed release, use documentation from its matching tag;
development checkouts can contain unreleased changes. Runtime capabilities
come from the connected server, not from a different version of the README.

## Key features

- **Read and inspect pages:** simplified HTML/text, JavaScript, screenshots, and bounded network/console captures.
- **Interact in a background tab:** `page_click`, `page_type`, `page_press`, and `page_drag` use trusted CDP input without moving the desktop cursor.
- **Use the existing profile:** authenticated downloads, cookies, storage, bookmarks, extensions, and temporary site-permission leases.
- **Control interruptions:** explicit dialog policies, condition-based waiting, and operation handles for collecting delayed results without replaying an action.
- **Separate concurrent tasks:** explicit browser/tab targets and owner-aware cleanup. Different tabs can run concurrently; shared-profile state is not isolated.
- **Connect multiple browsers:** one bridge can serve Chrome, Edge, Opera, and multiple profiles. Browser-level operations can work without a page tab.

## Capability model

BTAP exposes three capability layers so an agent can choose the narrowest
interface that matches the task:

- **Page capability** — DOM inspection, JavaScript, waiting, scrolling, screenshots, and
  `page_*` CDP input inside a named tab. This is the default path for ordinary
  web workflows and does not use the operating system mouse or keyboard.
- **Browser capability** — the real Chromium profile and its browser-native surface:
  tabs, downloads, cookies, storage, permissions, bookmarks, extensions,
  service-worker messaging, and raw CDP. These operations can work without a
  foreground tab.
- **Desktop capability** — not a general public surface. The only current opt-in
  is the lab-only `resolve_leave_dialog` final Enter fallback for a page leave
  dialog; browser chrome, native file pickers, extension UI, print/save dialogs,
  and other page-layer gaps remain unsupported. The seven global OS
  input/screenshot tools removed in 0.5.0 are not coming back as an ordinary
  web-workflow fallback.

`resolve_leave_dialog` remains a page-scoped, lab-only recovery workflow; its
final Enter fallback is a restricted exception, not a general desktop surface.
The live registry is returned in `get_setup_status`'s `data.capability_registry`, so a
client can inspect the actual tool inventory instead of guessing from package
extras or documentation. Each entry also reports whether a target is none,
optional, or required, whether the operation reads, writes, or may do either,
and whether desktop opt-in is involved. Every public tool now returns the
`btap.result.v1` envelope without renaming tools: successful operation data is
in `data`, explicit legacy failure payloads remain in `legacy`, and
`error`/`error_code`, `retryable`, `target`, and `diagnostics` provide stable
machine-readable status. Failures also set MCP `isError=true`. Arrays, objects,
HTML, and other large bodies are read from `data` or `legacy`; only small scalar
operation fields are projected at the top level for compatibility. Use the
envelope's retry verdict: an explicit `retry_safe=false` or possible execution
overrides a connection error's usual retry hint.

## When to use something else

Use BTAP when the task depends on your existing Chromium profile and should
normally stay in a background tab. It is not a headless test runner, does not
support Firefox/WebKit, and does not provide general desktop automation.

For isolated browser tests or accessibility-snapshot-based workflows, compare
[Playwright MCP](https://github.com/microsoft/playwright-mcp). For DevTools-led
debugging and performance analysis, compare
[Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp).
Reusing a live browser is not unique to BTAP; choose by the task and the APIs
you need. BTAP exposes all 49 tools, so clients that need a smaller tool set
must filter it themselves.

## Requirements

- Python 3.10+
- Chrome, Edge, or Opera
- Linux, macOS, or Windows. The ordinary page, browser, and CDP tools do not
  require OS-level input; only the lab-only `resolve_leave_dialog` fallback
  needs a usable desktop session.
- A running Chromium user session rather than an isolated headless container.
  There is no Docker image on purpose: the server attaches to the Chrome *you*
  are signed into, through an extension a human loads once.
- Claude Code, or any other MCP client

## Getting started

### 1. Install

Create a virtual environment and install the package. The optional `desktop`
extra is only needed for the remaining lab-only physical fallback:

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install browsertap-mcp
.\.venv\Scripts\browsertap.exe extension-path
```

**Linux or macOS**

```bash
python -m venv .venv
./.venv/bin/python -m pip install browsertap-mcp
./.venv/bin/browsertap extension-path
```

The core install (`pip install browsertap-mcp`) is sufficient for page, browser,
and CDP tools. It omits `pyautogui`, `mss`, and `pillow`, which are used by the
lab-only `resolve_leave_dialog` Enter fallback and its screen/input checks. Add
`[desktop]` only when you need that fallback.

To work on the project rather than only use it, install the checkout as editable
instead. Same extras; the extension directory and the skills are then read
straight out of the tree:

```bash
git clone https://github.com/LinVireo/browsertap-mcp.git
cd browsertap-mcp
python -m venv .venv
./.venv/bin/python -m pip install -e ".[dev,desktop]"
./.venv/bin/browsertap extension-path
```

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

#### The extension popup

The popup holds the badge toggle above and two buttons, both of which act on
the tab you are looking at:

| Control | What it does |
|---|---|
| **Refresh** | Lists that tab's cookies **including their values, and including `HttpOnly` ones** the page's own JavaScript cannot read. It is a debugging aid, so it deliberately shows what a `document.cookie` dump would hide. |
| **Copy** | Writes every listed cookie to the system clipboard as `name=value` lines. |

Treat both as handling live credentials: anything that later reads your
clipboard receives session cookies, and a screenshot of the popup captures
them. See [SECURITY.md](https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md)
for where this sits in the threat model.

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

For the least disruptive workflow, start with [`docs/USAGE.md`](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/USAGE.md): it explains which operations stay in a background tab, when foreground activation is needed, how the remaining physical fallback is gated, and when an image-capable model is useful.

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
browsertap --version            # print the installed package version
browsertap extension-path       # print the unpacked extension directory
browsertap skill-path           # print the directory holding the shipped agent skills
browsertap doctor               # diagnose the local setup, as JSON
browsertap bridge               # run the bridge in the foreground
browsertap bridge --restart     # restart the managed bridge; does not touch the browser
browsertap bridge --stop        # stop the exact managed bridge process
browsertap print-hermes-config  # print a Hermes config snippet
```

`doctor` reports the extension path, port state, and connected tab count. It also
returns a structured verdict: `cause` is one of `healthy`, `starting`,
`ext_never_registered`, `sw_slept_or_dropped`, `registering`, or
`bridge_unreachable`, and `advice` is the matching one-line fix. `starting`
means the bridge has just started and is waiting for the extension handshake;
its `action` is `wait_for_extension`, so wait a few seconds and run `doctor`
again. `registering` means the extension is connected but no normal `http(s)`
content tab is ready.

It always prints JSON on stdout, including when the configuration itself is
wrong. An unparseable `BROWSERTAP_BRIDGE_PORT` or a `BROWSERTAP_BRIDGE_HOST`
that does not resolve fails before any bridge call, and is reported as
`status: "initialization_failed"` with `action: "check_config"` plus the
offending `error` and `error_type` — not as a traceback. Exit status is `0` only
when `status` is `healthy` or `starting`.

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

These are instructions for an agent using BTAP, not for an agent editing BTAP's
source. Coding rules and test commands belong in
[AGENTS.md](https://github.com/LinVireo/browsertap-mcp/blob/main/AGENTS.md) and
[Contributing](https://github.com/LinVireo/browsertap-mcp/blob/main/CONTRIBUTING.md).

Point your client's skill manager **at that directory** rather than copying the
files. A copy looks correct for as long as the contents happen to agree, then
silently stops receiving updates when you upgrade the package. If you keep copies
anyway, `python -m scripts.check_tool_docs --check-installed-skills --skill-mirror
DIR` compares them against the shipped originals and names whichever one drifted.

### Upgrade

The marker below is maintained with this source tree. It is not proof that a
development checkout has been published; compare the installed package with its
release tag before using new tool signatures or the 0.5.0 migration notes.

Current release: unified Python package, bridge, and unpacked Chrome extension **0.4.20**.

The three components load updates separately:

1. Update the installed package with `pip install -U browsertap-mcp` (keep
   `[desktop]` if you use that extra), then restart its MCP session.
2. Run `browsertap doctor`. If it requests `restart_bridge`, use
   `browsertap bridge --restart` to load the daemon's updated code.
3. If it requests `reload_extension`, open `chrome://extensions` and press
   **Reload** on BrowserTap Bridge. Extension source changes require that manual step.

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
2. **BrowserBridge** — a local daemon on `127.0.0.1:18765` (WebSocket) and `:18766` (HTTP). It owns the extension connections, tracks sessions, and relays results. It runs detached from any MCP instance, and the MCP server starts it on demand with no console window. `client_id` identifies one connected browser/profile instance; `session_id` is its current composite `clientId:tabId` handle, not a permanent tab identity. Connected rows may also carry `tab_identity`. Several browsers and profiles coexist.
3. **MCP server** — exposes the whole thing as MCP tools. Each agent's MCP process keeps its own selected target and tab ownership registry.

Two channels reach the browser: a per-tab session channel, and a direct channel to the extension's service worker. The second one is why some tools keep working when every tab is closed.

## Behaviour you should know before driving it

**Selecting a tab does not raise it.** `switch_tab` defaults to `activate=false`: it only changes which tab later calls target. Nothing moves on screen until you call `activate_tab`, pass `switch_tab(activate=true)`, or approve a physical-input action. Page reading, JS, and the `page_*` input tools all work on a background tab.

**One coordinate space, inside the tab.** `page_click`/`page_drag` take **viewport** coordinates inside one tab and are dispatched through CDP — no cursor movement, no window focus, `foreground_changed: false` in the reply. There is no desktop-coordinate tool to confuse them with any more: the ones that took physical screen pixels were removed in 0.5.0.

**Two pixel units, and the screenshot does not use the one you click with.** Viewport coordinates are **CSS pixels** — the space `getBoundingClientRect` reports. A page screenshot comes back in **device pixels**, which is CSS × `devicePixelRatio`, so at 125% display scaling a point read off the picture is 25% too large for `page_click`; `capture_page_screenshot` reports `image_width`/`image_height` and `pixel_space: "device"` so the factor is visible instead of assumed. Reading a point off a picture is the one path with no hit test — prefer a `scan_page` selector, which is checked against the page before anything is dispatched.

**Automation profiles.** With `BROWSERTAP_MODE` unset, BTAP defaults to `lab` with `BROWSERTAP_LAB_NO_ELICIT=1` semantics. Lab permits site `allow` and the restricted leave-dialog fallback without elicitation; `safe` prompts for each site `allow` and refuses the physical Enter fallback. Neither profile is a confirmation prompt for every browser action. Ownership and target checks still apply, and the physical path keeps its OS lock, quiet-input gate and `on_screen` check. `input_quiet.enforced` says whether comparable input markers were available.

**Dialogs are explicit.** `execute_js(dialog_policy=...)`, `open_url(beforeunload=...)`, and `handle_dialog(action=...)` take `dismiss` (default), `accept`, or `manual`. The global default still preserves the page; only an explicit accept or lab's configured shell/IDE host heuristic leaves automatically. `handle_dialog` answers within three seconds or reports `no_dialog`/an explicit error. `resolve_leave_dialog` tries protocol accept twice and uses physical Enter only as a final, lab-approved fallback.

**Permissions are leases, not grants.** `set_site_permission` covers one origin for 60–600 seconds, records the prior setting, and restores it on expiry/reset/service-worker restart. `safe` prompts for every `allow`; default `lab` applies it without elicitation. Browser capabilities that cannot be restored return `unsupported` or `requires_user_action`.

**Challenges stay in your browser.** A Cloudflare Turnstile or similar widget is handled in the same connected tab, by `page_click`, with a bounded number of attempts. When the challenge has not moved, the result is `challenge_stalled` and BTAP stops so you can finish it yourself in that same tab. BTAP never launches Playwright, a headless browser, or a separate automation profile as a fallback — the whole point is your real, logged-in session.

**Changed tools need a reload.** Tool schemas and descriptions are read once when your client starts the MCP server; after upgrading, restart the MCP session or your client, or you will keep calling the old signatures. Extension changes need a manual reload at `chrome://extensions` — `chrome.runtime.reload()` restarts the service worker without re-reading the files from disk.

### Tab ownership in concurrent tasks

Classify every tab before using it. A **U (user) tab** existed in the first `list_tabs` snapshot; do not close it or navigate it by default. An **A (agent) tab** is created by this task's `open_new_tab`; save its `session_id`, `generation`, and `owner_id`, pass that explicit session to every operation, and call `close_tabs(..., owner_id=...)` in cleanup. A **B (borrowed) tab** is a temporarily used U tab; record its `original_url` and never close it. Restore a URL changed by this task only after confirming the same tab lifecycle still exists and the user has not since navigated it elsewhere.

Decision order: run `list_tabs`; borrow an existing match only for read-only/light work; open an A tab for searches, filters, sorting, pagination, scrolling, expand/collapse, navigation, forms, or other actions that change the page view or state; open an A tab when no match exists; finally close only A tabs. Never register the initial tab snapshot as owned, close a U/B tab, omit the explicit target for state changes, reuse an old native tab id, omit generation-aware cleanup, or leak an A tab.

For parallel agents, use **a separate A tab per agent and explicit `session_id`
on each call**. Different tabs can run concurrently both within one MCP process
and across independent MCP processes. Each process has its own default tab;
agents sharing a process also share that default, so `switch_tab` is not an
agent identity. Each call snapshots its target, and an explicitly targeted call
does not change another call's target or the process default.

A cooperative target lock covers each complete MCP call and its internal browser
roundtrips. A competing call to the same tab returns `target_busy`. The bridge
also reserves the target of each dispatched command: `wait=false` and response
timeouts keep that reservation until a definitive browser result or a confirmed tab
lifecycle end. Claim an `execute_js` result with `get_execute_js_result` from the
same MCP session; do not replay a script whose result is pending. Direct `/link`
and Python driver calls receive the bridge's per-command reservation, but need
an MCP command scope for the lock across multiple browser roundtrips.

A debugger timeout or detach can leave JavaScript running. Polling then reports
`status=in_progress`, `operation_status=outcome_unknown`, and
`reservation_held=true`; it does not permit replay. Manual dialogs keep the same
reservation, and only the originating MCP session can call `handle_dialog`.
Handling a dialog does not prove the script finished: when the extension cannot
return its final result, the operation remains `outcome_unknown`. The caller can
close its own tab with `close_tabs(..., owner_id=...)` to end that lifecycle.

Console and network capture mutations belong to the MCP session that started
them. Another session cannot restart, stop, or clear that capture (`capture_busy`);
ordinary page operations and non-clearing console reads remain available. After
the owning MCP process has exited, another session can reclaim its capture.
Process exit alone does not cancel page JavaScript or release a pending target;
the tab owner can close its own tab to recover. Agents
sharing one MCP process share this ownership too. These guards do not make a
multi-step workflow atomic or isolate cookies/storage shared by a profile.

### Structured statuses and recovery fields

Read `ok` and `error_code` first; operation statuses remain in `data`/`legacy`
and as small top-level compatibility fields. Failures set MCP `isError=true`:

| `status` / `error_code` | Meaning |
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
| `target_busy` | This tab is reserved by another call or a still-pending browser command. Check `delivery_state` and `retry_safe`; a call involving multiple tabs may have completed earlier steps. |
| `capture_busy` | Another MCP session owns this console/network capture. Its owner must stop it before another session can restart, stop, or clear it. |
| `ambiguous_browser` | Several browser/profile instances match and no unique browser was selected. Use `list_tabs`, then pass the chosen full `session_id` (or `client_id` where supported). |
| `input_activity_detected` | You used the mouse or keyboard during the post-approval quiet window, so no physical input was sent. |
| `activation_failed` | The target tab could not be confirmed on screen, so no physical input was sent. |
| `unsupported` | The browser or extension API cannot provide this (e.g. clipboard permission leases). |
| `challenge_stalled` | A browser challenge made no progress within the attempt bound; hand the tab back to the user. |
| `no_response` | The script did not reach the tab or timed out — do not blindly retry anything with side effects. |
| `not_found` | The selector matched nothing; no input was dispatched. |
| `bridge_error` | A bridge call failed. It may appear as `error_code` or a diagnostic field rather than the top-level status; run `list_tabs`/`doctor` before retrying. |
| `switched_session` | Supplemental field indicating that only an implicit dead default was replaced with another live tab. Verify the new target before continuing; explicitly directed dead sessions are never substituted. |

For delivery failures, only `delivery_state="undelivered"` proves the operation
was not sent. `sent_unconfirmed` means its ACK or HTTP response is missing, so it
must not trigger an automatic replay. Treat `delivered_no_result`, `navigated`,
and unknown delivery as potentially executed; recover by operation ID when one
is available and inspect the page before deciding the next action.

## Disclaimers

This server exposes your real browser profile to the connected MCP client,
including logged-in sessions. Its scope is browser automation, with only the
restricted physical leave-dialog fallback described below.

- One physical-input path is left after 0.5.0: `resolve_leave_dialog`'s Enter fallback, `lab` only, and only after two protocol-level attempts fail. It is real OS-level input rather than a synthetic page event, so it lands on whatever is on screen; `safe` refuses to send it at all. The `page_*` tools carry none of this exposure.
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

Most tools accept an optional `session_id` to target one specific tab; omitting it
uses this MCP process's current target. Pass it explicitly for state changes.
`client_id` distinguishes connected browser/profile instances; `session_id` is a
composite handle such as `chrome_a1b2c3:456`. Pass returned handles verbatim.
With multiple instances connected and no browser selected, routing returns
`ambiguous_browser`. Select a full `session_id` from `list_tabs` with `switch_tab`,
or supply it to the operation; `open_new_tab` also accepts `client_id`.
`browser="chrome"` alone is insufficient when multiple Chrome profiles match.
If Chrome reports an evidence-backed replacement for the same tab, BTAP returns
`rebound_from`, `replacement_session_id`, and `tab_identity`; otherwise an
explicit stale handle is refused. Tools marked **no tab needed** use the
extension's service worker and work with zero tabs, but still need a unique
browser/profile selection.

<details>
<summary><b>Tabs and navigation</b></summary>

- **get_setup_status** — report `package_version`, `bridge_version`, `extension_version`, `protocol_version`, connection state, ports, tabs, and the required recovery action. A missing bridge listener is started automatically when spawning is enabled; `restart_bridge_required=true` means a bridge that is still running must be replaced with `browsertap bridge --restart`. `reload_extension_required=true` identifies the unpacked-extension platform limit and requires a manual Reload; a version number that differs on its own no longer sets it, because Chrome parses `manifest.json` at load time and never re-parses it without a Reload, so a release bump would otherwise demand a click whose only effect is on that number. `restart_mcp_session_required=true` is the opposite direction: a component is *newer* than the running server, so the stale build is this process and only restarting the MCP session or client clears it — the other two flags stay false, because a restart or reload would report the same mismatch again. `extension_build_stamp` is the stronger signal and answers the question the four version fields cannot: it is a hash of the extension sources compiled into `background.js`, reported by the worker actually running, so comparing it to `expected_extension_build_stamp` (a fresh hash of the directory) is decisive in both directions where version equality was measured wrong twice. Read the answer from `extension_build_verdict`: `matches_tree` (the worker is running this code), `stale_worker` (it is not -- Reload), `stamp_not_regenerated` (an extension file was edited without running `python -m scripts.extension_stamp --write`, so the comparison proves nothing either way) or `unverifiable` (the extension predates the stamp, or the directory could not be read -- see `extension_build_error`). `extension_build_enforced=false` means no comparison happened, so treat it as unknown rather than as a pass. Answers while another tool is still running; `default_session_id` is this request's snapshot of the MCP process default and `default_session_settled=true` because another call's temporary target is isolated. No parameters.
- **get_automation_profile** — inspect whether the current MCP process uses `lab` or `safe`.
- **set_automation_profile** — switch the current MCP process between `lab|safe`; the override is not persisted and does not reload the extension.
  - `mode` (string): `lab` or `safe`
- **list_tabs** — list connected tabs under `data.tabs`, including their full session handles and `browser` fields. Answers while another tool is still running; `default_session_id` is this request's snapshot of the MCP process default and `default_session_settled=true`. Parallel agents should still pass an explicit target. No parameters.
- **list_all_tabs** — *(no tab needed)* list every open tab, including `chrome-extension://` pages that `list_tabs` hides. Those never become sessions, so they have no session id; drive them with `cdp_command(tab_id=...)`.
  - `session_id` (string, optional): which browser/profile to ask.
- **switch_tab** — set this MCP process's *target* tab for later calls. A `url_pattern` must match exactly one tab; if several match, select one with its full `session_id`. A `browser` filter matching multiple profiles also requires an explicit `session_id`. It does **not** raise the tab or focus the browser: `activate` defaults to `false`. Pass `activate=true`, or call `activate_tab`, when you need the tab in front.
  - `session_id` (string, optional), `url_pattern` (string, optional): substring match, `browser` (string, optional): `chrome`, `edge`, or `opera`, `activate` (boolean, optional): default `false`.
- **activate_tab** — bring a tab to the foreground and focus its window. This is the explicit way to raise a tab, and the only one that does not involve approving physical input. Check `on_screen` in the reply: BTAP first asks Windows to restore a minimised browser, but `on_screen=false` means visibility still could not be confirmed and screen-coordinate input must not be sent.
  - `session_id` (string, optional)
- **open_url** — navigate the current tab. Global behavior remains `dismiss`; lab automatically accepts beforeunload on configured shell/IDE hosts. If the extension's `navigate` route is unavailable on a heavy SPA, BTAP falls back to `Page.navigate`. A CDP result with `isDownload=true` returns `{type:"download",status:"triggered"}` instead of only `navigation_failed`; the accompanying `ERR_ABORTED` is normal for that download navigation.
  - `url` (string), `session_id` (string, optional), `timeout` (number, optional): default `15`, `beforeunload` (string, optional): default `dismiss`, `intent_leave` (boolean, optional): `false` forces page preservation
- **download_file** — download an HTTP(S) URL through Chrome's native download manager, using that browser profile's cookies and authenticated session. It waits by default and returns `status="completed"` plus a verified absolute `path`; interrupted downloads return `failed`, while a timeout or `wait=false` returns `in_progress` with `download_id`. An explicit `session_id` must still be live and is never replaced with another profile. Use this for attachments instead of page `fetch`.
  - `url` (string), `filename` (string, optional): relative download name, `directory` (string, optional): arbitrary absolute destination directory; creates parents, `wait` (boolean, optional): default `true`; `directory` requires `true`, `timeout` (number, optional): default 60 seconds, maximum 1800, `session_id` (string, optional): selects the browser profile, `overwrite` (boolean, optional): default `false`; an existing final destination raises an error unless explicitly `true`. If a directory download times out, `directory_applied=false`: the move is no longer tracked and Chrome may finish into its default download directory.
- **open_new_tab** — open a background tab in this MCP process's selected browser/profile, unless `session_id` or `client_id` selects another. Creates a unique `operation_id` and waits a bounded time for exact session/generation registration; pass `active=true` for foreground work. Returns `{operation_id,tab_id,session_id,generation,ready,owned,opener,owner_id,load_status}`. The extension deduplicates by operation id. Ownership requires a completed record with exact `client_id+tab_id+generation`, even when `ready=false`; `ready` only reports immediate availability for session tools. Before create dispatch, registry uncertainty returns `status="unknown",may_have_created=false,retry_safe=true`; after dispatch, uncertainty returns `may_have_created=true,retry_safe=false`. With `may_have_created=false,retry_safe=true`, resolve the reported failure and retry without `operation_id`. To recover a dispatched create with `retry_safe=false`, pass the same `operation_id`, returned `client_id`, and `owner_id`: recovery reads the durable record without replaying `tabs/create`. A failed recovery probe preserves that uncertainty and owner capability. If the initial recovery probe finds no record, `reconciliation.resume_required=false` directs the caller to `list_tabs()` and inspection of that browser instead of another recovery call. Missing records, matching URLs, or unchanged tab counts cannot prove non-creation or ownership; keep the outcome unknown unless exact identity and task ownership resolve it. Keep `owner_id` for cleanup of registered task-owned tabs with their exact session/generation. Use this native API for reliable new tabs; page `window.open()` or anchor clicks may be blocked without a user gesture.
  - `url` (string), `timeout` (number, optional): default `15`, `active` (boolean, optional): default `false`, `session_id` (optional browser/profile selector), `owner_id` (optional capability to group several tabs under one task owner), `operation_id` (optional recovery handle), `client_id` (optional browser/profile client selector for creation or recovery)
- **close_tabs** — *(no tab needed)* accept native numeric tab ids or full `client:tabId` session ids, including `chrome-extension://` tabs. The default `only_if_agent_owned=true` requires the `owner_id` returned by `open_new_tab` and verifies the current lifecycle generation before closing, so pre-existing user tabs and another agent's tabs are refused. If the user already closed an owned tab, cleanup returns `status=already_gone, closed_by=user` without reusing its native id. An actual owned close returns `closed_by=agent`; an explicit unowned/operator override returns `closed_by=none` so it is not counted as task-owned cleanup. If `already_gone` is returned but a page with the same work is still visible, call `list_all_tabs` and verify URL/title before deciding whether a new session/generation should be closed; BTAP never auto-transfers ownership by URL. A Chrome `tabs.onReplaced` identity mapping is safe and also migrates the ownership claim. Set `only_if_agent_owned=false` only when the operator explicitly asked to close an unowned/user tab.
  - `tab_id`, `session_id` (optional browser constraint), `owner_id` (required by the safe default), `only_if_agent_owned` (boolean, default `true`)
</details>

<details>
<summary><b>Page reading and execution</b></summary>

- **scan_page** — read the page as simplified HTML or text. Returns `links` mapping each `#rN` ref in the content to its absolute URL, and `offscreen` + `hint` when content was left outside the viewport. A background tab may report viewport height zero; ordinary DOM/text/API work still continues there, and only visual/layout fidelity requires explicit `activate_tab`. When the page can be probed, `render_state`/`content_ready` distinguish real content from a loading, hydrating, or shell-only SPA; retry or use `wait_for` before treating an empty shell as final content. `cutlist` (on by default) collapses long repeated lists and reports a CSS selector for each container it collapsed, derived from that container's own structure. This tool does not modify the page -- no attribute, no id, no `window` global -- so a scan is invisible to the page's own scripts.
  - `session_id` (string, optional), `text_only` (boolean, optional): default `false`, `cutlist` (boolean, optional): default `true`; collapse repetitive lists, `maxchars` (integer, optional): default `35000`, `instruction` (string, optional), `extra_js` (string, optional), `timeout` (number, optional): default `15`
- **wait_for** — wait until a condition holds, then return. Use this instead of polling `scan_page`, which re-serializes the whole DOM each time. The server schedules short synchronous page checks under one deadline, avoiding background-page timer throttling. Exactly one condition is required. `selector` accepts legacy CSS or the structured locator object described under background page input. A timeout with `operation_id` retains a pending check: query `get_execute_js_result` before issuing another operation; the expression is not replayed while that check is pending.
  - `selector` (string/object, optional): CSS or structured locator, `text` (string, optional): substring of body text, `url_pattern` (string, optional): regex on the URL, `js` (string, optional): expression to become truthy, `gone` (boolean, optional): wait for the condition to stop holding; default `false`, `timeout` (number, optional): default `15`, `session_id` (string, optional)
- **wait_for_url** — wait for navigation to settle: blocks until the tab URL matches `url_pattern` (regex, or plain substring — both are tried) and, unless `wait_ready=false`, `document.readyState` is `complete`; then returns final `url`, `title` and `ready_state`. Use after a click or `open_url` that navigates; `wait_for(url_pattern=...)` only checks the URL and can return while the new document is still blank. Uses the same bounded synchronous checks and pending-operation recovery as `wait_for`.
  - `url_pattern` (string): regex or substring to match against the URL, `timeout` (number, optional): default 15, `wait_ready` (boolean, optional): require `readyState === 'complete'`, default `true`, `session_id` (string, optional)
- **scroll_page** — scroll and report the new position, so a long page can be read in passes.
  - `to` (string, optional): default `bottom`; also accepts `top`, a pixel offset, or a CSS selector to bring into view, `session_id` (string, optional), `timeout` (number, optional): default `15`
- **execute_js** — run JavaScript in the page and return the result. `timeout` is one end-to-end deadline covering dialog-policy setup, monitor snapshots, delivery/retry, navigation inspection, and cleanup; an explicit `session_id` is forwarded through every one of those roundtrips instead of relying on the process default. Set `wait=false` for a genuinely long task: once the extension acknowledges delivery, BTAP returns `status="in_progress"` plus an `operation_id`; claim the result with `get_execute_js_result` instead of replaying the script. `dialog_policy="manual"` is intentionally unavailable in background mode. When a script navigates the page, `status` is `navigated` (not `success`) with `landed_url`; the script's return value is genuinely lost in that case and is reported as such rather than substituted. `dialog_policy` decides what happens if the script opens `alert`/`confirm`/`prompt`: `dismiss` (default) and `accept` answer it and report it under `dialogs`, while `manual` pauses a synchronous script with the native dialog still open and returns `blocked_by_dialog` — call `handle_dialog` to release it. A tab already holding a manual pause returns `busy` immediately. Use `wait_for`/`wait_for_url` instead of delayed `setTimeout` or sleep Promises when waiting for page state. When the JSON-encoded `js_return` exceeds the 24 KiB UTF-8 inline limit, BTAP writes the complete value to a private temporary JSON file and returns `result_file`, `result_bytes`, `result_sha256`, and `result_format` instead of a truncated inline value.
  - A `Cannot access contents of the page` error must be classified before retrying: if the script attempted `window.open` or navigation, use `open_new_tab` (Chrome may block it without a user gesture); if injection into the current tab is forbidden, choose a normal scriptable `http/https` tab or the supported CDP route. Do not treat the message as proof that reading the current page failed.
  - `script` (string), `session_id` (string, optional), `no_monitor` (boolean, optional): default `false`, `timeout` (number, optional): default `15`, `dialog_policy` (string, optional): `dismiss` (default), `accept`, or `manual`, `wait` (boolean, optional): default `true`
- **get_execute_js_result** — read or briefly wait for an `operation_id`, from the same MCP session that submitted it. Accepts handles from `execute_js` and other timed-out bridge commands. Querying never replays the operation. A completed result can be read repeatedly, including after a lost query response; pending, unknown/expired, and foreign handles return explicit statuses or errors. After reservation expiry, the first valid late terminal reply is retained as `late_result` (`success` and `data`), with `late_reply_age` in seconds. The original `unknown` receipt and `retry_safe=false` remain; the late reply neither restores the reservation nor renews retention. Results are retained for up to 10 minutes, with at most 512 completed operation records; capacity pressure can evict them earlier. A missing result does not prove the operation was never executed. Large successful values use the same lossless `result_file` metadata as `execute_js`; for a late value, that metadata is inside `late_result` and its `data` becomes null.
  - `operation_id` (string), `timeout` (number, optional): default `0`, range `0`–`120`
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

Trusted CDP input events delivered to one named tab. They do **not** activate the tab, focus its window, or move the desktop cursor — every reply carries `foreground_changed: false` and `input_mode: "cdp"`. All coordinates are **viewport CSS pixels** (relative to the top-left of the page area, the space `getBoundingClientRect` reports), never desktop pixels and never the device pixels `capture_page_screenshot` returns.

Pass `session_id` explicitly: the call holds that target in its own context without changing this MCP process's default. A stale handle without evidence of a same-tab replacement is refused. Another MCP call using the same tab can return `target_busy`; see the concurrent-task boundaries above.

`selector` remains backward-compatible with CSS strings and also accepts a locator object with exactly one primary key: `css`, `role` (optional `name`), `text`, or `label`. Inside a locator object, `selector` is accepted as a compatibility alias for the `css` primary key. `exact` applies to role/name or text matching; `frame` walks one or more same-origin iframe locators; `shadow` walks open Shadow DOM hosts. A click-only frame-relative point can use `{"frame": [...], "x": 20, "y": 30}`; `x`/`y` are CSS coordinates in the final same-origin frame's viewport and are converted to top-document coordinates before dispatch. Zero matches return `not_found`, multiple matches return `ambiguous`, and cross-origin or closed roots are reported without dispatching input. Selector clicks that cross an iframe chain with a non-identity CSS transform return `unsupported_frame_transform` for the same reason; query/type paths remain available.

- **page_click** — click a CSS/structured `selector` or viewport coordinates. Exactly one targeting mode: either `selector`, or both `x` and `y`. With a selector, each omitted offset axis uses the element centre; a supplied `offset_x` or `offset_y` is measured from the element's top-left corner on that axis. A structured selector of the form `{"frame": [...], "x": 20, "y": 30}` is a frame-relative point: it enters the listed same-origin frames, adds their client offsets, and dispatches at the resulting top-document CSS coordinates. Point mode is intentionally not hit-tested because the caller named a pixel, not an element. Missing, ambiguous, non-interactable, cross-origin-frame, closed-shadow, and CSS-transformed-frame targets return structured status without input dispatch; the transformed-frame status is `unsupported_frame_transform`. In selector mode the point is also hit-tested in the page before dispatch: a target below the fold is scrolled into view (`scrolled_into_view`), one whose pixel belongs to something else returns `obscured` with `occluded_by` naming the overlay, and one still off screen returns `outside_viewport` — in both cases nothing is clicked, because a dispatched click would have landed on the other element and reported success. A verified click carries `hit_verified: true`. Coordinate mode is not hit-tested: coordinates name a pixel, not an element — and a pixel read off `capture_page_screenshot` is a *device* pixel, so it needs dividing by `devicePixelRatio` first. Challenge replies keep the bounded `challenge_detected`/`attempts`/`challenge_stalled` behavior.
  - `selector` (string/object, optional), `x` (number, optional), `y` (number, optional), `offset_x` (number, optional), `offset_y` (number, optional), `button` (string, optional): default `left`, `clicks` (integer, optional): default `1`, `session_id` (string, optional), `timeout` (number, optional): default `15`
- **page_type** — insert text into a CSS/structured-locator field, or into whatever already has focus when `selector` is omitted. Xterm.js containers/descendants retarget to `.xterm-helper-textarea`. Missing, ambiguous, read-only, or otherwise unusable targets return a structured status without dispatching text or keys; invalid legacy CSS returns `status="invalid_selector"` instead of a raw `SyntaxError`. Successful and failed target resolution includes a redacted `active_element` descriptor and `focus_confirmed` when focus was attempted, so omitted-selector input is auditable. `clear=true` selects the existing value first; `submit_key` sends one key afterwards.
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
- **save_pdf** — bounded `Page.printToPDF`; validates PDF bytes and atomically writes `save_path`. `save_path` is **relative** and resolves under `~/Downloads/browsertap`; an absolute path or a `..` escape is rejected with `ValueError`. A timeout forcibly releases its debugger lease.
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

- **capture_page_screenshot** — page capture via CDP with viewport, `full_page`, or explicit `clip` modes. PNG, JPEG, and WebP are supported; `quality` is valid only for JPEG/WebP. Returns text metadata plus attached MCP image content; `save_path` only adds a disk copy, and it is **relative** — it resolves under `~/Downloads/browsertap`, with absolute paths and `..` escapes rejected. Base64 is omitted unless explicitly requested. The metadata names its own units: `image_width`/`image_height` parsed from the returned bytes and `pixel_space: "device"` (CSS × `devicePixelRatio`), so a point read off the picture is not fed straight to `page_click`. A header it cannot parse reports `null` dimensions plus a `dimensions_note` rather than a guess — `size` is the byte count, not a dimension.
  - `session_id` (string, optional), `tab_id` (integer, optional), `format` (string, optional): default `png`, `full_page` (boolean, optional): default `false`, `clip` (object, optional): `x`,`y`,`width`,`height`, optional `scale`, `quality` (integer, optional): 0–100 for JPEG/WebP, `save_path` (string, optional), `return_base64` (boolean, optional): default `false`, `timeout` (number, optional): default `20`
</details>

<details>
<summary><b>Removed in 0.5.0: OS-level input and desktop capture</b></summary>

`mouse_move`, `mouse_click`, `mouse_drag`, `type_text`, `hotkey`, `pointer_info`
and `capture_desktop_screenshot` no longer exist. They drove the whole desktop
rather than one tab, so they acted on whatever happened to be on screen. What to
call instead:

| Removed | Use |
| --- | --- |
| `mouse_click` | `page_click` |
| `mouse_move` | not needed — `page_click` positions itself |
| `mouse_drag` | `page_drag` |
| `type_text` | `page_type` |
| `hotkey` | `page_press` |
| `pointer_info` | `execute_js` to read element geometry |
| `capture_desktop_screenshot` | `capture_page_screenshot` |

A failing `page_*` call is a targeting problem, not a reason to look for a
screen-coordinate fallback — re-read the page with `scan_page` and fix the
locator. Browser chrome, native file pickers, extension popups and OS dialogs
are outside what a page-level protocol event can reach, and are unsupported
rather than served by a desktop path.

One physical path survives: `resolve_leave_dialog` may send Enter after its two
protocol attempts fail, in `lab` only; pure probe timeouts do not trigger it.
`safe` returns `requires_user_action` without sending Enter. Lab skips elicitation
by default; when lab approval prompts are enabled, a declined, cancelled or
unavailable prompt also prevents input. The physical gate keeps its cross-process lock (contended → `busy`, returned
immediately, never queued), a short quiet window (you touched the mouse or
keyboard → `input_activity_detected`, nothing sent), then raise the target tab,
then act. What that window can detect depends on the OS: only Windows exposes a
last-input timestamp, and the pointer position is unavailable under Wayland, in a
headless container, and on macOS without the accessibility permission. With no
signal at all the window still elapses but has nothing to compare, so the result
carries an `input_quiet` block naming the markers it sampled, with
`enforced: false` when there were none — read a pass on such a machine as
unverified rather than as an idle desktop. If the tab cannot be confirmed on
screen the result is `activation_failed` and nothing is sent, so a minimised
window produces an error rather than an Enter into the wrong place.

</details>

## Troubleshooting

Run `browsertap doctor` first. For connection, version, dialog,
permission, and physical-input recovery procedures, see the dedicated
[troubleshooting guide](https://github.com/LinVireo/browsertap-mcp/blob/main/docs/TROUBLESHOOTING.md).

## License

MIT — see [LICENSE](https://github.com/LinVireo/browsertap-mcp/blob/main/LICENSE),
which ships inside both the wheel and the sdist. Keep it if you fork or
redistribute this.

BTAP is maintained by `LinVireo`, and the canonical public repository for this
distribution is `LinVireo/browsertap-mcp`.
