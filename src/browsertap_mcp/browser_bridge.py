import hashlib
import hmac
import json
import logging
import math
import os
import queue
import re
import secrets
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import bottle
import requests
from bottle import request
from simple_websocket_server import WebSocket, WebSocketServer

from ._version import __version__
from .paths import (
    DEFAULT_STATE_DIR_NAME,
    LEGACY_STATE_DIR_NAME,
    STATE_DIR_ENV,
    state_dir,
)

logger = logging.getLogger(__name__)


#: Longest path kept in a log line. Enough to tell two pages of one site apart.
LOG_URL_PATH_LIMIT = 48


def redact_url(url: Any, *, path_limit: int = LOG_URL_PATH_LIMIT) -> str:
    """Return a URL trimmed to what a log needs: origin plus a short path.

    ``bridge.log`` is the file operators are told to attach to a bug report, and
    it survives for the life of an install. Logging the tab URL verbatim put the
    query string in it -- OAuth codes, signed download links, session tokens and
    search terms all live there -- so a routine "here is my log" handed those out
    with it. The origin and a truncated path answer every question the log is
    actually read for ("which tab was this?"), so drop the rest at the source
    rather than asking anyone to scrub the file afterwards.

    Marks what it removed instead of hiding it: a trailing ``?...`` means there
    was a query, ``#...`` a fragment, and ``...`` a path that was longer.
    """
    if not isinstance(url, str) or not url.strip():
        return "<no url>"
    text = url.strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "<unparsable url>"
    if not parsed.scheme:
        # A bare relative reference carries no origin worth keeping.
        return "<relative url>"
    if parsed.scheme in {"file", "data", "blob", "javascript"}:
        # A local path, an inline payload and a bookmarklet are all content, not
        # location. None of them identify a tab better than the scheme does.
        return f"{parsed.scheme}:<redacted>"
    host = parsed.netloc
    if "@" in host:  # strip any userinfo, credentials included
        host = host.rsplit("@", 1)[1]
    path = parsed.path or ""
    if len(path) > path_limit:
        path = path[:path_limit] + "..."
    suffix = "?..." if parsed.query else ""
    if parsed.fragment:
        suffix += "#..."
    # about:blank and chrome://newtab differ in whether they have an authority;
    # inventing "//" for the first spells a URL that does not exist.
    separator = "://" if parsed.netloc else ":"
    return f"{parsed.scheme}{separator}{host}{path}{suffix}"


def redact_pattern(pattern: Any, *, limit: int = LOG_URL_PATH_LIMIT) -> str:
    """Redact a caller-supplied URL *pattern* the same way, without over-cutting.

    ``set_session`` matches this as a plain substring, so it is normally a bare
    host or a path fragment -- ``redact_url`` would reduce that to
    ``<relative url>`` and log nothing useful. It can still be a whole URL that
    the caller pasted, query string included, so everything past a ``?`` or
    ``#`` goes, and the markers say so exactly as above.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return "<no pattern>"
    text, marker = pattern.strip(), ""
    for cut in ("?", "#"):
        head, sep, _ = text.partition(cut)
        if sep:
            text, marker = head, sep + "..."
            break
    if len(text) > limit:
        text, marker = text[:limit], "..." + marker
    return text + marker


class SessionNotConnectedError(ValueError):
    error_code = "session_not_connected"


class SessionDisconnectedError(ValueError):
    error_code = "session_disconnected"


class ExtensionNotConnectedError(ValueError):
    error_code = "extension_not_connected"


_PAGE_ACCESS_ERROR_PATTERNS = (
    "cannot access contents of the page",
    "extensions gallery cannot be scripted",
    "cannot access a chrome:// url",
    "cannot access contents of a chrome-extension",
)
_PAGE_POPUP_ERROR_PATTERNS = (
    "popup",
    "user gesture",
    "not allowed to open",
)


def _page_error_source(detail: Any) -> tuple[str, dict[str, Any]]:
    """Return a browser error message and its structured source fields."""
    if isinstance(detail, dict):
        nested = detail.get("error")
        source = nested if isinstance(nested, dict) else detail
        message = source.get("message")
        if not isinstance(message, str) or not message:
            message = detail.get("message")
        if not isinstance(message, str) or not message:
            message = nested if isinstance(nested, str) else detail.get("error")
        return str(message or detail), source
    return str(detail), {}


def _page_script_intent(script: str) -> Optional[str]:
    if not isinstance(script, str):
        return None
    if re.search(
        r"(?:window\.open\s*\(|(?:window\.)?location(?:\.assign|\.replace)?\s*=|target\s*=\s*['\"]_blank)",
        script,
        re.IGNORECASE,
    ):
        return "new_tab_or_navigation"
    return None


def _page_execution_metadata(
    detail: Any,
    *,
    script: str = "",
    explicit_code: Optional[str] = None,
    explicit_diagnostics: Optional[dict[str, Any]] = None,
) -> tuple[str, bool, dict[str, Any]]:
    """Classify a page-side failure without pretending to know its root cause."""
    message, source = _page_error_source(detail)
    lower = message.lower()
    container = detail if isinstance(detail, dict) else {}
    diagnostics = dict(explicit_diagnostics or {})

    def field(name: str) -> Any:
        if name in container:
            return container[name]
        return source.get(name)

    dispatched = field("dispatched")
    may_have_executed = field("may_have_executed")
    retryable = field("retryable")
    csp = bool(field("csp"))
    if dispatched is not None:
        diagnostics["dispatched"] = bool(dispatched)
    if may_have_executed is not None:
        diagnostics["may_have_executed"] = bool(may_have_executed)
    if retryable is not None:
        diagnostics["retryable"] = bool(retryable)
    if csp:
        diagnostics["csp"] = True

    code = explicit_code
    if not code or code == "internal_error":
        code = container.get("error_code") or source.get("error_code")
    if not code:
        code = container.get("code") or source.get("code")
    access_denied = any(pattern in lower for pattern in _PAGE_ACCESS_ERROR_PATTERNS)
    if not code:
        code = "page_access_denied" if access_denied else "page_execution_failed"

    intent = _page_script_intent(script)
    if intent:
        diagnostics["script_intent"] = intent
        if any(pattern in lower for pattern in _PAGE_POPUP_ERROR_PATTERNS):
            diagnostics["policy_constraint"] = "popup_or_user_gesture"
            diagnostics["next_action"] = "open_new_tab"
        elif access_denied:
            diagnostics["next_action"] = "open_new_tab_or_choose_scriptable_tab"

    if access_denied:
        diagnostics.setdefault("access_kind", "script_injection")
        diagnostics.setdefault(
            "next_action",
            "list_tabs_then_retry_or_use_supported_cdp",
        )

    # A browser error that reached the page route is not safe to replay unless
    # the browser explicitly proved that it never dispatched it.
    if retryable is not None:
        retry_safe = bool(retryable)
    else:
        retry_safe = dispatched is False and may_have_executed is False and not access_denied
    diagnostics.setdefault("retry_safe", retry_safe)
    return str(code), retry_safe, diagnostics


def _is_page_execution_error(detail: Any, error_code: Optional[str] = None) -> bool:
    if isinstance(error_code, str) and error_code in {
        "page_access_denied", "page_execution_failed", "page_injection_blocked",
        "debugger_detached", "cdp_error",
    }:
        return True
    message, _ = _page_error_source(detail)
    lower = message.lower()
    return any(pattern in lower for pattern in _PAGE_ACCESS_ERROR_PATTERNS)


class PageExecutionError(RuntimeError):
    """A page-side execution failure with delivery and retry facts attached."""

    error_code = "page_execution_failed"

    def __init__(
        self,
        detail: Any,
        *,
        script: str = "",
        error_code: Optional[str] = None,
        diagnostics: Optional[dict[str, Any]] = None,
    ) -> None:
        code, retry_safe, facts = _page_execution_metadata(
            detail,
            script=script,
            explicit_code=error_code,
            explicit_diagnostics=diagnostics,
        )
        super().__init__(detail)
        self.error_code = code
        self.retry_safe = retry_safe
        self.diagnostics = facts


# Delivery states a caller may retry without risking a duplicate side effect.
#
# 'undelivered' is provable from the bridge's own bookkeeping: the payload never
# left this process (the total deadline was gone before dispatch, or an http
# session's queue was never polled).
#
# 'sent_unconfirmed' is weaker — protocol-derived, not proven. The frame was
# written to a live extension socket and no ACK came back. background.js ACKs
# *before* it executes and skips execution outright when that ACK cannot be
# sent, so no ACK normally means nothing ran. The residual window is an ACK that
# was sent and then lost (socket death, or the WS server rebuilding between the
# extension's write and ours), in which case the script did run. Callers doing
# irreversible work should branch on delivery_state instead of trusting
# retry_safe, and 'delivered_no_result'/'navigated' are never retry-safe.
RETRY_SAFE_DELIVERY_STATES = frozenset({'undelivered', 'sent_unconfirmed'})

# How long a tab must have been registered before failover will prefer it. A
# content script connects around document_idle, i.e. before the page has settled,
# so a session that appeared a moment ago names a tab that is probably still
# loading. See BrowserBridge._pick_failover_session for what that broke.
FAILOVER_SETTLE_SECONDS = 2.0

# A newly spawned daemon can answer before the MV3 worker has completed its
# first handshake. Keep that normal startup window distinct from a broken
# extension so doctor tells the caller to wait instead of reload.
BRIDGE_STARTUP_GRACE_SECONDS = 10.0

# How long the socket owning a clientId namespace may say nothing before another
# socket is allowed to take it over. The extension pings every 20s (KEEPALIVE_MS
# in background.js), so this is three missed ticks: long enough that a live
# extension is never displaced, short enough that a half-open zombie cannot hold
# the namespace for more than a minute. See BrowserBridge._claim_ext_client for
# why refusing a takeover outright is not an option.
CLIENT_TAKEOVER_GRACE_SECONDS = 60.0

# Pages Chrome refuses to script no matter who asks. The extensions gallery is
# the trap in this list: it is an ordinary https:// page, so the content script
# registers a session there and the tab joins every automatic pick like any
# other, yet the injection is rejected with "The extensions gallery cannot be
# scripted." and nothing is dispatched -- which reads as a bridge fault rather
# than a bad target. Measured in a live run: the failover escape hatch chose
# chromewebstore.google.com/category/extensions and failed deterministically,
# where the same test passed the moment that tab was not the newest one.
#
# Only automatic picks consult this. A caller that names such a tab still gets
# the real error, because refusing on its behalf would hide which tab it asked
# for -- same rule as a dead tab that was named explicitly.
UNSCRIPTABLE_URL_PREFIXES = (
    'chrome://', 'edge://', 'about:', 'devtools://', 'view-source:',
    'chrome-extension://', 'moz-extension://', 'chrome-untrusted://',
    'https://chromewebstore.google.com/', 'https://chrome.google.com/webstore',
    'https://microsoftedge.microsoft.com/addons',
)


def is_scriptable_url(url: Any) -> bool:
    """Whether an automatic pick may land on *url*.

    Unknown and empty URLs count as scriptable: a session only exists because a
    content script ran in it, so the benefit of the doubt matches how the tab
    got here, and being wrong costs one ordinary error instead of narrowing the
    pool to nothing.
    """
    if not isinstance(url, str):
        return True
    text = url.strip().lower()
    if not text:
        return True
    return not text.startswith(UNSCRIPTABLE_URL_PREFIXES)


def _prefer_scriptable(pool: list) -> list:
    """Drop un-scriptable tabs from *pool*, unless that would empty it.

    Same shape as the settle filter below: a preference, not a requirement. A
    browser showing nothing but chrome:// pages behaves exactly as it did
    before rather than starting to refuse.
    """
    return [s for s in pool if is_scriptable_url(getattr(s, 'url', ''))] or pool



class BridgeNoResponseError(RuntimeError):
    """A command has no result, with explicit delivery and retry semantics."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "no_response",
        delivery_state: str,
        retry_safe: bool,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.delivery_state = delivery_state
        self.retry_safe = bool(retry_safe)


