# Security Policy

`browsertap-mcp` controls a real browser profile and may expose logged-in
page content, cookies, downloads, screenshots, and OS-level input to an MCP
client. Treat the client, its model, and every enabled tool as part of the same
trust boundary.

## Supported versions

Security fixes are applied to the current `0.4.x` release line. Reproduce a
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
  the pid record, and `bridge.log`. Its contents are readable by the local
  user; BTAP does not rely on file permissions for the token's secrecy, only on
  the local-user trust boundary stated above.
- `bridge.log` is the file operators are asked to attach to a bug report, so
  what may be written into it is a policy, not an accident. Page URLs are
  redacted at the log call: the scheme, host, and a truncated path survive;
  query strings and fragments become `?...`/`#...`, credentials in the
  authority are dropped, and `file:`, `data:`, `blob:`, and `javascript:` URLs
  are reduced to `<scheme>:<redacted>` because for those the location *is* the
  content. A caller's `wait_for_url` pattern gets the same treatment. The token
  value is never logged in any form; the log records only the path of the file
  it was read from, and the diagnostics in `get_setup_status` compare tokens as
  a truncated `sha256:` fingerprint rather than by value. What the log does
  still contain is tab ids, timings, client
  names, error text from the browser, and enough of each URL to identify a
  site — review it before attaching it, and note that `execute_js` script text
  or page data reaching the log through an exception message is not redacted.
  It caps at 5 MB: the daemon copies the file to `bridge.log.old` and truncates
  in place every 5 minutes when oversized, so exactly one previous generation
  is kept and both files need the same review.
- WebSocket handshakes accept extension origins by default and reject missing
  origins. `BROWSERTAP_WS_ALLOWED_ORIGINS` adds exact trusted origins;
  `BROWSERTAP_WS_ALLOW_NO_ORIGIN=1` permits origin-less local clients. Both
  expand the attack surface and should remain unset in normal installations.
  Scope this guarantee correctly: the default check is a prefix match on
  extension URL schemes and the WebSocket port carries no token, so it keeps
  ordinary web pages out but does not distinguish the real extension from a
  local process that sends an extension-shaped `Origin` header.
- The extension has broad browser permissions because BTAP can inspect and
  modify the real session, including cookies, downloads, tabs, bookmarks,
  extension management, CDP debugger access, and site content on `<all_urls>`.
  The popup's cookie viewer intentionally exposes cookie values and copies a
  `name=value` string to the clipboard when refreshed. Extension installation
  is therefore an explicit trust decision.
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
- The shipped default mode is `lab`, which skips elicitation so continuous
  automation is not interrupted: on a default install no physical-input or
  site-allow action asks for approval. Set `BROWSERTAP_MODE=safe` to be
  prompted per physical-input or site-allow action, or
  `BROWSERTAP_LAB_NO_ELICIT=0` to keep `lab` but restore prompts. Both modes
  retain ownership checks, the physical-input lock, the quiet-input gate,
  activation checks, and temporary permission cleanup.
- Physical input disables pyautogui's corner failsafe
  (`pyautogui.FAILSAFE = False`) so a pointer that happens to pass a screen
  corner cannot abort automation mid-sequence. The tradeoff is explicit: moving
  the mouse to a corner is not an escape hatch. Stop the MCP client, or use
  `safe` mode, to keep a manual veto.
- `upload_files` attaches files by path and only checks that the path is an
  existing file: there is no directory allowlist. A client that can name a path
  the user can read can therefore attach it to a page's file input. Restrict
  this the way you would restrict any other file-reading tool.
- A page screenshot is scoped to a browser tab; a desktop screenshot may
  include any visible application. Review screenshots before sharing them.

BTAP is an automation tool, not a sandbox or security boundary. Use a browser
profile and accounts appropriate for the MCP client, and avoid shared or
production machines when the impact of a mistaken action would be unacceptable.
