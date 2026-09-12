# Transport authentication

Read before changing HTTP authentication, rejected-request handling, extension
client ownership, reconnect grace periods, or logging of browser data.

Start with [AGENTS.md](../../AGENTS.md) for the task entry point. Numbered
sections retain the original guide's references; other section numbers refer
to that root guide. Commands and release gates remain in
[CONTRIBUTING.md](../../CONTRIBUTING.md).

## 6. The HTTP port is token-authenticated

Port 18766 (`/link`, `/api/result`, `/api/longpoll`) requires a bearer token,
which protects that HTTP command channel from processes without the token.
The port-18765 WebSocket used by the extension checks its exact Origin instead:
derive the packaged extension ID from manifest.key when present, or Chromium's
absolute native-path algorithm for an unpacked directory. An invalid manifest
leaves no default trusted Origin. Never learn trust from ext_ready/clientId or
accept every chrome-extension:// Origin. Local non-browser processes can still
forge an allowed Origin, so this is not local-process authentication.
`BROWSERTAP_WS_ALLOWED_ORIGINS` applies to both WebSocket and HTTP origin checks;
HTTP without an Origin header still requires its token. The separate
`BROWSERTAP_WS_ALLOW_NO_ORIGIN` option applies only to WebSocket clients.
Full operator detail is in
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

Resolve explicit state/token overrides against the launching cwd before daemon
spawn changes it. Preserve default/legacy source labels when no override exists.
Token diagnostics distinguish missing, empty, ready, unreadable and invalid UTF-8;
metadata failures leave existence unknown. Never log token bytes, including a
`UnicodeDecodeError`'s raw representation, or replace an existing unreadable file.
Configuration validation must precede network/spawn effects without making
imports or package-path commands depend on valid network settings.

Windows token I/O uses one native handle for security and contents. Verify a
protected current-user-only DACL before the first write, and harden/verify an
existing current-owned regular file before reading it. Security failure must
not replace an existing token, take another user's ownership or invent an
in-memory fallback. New creation remains exclusive until its final write;
failed creation cleanup addresses that handle, never a newly resolved pathname.
POSIX retains exclusive creation with mode 0600. Diagnostics can harden an
existing Windows file but must not create one.

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

- Logging uses `redact_url` for URLs, `redact_pattern` for caller patterns and
  hash references for arbitrary protocol identifiers. Exception boundaries log
  fixed operation names and exception types only; do not attach an exception
  object, traceback, source line or raw browser payload to a LogRecord. Catch
  unexpected HTTP callback failures before Bottle's wsgi.errors fallback while
  preserving HTTPResponse/authentication handling and body draining. Behavioral
  regressions in `tests/test_log_payload_boundary.py` use synthetic sensitive
  strings; `tests/test_log_redaction.py` also checks URL/pattern call sites.
