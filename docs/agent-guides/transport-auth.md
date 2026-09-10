# Transport authentication

Read before changing HTTP authentication, rejected-request handling, extension
client ownership, reconnect grace periods, or logging of browser data.

Start with [AGENTS.md](../../AGENTS.md) for the task entry point. Numbered
sections retain the original guide's references; other section numbers refer
to that root guide. Commands and release gates remain in
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## 6. The HTTP port is token-authenticated

Port 18766 (`/link`, `/api/result`, `/api/longpoll`) requires a bearer token,
which closes the hole where any local process could execute JS in your browser.
The port-18765 WebSocket used by the extension is checked by origin instead and
is unaffected. Full operator detail is in
[SECURITY.md](../../SECURITY.md) and [docs/TROUBLESHOOTING.md](../TROUBLESHOOTING.md);
the two facts that catch people writing code here:

- A rejected request returns **plain text** `unauthorized: missing or bad bridge
  token`, **not** JSON. A client that parses `{"error": ...}` reports a parse
  failure instead of the real cause.
- Every authenticated route must **drain the request body before** returning 401,
  via `check_link_token_drained` rather than `check_link_token`. On Windows,
  wsgiref resets the connection on a rejected request that still has an unread
  body -- guaranteed once the body exceeds the socket buffer -- so the caller
  sees a dropped connection instead of a 401. Copy that helper for any new
  authenticated route.

The WebSocket port is the asymmetric half, and the thing that makes it
survivable is not the origin check. `clientId` arrives in the message body and
the sender chooses it, so writing it straight into `ext_clients` handed the
namespace to whoever spoke last -- and every later `ext_cmd` with it.
`_claim_ext_client` binds an id to one socket instead, which needs no protocol
change because `handle_close` already unbinds a socket that goes away: an
ordinary reconnect finds no incumbent, so only a *still-open* one can be in the
way. Two halves of that must survive an edit:

- **A refused socket is closed, and nothing else about it is recorded.** Not its
  tabs, and not `last_ext_seen` / `client_last_seen` -- refreshing those would
  let a stranger make a dead extension read as freshly checked in, which is the
  one field `doctor` uses to tell those apart. Closing is what makes it
  self-heal: the real extension reconnects and wins the moment the stale socket
  ages out, where a silently ignored socket would look connected forever.
- **`CLIENT_TAKEOVER_GRACE_SECONDS` is not padding.** A half-open socket (TCP
  ESTABLISHED, peer gone) is real here -- it is why the extension pings at all
  -- so refusing every newcomer unconditionally locks the bridge out until a
  human restarts it. The stamp it is measured against is refreshed by every
  message the owner sends, keepalive pings included, so an idle browser at 20 s
  per ping is never near the 60 s window.

Refusals are counted rather than swallowed (`rejected_client_takeovers` in
`diagnose`), because the failure this prevents is silent by construction.

Because this lives in the bridge, a change here needs a bridge restart
(section 1). Restarting your editor is not what does it.

## 8. Odds and ends

- What may be written into that log is a **policy**, not a formatting choice:
  `redact_url` at every logging call site, `redact_pattern` for a caller's search
  string, and never the token in any form. `SECURITY.md` states it to operators
  and `tests/test_log_redaction.py` scans the module's source for a `logger.*`
  line carrying a raw `.url` / `['url']` / `url_pattern`, so a new log line that
  writes a URL straight through fails the offline suite rather than shipping.
