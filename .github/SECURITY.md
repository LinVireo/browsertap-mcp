# Security Policy

`browsertap-mcp` controls a real browser profile and may expose logged-in
page content, cookies, downloads, screenshots, the lab-only physical Enter
fallback, and explicit Windows native-file-dialog cancellation to an MCP client.
Treat the client, its model, and every enabled tool
as part of the same trust boundary.

## Supported versions

Security fixes are applied to the current `0.5.x` release line. Reproduce a
report against the latest release before submitting it when practical.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting/security advisory flow for this
repository. If that option is unavailable, open a minimal issue asking for a
private contact channel; do not include working exploits, tokens, cookies,
personal screenshots, or browser-profile data in a public issue.

Maintainers target an acknowledgement within three business days and an initial
triage/status update within seven business days. Remediation timing depends on
severity and reproducibility; these targets are response goals, not a promise
that every report is fixed within seven days.

Include the affected BTAP version, browser/version, operating system, expected
security boundary, reproduction steps with synthetic data, and whether the
issue requires a connected extension, an authenticated `/link` request, or
physical input.

## Security model

- The bridge binds to loopback by default. Loopback prevents remote network
  access but does not isolate processes already running as the same local user:
  they can reach local ports and may be able to read that user's files. Do not
  expose the WebSocket, HTTP, or lock ports (`18765`–`18767` by default) to an
  untrusted network. A custom non-loopback deployment needs its own firewall,
  authentication, and transport protection.
- State the consequence plainly: `/link` is a command channel, so any local
  process that can read the token file can POST one `execute_js` and run
  arbitrary JavaScript in the user's logged-in browser. Reading the token is
  therefore equivalent to taking over every session that browser profile is
  signed into. Treat the token file like a password, not like a config value.
- The `/link`, `/api/result`, and `/api/longpoll` HTTP routes require the
  persistent per-user token stored at `~/.browsertap/bridge-token`. Keep
  the file out of repositories, diagnostics, screenshots, and support bundles.
- `BROWSERTAP_BRIDGE_AUTH=off` (also `0`, `false`, `disabled`) disables that
  authentication entirely: every token-guarded route then accepts any local
  request, with the consequence described above. It exists only for an
  explicitly trusted local compatibility setup. `get_setup_status` and
  `browsertap doctor` report it as `state_paths.auth_enabled`, for the MCP
  session and for the running daemon separately, because the two are different
  processes with different environments and only the daemon's answer decides
  whether a route is actually guarded. Leave it unset.
- The state directory reported as `state_paths.state_dir` (`~/.browsertap`, or
  `~/.agent-browser-mcp` on an install that predates 0.4.0) holds the token,
  the pid record, and `bridge.log`. On Windows, a new token receives an explicit
  current-user owner and protected owner-only DACL before its first byte is
  written. Existing token files must belong to that user; BTAP hardens and
  verifies their DACL before reading through the same file handle. A failed
  security check refuses the read without replacing the file or token. Even a
  diagnostic read can harden an existing DACL. POSIX creation writes a private
  `0600` candidate completely before atomic no-replace publication, so concurrent
  readers cannot adopt a short-write prefix as the token.
  These controls exclude other ordinary OS users; they do not isolate same-user
  processes or privileged administrators, or retract previously disclosed tokens.
- `bridge.log` is the file operators are asked to attach to a bug report, so
  what may be written into it is a policy, not an accident. Page URLs are
  redacted at the log call: the scheme, host, and a truncated path survive;
  query strings and fragments become `?...`/`#...`, credentials in the
  authority are dropped, and `file:`, `data:`, `blob:`, and `javascript:` URLs
  are reduced to `<scheme>:<redacted>` because for those the location *is* the
  content. A caller's `wait_for_url` pattern gets the same treatment. The token
  value is never logged in any form; the log records only the path of the file
  it was read from, and the diagnostics in `get_setup_status` compare tokens as
  a truncated `sha256:` fingerprint rather than by value. Bridge request and
  WebSocket exception handlers log fixed operation names and exception types,
  without exception text, source lines, request/result bodies or tracebacks.
  Arbitrary protocol identifiers and rejected Origins use short SHA-256
  references for correlation. Unexpected HTTP callback errors are caught before
  the web framework can print their traceback. Logs still contain timings,
  local paths, socket addresses and enough of each URL to identify a site;
  review them before attaching them to a report.
  It caps at 5 MB: the daemon copies the file to `bridge.log.old` and truncates
  in place every 5 minutes when oversized, so exactly one previous generation
  is kept and both files need the same review.