def _no_response_result(
    message: str,
    *,
    delivery_state: str,
    executed_tab_id: Optional[int],
    extra: Optional[dict[str, Any]] = None,
    closed: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "result": message,
        "error_code": "no_response",
        "delivery_state": delivery_state,
        "retry_safe": delivery_state in RETRY_SAFE_DELIVERY_STATES,
        "executed_tab_id": executed_tab_id,
        **(extra or {}),
    }
    if closed:
        result["closed"] = 1
    return result


def _error_payload(exc: Exception, *, prefix: str = "") -> dict[str, Any]:
    message = f"{prefix}: {exc}" if prefix else str(exc)
    payload: dict[str, Any] = {
        "error": message,
        "error_code": str(getattr(exc, "error_code", "internal_error")),
    }
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict):
        payload["diagnostics"] = diagnostics
    return payload

# --- /link 鉴权 --------------------------------------------------------------
# WS 口靠 origin 前缀挡住网页（扩展读不到磁盘上的密钥，只能这么做）；但 /link 是
# 命令通道，本机任意进程都能 POST 一条 execute_js 在用户登录态的 Chrome 里跑任意
# JS。BTAP 默认用用户目录中的持久 token 鉴权；旧环境变量只用于一次迁移。
TOKEN_ENV = 'BROWSERTAP_BRIDGE_TOKEN'
TOKEN_FILE_ENV = 'BROWSERTAP_BRIDGE_TOKEN_FILE'
TOKEN_AUTH_ENV = 'BROWSERTAP_BRIDGE_AUTH'

# Extra socket budget a remote (is_remote=True) client grants itself on top of
# the command timeout it hands the daemon. Without it the HTTP read deadline and
# the daemon's own deadline expire at the same instant, so a timed-out command
# almost always dies at the transport layer first and the caller gets a bare
# TimeoutError instead of the daemon's structured verdict (delivery_state /
# retry_safe / executed_tab_id) — which is exactly the information needed to
# decide whether retrying is safe. An MCP server process is normally remote (see
# get_driver), so this is the main path, not an exotic one. The cost is that a
# misbehaving daemon can overrun the caller's total deadline by at most this
# margin; loopback HTTP needs only milliseconds of it.
REMOTE_TRANSPORT_MARGIN = 2.0


def _positive_timeout(value: Any, *, name: str = "timeout") -> float:
    """Normalize one timeout without allowing an unbounded/non-running wait."""
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be a finite number greater than zero"
        ) from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")
    return normalized


def _timeout_or_default(value: Any, default: float) -> float:
    return _positive_timeout(default if value is None else value)


def bridge_token_path() -> Path:
    """Return the one persistent token location shared by all BTAP processes."""
    configured = (os.environ.get(TOKEN_FILE_ENV) or '').strip()
    return Path(configured).expanduser() if configured else state_dir() / 'bridge-token'


def _read_token_file(path: Path) -> str:
    try:
        value = path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return ''
    return value


def _persist_token(path: Path, token: str) -> str:
    """Create the token file once; concurrent starters converge on its value."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            pending = memoryview((token + '\n').encode('utf-8'))
            while pending:
                written = os.write(fd, pending)
                if written <= 0 or written > len(pending):
                    raise OSError(f'invalid token write count: {written}')
                pending = pending[written:]
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return token
    except FileExistsError:
        # O_EXCL exposes the file just before the winning process writes its
        # contents. Give that tiny window time to close instead of inventing a
        # different in-memory token that would immediately split the clients.
        for _ in range(20):
            stored = _read_token_file(path)
            if stored:
                return stored
            time.sleep(0.01)
        raise RuntimeError(f'BTAP bridge token file is empty: {path}')
    except OSError as exc:
        raise RuntimeError(f'BTAP cannot persist its bridge token at {path}: {exc}') from exc


def bridge_token() -> str:
    """Read or create the stable per-user token shared by every BTAP process.

    The old env var is a one-time bootstrap for existing installs. Once the file
    exists, later editor environments cannot rotate or replace it. Authentication
    can only be disabled deliberately via ``BROWSERTAP_BRIDGE_AUTH=off``.
    """
    auth_mode = (os.environ.get(TOKEN_AUTH_ENV) or '').strip().lower()
    if auth_mode in {'0', 'false', 'off', 'disabled'}:
        return ''
    path = bridge_token_path()
    stored = _read_token_file(path)
    if stored:
        return stored
    legacy = (os.environ.get(TOKEN_ENV) or '').strip()
    return _persist_token(path, legacy or secrets.token_urlsafe(32))


def token_fingerprint(token: Any) -> Optional[str]:
    """A short, comparable stand-in for a token that is safe to print.

    Diagnostics have to answer "are the bridge and this client using the same
    token?" without ever putting the token itself in a log, a bug report or an
    MCP result. 32 bits of a SHA-256 is enough to spot a mismatch and far too
    little to work backwards from a 256-bit secret.
    """
    if not isinstance(token, str) or not token:
        return None
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def state_paths_report(*, enforced_token: Any = None) -> dict[str, Any]:
    """Report where *this* process resolved its state directory and token file.

    Both are resolved per process from the environment, and the two that matter
    are started by different things at different times: the daemon by whichever
    MCP session lost the spawn race, the client by the editor. When their
    environments disagree the only symptom is a 401 whose body says nothing
    about paths, and an install that predates 0.4.0 adds a second way to differ
    (``~/.agent-browser-mcp`` is still used when it is the only directory that
    exists -- see :func:`paths.state_dir`). Nothing here discloses the token:
    ``token_fingerprint`` is a hash, and ``enforced_token`` -- the value the
    daemon locked into memory at start-up -- only ever contributes one.

    That last field is what separates the two ways a token check can fail: a
    ``token_matches_file`` of false with equal paths means the daemon is running
    from before the file changed, so restarting the bridge fixes it; unequal
    paths mean the environments differ and restarting fixes nothing.
    """
    directory = state_dir()
    configured_dir = bool((os.environ.get(STATE_DIR_ENV) or "").strip())
    if configured_dir:
        kind = "env"
    elif directory.name == LEGACY_STATE_DIR_NAME:
        kind = "legacy"
    else:
        kind = "default"
    token_path = bridge_token_path()
    stored = _read_token_file(token_path)
    auth_mode = (os.environ.get(TOKEN_AUTH_ENV) or "").strip().lower()
    report: dict[str, Any] = {
        "state_dir": str(directory),
        "state_dir_exists": directory.is_dir(),
        "state_dir_kind": kind,
        "state_dir_env": STATE_DIR_ENV if configured_dir else None,
        "default_state_dir_name": DEFAULT_STATE_DIR_NAME,
        "token_file": str(token_path),
        "token_file_exists": bool(stored),
        "token_file_from_env": bool((os.environ.get(TOKEN_FILE_ENV) or "").strip()),
        "auth_enabled": auth_mode not in {"0", "false", "off", "disabled"},
        "token_fingerprint": token_fingerprint(stored),
    }
    # An empty enforced token is not a mismatch: that is what `bridge_token`
    # returns when authentication is deliberately off, and reporting it as one
    # would turn every BROWSERTAP_BRIDGE_AUTH=off install into a false alarm.
    # `auth_enabled` is the field that describes that case.
    if isinstance(enforced_token, str) and enforced_token:
        report["enforced_token_fingerprint"] = token_fingerprint(enforced_token)
        report["token_matches_file"] = bool(
            stored and hmac.compare_digest(stored, enforced_token)
        )
    return report


def header_token(headers) -> str:
    """从 Authorization: Bearer <t> 或 X-Bridge-Token 取 token，取不到返回 ''。

    两个头都收：Authorization 是常规写法，X-Bridge-Token 留给设不了
    Authorization 的调用方（某些 userscript / 反代）。
    """
    auth = (headers.get('Authorization', '') or '').strip()
    if auth[:7].lower() == 'bearer ':
        return auth[7:].strip()
    return (headers.get('X-Bridge-Token', '') or '').strip()


def check_link_token(headers, want: str) -> None:
    """token 不对就抛 401。want 为空仅表示鉴权被显式关闭。"""
    if not want:
        return
    got = header_token(headers)
    # compare_digest 而非 ==：避免按字节提前返回泄露前缀。
    if not got or not hmac.compare_digest(got, want):
        raise bottle.HTTPResponse(
            status=401, body='unauthorized: missing or bad bridge token')


def drain_request_body() -> None:
    """Consume whatever the current request still has unread.

    Bottle reads a body only for the content types it parses, so a request this
    server declines to act on can leave its bytes in the socket -- and wsgiref on
    Windows then resets the connection instead of delivering the response, which
    reads to the caller as a dead bridge rather than as the error it actually
    was. Measured on this server: a small refused POST aborts a few percent of
    the time, one larger than the socket buffer aborts every time. Reading past
    MEMFILE_MAX covers a body big enough that bottle spilled it to disk.

    Every route that can answer without parsing the body needs this, which is why
    it is one function and not an idiom repeated per route -- a refusal that
    forgets it does not fail, it turns into a transport error somewhere else.
    """
    try:
        request.body.read(request.MEMFILE_MAX + 1)
    except Exception:
        pass


def check_link_token_drained(headers, want: str) -> None:
    """check_link_token，但被拒的请求不会把自己的 body 留在 socket 里。

    The 401 has to survive the body it just refused to read; see
    `drain_request_body`. The response object is preserved, and the route body
    never runs.
    """
    try:
        check_link_token(headers, want)
    except bottle.HTTPResponse:
        drain_request_body()
        raise


# 单参数版：want 从共享文件现读。测试与外部调用方按此名引用；桥内部用
# check_link_token(headers, want) 传已锁定的 token。
def require_link_token(headers) -> None:
    check_link_token(headers, bridge_token())


def json_object_body():
    """The request's JSON object, or None if the client did not send one.

    Draining is the whole point, and it is the same trap
    ``check_link_token_drained`` exists for one step later. ``request.json`` is
    None for a body bottle will not parse and raises for one that is malformed,
    so reading a field off it raised ``AttributeError``, bottle turned that into
    a 500, and the request's bytes were still unread in the socket -- where
    wsgiref on Windows answers by resetting the connection. The caller then sees
    a dropped connection and cannot tell a malformed request from a dead bridge.
    Read past ``MEMFILE_MAX`` so nothing is left even for a body bottle spilled
    to disk.

    Returning None rather than raising leaves the answer to the route: these
    three speak different protocols to different clients, and a shared error
    shape would be wrong for at least two of them.
    """
    try:
        data = request.json
    except Exception:
        data = None
    if isinstance(data, dict):
        return data
    try:
        request.body.read(request.MEMFILE_MAX + 1)
    except Exception:
        pass
    return None


# How long an HTTP session may go without long-polling before it counts as gone.
# That transport has no close event -- a WebSocket does -- so silence is the only
# signal there is, and every answered poll refreshes `connect_at`.
HTTP_SESSION_IDLE_SECONDS = 60

# How long one poll waits for a command before answering "ask again". It has to
# stay well under the idle timeout above: the clock the timeout measures only
# advances when a poll returns, so a window as long as the timeout would let a
# client that polls without pause still be counted as silent.
HTTP_POLL_SECONDS = 5

# The transports a client may register as: `ws` is a page's content script,
# `ext_ws` the extension's own service worker, `http` a long-poll client that has
# no socket at all.
_SOCKET_TYPES = frozenset({'ws', 'ext_ws'})
_QUEUE_TYPE = 'http'


class Session:
    """One connected tab, and the single channel that reaches it.

    The channel is stored once. Two mutually exclusive slots -- a socket and a
    queue -- meant the "exactly one is set" invariant was maintained by hand in
    the constructor and again in `reconnect`, where a transport matching neither
    branch left the *previous* one's handle in place: a client registering as
    something this bridge does not serve inherited whatever socket or queue was
    there before it. Deriving both names from one slot makes that unrepresentable
    rather than merely fixed, and it is what lets `reconnect` *be* the
    constructor's body instead of a second copy of it that can drift.

    `type` is derived from `info` for the same reason. Copying it out at
    construction meant the paths that refresh `info` alone left a session whose
    declared transport and whose actual routing disagreed.
    """

    def __init__(self, session_id, info, client=None):
        self.id = session_id
        self.reconnect(client, info)

    def reconnect(self, client, info):
        """Rebind to a new channel; connecting for the first time is the same act."""
        self.info = info
        self.client = client
        self.connect_at = time.time()
        self.disconnect_at = None

    @property
    def type(self):
        return self.info.get('type', 'ws')

    @property
    def url(self):
        return self.info.get('url', '')

    @property
    def ws_client(self):
        """The socket, or None when this session is not reached over one."""
        return self.client if self.type in _SOCKET_TYPES else None

    @property
    def http_queue(self):
        """The long-poll queue, or None when this session is not reached over one."""
        return self.client if self.type == _QUEUE_TYPE else None

    def is_active(self):
        """Whether this session can still be reached.

        The expiry check *records* the transition rather than merely reporting it,
        which is deliberate: `clean_sessions` reaps on `disconnect_at`, so an HTTP
        session that timed out without ever being stamped would sit in the table
        for the life of a daemon that outlives every MCP session.
        """
        if (self.type == _QUEUE_TYPE
                and time.time() - self.connect_at > HTTP_SESSION_IDLE_SECONDS):
            self.mark_disconnected()
        return self.disconnect_at is None

    def mark_disconnected(self):
        # Records the transition, not the state: callers re-run this over the
        # whole table on every extension snapshot, so a tab that is already
        # gone would be re-stamped a few times a second. That kept
        # `now - disconnect_at` under `clean_sessions`'s reap window forever,
        # so dead sessions were never dropped from a daemon that outlives every
        # MCP session -- and their log lines buried everything else.
        if self.disconnect_at is not None:
            return
        logger.info("Tab disconnected: %s (session=%s)", redact_url(self.url), self.id)
        self.disconnect_at = time.time()


class BrowserBridge:
    # Refused ext_clients takeovers, and the last one, for diagnose(). Class
    # level so a driver built without __init__ still reads a number instead of
    # raising -- the offline tests build one that way because __init__ binds
    # ports, and remote mode skips half of it too.
    rejected_client_takeovers = 0
    last_rejected_takeover = None

    def __init__(self, host: str = '127.0.0.1', port: int = 18765):
        self.host, self.port = host, port
        self.started_at = time.time()
        self.sessions, self.results, self.acks = {}, {}, {}
        # Old session handles remain useful only when the extension provided
        # explicit replacement evidence (`tabs.onReplaced`). Never infer this
        # map from URL/title or from a reused native id.
        self._rebindings = {}
        # Commands are completed by the HTTP/WS server threads. A condition lets
        # the waiting caller resume as soon as one of those threads records a
        # result, ACK or session lifecycle change instead of paying the old
        # 50 ms polling slice on every successful bridge round trip.
        self._activity_condition = threading.Condition()
        self._activity_serial = 0
        self.default_session_id = None
        self.latest_session_id = None
        # Last time ANY extension pushed ext_ready/tabs_update. Lets `doctor`
        # tell "extension never registered" (0 or stale) from "registered but
        # just dropped" — the single most useful signal for classifying why
        # /link is empty. None until the first registration this daemon sees.
        self.last_ext_seen = None
        # Per-client last-seen: which browser is stale vs which just dropped.
        self.client_last_seen = {}
        # client_id -> {'ws','browser','ts'}: the extension's own socket, one per
        # browser, independent of how many tabs exist. Addresses the service
        # worker directly for tab-less commands; see ext_cmd().
        self.ext_clients = {}
        with socket.socket() as _probe:
            _probe.settimeout(1)
            self.is_remote = _probe.connect_ex((host, port+1)) == 0
        if not self.is_remote:
            # Both bundled servers set SO_REUSEADDR, which on Windows lets a
            # second process bind the very same ports and steal a share of the
            # connections (extension on one host, /link on the other -> lost
            # results). An exclusive lock socket on port+2 guarantees exactly
            # one host; losers wait for the winner and go remote.
            self._host_lock = self._acquire_host_lock()
            if self._host_lock is None:
                for _ in range(20):
                    time.sleep(0.25)
                    with socket.socket() as s:
                        s.settimeout(1)
                        if s.connect_ex((host, port + 1)) == 0: break
                self.is_remote = True
        if not self.is_remote:
            self.start_ws_server()
            self.start_http_server()
        else:
            self.remote = f'http://{self.host}:{self.port+1}/link'
            # trust_env=False: HTTP(S)_PROXY env vars (no NO_PROXY) would route
            # loopback bridge calls through the system proxy — a restart there
            # surfaces as 502/refused, i.e. phantom bridge outages. Session
            # also gives us connection pooling for repeated calls.
            self._http = requests.Session()
            self._http.trust_env = False

    def _activity_snapshot(self) -> Optional[int]:
        """Return the current activity generation, or None for legacy stubs."""
        condition = getattr(self, '_activity_condition', None)
        if condition is None:
            return None
        with condition:
            return self._activity_serial

    def _notify_activity(self) -> None:
        """Wake command/session waiters after publishing their new state."""
        condition = getattr(self, '_activity_condition', None)
        if condition is None:
            return
        with condition:
            self._activity_serial += 1
            condition.notify_all()

    def _wait_for_activity(self, seen: Optional[int], timeout: float) -> Optional[int]:
        """Wait until activity advances, retaining sleep fallback for test stubs."""
        if timeout <= 0:
            return seen
        condition = getattr(self, '_activity_condition', None)
        if condition is None or seen is None:
            time.sleep(timeout)
            return None
        with condition:
            # The generation closes the check-then-wait race: if a result landed
            # after the caller inspected `results` but before acquiring this
            # lock, it observes the changed value and does not sleep.
            if self._activity_serial == seen:
                condition.wait(timeout)
            return self._activity_serial

    def _acquire_host_lock(self):
        s = socket.socket()
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((self.host, self.port + 2))
            s.listen(1)
            return s  # held open for the process lifetime
        except OSError:
            s.close()
            return None

    def start_http_server(self):
        self.app = app = bottle.Bottle()
        # 启动时锁定一次。所有进程从同一个持久文件取 token；运行期间若有人做完整
        # 用户数据清理，旧 daemon 仍保持原值，必须在清理前先停止它。
        self.link_token = bridge_token()
        if self.link_token:
            logger.info("Bridge token authentication enabled (file=%s)", bridge_token_path())

        @app.hook('before_request')
        def _reject_cross_origin():
            # These routes execute JS in the user's logged-in tabs, so nothing
            # that carries a web Origin may reach them. In practice bottle's
            # request.json requires Content-Type: application/json, which forces
            # a CORS preflight this server never answers — but that is a
            # side effect of the parser, not a decision. Make it explicit, and
            # deliberately send no Access-Control-* headers so no browser ever
            # gets permission to read a response.
            origin = request.headers.get('Origin', '') or ''
            if origin and not origin.startswith(
                    ('chrome-extension://', 'moz-extension://',
                     'safari-web-extension://', 'extension://')):
                extra = os.environ.get('BROWSERTAP_WS_ALLOWED_ORIGINS', '')
                if not any(origin == o.strip() for o in extra.split(',') if o.strip()):
                    raise bottle.HTTPResponse(status=403, body='forbidden origin')

        @app.route('/api/longpoll', method=['GET', 'POST'])
        def long_poll():
            # 结果回传通道与 /link 同源：token 已配置时必须同样鉴权，否则本机
            # 任意进程可在无 Origin 头的情况下注册伪造会话（本地纵深防御）。
            # 显式关闭鉴权时保持旧版 userscript 轮询客户端兼容。被拒时按
            # check_link_token_drained 排空 body，401 才不会退化成连接中断。
            check_link_token_drained(request.headers, self.link_token)
            data = json_object_body()
            session_id = (data or {}).get('sessionId')
            if not session_id:
                # Either a body this route could not read, or one naming no
                # session. Both are unanswerable rather than merely empty, and
                # both have the same fix: a session with no id cannot be named by
                # `execute_js`, so it could never be handed a command and would
                # poll forever while occupying a row in the table. `ret` is free
                # text to this route's clients -- they compare against the two
                # values below and poll again on anything else -- so an
                # unfamiliar value is the in-protocol way to say this.
                return json.dumps({"id": "", "ret": "invalid request"})
            session_info = {'url': data.get('url'), 'title': data.get('title', ''),
                            'type': _QUEUE_TYPE}
            session = self.sessions.get(session_id)
            if session is None:
                session = self.sessions[session_id] = Session(
                    session_id, session_info, queue.Queue())
                logger.info("Browser HTTP connected: %s (session=%s)",
                            redact_url(session.url), session_id)
            elif session.type == _QUEUE_TYPE:
                # The same client polling again, so rebind it to the queue it
                # already has rather than a fresh one: a command handed over
                # while it was between polls is sitting in there, and swapping
                # the queue would drop it with nothing anywhere reporting a loss.
                # Going through `reconnect` is also what refreshes `info` -- the
                # three field writes this replaces left a resurrected session
                # describing whichever URL the tab had on its first ever poll.
                session.reconnect(session.client, session_info)
            elif session.is_active():
                # A live socket already reaches this tab. Refusing is what keeps
                # a command from being handed to the weaker of two channels.
                return json.dumps({"id": "", "ret": "use ws"})
            else:
                # The socket is gone and this client speaks for the tab now.
                session.reconnect(queue.Queue(), session_info)
            self._notify_activity()
            try:
                msg = session.http_queue.get(timeout=HTTP_POLL_SECONDS)
            except queue.Empty:
                return json.dumps({"id": "", "ret": "next long-poll"})
            # Handing the payload over is the acknowledgement: this transport has
            # no ack frame the way the socket does, so the moment the command
            # leaves is the only evidence there is that it was delivered.
            try:
                exec_id = json.loads(msg).get('id')
            except Exception:
                # Everything on this queue was serialised by `_send_to_session`,
                # so a payload that will not parse is this bridge's own bug, not
                # a client's. Hand it over anyway -- refusing would strand a
                # caller already waiting on its result -- but say so.
                logger.exception("unparseable long-poll payload for session=%s", session_id)
            else:
                # An id-less payload acknowledges nothing. Recording it under the
                # empty string, as this did, put a key in `acks` that no waiter
                # ever looks up -- `exec_id` is a uuid4 -- so it sat there until
                # the 600-second sweep collected it.
                if exec_id:
                    self.acks[exec_id] = time.time()
                    self._notify_activity()
            return msg

        @app.route('/api/result', method=['GET','POST'])
        def result():
            # 与 /api/longpoll 同理：token 已配置时伪造结果注入同样需要鉴权。
            check_link_token_drained(request.headers, self.link_token)
            data = json_object_body()
            if data is None:
                # Same unread-body trap as the poll route, and the same reason it
                # cannot just let the exception out: this answers a client that is
                # posting a result, so a 500 plus a reset connection would make it
                # retry a delivery that was never wrong about anything but its
                # framing.
                return 'ok'
            kind = data.get('type')
            if kind not in ('result', 'error'):
                return 'ok'
            # One record either way. Split in two, the two branches differed only
            # in `success` and in which field the payload was read from, so every
            # later addition to the shape had to be made twice -- `newTabs`,
            # `tabId` and `ts` are all there because they were.
            self.results[data.get('id')] = {
                'success': kind == 'result',
                'data': data.get('result' if kind == 'result' else 'error'),
                'newTabs': data.get('newTabs', []),
                'tabId': data.get('tabId'),
                'ts': time.time(),
            }
            self._notify_activity()
            return 'ok'

        @app.route('/link', method=['GET','POST'])
        def link():
            # 命令通道，先鉴权再解析 body；仅显式关闭鉴权时是 no-op。被拒的请求
            # 由 check_link_token_drained 排空 body，否则 Windows 上 401 会退化成
            # 连接重置。
            check_link_token_drained(request.headers, self.link_token)
            data = json_object_body()
            if data is None:
                return json.dumps({'r': {
                    'error': 'body must be a JSON object containing cmd',
                    'error_code': 'invalid_request',
                }},
                                  ensure_ascii=False)
            cmd = data.get('cmd')
            if cmd == 'get_all_sessions':
                try:
                    return json.dumps({'r': self.get_all_sessions()}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='get_all_sessions failed')},
                                      ensure_ascii=False)
            if cmd == 'diagnose':
                try:
                    return json.dumps({'r': self.diagnose()}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='diagnose failed')},
                                      ensure_ascii=False)
            if cmd == 'find_session':
                url_pattern = data.get('url_pattern', '')
                try:
                    return json.dumps({'r': self.find_session(url_pattern)}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='find_session failed')},
                                      ensure_ascii=False)
            if cmd == 'resolve_session':
                try:
                    session_id = data.get('sessionId')
                    if session_id is None:
                        return json.dumps({'r': None}, ensure_ascii=False)
                    return json.dumps(
                        {'r': self._resolve_local_session_target(str(session_id))},
                        ensure_ascii=False,
                    )
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='resolve_session failed')},
                                      ensure_ascii=False)
            if cmd == 'ext_cmd':
                try:
                    payload = data.get('payload')
                    if (
                        not isinstance(payload, dict)
                        or not isinstance(payload.get('cmd'), str)
                        or not payload.get('cmd').strip()
                    ):
                        return json.dumps({'r': {
                            'error': (
                                'ext_cmd requires payload to be a JSON object with a non-empty string cmd field; '
                                'send {"cmd":"ext_cmd","payload":{"cmd":"tabs",...}}'
                            ),
                            'error_code': 'invalid_payload',
                            'hint': 'Do not put the extension command at the /link top level.',
                        }}, ensure_ascii=False)
                    timeout = _positive_timeout(data.get('timeout', 15.0))
                    result = self.ext_cmd(payload,
                                          client_id=data.get('clientId'),
                                          timeout=timeout)
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e)}, ensure_ascii=False)
            if cmd == 'execute_js':
                session_id = data.get('sessionId')
                code = data.get('code')
                # Absent means False: an older client that doesn't send the flag
                # gets the safe behaviour (no substitute tab), not the old one.
                allow_failover = str(data.get('allowFailover', '0')) == '1'
                try:
                    timeout = _positive_timeout(data.get('timeout', 10.0))
                    result = self.execute_js(code, timeout=timeout, session_id=session_id,
                                             allow_failover=allow_failover)
                    logger.debug("Remote execute_js completed (session=%s)", session_id)
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e)}, ensure_ascii=False)
            # 未知 cmd 必须报错而不是含糊地回 "ok"：调用方会把裸 "ok" 当成功，
            # 而实际上什么都没执行（例如把 ext_cmd 的 payload 直接当顶层 cmd 发）。
            return json.dumps(
                {'r': {
                    'error': f'unknown cmd: {cmd!r}; extension commands require cmd=ext_cmd plus payload',
                    'error_code': 'unknown_command',
                }},
                ensure_ascii=False)
        def run():
            from socketserver import ThreadingMixIn
            from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
            class _T(ThreadingMixIn, WSGIServer):
                # A request thread must never outlive shutdown: server_close()
                # would otherwise join a long-poll that still has seconds to go.
                daemon_threads = True
            class _H(WSGIRequestHandler):
                def log_request(self, *a): pass
            # Keep the listener reachable. The daemon never needs this — it dies
            # with its process — but a caller that owns the object rather than the
            # process (a test fixture) can then close the port instead of leaking
            # a listener that later steals requests aimed at its successor.
            self.http_server = make_server(
                self.host, self.port + 1, app, server_class=_T, handler_class=_H)
            self.http_server.serve_forever()
        self.http_server = None
        http_thread = threading.Thread(target=run, daemon=True)
        http_thread.start()

    def stop_http_server(self):
        """Close the /link listener started by start_http_server, if any.

        No-op when the server never got as far as binding (a stubbed thread, a
        remote-mode bridge). shutdown() must come from another thread than
        serve_forever, which is exactly the caller's situation here.
        """
        server = getattr(self, 'http_server', None)
        if server is None:
            return
        self.http_server = None
        try:
            server.shutdown()
        finally:
            server.server_close()

    @staticmethod
    def _ws_origin(sock) -> str:
        """Origin header from the WS handshake, '' if absent."""
        try:
            return sock.request.headers.get('Origin', '') or ''
        except Exception:
            return ''

    def _origin_allowed(self, sock) -> bool:
        """Only the extension service worker may drive the bridge.

        Its handshake Origin is chrome-extension://<id> (also
        moz-extension:// / safari-web-extension:// on other browsers). Web
        pages always send https?://..., so a prefix allowlist keeps every
        site out while needing no shared secret — which matters because an
        extension cannot read a token file off disk.

        A non-browser local client (curl, a test script) sends no Origin at
        all. That is allowed only when BROWSERTAP_WS_ALLOW_NO_ORIGIN=1,
        so the default posture stays closed.
        """
        origin = self._ws_origin(sock)
        if not origin:
            return os.environ.get('BROWSERTAP_WS_ALLOW_NO_ORIGIN', '') == '1'
        allowed = ('chrome-extension://', 'moz-extension://',
                   'safari-web-extension://', 'extension://')
        if origin.startswith(allowed):
            return True
        extra = os.environ.get('BROWSERTAP_WS_ALLOWED_ORIGINS', '')
        return any(origin == o.strip() for o in extra.split(',') if o.strip())

    def clean_sessions(self):
        now = time.time()
        # Snapshot keys, then re-fetch with .get: another thread (WS handler or a
        # concurrent clean_sessions from a parallel /link call) may delete a
        # session between the snapshot and access, so both the read and the
        # delete must tolerate a missing key instead of raising KeyError.
        for sid in list(self.sessions.keys()):
            session = self.sessions.get(sid)
            if session is None: continue
            if not session.is_active() and session.disconnect_at is not None \
                    and now - session.disconnect_at > 600:
                self.sessions.pop(sid, None)
        for old_sid, binding in list(getattr(self, '_rebindings', {}).items()):
            replacement = str(binding.get('replacement_session_id', ''))
            if (not replacement
                    or now - float(binding.get('ts', now)) > 600
                    or not (self.sessions.get(replacement)
                            and self.sessions[replacement].is_active())):
                self._rebindings.pop(old_sid, None)
        # Results/acks that arrive after their caller already timed out would
        # otherwise accumulate forever in a long-lived daemon.
        for r_id in list(self.results.keys()):
            entry = self.results.get(r_id)
            if isinstance(entry, dict) and now - entry.get('ts', now) > 600:
                self.results.pop(r_id, None)
        for a_id in list(self.acks.keys()):
            ts = self.acks.get(a_id)
            if not isinstance(ts, float) or now - ts > 600:
                self.acks.pop(a_id, None)
        # Per-client heartbeat stamps leak otherwise: client_id is random per
        # extension install/profile, so every reinstall adds a new key that
        # never goes away. Drop stamps for clients with no active session and
        # no heartbeat in 10 min.
        live_clients = {s.info.get('client_id') for s in list(self.sessions.values())
                        if s.is_active() and s.info.get('client_id')}
        for cid in list(self.client_last_seen.keys()):
            entry = self.client_last_seen.get(cid)
            if cid not in live_clients and (not isinstance(entry, dict)
                    or now - entry.get('ts', now) > 600):
                self.client_last_seen.pop(cid, None)

    def start_ws_server(self) -> None:
        driver = self
        class JSExecutor(WebSocket):
            def handle(self) -> None:
                # close() in connected() only queues the close frame, so a
                # rejected peer can still get a data frame in before the socket
                # actually goes away. Re-check here so nothing it sends is ever
                # interpreted.
                if not driver._origin_allowed(self):
                    return
                try:
                    data = json.loads(self.data)
                    if data.get('type') == 'ready':
                        session_id = data.get('sessionId')
                        session_info = {'url': data.get('url'), 'title': data.get('title', ''),
                            'connected_at': time.time(), 'type': 'ws'}
                        driver._register_client(session_id, self, session_info)
                    elif data.get('type') in ['ext_ready', 'tabs_update']:
                        tabs = data.get('tabs', [])
                        # Namespace sessions per browser instance so Chrome/Edge (and
                        # multiple profiles) don't collide on identical small tab ids.
                        # Fall back to a per-connection uuid if an older extension
                        # doesn't send clientId — id(self) is unusable here because
                        # addresses get reused after GC, colliding namespaces.
                        client_id = data.get('clientId')
                        if not client_id:
                            if not hasattr(self, '_fallback_cid'):
                                self._fallback_cid = f"conn_{uuid.uuid4().hex[:10]}"
                            client_id = self._fallback_cid
                        browser = data.get('browser', '')
                        # Keep the CLIENT-level socket. It is one per browser and
                        # exists regardless of tab count, so SW-side commands
                        # (chrome.tabs.create) still work with zero open tabs —
                        # per-tab sessions can't express that. The claim can be
                        # refused, and then everything below is skipped on
                        # purpose: a socket that does not own the namespace must
                        # not push tabs into it, and must not move the last-seen
                        # clocks either, or `doctor` would report a dead
                        # extension as having just checked in. Closing it is what
                        # makes the refusal self-healing -- the real extension
                        # reconnects and wins the moment the zombie ages out,
                        # where a silently ignored socket would look connected
                        # and receive nothing forever.
                        if not driver._claim_ext_client(client_id, browser, self):
                            try: self.close()
                            except Exception: pass
                            return
                        driver.last_ext_seen = time.time()
                        driver.client_last_seen[client_id] = {'ts': time.time(), 'browser': browser}
                        driver._apply_extension_tabs(client_id, browser, tabs, self)
                    elif data.get('type') == 'ping':
                        # Liveness reply so the extension can tell a live socket
                        # from a half-open zombie (TCP ESTABLISHED but dead). No
                        # pong within a couple keepalive ticks => extension force-
                        # reconnects instead of pushing tabs into a black hole.
                        # It is also the only regular traffic on an idle browser,
                        # so it is what keeps a namespace owner's ts fresh.
                        driver._touch_ext_client(self)
                        try: self.send_message(json.dumps({'type': 'pong'}))
                        except Exception: pass
                    elif data.get('type') == 'ack':
                        driver.acks[data.get('id','')] = time.time()
                        driver._notify_activity()
                    elif data.get('type') == 'result':
                        driver.results[data.get('id')] = {'success': True, 'data': data.get('result'), 'newTabs': data.get('newTabs', []), 'tabId': data.get('tabId'), 'ts': time.time()}
                        driver._notify_activity()
                    elif data.get('type') == 'error':
                        driver.results[data.get('id')] = {'success': False, 'data': data.get('error'), 'newTabs': data.get('newTabs', []), 'tabId': data.get('tabId'), 'ts': time.time()}
                        driver._notify_activity()
                except Exception:
                    logger.exception("Error handling WebSocket message")
            def connected(self):
                # WebSocket is exempt from the same-origin policy and needs no
                # CORS preflight, so without this check ANY page the user visits
                # could open ws://127.0.0.1:18765 and speak the protocol — most
                # damagingly send ext_ready with a chosen clientId, hijacking
                # ext_clients so every later ext_cmd goes to the attacker's
                # socket. The only legitimate client is the extension service
                # worker, whose handshake Origin is chrome-extension://<id>.
                if not driver._origin_allowed(self):
                    origin = driver._ws_origin(self)
                    logger.warning("Rejected WS connection from %s, origin=%r", self.address, origin)
                    try: self.close()
                    except Exception: pass
                    return
                logger.info("New WS connection from %s", self.address)
            def handle_close(self):
                logger.info("WS connection closed: %s", self.address)
                driver._unregister_client(self)

        # First bind stays in the caller's thread so startup failures are loud.
        self.server = WebSocketServer(self.host, self.port, JSExecutor)

        def run():
            # serve_forever has no internal guard: any exception that escapes
            # the poll loop silently kills the thread while the LISTEN socket
            # keeps accepting into the kernel backlog - clients show
            # ESTABLISHED but no handshake ever completes. Rebuild and go on.
            srv = self.server
            while True:
                try:
                    srv.serve_forever()
                except Exception:
                    logger.exception("WS server loop crashed; rebuilding in 1s")
                try:
                    srv.close()
                except Exception:
                    pass
                time.sleep(1)
                try:
                    srv = self.server = WebSocketServer(self.host, self.port, JSExecutor)
                    logger.info("WS server rebuilt on ws://%s:%s", self.host, self.port)
                except Exception:
                    logger.exception("WS server rebuild failed")
                    time.sleep(2)

        server_thread = threading.Thread(target=run)
        server_thread.daemon = True
        server_thread.start()
        logger.info("WebSocket server running on ws://%s:%s", self.host, self.port)

    def _apply_extension_tabs(self, client_id: str, browser: str,
                              tabs: list[dict], client: WebSocket) -> None:
        """Apply one extension tab snapshot, respecting tab lifecycle generations."""
        def _sid(tab_id): return f"{client_id}:{tab_id}"

        current_tab_ids = {_sid(tab['id']) for tab in tabs}
        logger.debug("Received tabs update from %s (%s): %s", client_id, browser, current_tab_ids)
        rebindings = getattr(self, '_rebindings', None)
        if rebindings is None:
            rebindings = self._rebindings = {}
        prior_by_identity = {}
        current_identity_counts = {}
        for tab in tabs:
            identity = tab.get('tab_identity')
            if identity:
                key = str(identity)
                current_identity_counts[key] = current_identity_counts.get(key, 0) + 1
        for sid, sess in list(self.sessions.items()):
            if (sess.type == 'ext_ws'
                    and sess.info.get('client_id') == client_id
                    and sid not in current_tab_ids):
                identity = sess.info.get('tab_identity')
                if identity:
                    prior_by_identity.setdefault(str(identity), []).append(sid)
        # Only sweep sessions belonging to THIS client; another browser's
        # update must not disconnect this browser's tabs.
        lifecycle_changed = False
        for sid in list(self.sessions.keys()):
            sess = self.sessions.get(sid)
            if sess is None:
                continue
            if (sess.type == 'ext_ws'
                    and sess.info.get('client_id') == client_id
                    and sid not in current_tab_ids):
                lifecycle_changed = lifecycle_changed or sess.is_active()
                sess.mark_disconnected()
        for tab in tabs:
            session_id = _sid(tab['id'])
            session_info = {
                'url': tab.get('url'),
                'title': tab.get('title', ''),
                'connected_at': time.time(),
                'type': 'ext_ws',
                'client_id': client_id,
                'browser': browser,
                'tab_id': tab['id'],
            }
            if tab.get('tab_identity'):
                session_info['tab_identity'] = str(tab['tab_identity'])
            if tab.get('generation') is not None:
                session_info['generation'] = str(tab['generation'])
            sess = self.sessions.get(session_id)
            identity = session_info.get('tab_identity')
            prior = prior_by_identity.get(identity, []) if identity else []
            if (not sess and identity and current_identity_counts.get(identity) == 1
                    and len(prior) == 1 and prior[0] != session_id):
                old_sid = prior[0]
                rebindings[old_sid] = {
                    'replacement_session_id': session_id,
                    'tab_identity': identity,
                    'reason': 'chrome.tabs.onReplaced',
                    'ts': time.time(),
                }
                logger.info(
                    "Tab handle rebound for identity %s: %s -> %s",
                    identity,
                    old_sid,
                    session_id,
                )
            old_generation = sess.info.get('generation') if sess else None
            new_generation = session_info.get('generation')
            if (sess and sess.is_active() and old_generation is not None
                    and new_generation is not None
                    and str(old_generation) != str(new_generation)):
                # Same client:tabId, different native tab lifetime. Replace the
                # session object so waiters cannot mistake the old registration
                # for the tab chrome.tabs.create just returned.
                logger.info(
                    "Tab generation changed for %s: %s -> %s",
                    session_id,
                    old_generation,
                    new_generation,
                )
                sess.mark_disconnected()
                self.sessions.pop(session_id, None)
                sess = None
                lifecycle_changed = True
            if sess and sess.is_active():
                # Both together, and in this order: `type` is read out of `info`,
                # so refreshing one without the other is what used to leave a
                # session routing over a transport it no longer claimed.
                sess.info = session_info
                sess.client = client
            else:
                self._register_client(session_id, client, session_info)
        if lifecycle_changed:
            self._notify_activity()

    def _resolve_local_session_target(self, session_id: str) -> Optional[dict[str, Any]]:
        sid = str(session_id)
        session = self.sessions.get(sid)
        if session is not None and not hasattr(session, 'disconnect_at'):
            return None
        if session and getattr(session, 'disconnect_at', None) is None:
            # WebSocket sessions have an explicit disconnect transition. Avoid
            # calling is_active() here: it is stateful for HTTP expiry and an
            # extra probe would consume a transport's lifecycle edge before
            # execute_js gets to classify it.
            if getattr(session, 'type', None) != _QUEUE_TYPE or session.is_active():
                return {'session_id': sid}
        binding = getattr(self, '_rebindings', {}).get(sid)
        if not isinstance(binding, dict):
            return None
        replacement = str(binding.get('replacement_session_id', ''))
        current = self.sessions.get(replacement)
        if not replacement or not current or getattr(current, 'disconnect_at', None) is not None:
            getattr(self, '_rebindings', {}).pop(sid, None)
            return None
        return {
            'session_id': replacement,
            'rebound_from': sid,
            'replacement_session_id': replacement,
            'tab_identity': binding.get('tab_identity'),
            'reason': binding.get('reason', 'chrome.tabs.onReplaced'),
        }

    def resolve_session_target(self, session_id: str, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Resolve a current session handle, rebinding only with tab identity evidence."""
        sid = str(session_id)
        if self.is_remote:
            envelope = self._remote_cmd(
                {'cmd': 'resolve_session', 'sessionId': sid},
                timeout=_timeout_or_default(timeout, 5),
            )
            result = envelope.get('r')
            if result is None:
                return None
            if not isinstance(result, dict):
                raise RuntimeError('Bridge returned a malformed session resolution')
            return result
        return self._resolve_local_session_target(sid)

    def _register_client(self, session_id: str, client: WebSocket, session_info) -> None:
        # One lookup, and the two branches now differ only in what they say:
        # binding a channel is the same act whether or not there was one before it
        # (`Session.__init__` is `reconnect`), so the only thing left that is
        # genuinely conditional is whether the table gains a row. The log line is
        # read after the bind either way, which is what makes it report the URL the
        # tab has now rather than the one it registered with.
        session = self.sessions.get(session_id)
        if session is None:
            session = self.sessions[session_id] = Session(session_id, session_info, client)
            logger.info("New tab connected: %s (session=%s)", redact_url(session.url), session_id)
        else:
            session.reconnect(client, session_info)
            logger.info("Tab reconnected: %s (session=%s)", redact_url(session.url), session_id)

        self.latest_session_id = session_id
        if self.default_session_id is None: self.default_session_id = session_id
        self._notify_activity()

    def _unregister_client(self, client: WebSocket) -> None:
        # Snapshot both dicts before iterating, for the same reason
        # clean_sessions and _live_default_session_id do: a second WS connection
        # closing (or a tab registering) concurrently mutates them mid-loop and
        # raises "dictionary changed size during iteration", which here would
        # abort the disconnect cleanup halfway and leave a dead socket in place
        # for ext_cmd to write into.
        lifecycle_changed = False
        for session in list(self.sessions.values()):
            if session.ws_client == client:
                lifecycle_changed = lifecycle_changed or session.is_active()
                session.mark_disconnected()
        # Drop the client-level socket too, else ext_cmd would keep writing into
        # a closed connection instead of failing over to a live browser.
        for cid in [c for c, v in list(self.ext_clients.items()) if v.get('ws') is client]:
            self.ext_clients.pop(cid, None)
        if lifecycle_changed:
            self._notify_activity()

    def _claim_ext_client(self, client_id: str, browser: str, client: WebSocket) -> bool:
        """Bind client_id to this socket, or refuse. True once it owns it.

        `clientId` is self-reported and used to be written straight into
        ext_clients, so the last socket to send ext_ready took the namespace
        outright. The WS port is guarded by the handshake Origin alone -- a
        prefix the extension cannot keep secret and any local process can put in
        a header -- so that overwrite was the whole exploit: forge one ext_ready
        carrying the browser's clientId and every later ext_cmd is written to the
        forger's socket instead of the extension's. The HTTP port demands a
        bearer token for strictly less than that.

        First live socket wins closes it with no protocol change, because
        handle_close() already unbinds a socket that goes away: an ordinary
        reconnect finds no incumbent and is not a conflict at all. Only an
        incumbent that is still open can be in the way.

        The grace window is what keeps this from becoming a lockout. A half-open
        zombie (TCP ESTABLISHED, peer already gone) is real here -- it is the
        reason the extension pings at all -- and refusing every newcomer against
        one would wedge the bridge until a human restarted it. So an incumbent
        that has said nothing for CLIENT_TAKEOVER_GRACE_SECONDS is treated as
        gone and handed over. Its `ts` is refreshed by every message it sends,
        keepalive pings included, so it measures the socket's own liveness rather
        than when it first connected.
        """
        now = time.time()
        incumbent = self.ext_clients.get(client_id)
        if incumbent is not None and incumbent.get('ws') is not client:
            stamp = incumbent.get('ts')
            # An entry with no usable stamp cannot prove it is alive, and the
            # lockout is the worse failure, so it counts as ancient.
            idle = now - stamp if isinstance(stamp, (int, float)) else now
            if idle <= CLIENT_TAKEOVER_GRACE_SECONDS:
                self.rejected_client_takeovers += 1
                self.last_rejected_takeover = {
                    # Truncated because it is the newcomer's own string, and
                    # this ends up in a diagnose() report.
                    'client_id': str(client_id)[:64],
                    'incumbent_idle_seconds': round(idle, 1),
                    'peer': str(getattr(client, 'address', '')),
                    'ts': now,
                }
                logger.warning(
                    "Refused WS takeover of client_id=%r from %s: the socket holding it "
                    "was heard from %.1fs ago (grace %.0fs)",
                    str(client_id)[:64], getattr(client, 'address', '?'), idle,
                    CLIENT_TAKEOVER_GRACE_SECONDS)
                return False
            logger.warning(
                "Handing client_id=%r to a new socket: the previous one went silent "
                "%.1fs ago", str(client_id)[:64], idle)
        self.ext_clients[client_id] = {'ws': client, 'browser': browser, 'ts': now}
        return True

    def _touch_ext_client(self, client: WebSocket) -> None:
        """Mark whatever namespaces this socket owns as heard-from, just now.

        Only ext_clients[*]['ts'] moves. last_ext_seen and client_last_seen are
        left alone on purpose: those are `doctor`'s answer to "has the extension
        pushed tabs recently", and a keepalive ping is not that.
        """
        now = time.time()
        for entry in list(self.ext_clients.values()):
            if entry.get('ws') is client: entry['ts'] = now

    def _live_default_session_id(self) -> Optional[str]:
        """The remembered default tab, or a fresh pick if it has died.

        Tab ids churn — closing a tab, restarting the browser or reloading the
        extension all invalidate them — and this default is set once, when the
        first tab ever registers. Keeping a dead one poisons every call that
        does not name a tab: the caller gets "run switch_tab first" about a tab
        it never picked, so agents end up doing list_tabs + switch_tab before
        every single action. Re-picking is only safe on this implicit path.
        """
        cur = self.default_session_id
        if cur:
            current = self.sessions.get(cur)
            # Lightweight test/compatibility session objects predate the
            # disconnect_at field; retain their explicit is_active contract.
            if current is not None and not hasattr(current, 'disconnect_at'):
                if current.is_active():
                    return cur
            resolved = self._resolve_local_session_target(str(cur))
            if resolved:
                selected = str(resolved['session_id'])
                if selected != str(cur):
                    logger.info("Default session %s rebound to %s", cur, selected)
                    self.default_session_id = selected
                return selected
        # Snapshot before iterating: the WS thread may insert/remove sessions
        # (tabs_update) while this runs — clean_sessions already snapshots for
        # exactly that reason; missing the snapshot here raises RuntimeError.
        alive = [s for s in list(self.sessions.values()) if s.is_active()]
        if not alive:
            return cur  # nothing to pick; let the caller's error path report it
        # Skip tabs Chrome will not let us script, for the same reason this
        # re-picks at all: the caller named nothing, so handing it a tab where
        # every call fails is worse than handing it any other live tab.
        alive = _prefer_scriptable(alive)
        latest = self.sessions.get(self.latest_session_id)
        chosen = latest if latest in alive else alive[-1]
        if cur:
            logger.info("Default session %s is stale; selected %s", cur, chosen.id)
        self.default_session_id = chosen.id
        return chosen.id

    def _pick_failover_session(self, pool):
        """Pick the live tab least likely to vanish mid-command.

        Reached only when the caller named no target, or explicitly allowed any
        tab, so there is no preference left to honour except survival. This used
        to prefer `latest_session_id`, which is exactly backwards: a session
        registers when its tab is *created*, so the freshest entry is the one
        still loading, and running there loses the CDP fallback's debugger to
        the commit that follows ("Detached while handling command"). Live runs
        reproduced that against a browser someone was using, while the same test
        passed against an idle one.

        So prefer candidates that have been registered long enough to have
        finished loading, and keep the previous order *inside* that set — a
        settled newest tab still wins, and a browser with nothing settled yet
        behaves exactly as before rather than refusing.

        Un-scriptable tabs are filtered first, on the same terms: the gallery
        page below is where this went wrong a second time.
        """
        now = time.time()
        usable = _prefer_scriptable(pool)
        settled = [
            s for s in usable
            if now - float(getattr(s, 'connect_at', 0.0) or 0.0) >= FAILOVER_SETTLE_SECONDS
        ]
        candidates = settled or usable
        latest = self.sessions.get(self.latest_session_id)
        return latest if latest in candidates else candidates[-1]

    def execute_js(self, code, timeout=15, session_id=None, allow_failover=False) -> Any:
        """Run JS in a tab.

        allow_failover=False by default: if the target session is gone we raise
        instead of running the script on some other live tab, because the script
        may have side effects the caller only meant for the tab it named.
        """
        if not isinstance(code, str):
            raise ValueError("code must be a string")
        timeout = _positive_timeout(timeout)
        deadline = time.monotonic() + timeout

        def remaining():
            return max(0.0, deadline - time.monotonic())

        # Whether the caller actually named a tab. An explicitly named dead tab
        # must still be refused below; an implicit one the driver supplied from
        # its own memory must not be, since the caller never chose it.
        caller_named_target = session_id is not None
        rebound = None
        if session_id is not None and not self.is_remote:
            rebound = self.resolve_session_target(str(session_id))
            if rebound and rebound.get('session_id'):
                session_id = str(rebound['session_id'])
        if session_id is None:
            session_id = self._live_default_session_id()
        if self.is_remote:
            logger.debug("Dispatching remote execute_js (session=%s)", session_id)
            # HTTP timeout must outlast the JS timeout or long scripts die at
            # the transport layer before the bridge can answer. The daemon
            # answers *at* `timeout`, so an equal socket deadline is a coin flip
            # that usually lands on a transport TimeoutError and throws away the
            # structured delivery verdict; REMOTE_TRANSPORT_MARGIN buys the
            # daemon's reply the time it needs to arrive.
            envelope = self._remote_cmd({"cmd": "execute_js", "sessionId": session_id,
                                         "code": code, "timeout": str(timeout),
                                         "allowFailover": "1" if allow_failover else "0"},
                                        timeout=max(0.001, remaining())
                                        + REMOTE_TRANSPORT_MARGIN)
            response = envelope.get('r', {})
            if not isinstance(response, dict):
                raise RuntimeError("Bridge returned a malformed execute_js result")
            if response.get('error'):
                err = response['error']
                error_code = response.get('error_code')
                # Keep the exception type the same across local and remote: the
                # bridge flattens everything to a string over HTTP, so a caller
                # catching ValueError locally would miss it in remote mode.
                if error_code == SessionNotConnectedError.error_code:
                    exc = SessionNotConnectedError(err)
                    if isinstance(response.get("diagnostics"), dict):
                        exc.diagnostics = dict(response["diagnostics"])
                    raise exc
                if error_code == SessionDisconnectedError.error_code:
                    raise SessionDisconnectedError(err)
                if _is_page_execution_error(err, error_code):
                    raise PageExecutionError(
                        err,
                        script=code,
                        error_code=error_code,
                        diagnostics=response.get("diagnostics"),
                    )
                raise Exception(err)
            if isinstance(response, dict) and response.get('tabId') is not None:
                # The executor (extension/bridge) names the tab it used; surface
                # it under the same key the local path uses so callers read one
                # field regardless of transport.
                response = dict(response)
                response['executed_tab_id'] = int(response['tabId'])
            return response

        switched_from = None
        session = self.sessions.get(session_id)
        if not session or not session.is_active():
            # A just-created or reloaded tab can register during this grace
            # window. Wait on bridge activity so a 100 ms recovery costs about
            # 100 ms, while retaining the same three-second upper bound.
            grace_deadline = min(deadline, time.monotonic() + 3.0)
            activity_serial = self._activity_snapshot()
            while True:
                session = self.sessions.get(session_id)
                if session and session.is_active():
                    break
                wait_for = max(0.0, grace_deadline - time.monotonic())
                if wait_for <= 0:
                    break
                activity_serial = self._wait_for_activity(activity_serial, wait_for)
            if not session or not session.is_active():
                alive_sessions = [s for s in list(self.sessions.values()) if s.is_active()]
                # Do NOT execute on a substitute tab. This used to pick a live
                # session and run the script there anyway, so "click checkout"
                # or "delete this row" landed on whatever tab happened to be
                # alive, with only a switched_session field to say so after the
                # side effect had already happened. Refuse and name the
                # candidates; the caller re-targets explicitly.
                # An implicit target that died mid-call is the driver's own
                # bookkeeping problem, not a caller mistake, so recover from it
                # like _live_default_session_id would rather than refusing.
                if (allow_failover or not caller_named_target) and alive_sessions:
                    want_client = str(session_id).rsplit(':', 1)[0] if session_id and ':' in str(session_id) else None
                    same_client = [s for s in alive_sessions if want_client and str(s.id).startswith(want_client + ':')]
                    pool = same_client or alive_sessions
                    session = self._pick_failover_session(pool)
                    logger.warning(
                        "Session %s is disconnected; switched to active session %s",
                        session_id,
                        session.id,
                    )
                    switched_from = session_id
                    session_id = self.default_session_id = session.id
                if not session or not session.is_active():
                    if alive_sessions:
                        cands = ', '.join(str(s.id) for s in alive_sessions[:8])
                        exc = SessionNotConnectedError(
                            f"Session {session_id} is not connected. BTAP refused to execute on a "
                            f"different tab. Active sessions: {cands}. Select the intended target "
                            "with switch_tab and retry."
                        )
                        exc.diagnostics = {
                            "stale_session_id": str(session_id),
                            "replacement_candidates": [
                                {
                                    "id": str(s.id),
                                    "url": s.info.get("url"),
                                    "title": s.info.get("title"),
                                    "generation": s.info.get("generation"),
                                }
                                for s in alive_sessions[:8]
                            ],
                            "next_action": "list_tabs_then_retry",
                        }
                        raise exc
                    exc = SessionNotConnectedError(
                        f"Session {session_id} is not connected. The explicitly requested tab is stale; "
                        "run list_tabs and retry with a live session_id."
                    )
                    exc.diagnostics = {
                        "stale_session_id": str(session_id),
                        "replacement_candidates": [],
                        "next_action": "list_tabs_then_retry",
                    }
                    raise exc
        # Callers (and the AI driving them) must learn their target changed.
        extra = {'switched_session': session_id, 'switched_from': switched_from} if switched_from else {}
        if rebound and rebound.get('rebound_from'):
            extra.update({
                'rebound_from': rebound.get('rebound_from'),
                'replacement_session_id': rebound.get('replacement_session_id', session_id),
                'tab_identity': rebound.get('tab_identity'),
                'rebind_reason': rebound.get('reason', 'chrome.tabs.onReplaced'),
            })

        tp = session.type
        # Not an assert: python -O strips those, and what it guards is not a
        # tidy fall-through. Every dispatch below is `if tp in ['ws','ext_ws']
        # ... elif tp == 'http'`, and so is the whole deadline block, so an
        # unrecognised type sends nothing, then reaches `remaining() <= 0` with
        # no branch to return from and spins the polling loop forever -- with
        # `wait_for` negative there is not even a sleep in it. Measured with
        # this line disabled: a 2-second call had not come back at 60 s, one
        # core pegged. An unknown type is only ever a bug here (a transport
        # added to Session and not to this dispatch), so it must raise, and it
        # must raise under -O.
        if tp not in ('ws', 'http', 'ext_ws'):
            raise ValueError(f"Unsupported session type: {tp}")
        exec_id = str(uuid.uuid4())
        payload_dict = {'id': exec_id, 'code': code}
        if tp == 'ext_ws':
            # session.id is now "client_id:tab_id"; use the raw browser tab id.
            payload_dict['tabId'] = int(session.info.get('tab_id', str(session.id).rsplit(':', 1)[-1]))
        payload = json.dumps(payload_dict)
        # Which tab this command names, derived from the session the driver
        # resolved — so every return path (success, reload, timeout) reports
        # the executor's target instead of a later guess from driver memory.
        exec_tab_id = int(session.info.get('tab_id', str(session.id).rsplit(':', 1)[-1])) if tp == 'ext_ws' else None

        # A disconnected tab may recover during the bounded grace sleep above.
        # Recovery does not renew the caller's budget: once the deadline is
        # spent, do not dispatch a side-effecting script merely because the
        # socket came back at the last instant.
        if remaining() <= 0:
            if tp in ['ws', 'ext_ws']:
                return _no_response_result(
                    (
                        f"No response data in {timeout}s "
                        "(no ACK, script was not dispatched before the deadline)"
                    ),
                    delivery_state="undelivered",
                    executed_tab_id=exec_tab_id,
                    extra=extra,
                )
            return _no_response_result(
                (
                    f"Session {session_id} no response in {timeout}s "
                    "(script not polled)"
                ),
                delivery_state="undelivered",
                executed_tab_id=exec_tab_id,
                extra=extra,
            )

        if tp in ['ws', 'ext_ws']:
            try: session.ws_client.send_message(payload)
            except Exception as e:
                # Half-open socket (e.g. after system sleep): mark it dead so
                # the next call fails over instead of hitting it again.
                session.mark_disconnected()
                self._notify_activity()
                raise SessionDisconnectedError(
                    f"Session {session_id} disconnected ({e}); it was marked inactive. Retry after list_tabs."
                )
        elif tp == 'http': session.http_queue.put(payload)

        self.clean_sessions()
        hasjump = acked = False
        missing_result = object()
        result = missing_result
        activity_serial = self._activity_snapshot()

        while result is missing_result:
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            wait_for = min(0.05, remaining())
            if wait_for > 0:
                activity_serial = self._wait_for_activity(activity_serial, wait_for)
            # A result can arrive during the final polling sleep exactly as the
            # deadline expires. Consume that real reply instead of dropping it
            # below and reporting an ambiguous timeout for a completed script.
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            if not acked and exec_id in self.acks:
                acked = True
            if tp in ['ws', 'ext_ws']:
                if not session.is_active(): hasjump = True
                if hasjump and session.is_active():
                    # 脚本随 reload 作废：晚到的结果不应再被任何人读到，直接丢弃。
                    self.results.pop(exec_id, None)
                    self.acks.pop(exec_id, None)
                    return _no_response_result(
                        f"Session {session_id} reloaded.",
                        delivery_state="navigated",
                        executed_tab_id=exec_tab_id,
                        extra=extra,
                        closed=True,
                    )
            if remaining() <= 0:
                # Make the deadline decision with one atomic dict operation.
                # A result present now is consumed; one arriving after this
                # point is genuinely late and is cleaned up below.
                result = self.results.pop(exec_id, missing_result)
                if result is not missing_result:
                    break
                # 清理本次 exec_id 的残留键：超时返回后 results/acks 里不会
                # 再有调用方来取，留着只会累积到 clean_sessions 的 600s TTL——
                # 高频超时场景下等于是长期泄漏（与 ext_cmd 超时路径一致）。
                self.results.pop(exec_id, None)
                self.acks.pop(exec_id, None)
                if tp in ['ws', 'ext_ws']:
                    if hasjump:
                        return _no_response_result(
                            f"Session {session_id} reloaded and new page is loading...",
                            delivery_state="navigated",
                            executed_tab_id=exec_tab_id,
                            extra=extra,
                            closed=True,
                        )
                    if acked:
                        return _no_response_result(
                            f"No response data in {timeout}s (ACK received, script may still be running)",
                            delivery_state="delivered_no_result",
                            executed_tab_id=exec_tab_id,
                            extra=extra,
                        )
                    return _no_response_result(
                        (
                            f"No response data in {timeout}s (sent but no ACK; the extension "
                            "acknowledges before it executes, so the script most likely "
                            "did not run)"
                        ),
                        # Not 'undelivered': the payload did reach a live socket.
                        # Only the acknowledgement is missing, so the guarantee
                        # here rests on the ACK-first protocol, not on proof.
                        delivery_state="sent_unconfirmed",
                        executed_tab_id=exec_tab_id,
                        extra=extra,
                    )
                elif tp == 'http':
                    if acked:
                        return _no_response_result(
                            f"Session {session_id} no response in {timeout}s (delivered but no result)",
                            delivery_state="delivered_no_result",
                            executed_tab_id=exec_tab_id,
                            extra=extra,
                        )
                    return _no_response_result(
                        f"Session {session_id} no response in {timeout}s (script not polled)",
                        delivery_state="undelivered",
                        executed_tab_id=exec_tab_id,
                        extra=extra,
                    )

        if result is missing_result:
            raise RuntimeError("execute_js completed without a result")
        if exec_id in self.acks: self.acks.pop(exec_id)
        if not result['success']:
            raise PageExecutionError(result.get('data'), script=code)
        rr = {'data': result['data'], **extra}
        # Prefer the tab the executor echoed back (covers failover and remote
        # transports); fall back to the session we resolved locally.
        rtab = result.get('tabId')
        rr['executed_tab_id'] = int(rtab) if rtab is not None else exec_tab_id
        newtabs = result.get('newTabs', [])
        for tab in newtabs:
            tab.pop('ts', None)
        if newtabs: rr['newTabs'] = newtabs
        return rr

    def ext_cmd(self, cmd: dict, client_id=None, timeout=15):
        """Send a command straight to an extension service worker.

        The extension's socket is per-browser, so this works with ZERO open
        tabs: background.js routes any payload carrying `.cmd` to
        handleExtMessage, which runs in the SW. Plain JS still needs a tab
        (handleWsExec requires tabId) — that asymmetry is the whole point.
        """
        if not isinstance(cmd, dict):
            raise ValueError("cmd must be a JSON object")
        timeout = _positive_timeout(timeout)
        if not isinstance(cmd.get("cmd"), str) or not cmd["cmd"].strip():
            raise ValueError(
                'ext_cmd payload requires a non-empty string cmd field; '
                'for /link use {"cmd":"ext_cmd","payload":{"cmd":"tabs",...}}'
            )
        deadline = time.monotonic() + timeout

        def remaining():
            return max(0.0, deadline - time.monotonic())

        if self.is_remote:
            # Same margin as execute_js: the daemon raises its own TimeoutError
            # at `timeout` and only then writes the response, so the socket has
            # to outlive that instant for the 'did not respond within' branch
            # below to be reachable at all.
            envelope = self._remote_cmd({"cmd": "ext_cmd", "payload": cmd,
                                         "clientId": client_id, "timeout": str(timeout)},
                                        timeout=max(0.001, remaining())
                                        + REMOTE_TRANSPORT_MARGIN)
            response = envelope.get('r', {})
            if not isinstance(response, dict):
                raise RuntimeError("Bridge returned a malformed ext_cmd result")
            if response.get('error'):
                err = str(response['error'])
                # Keep the timeout distinguishable across the HTTP hop, so
                # callers like newtab() can tell "never answered" (do not
                # retry, it may have happened) from "no such route".
                if 'did not respond within' in err:
                    raise TimeoutError(err)
                raise Exception(err)
            return response

        if client_id is None:
            # Prefer the browser the caller already works in, so a stray command
            # can't open tabs in the other browser.
            want = (str(self.default_session_id).rsplit(':', 1)[0]
                    if self.default_session_id and ':' in str(self.default_session_id) else None)
            if want and want in self.ext_clients: client_id = want
            elif self.ext_clients:
                client_id = max(self.ext_clients.items(), key=lambda kv: kv[1].get('ts', 0))[0]
        entry = self.ext_clients.get(client_id)
        if not entry:
            raise ExtensionNotConnectedError(
                "No browser extension is connected; verify that the extension is loaded and the bridge is running"
            )

        exec_id = str(uuid.uuid4())
        try:
            # Send commands via 'cmd' field (not 'code') to prevent JSON.parse
            # confusion: the extension can now route by field presence rather
            # than by trying to parse a JS string as JSON.
            entry['ws'].send_message(json.dumps({'id': exec_id, 'cmd': cmd}))
        except Exception as e:
            self.ext_clients.pop(client_id, None)
            raise ExtensionNotConnectedError(
                f"Extension client {client_id} disconnected ({e}); it was removed. Retry after it reconnects."
            )

        missing_result = object()
        result = missing_result
        activity_serial = self._activity_snapshot()
        while result is missing_result:
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            wait_for = min(0.05, remaining())
            if wait_for > 0:
                activity_serial = self._wait_for_activity(activity_serial, wait_for)
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            if remaining() <= 0:
                result = self.results.pop(exec_id, missing_result)
                if result is not missing_result:
                    break
                # MUST raise, not return. Returning a dict here made callers
                # like set_extension_enabled / close_tabs / open_new_tab report
                # status:"ok" for a mutation that never reached the browser —
                # the agent then acts on a success that did not happen.
                self.results.pop(exec_id, None)
                self.acks.pop(exec_id, None)
                raise TimeoutError(
                    f"extension {client_id} did not respond within {timeout}s; "
                    f"the command may not have reached the browser (service worker "
                    f"asleep, or no scriptable tab to wake it)")
        if result is missing_result:
            raise RuntimeError("ext_cmd completed without a result")
        self.acks.pop(exec_id, None)
        if not result['success']: raise Exception(result['data'])
        return {'data': result['data'], 'client_id': client_id}

    def _remote_cmd(self, cmd, timeout=30):
        if not isinstance(cmd, dict):
            raise ValueError("cmd must be a JSON object")
        timeout = _positive_timeout(timeout)
        headers = {"Content-Type": "application/json"}
        # Every client reads the same persistent per-user token file. This is
        # independent of which editor launched the MCP process.
        token = bridge_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Bridge-Token"] = token
        try:
            resp = self._http.post(self.remote, headers=headers, json=cmd, timeout=timeout)
        except requests.exceptions.Timeout as exc:
            # Normalize requests/urllib3 transport timeouts at the driver
            # boundary. Higher layers use TimeoutError to distinguish a
            # recoverable lost response from a definitive command failure.
            raise TimeoutError(
                f"bridge HTTP request timed out after {timeout}s"
            ) from exc
        if resp.status_code == 401:
            # 别把 401 的 HTML/文本喂给 .json()（会变成一句看不懂的 JSONDecodeError）。
            raise PermissionError(
                "Bridge request rejected (401): the MCP process and daemon use different BTAP tokens. "
                f"Confirm both use {bridge_token_path()}, then run `browsertap bridge --restart`. "
                f"{TOKEN_ENV} is only a one-time migration source for older installs."
            )
        if resp.status_code >= 400:
            # 桥端未捕获的异常会变成 500 HTML 页；同样不能喂给 .json()，
            # 否则调用方看到的是 JSONDecodeError 而不是真实成因。
            body = resp.text if hasattr(resp, 'text') else ''
            snippet = (body[:300] if body else '(empty response)').replace('\n', ' ')
            raise RuntimeError(f"Bridge returned HTTP {resp.status_code}: {snippet}")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Bridge returned malformed JSON: expected an object")
        return payload

    def get_all_sessions(self, timeout=None):
        normalized_timeout = None if timeout is None else _positive_timeout(timeout)
        if self.is_remote:
            envelope = self._remote_cmd(
                {"cmd": "get_all_sessions"},
                timeout=_timeout_or_default(normalized_timeout, 30),
            )
            result = envelope.get('r', [])
            if not isinstance(result, list):
                raise RuntimeError("Bridge returned a malformed session list")
            return result
        self.clean_sessions()
        return [{'id': session.id, **session.info} for session in list(self.sessions.values())
                if session.is_active()]

    def get_session_dict(self, timeout=None):
        return {session['id']: session['url'] for session in self.get_all_sessions(timeout=timeout)}

    def diagnose(self, timeout=None):
        """Classify why the bridge might be unusable, mapping to SKILL.md causes.

        Returns a dict the CLI/MCP `doctor` prints so the user never has to run
        netstat + curl + read the badge by hand. `cause` is the machine-readable
        verdict; `advice` is the one-line human fix.
        """
        normalized_timeout = None if timeout is None else _positive_timeout(timeout)
        if self.is_remote:
            # We're a thin client; ask the real host for its self-diagnosis.
            request_timeout = _timeout_or_default(normalized_timeout, 6)
            try:
                envelope = self._remote_cmd(
                    {"cmd": "diagnose"}, timeout=request_timeout
                )
                result = envelope.get('r', {})
                if not isinstance(result, dict):
                    raise RuntimeError("Bridge returned a malformed diagnosis")
                return result
            except Exception as e:
                return {
                    "cause": "bridge_unreachable",
                    "ok": False,
                    "advice": (
                        "The bridge daemon is unreachable. BTAP normally starts it automatically; "
                        "run `browsertap bridge --restart` if recovery does not occur."
                    ),
                    "error": str(e),
                }
        self.clean_sessions()
        now = time.time()
        active = [s for s in list(self.sessions.values()) if s.is_active()]
        ever = self.last_ext_seen is not None
        stale = ever and (now - self.last_ext_seen) > 90
        started_at = getattr(self, "started_at", None)
        uptime = (now - started_at) if isinstance(started_at, (int, float)) else None
        # Snapshot, and never index a heartbeat entry directly: the WS thread
        # writes client_last_seen while doctor reads it, and diagnose is the tool
        # people run *because* something is already wrong — it must not be the
        # thing that raises.
        per_client = {cid: {"browser": v.get("browser", ""),
                            "seconds_ago": round(now - v.get("ts", now), 1)}
                      for cid, v in list(self.client_last_seen.items())
                      if isinstance(v, dict)}
        if active:
            cause, ok, advice = "healthy", True, f"{len(active)} tab(s) registered; bridge and extension are connected."
        elif not ever and uptime is not None and uptime < BRIDGE_STARTUP_GRACE_SECONDS:
            cause, ok, advice = "starting", False, (
                f"The bridge started {round(max(0.0, uptime), 1)}s ago and is waiting for the "
                "extension handshake. Wait a few seconds and run doctor again before reloading."
            )
        elif not ever:
            cause, ok, advice = "ext_never_registered", False, (
                "The extension has never connected. Check chrome://extensions for errors or a disabled "
                "extension, then keep at least one normal http:// or https:// page open."
            )
        elif stale:
            cause, ok, advice = "sw_slept_or_dropped", False, (
                f"The extension last checked in {round(now - self.last_ext_seen)}s ago. A normal open page "
                "should reconnect within 5 seconds; otherwise reload BrowserTap Bridge once in "
                "chrome://extensions."
            )
        else:
            cause, ok, advice = "registering", False, (
                "The extension is connected but no active content tab is registered. Wait briefly or open "
                "a normal http:// or https:// page; chrome:// and blank pages do not register."
            )
        extension_status = None
        extension_status_error = None
        try:
            raw_status = self.ext_cmd(
                {"cmd": "bridge_status"},
                timeout=_timeout_or_default(normalized_timeout, 5),
            )
            extension_status = raw_status.get("data", raw_status)
        except Exception as e:
            extension_status_error = str(e)
        result = {
            "cause": cause, "ok": ok, "advice": advice,
            "bridge_version": __version__,
            "active_tabs": len(active),
            "ever_registered": ever,
            "last_ext_seen_seconds_ago": round(now - self.last_ext_seen, 1) if ever else None,
            "bridge_started_at": started_at,
            "bridge_uptime_seconds": round(max(0.0, uptime), 1) if uptime is not None else None,
            "startup_grace_seconds": BRIDGE_STARTUP_GRACE_SECONDS,
            "clients": per_client,
            # Zero unless something spoke the extension's protocol while the
            # real extension still held the namespace; see _claim_ext_client.
            "rejected_client_takeovers": self.rejected_client_takeovers,
            # Resolved by the daemon, which is the process that actually reads
            # the token file -- a client's own answer can differ, and that
            # difference is the whole diagnosis.
            "state_paths": state_paths_report(
                enforced_token=getattr(self, "link_token", None)
            ),
        }
        if isinstance(extension_status, dict):
            result["extension_version"] = (
                extension_status.get("extension_version")
                or extension_status.get("manifest_version")
            )
            result["protocol_version"] = extension_status.get("protocol_version")
            result["extension_capabilities"] = extension_status.get("capabilities", {})
            # A literal in the worker's own source, so unlike the three versions
            # above it says which *code* answered. Forwarded rather than left to the
            # server's fallback probe because that probe only fires when a version
            # is missing, and a bridge old enough to omit this still reports those.
            result["extension_build_stamp"] = extension_status.get("build_stamp")
        if extension_status_error:
            result["extension_status_error"] = extension_status_error
        if self.last_rejected_takeover:
            result["last_rejected_takeover"] = dict(self.last_rejected_takeover)
        return result

    def find_session(self, url_pattern: str):
        if not isinstance(url_pattern, str):
            raise ValueError("url_pattern must be a string")
        if url_pattern == '':
            session = self.sessions.get(self.latest_session_id)
            return [(session.id, session.info)] if session and session.is_active() else []
        matching_sessions = []
        # Snapshot: this runs on a caller thread while the WS thread registers
        # and drops tabs (see clean_sessions / _live_default_session_id).
        for session in list(self.sessions.values()):
            if not session.is_active(): continue
            url = session.info.get('url')
            if isinstance(url, str) and url_pattern in url:
                matching_sessions.append((session.id, session.info))
        return matching_sessions

    def set_session(self, url_pattern: str) -> Optional[str]:
        if not isinstance(url_pattern, str):
            raise ValueError("url_pattern must be a string")
        if self.is_remote:
            envelope = self._remote_cmd(
                {"cmd": "find_session", "url_pattern": url_pattern}
            )
            matched = envelope.get('r', [])
            if not isinstance(matched, list):
                raise RuntimeError("Bridge returned a malformed session match list")
        else:
            matched = self.find_session(url_pattern)
        if not matched:
            logger.warning("No session URL contains %r", redact_pattern(url_pattern))
            return None
        if len(matched) > 1:
            candidates = ", ".join(
                f"{session_id} ({info.get('url', '')})"
                for session_id, info in matched[:8]
            )
            raise ValueError(
                f"URL pattern {url_pattern!r} matched {len(matched)} sessions: "
                f"{candidates}. Pass the full session_id to select one."
            )
        self.default_session_id, info = matched[0]
        logger.info(
            "Default session set to %s: %s",
            self.default_session_id,
            redact_url(info.get('url')),
        )
        return self.default_session_id

    def jump(self, url, timeout=10): self.execute_js(f"window.location.href={json.dumps(url)}", timeout=timeout)
    def newtab(self, url=None, client_id=None, timeout=15, active=False,
               operation_id=None):
        if url is None: url = "about:blank"
        if operation_id is None or not str(operation_id).strip():
            raise ValueError("newtab requires a non-empty operation_id")
        # Native chrome.tabs.create, addressed to the extension itself so it
        # works with no tabs open. (The old GM_openInTab was a Tampermonkey-only
        # API absent from a plain extension, so it always threw ReferenceError.)
        payload = {
            "cmd": "tabs",
            "method": "create",
            "url": url,
            "active": bool(active),
        }
        payload["operation_id"] = str(operation_id)
        if client_id is not None:
            # The extension must persist the browser namespace with the
            # operation so a completed result remains addressable even when
            # there are no scriptable content sessions.
            payload["client_id"] = str(client_id)
        try:
            return self.ext_cmd(payload, client_id=client_id, timeout=timeout)
        except TimeoutError:
            # The extension got the command but never answered. The session
            # route would talk to the same asleep service worker, so retrying
            # there just burns another timeout — and if the tab DID open, the
            # fallback would open a second one. Surface it instead.
            raise
        except Exception:
            # Never replay a tab creation through another route. Even with an
            # operation id, a different route can select another browser; the
            # caller must reconcile through the pinned extension client.
            raise


if __name__ == "__main__":
    driver = BrowserBridge(host='127.0.0.1', port=18765)