- WebSocket handshakes accept only the packaged extension's exact Origin by
  default and reject missing origins. Its ID comes from `manifest.key` when
  present (strict Base64 or Chromium's PEM format), otherwise Chromium's hash
  of the canonical native path printed by `browsertap extension-path`.
  Installs loaded from that path retain their identity without a new Web Store
  key. Path spelling matters: a case alias or junction used to load an unpacked
  extension can produce a different ID and needs its own explicit Origin.
  The daemon's `ws_origin_policy` reports the default Origin and identity source;
  an unreadable manifest, invalid JSON or invalid key leaves no default trusted
  Origin. This check does not validate the full Chrome manifest schema.
  Despite its name, `BROWSERTAP_WS_ALLOWED_ORIGINS` adds exact trusted
  origins to both the WebSocket handshake and the HTTP origin check. HTTP
  requests without an `Origin` header remain permitted by that check and still
  require the token on authenticated routes. `BROWSERTAP_WS_ALLOW_NO_ORIGIN=1`
  permits origin-less WebSocket clients only. Both
  expand the attack surface and should remain unset in normal installations.
  The browser controls an extension's Origin: another installed extension with
  a different ID cannot register under a new `clientId` or inject its tabs.
  The WebSocket port still carries no token, so a non-browser local process can
  forge the pinned Origin. Pinning does not authenticate local processes.
  For an intentionally separate unpacked copy, trust only its complete Origin
  via the explicit list. Changing a key or package path requires a bridge
  restart and checking the browser's installed extension identity.
- The client id in an `ext_ready` message is self-reported, and it used to be
  written straight into the routing table, so one forged message re-pointed
  every subsequent command at the sender. A client id now stays bound to the
  socket holding it: a second socket claiming the same id is refused and closed
  while the incumbent is still being heard from, and the refusal is counted in
  `browsertap doctor` as `rejected_client_takeovers`, with the last one
  described under `last_rejected_takeover`. A non-zero count on a machine where
  only the browser should be speaking that protocol is worth investigating.
  Takeover is allowed only after the incumbent has been silent for a minute,
  because a socket can be dead while the operating system still reports it as
  connected, and refusing forever would leave the bridge unusable until someone
  restarted it by hand. This guard protects an occupied client id; it does not
  prevent a process that forged an allowed Origin from claiming a new id.
- The extension has broad browser permissions because BTAP can inspect and
  modify the real session, including cookies, downloads, tabs, bookmarks,
  extension management, CDP debugger access, and site content on `<all_urls>`.
  The popup's cookie viewer intentionally exposes cookie values, including
  `HttpOnly` ones, when you press Refresh, and its Copy button then writes a
  `name=value` string for every one of them to the system clipboard. Both are
  gestures: opening the popup reads nothing and writes nothing, so the clipboard
  is never replaced by a visit that was only meant to toggle the page indicator.
  Management of whatever the clipboard then holds is yours. Extension
  installation is therefore an explicit trust decision.
- `get_cookies` returns complete cookie values, including `HttpOnly` cookies
  that page JavaScript cannot read, into the MCP client's context. Anything the
  client logs, caches, or forwards therefore carries live session credentials.
- On ordinary pages the content script may display a small connection-status
  badge (`BTAP: checking`, `BTAP: connected`, or `BTAP: disconnected`). It is
  presentation-only and contains no page content, cookie, token, or URL data.
  The popup toggle only hides the badge; it does not disable the bridge,
  keepalive, or automatic reconnect.
- `declarativeNetRequest` is used to remove CSP response headers temporarily
  from only the tab executing an eval-based command. The rule is a session rule,
  reference-counted per tab, and removed in `finally` cleanup. It is not an
  optional permission and it weakens that tab's page policy while active, so
  only run commands against pages appropriate for the MCP client's trust level.
- Page content is untrusted and may contain prompt injection. A successful
  browser connection does not make instructions found in a page trustworthy.
- Dialog helpers no longer run as default MAIN-world content scripts. Commands
  using `accept`/`dismiss` install temporary scopes and restore their owned page
  properties after the last scope or its expiry. MAIN world remains page-owned:
  a page can observe the active helper or prevent restoration by locking a
  property. Old documents retain wrappers from an earlier extension until normal
  navigation/refresh; reloading the extension alone does not remove them.
- Every MCP tool advertises explicit read-only, destructive, idempotent and
  open-world hints. Optional script/clear/file-write paths are included in the
  classification. Hints are metadata for the host, not authorization or a
  concurrency lock; they do not constrain arbitrary JavaScript or CDP.
- Public `cdp_command` and `cdp_batch` reject the following high-risk methods
  before dispatch: `Browser.*` except read-only
  diagnostics, `Storage.clear*`, `Network.clearBrowserCookies`,
  `Network.clearBrowserCache`, `Network.setUserAgentOverride`,
  `Emulation.setUserAgentOverride`, `Page.close`, `Page.setDownloadBehavior`, `Target.closeTarget`,
  `Target.disposeBrowserContext`, and the opaque `Target.sendMessageToTarget`.
  Batches are checked in full before any member runs. Use dedicated BTAP tools
  for their ownership or restore behavior. An intentional raw override requires
  both `BROWSERTAP_ALLOW_UNSAFE_CDP=1` and `lab`; `safe` always keeps the guard.
  `raw_cdp_blocked` is undelivered and is not a transient retry condition.
  This prevents common mistakes, not all state changes: allowed methods such
  as `Runtime.evaluate` and `Storage.setCookies` can still modify page or profile
  state. It is not a CDP sandbox.
- The shipped default mode is `lab`, which skips elicitation so continuous
  automation is not interrupted: on a default install no physical-input or
  site-allow action asks for approval. Set `BROWSERTAP_MODE=safe` to be
  prompted per physical-input or site-allow action, or
  `BROWSERTAP_LAB_NO_ELICIT=0` to keep `lab` but restore prompts. Both modes
  retain ownership checks, the physical-input lock, the quiet-input gate,
  activation checks, and temporary permission cleanup.
  An approval refusal reports `requires_user_action` with a stable `reason`:
  `elicitation_unsupported`, `declined`, `timeout`, `cancelled`, or `error`.
  None of these dispatches the requested action.
- The remaining physical Enter fallback disables pyautogui's corner failsafe
  (`pyautogui.FAILSAFE = False`) so a pointer that happens to pass a screen
  corner cannot abort automation mid-sequence. The tradeoff is explicit: moving
  the mouse to a corner is not an escape hatch. Stop the MCP client, or use
  `safe` mode, to keep a manual veto.
- Native-file-dialog inspection/cancellation requires explicit `desktop_opt_in`
  and the desktop extra. It checks registered Chrome/Edge process identity,
  native owner/controls, foreground and hit targets. Inspection installs a
  short-lived per-ticket window property; cancellation consumes that ticket and
  sends one bounded Cancel message after the physical lease and observed Windows
  quiet-input gate. Safe mode requires approval. These checks reduce accidental
  targeting but are not atomic with OS message dispatch and do not isolate a
  hostile process running as the same user. Unsupported layouts are refused;
  uncertain outcomes are never automatically replayed.
- `upload_files` attaches files by path and only checks that the path is an
  existing file: there is no directory allowlist. A client that can name a path
  the user can read can therefore attach it to a page's file input. Restrict
  this the way you would restrict any other file-reading tool.
- File-writing tools (`save_pdf`, `capture_page_screenshot`) validate that
  user-supplied paths stay within `~/Downloads/browsertap` by default. Absolute paths, `..` parent directory
  traversal, and symlink escape attempts are rejected with a `ValueError`. This
  prevents arbitrary filesystem writes through path traversal attacks. The
  validation applies to the `save_path` parameter; when that parameter is
  omitted the file is not written to disk and no path validation occurs.
- `capture_page_screenshot` is scoped to a browser tab; the removed desktop
  screenshot surface is not available. Review screenshots before sharing them.

BTAP is an automation tool, not a sandbox or security boundary. Use a browser
profile and accounts appropriate for the MCP client, and avoid shared or
production machines when the impact of a mistaken action would be unacceptable.
