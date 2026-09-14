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
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Optional, TypeGuard
from urllib.parse import urlsplit

import bottle
import requests
from bottle import request
from simple_websocket_server import WebSocket, WebSocketServer

from . import _token_file
from ._version import __version__
from .capture_ownership import CaptureOperation, CaptureOwnershipRegistry
from .command_scope import guard_targets, has_command_scope, mark_dispatched
from .extension_origin import origin_is_allowed, origin_policy_report
from .paths import (
    DEFAULT_STATE_DIR_NAME,
    LEGACY_STATE_DIR_NAME,
    STATE_DIR_ENV,
    state_dir,
    validate_bridge_port,
)
from .pending_operations import PendingOperations
from .requester_liveness import process_requester_id, requester_liveness
from .runtime_identity import (
    compare_source_identities,
    current_source_identity,
    loaded_source_identity,
)

logger = logging.getLogger(__name__)
_DRIVER_STATE_LOCK = threading.RLock()
_UNSET_DEFAULT = object()
MAX_TAB_SNAPSHOT_SIZE = 4096


def _log_reference(value: Any) -> str:
    """Correlate protocol identifiers without logging their arbitrary contents."""
    if value is None:
        return '<none>'
    if not isinstance(value, str):
        return '<invalid>'
    return 'sha256:' + hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()[:12]


def _valid_protocol_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= 512 and bool(value.strip())


def _valid_tab_id(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2147483647


def _normalize_tab_id(value: Any) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    elif type(value) is float and value.is_integer():
        value = int(value)
    if not _valid_tab_id(value):
        raise ValueError('tabId must be an integer between 0 and 2147483647')
    return int(value)


def _normalize_ext_command(command: dict) -> tuple[dict, list[int]]:
    """Copy the wire payload and resolve its complete target set before dispatch."""
    command = dict(command)
    tab_ids: list[int] = []

    def collect_tab_ids(payload: dict) -> None:
        tab_id = payload.get('tabId')
        if tab_id is None:
            return
        if isinstance(tab_id, list):
            payload['tabId'] = [_normalize_tab_id(value) for value in tab_id]
            tab_ids.extend(payload['tabId'])
        else:
            payload['tabId'] = _normalize_tab_id(tab_id)
            tab_ids.append(payload['tabId'])

    collect_tab_ids(command)
    if command['cmd'] == 'batch':
        children = command.get('commands')
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise ValueError('batch commands must be a list of JSON objects')
        command['commands'] = [dict(child) for child in children]
        for child in command['commands']:
            if 'tabId' not in child and 'tabId' in command:
                child['tabId'] = command['tabId']
            if child.get('cmd') == 'cdp':
                # WS batches have no sender tab. Resolve null/omitted targets
                # here so a later child cannot fail after earlier side effects.
                tab_id = child.get('tabId')
                if tab_id is None:
                    tab_id = command.get('tabId')
                child['tabId'] = _normalize_tab_id(tab_id)
                if child['tabId'] == 0:
                    # The extension uses || for CDP targets; zero would silently
                    # fall back to another tab or an absent sender.
                    raise ValueError('batch CDP commands require a positive integer tabId')
            collect_tab_ids(child)
    return command, list(dict.fromkeys(tab_ids))


def _valid_page_fields(value: dict) -> bool:
    return all(value.get(key) is None or isinstance(value[key], str)
               for key in ('url', 'title', 'generation', 'tab_identity'))


def _valid_tab_snapshot(tabs: Any) -> bool:
    if not isinstance(tabs, list) or len(tabs) > MAX_TAB_SNAPSHOT_SIZE:
        return False
    seen = set()
    for tab in tabs:
        if (not isinstance(tab, dict) or not _valid_tab_id(tab.get('id'))
                or tab['id'] in seen or not _valid_page_fields(tab)):
            return False
        seen.add(tab['id'])
    return True


def _timestamp(value: Any) -> Optional[float]:
    if type(value) not in (int, float):
        return None
    try:
        stamp = float(value)
    except OverflowError:
        return None
    return stamp if math.isfinite(stamp) else None


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
    retry_safe = False
    diagnostics: dict[str, Any]


class SessionDisconnectedError(ValueError):
    error_code = "session_disconnected"
    retry_safe = False
    delivery_state = "sent_unconfirmed"


class ExtensionNotConnectedError(ValueError):
    error_code = "extension_not_connected"
    retry_safe = True
    delivery_state = "undelivered"


class AmbiguousBrowserError(ValueError):
    error_code = "ambiguous_browser"
    retry_safe = False
    delivery_state = "undelivered"

    def __init__(self, clients: list[str]) -> None:
        clients = sorted(set(clients))
        super().__init__(
            f"Multiple browser clients match: {', '.join(clients)}. "
            "Pass a composite session_id or client_id from list_tabs/get_setup_status."
        )
        self.diagnostics = {"candidate_client_ids": clients, "next_action": "select_browser_client"}


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
    for name in (
        "failed_command_index", "commands_dispatched", "commands_completed", "input_dispatched",
        # What became of a page script after its evaluate timed out; the
        # extension only sets these on a dispatched cdp_timeout.
        "zombie", "zombie_detail",
    ):
        if field(name) is not None:
            diagnostics[name] = field(name)

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
        "batch_guard_failed", "capture_busy",
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
# An ACK can be lost after execution starts. Missing confirmation never proves
# that replaying a click, navigation or arbitrary script is safe.
RETRY_SAFE_DELIVERY_STATES = frozenset({'undelivered'})

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
        operation_id: Optional[str] = None,
        reservation_held: Optional[bool] = None,
        poll_with: Optional[str] = None,
        diagnostics: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.delivery_state = delivery_state
        self.retry_safe = bool(retry_safe)
        if operation_id is not None:
            self.operation_id = str(operation_id)
        if reservation_held is not None:
            self.reservation_held = bool(reservation_held)
        if poll_with is not None:
            self.poll_with = str(poll_with)
        if isinstance(diagnostics, dict):
            self.diagnostics = dict(diagnostics)


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
        "error_code": str(getattr(exc, "error_code", "timeout" if isinstance(exc, TimeoutError) else "internal_error")),
    }
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict):
        payload["diagnostics"] = diagnostics
    for name in (
        "delivery_state", "retry_safe", "operation_id", "reservation_held",
        "poll_with",
    ):
        value = getattr(exc, name, None)
        if value is not None:
            payload[name] = value
    return payload


def _attach_operation(
    exc: Any, operation_id: str, *, outcome_unknown: bool = False,
    reservation_held: Optional[bool] = True,
) -> None:
    exc.operation_id = operation_id
    exc.poll_with = 'get_execute_js_result'
    diagnostics = dict(getattr(exc, 'diagnostics', {}) or {})
    diagnostics.update({
        'operation_id': operation_id, 'poll_with': 'get_execute_js_result',
    })
    # Only the daemon knows whether the operation still reserves its target.
    if reservation_held is not None:
        diagnostics['reservation_held'] = reservation_held
    if outcome_unknown:
        diagnostics['operation_status'] = 'outcome_unknown'
        exc.delivery_state = 'delivered_no_result'
        exc.retry_safe = False
    exc.diagnostics = diagnostics


def _execution_may_continue(result: dict[str, Any]) -> bool:
    """A response watchdog can expire without cancelling dispatched page JS."""
    if result.get('success') is not False:
        return False
    detail = result.get('data')
    message, source = _page_error_source(detail)
    container = detail if isinstance(detail, dict) else {}
    if container.get('dispatched', source.get('dispatched')) is False:
        return False
    lower = message.lower()
    if 'debugger attach exceeded' in lower:
        return False
    # The extension's zombie probe settles the one question this function
    # exists to leave open. `killed` means the page script was terminated and
    # the thread answers again: no result can arrive, and a reservation would
    # protect nothing -- measured 2026-09-10, the next call on that tab got
    # target_busy against a script that was already dead. The other verdicts
    # (not_blocking: may still be awaiting async; still_running; unknown;
    # blocked_by_dialog) leave the outcome open and the reservation held.
    if container.get('zombie', source.get('zombie')) == 'killed':
        return False
    code = container.get('code') or source.get('code') or ''
    # executeScript's deadline races its Promise; it does not cancel the
    # injection. Apply the same bounded unknown-outcome retention as CDP.
    # The explicit pre-dispatch and terminated-script guards above still win.
    return str(code) in {'exec_timeout', 'cdp_timeout', 'debugger_detached'} or any(
        marker in lower for marker in (
            'cdp_timeout:', 'debugger_detached:', 'detached while handling command',
            'csp-relaxed injection timed out',
            'manual dialog ownership expired while waiting for an outcome',
        )
    )


def _raise_remote_error(response: dict[str, Any], *, script: str = "") -> None:
    code = response.get("error_code")
    message = response.get("error")
    exc: Any
    if code == "timeout" or (code == "internal_error" and 'did not respond within' in str(message)):
        exc = TimeoutError(message)
    elif response.get("delivery_state") is not None and code not in {
        "session_not_connected", "session_disconnected", "extension_not_connected", "ambiguous_browser",
    }:
        exc = BridgeNoResponseError(
            str(message), error_code=str(code or "transport_error"),
            delivery_state=str(response["delivery_state"]),
            retry_safe=bool(response.get("retry_safe", False)),
            operation_id=response.get("operation_id"),
            reservation_held=response.get("reservation_held"),
            poll_with=response.get("poll_with"),
            diagnostics=response.get("diagnostics"),
        )
    elif code == "ambiguous_browser":
        exc = AmbiguousBrowserError(response.get("diagnostics", {}).get("candidate_client_ids", []))
    elif code == "session_not_connected":
        exc = SessionNotConnectedError(message)
    elif code == "session_disconnected":
        exc = SessionDisconnectedError(message)
    elif code == "extension_not_connected":
        exc = ExtensionNotConnectedError(message)
    elif _is_page_execution_error(message, code) or isinstance(message, dict):
        exc = PageExecutionError(message, script=script, error_code=code, diagnostics=response.get("diagnostics"))
    else:
        exc = RuntimeError(message)
        exc.error_code = code or "internal_error"
        exc.retry_safe = bool(response.get("retry_safe", False))
    if isinstance(response.get("diagnostics"), dict):
        exc.diagnostics = dict(response["diagnostics"])
    for name in (
        "delivery_state", "retry_safe", "operation_id", "reservation_held",
        "poll_with",
    ):
        if name in response:
            setattr(exc, name, response[name])
    raise exc

# --- /link 鉴权 --------------------------------------------------------------
# WS 口靠 origin 前缀挡住网页（扩展读不到磁盘上的密钥，只能这么做）；但 /link 是
# 命令通道，本机任意进程都能 POST 一条 execute_js 在用户登录态的 Chrome 里跑任意
# JS。BTAP 默认用用户目录中的持久 token 鉴权；旧环境变量只用于一次迁移。
TOKEN_ENV = 'BROWSERTAP_BRIDGE_TOKEN'  # noqa: S105 - environment variable name
TOKEN_FILE_ENV = 'BROWSERTAP_BRIDGE_TOKEN_FILE'  # noqa: S105 - environment variable name
TOKEN_AUTH_ENV = 'BROWSERTAP_BRIDGE_AUTH'  # noqa: S105 - environment variable name

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


def tcp_port_open(host: str, port: int, *, timeout: float = 1) -> bool:
    """Probe TCP addresses without hiding resolver/socket errors from diagnostics."""
    error: OSError | None = None
    probed = False
    for family, kind, protocol, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM,
    ):
        try:
            with socket.socket(family, kind, protocol) as probe:
                probe.settimeout(timeout)
                if probe.connect_ex(address) == 0:
                    return True
                probed = True
        except OSError as exc:
            error = exc
    if not probed and error is not None:
        raise error
    return False


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
    return Path(configured).expanduser().resolve() if configured else state_dir() / 'bridge-token'


@dataclass(frozen=True)
class _TokenFileState:
    status: str
    exists: bool | None
    value: str = field(default='', repr=False)
    error: str | None = None


def _token_file_state(path: Path) -> _TokenFileState:
    """Read a token without confusing absent, empty and unreadable files.

    Errors contain fixed reason codes, never exception text or undecodable
    bytes. The value is excluded from repr so inspecting this state stays safe.
    """
    try:
        value = _token_file.read_text(path).strip()
    except FileNotFoundError:
        return _TokenFileState('missing', False)
    except UnicodeError:
        return _TokenFileState('invalid_encoding', True, error='invalid_utf8')
    except OSError as exc:
        exists: bool | None
        try:
            path.stat()
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError:
            exists = None
        reason = 'permission_denied' if isinstance(exc, PermissionError) else 'io_error'
        return _TokenFileState('unreadable', exists, error=reason)
    return _TokenFileState('ready' if value else 'empty', True, value)


def _read_token_file(path: Path) -> str:
    """Keep the legacy read-or-empty contract for callers that only need a value."""
    return _token_file_state(path).value


def _token_file_error(path: Path, state: _TokenFileState) -> RuntimeError:
    reason = f' ({state.error})' if state.error else ''
    return RuntimeError(f'BTAP bridge token file is {state.status}: {path}{reason}')


def _persist_token(path: Path, token: str) -> str:
    """Create the token file once; concurrent starters converge on its value."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _token_file.create(path) as fd:
            pending = memoryview((token + '\n').encode('utf-8'))
            while pending:
                written = os.write(fd, pending)
                if written <= 0 or written > len(pending):
                    raise RuntimeError(
                        f'BTAP cannot persist its bridge token at {path}: '
                        f'invalid token write count: {written}'
                    )
                pending = pending[written:]
        return token
    except FileExistsError:
        # POSIX publishes a complete candidate; Windows reads wait for the
        # creator's exclusive handle. The short empty-file wait also tolerates
        # older creators without replacing an existing token with our candidate.
        for _ in range(20):
            state = _token_file_state(path)
            if state.status == 'ready':
                return state.value
            if state.status != 'empty':
                raise _token_file_error(path, state) from None
            time.sleep(0.01)
        raise RuntimeError(f'BTAP bridge token file is empty: {path}') from None
    except UnicodeError:
        raise RuntimeError(f'BTAP cannot persist its bridge token at {path} (invalid_utf8)') from None
    except OSError as exc:
        reason = 'permission_denied' if isinstance(exc, PermissionError) else 'io_error'
        raise RuntimeError(f'BTAP cannot persist its bridge token at {path} ({reason})') from None


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
    state = _token_file_state(path)
    if state.status == 'ready':
        return state.value
    if state.status not in {'missing', 'empty'}:
        raise _token_file_error(path, state) from None
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
    token_state = _token_file_state(token_path)
    stored = token_state.value
    auth_mode = (os.environ.get(TOKEN_AUTH_ENV) or "").strip().lower()
    try:
        directory_exists: bool | None = directory.is_dir()
    except OSError:
        # Directory metadata may be denied along with the token. Keep the
        # available token diagnosis and leave existence explicitly unknown.
        directory_exists = None
    report: dict[str, Any] = {
        "state_dir": str(directory),
        "state_dir_exists": directory_exists,
        "state_dir_kind": kind,
        "state_dir_env": STATE_DIR_ENV if configured_dir else None,
        "default_state_dir_name": DEFAULT_STATE_DIR_NAME,
        "token_file": str(token_path),
        "token_file_exists": token_state.exists,
        "token_file_status": token_state.status,
        "token_file_error": token_state.error,
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
            stored and hmac.compare_digest(stored.encode('utf-8'), enforced_token.encode('utf-8'))
        )
    return report


def header_token(headers) -> str:
    """从 Authorization: Bearer <t> 或 X-Bridge-Token 取 token，取不到返回 ''。

    两个头都收：Authorization 是常规写法，X-Bridge-Token 留给设不了
    Authorization 的调用方（某些 userscript / 反代）。
    """
    try:
        auth = (headers.get('Authorization', '') or '').strip()
        if auth[:7].lower() == 'bearer ':
            return auth[7:].strip()
        return (headers.get('X-Bridge-Token', '') or '').strip()
    except UnicodeError:
        # Bottle decodes WSGI headers as UTF-8; malformed bytes are not credentials.
        return ''


def check_link_token(headers, want: str) -> None:
    """token 不对就抛 401。want 为空仅表示鉴权被显式关闭。"""
    if not want:
        return
    got = header_token(headers)
    # compare_digest 而非 ==：避免按字节提前返回泄露前缀。
    if not got or not hmac.compare_digest(got.encode('utf-8'), want.encode('utf-8')):
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
    except Exception:  # noqa: S110 - body draining is best-effort during refusal
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
    except Exception:  # noqa: S110 - malformed request bodies are discarded
        data = None
    if isinstance(data, dict):
        return data
    try:
        request.body.read(request.MEMFILE_MAX + 1)
    except Exception:  # noqa: S110 - malformed request body discard is best-effort
        pass
    return None


# How long an HTTP session may go without long-polling before it counts as gone.
# That transport has no close event -- a WebSocket does -- so silence is the only
# signal there is, and each accepted poll refreshes `last_activity_at`.
HTTP_SESSION_IDLE_SECONDS = 60

# How long one poll waits for a command before answering "ask again". It has to
# stay well under the idle timeout above: the clock the timeout measures only
# advances when the next poll arrives, so a window as long as the timeout would let a
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

    client: Any
    connect_at: float
    last_activity_at: float
    disconnect_at: Optional[float]

    def __init__(self, session_id, info, client=None):
        self.id = session_id
        self.reconnect(client, info)

    def reconnect(self, client, info):
        """Refresh traffic, starting a new age only when the connection changes."""
        now = time.time()
        continuing = (
            hasattr(self, 'connect_at')
            and self.client is client
            and self.type == info.get('type', 'ws')
            and self.disconnect_at is None
            and (self.type != _QUEUE_TYPE
                 or now - self.last_activity_at <= HTTP_SESSION_IDLE_SECONDS)
        )
        if not continuing:
            self.connect_at = now
        self.last_activity_at = now
        self.info = dict(info, connected_at=self.connect_at)
        self.client = client
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
                and time.time() - self.last_activity_at > HTTP_SESSION_IDLE_SECONDS):
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
        logger.info("Tab disconnected: %s (session=%s)", redact_url(self.url), _log_reference(self.id))
        self.disconnect_at = time.time()


class BrowserBridge:
    # Refused ext_clients takeovers, and the last one, for diagnose(). Class
    # level so a driver built without __init__ still reads a number instead of
    # raising -- the offline tests build one that way because __init__ binds
    # ports, and remote mode skips half of it too.
    rejected_client_takeovers = 0
    last_rejected_takeover = None
    rejected_operation_replies = 0
    last_rejected_operation_reply = None

    @property
    def default_session_id(self):
        from .command_scope import get_scoped_default_session

        with _DRIVER_STATE_LOCK:
            fallback = getattr(self, '_default_session_id', None)
            revision = getattr(self, '_default_session_revision', 0)
            return get_scoped_default_session(self, fallback, revision=revision)

    @default_session_id.setter
    def default_session_id(self, value):
        from .command_scope import set_scoped_default_session

        with _DRIVER_STATE_LOCK:
            fallback = getattr(self, '_default_session_id', None)
            revision = getattr(self, '_default_session_revision', 0)
            if not set_scoped_default_session(self, value, fallback, revision=revision):
                self._default_session_id = value
                self._default_session_revision = revision + 1

    def publish_default_session_id(
        self, value, *, expected=_UNSET_DEFAULT, expected_revision=_UNSET_DEFAULT,
    ) -> bool:
        with _DRIVER_STATE_LOCK:
            if expected is not _UNSET_DEFAULT and getattr(self, '_default_session_id', None) != expected:
                return False
            revision = getattr(self, '_default_session_revision', 0)
            if expected_revision is not _UNSET_DEFAULT and revision != expected_revision:
                return False
            self._default_session_id = value
            self._default_session_revision = revision + 1
            return True

    def _requester(self, requester_id=None) -> str:
        if requester_id is not None:
            return str(requester_id)
        return process_requester_id()

    def _operation_state(self) -> PendingOperations:
        with _DRIVER_STATE_LOCK:
            if not hasattr(self, '_pending_operations'):
                self._pending_operations = PendingOperations(requester_liveness=requester_liveness)
            return self._pending_operations

    def _capture_state(self) -> CaptureOwnershipRegistry:
        with _DRIVER_STATE_LOCK:
            if not hasattr(self, '_capture_ownership'):
                self._capture_ownership = CaptureOwnershipRegistry(requester_liveness=requester_liveness)
                self._capture_commands: dict[str, CaptureOperation] = {}
            return self._capture_ownership

    def _complete_operation(self, operation_id: str, result: dict[str, Any]) -> bool:
        uncertain = _execution_may_continue(result)
        with _DRIVER_STATE_LOCK:
            operations = self._operation_state()

            def settle_active_reply() -> None:
                capture_commands = getattr(self, '_capture_commands', {})
                capture_operation = capture_commands.get(operation_id)
                data = result.get('data')
                succeeded = result.get('success') is True and not (
                    isinstance(data, dict) and data.get('ok') is False
                )
                if capture_operation is not None:
                    self._capture_state().finish(capture_operation, success=succeeded)
                    # Subsequent replies are observations only; command
                    # bookkeeping need not outlive this first reply.
                    capture_commands.pop(operation_id, None)
                if succeeded and isinstance(data, dict) and data.get('pending_execution') is False:
                    operations.observe_manual_execution(operation_id)

            # The registry runs settlement only while the operation is active,
            # before releasing its target. A late reply only adds evidence.
            return operations.complete(
                operation_id, result, outcome_unknown=uncertain, on_active_reply=settle_active_reply,
            )

    def _sync_pending_operations(self) -> None:
        with _DRIVER_STATE_LOCK:
            operations = getattr(self, '_pending_operations', None)
            if operations is None:
                return
            for operation_id in operations.pending_ids():
                if operation_id in self.acks:
                    operations.acknowledge(operation_id)
                result = self.results.get(operation_id)
                if isinstance(result, dict):
                    self._complete_operation(operation_id, result)
            retained = operations.retained_ids()
            # Command bookkeeping follows result retention; capture ownership
            # survives an unanswered command until a proven stop or lifecycle end.
            for cache in (self.results, self.acks, getattr(self, '_capture_commands', {})):
                for operation_id in list(cache):
                    if operation_id not in retained:
                        cache.pop(operation_id, None)

    def _record_operation_reply(
        self,
        data: dict[str, Any],
        *,
        transport: str,
        owner: Any = None,
    ) -> bool:
        """Publish one inbound ACK/result only for the operation that sent it."""
        operation_id = data.get('id')
        kind = data.get('type')
        valid = (
            kind in ('ack', 'result', 'error')
            # Completed undefined values are sent as explicit null. An absent
            # result is an incomplete receipt and cannot settle the operation.
            and (kind != 'result' or 'result' in data)
            and (data.get('tabId') is None or _valid_tab_id(data['tabId']))
            and _valid_tab_snapshot(data.get('newTabs', []))
        )
        # Lifecycle cleanup uses the same lock: it cannot end an operation
        # between reply validation and publication.
        with _DRIVER_STATE_LOCK:
            operations = self._operation_state()
            if (not _valid_protocol_id(operation_id) or not valid
                    or not operations.accepts_reply(operation_id, transport, owner, allow_late=kind != 'ack')):
                self.rejected_operation_replies += 1
                self.last_rejected_operation_reply = {
                    'operation_id': operation_id[:64] if isinstance(operation_id, str) else '',
                    'type': str(kind)[:32],
                    'transport': transport,
                    'ts': time.time(),
                }
                return False
            if kind == 'ack':
                self.acks[operation_id] = time.time()
                operations.acknowledge(operation_id)
            else:
                self.results[operation_id] = {
                    'success': kind == 'result',
                    'data': data.get('result' if kind == 'result' else 'error'),
                    'newTabs': data.get('newTabs', []),
                    'tabId': data.get('tabId'),
                    'ts': time.time(),
                }
                # Commit before releasing the publication lock, so a duplicate
                # cannot replace a terminal reply before waiters are notified.
                self._complete_operation(operation_id, self.results[operation_id])
        self._notify_activity()
        return True

    def _finish_tab_operations(
        self, session_id: str, reason: str, *, expected_generation: str | None = None,
    ) -> None:
        with _DRIVER_STATE_LOCK:
            if ':' in str(session_id):
                client_id, tab_id = str(session_id).rsplit(':', 1)
                # A close reply can race the next tabs snapshot. If Chrome has
                # already reused the native id for a replacement tab, the
                # close must not settle operations that belong to that new
                # generation. The snapshot path will settle the old session.
                current = self.sessions.get(str(session_id))
                if (expected_generation is not None and current is not None
                        and str(current.info.get('generation')) != str(expected_generation)):
                    return
                # Session ids normally end in Chrome's numeric native tab id,
                # but this cleanup path also sees stale/caller-supplied handles.
                # A malformed suffix must not prevent the operation registry
                # below from settling the target.
                try:
                    native_tab_id = int(tab_id)
                except (TypeError, ValueError):
                    native_tab_id = None
                if native_tab_id is not None:
                    self._capture_state().release_tab(client_id, native_tab_id)
                    for key, capture in list(self._capture_commands.items()):
                        if capture.client_id == client_id and capture.tab_id == native_tab_id:
                            self._capture_commands.pop(key, None)
            self._operation_state().finish_target(str(session_id), reason)

    def __init__(self, host: str = '127.0.0.1', port: int = 18765):
        port = validate_bridge_port(port)
        self.host, self.port = host, port
        self.started_at = time.time()
        self.sessions: dict[str, Session] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.acks: dict[str, float] = {}
        self.rejected_client_takeovers = 0
        self.last_rejected_takeover = None
        self.rejected_operation_replies = 0
        self.last_rejected_operation_reply = None
        # Old session handles remain useful only when the extension provided
        # explicit replacement evidence (`tabs.onReplaced`). Never infer this
        # map from URL/title or from a reused native id.
        self._rebindings: dict[str, dict[str, Any]] = {}
        # Commands are completed by the HTTP/WS server threads. A condition lets
        # the waiting caller resume as soon as one of those threads records a
        # result, ACK or session lifecycle change instead of paying the old
        # 50 ms polling slice on every successful bridge round trip.
        self._activity_condition = threading.Condition()
        self._activity_serial = 0
        self.default_session_id: Optional[str] = None
        self.latest_session_id: Optional[str] = None
        # Last time ANY extension pushed ext_ready/tabs_update. Lets `doctor`
        # tell "extension never registered" (0 or stale) from "registered but
        # just dropped" — the single most useful signal for classifying why
        # /link is empty. None until the first registration this daemon sees.
        self.last_ext_seen: Optional[float] = None
        # Per-client last-seen: which browser is stale vs which just dropped.
        self.client_last_seen: dict[str, dict[str, Any]] = {}
        # client_id -> {'ws','browser','ts'}: the extension's own socket, one per
        # browser, independent of how many tabs exist. Addresses the service
        # worker directly for tab-less commands; see ext_cmd().
        self.ext_clients: dict[str, dict[str, Any]] = {}
        self.is_remote = tcp_port_open(host, port + 1)
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
                    if tcp_port_open(host, port + 1):
                        break
                self.is_remote = True
        if not self.is_remote:
            self.start_ws_server()
            self.start_http_server()
        else:
            url_host = f'[{self.host}]' if ':' in self.host else self.host
            self.remote = f'http://{url_host}:{self.port+1}/link'
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
        self._sync_pending_operations()
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

    def _listener_address(self) -> tuple[int, str]:
        """Keep the lock, WS and HTTP listeners on the same resolved address."""
        endpoint = getattr(self, '_listen_endpoint', None)
        if endpoint is None:
            family, _, _, _, address = socket.getaddrinfo(
                self.host, self.port, type=socket.SOCK_STREAM,
            )[0]
            host = str(address[0])
            if family == socket.AF_INET6 and len(address) == 4 and address[3]:
                host = f'{host}%{address[3]}'
            endpoint = self._listen_endpoint = (family, host)
        return endpoint

    def _acquire_host_lock(self):
        family, host = self._listener_address()
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((host, self.port + 2))
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

        @app.install
        def _redacted_errors(callback):
            # Bottle's catchall writes exception text/source lines to wsgi.errors.
            # Stop unexpected callback failures here, before that logging boundary.
            operation = callback.__name__
            if operation not in ('long_poll', 'result', 'link', '_reject_cross_origin'):
                operation = 'unknown'

            @wraps(callback)
            def guarded(*args, **kwargs):
                try:
                    return callback(*args, **kwargs)
                except (bottle.HTTPResponse, MemoryError):
                    raise
                except Exception as exc:
                    logger.error("HTTP request failed (operation=%s; error_type=%s)",
                                 operation, type(exc).__name__)
                    drain_request_body()
                    raise bottle.HTTPError(500, 'Internal Server Error') from None
            return guarded

        @app.hook('before_request')
        @_redacted_errors
        def _reject_cross_origin():
            # These routes execute JS in the user's logged-in tabs, so nothing
            # that carries a web Origin may reach them. In practice bottle's
            # request.json requires Content-Type: application/json, which forces
            # a CORS preflight this server never answers — but that is a
            # side effect of the parser, not a decision. Make it explicit, and
            # deliberately send no Access-Control-* headers so no browser ever
            # gets permission to read a response.
            try:
                origin = request.headers.get('Origin', '') or ''
            except UnicodeError:
                drain_request_body()
                raise bottle.HTTPResponse(status=403, body='forbidden origin') from None
            if origin and not origin_is_allowed(origin):
                drain_request_body()
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
            if not _valid_protocol_id(session_id) or not _valid_page_fields(data):
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
            with _DRIVER_STATE_LOCK:
                session = self.sessions.get(session_id)
                if session is None:
                    session = self.sessions[session_id] = Session(
                        session_id, session_info, queue.Queue())
                    logger.info("Browser HTTP connected: %s (session=%s)",
                                redact_url(session.url), _log_reference(session_id))
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
                    # Serialize the fallback with old-socket cleanup, so a late
                    # close cannot disconnect the queue that just replaced it.
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
            except Exception as exc:
                # Everything on this queue was serialised by `_send_to_session`,
                # so a payload that will not parse is this bridge's own bug, not
                # a client's. Hand it over anyway -- refusing would strand a
                # caller already waiting on its result -- but say so.
                logger.error("unparseable long-poll payload (session=%s; error_type=%s)",
                             _log_reference(session_id), type(exc).__name__)
            else:
                # An id-less payload acknowledges nothing. Recording it under the
                # empty string, as this did, put a key in `acks` that no waiter
                # ever looks up -- `exec_id` is a uuid4 -- so it sat there until
                # the 600-second sweep collected it.
                if exec_id:
                    self._record_operation_reply(
                        {'type': 'ack', 'id': exec_id},
                        transport='http', owner=session_id,
                    )
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
            self._record_operation_reply(
                data, transport='http', owner=data.get('sessionId'),
            )
            return 'ok'

        @app.route('/link', method=['GET','POST'])
        def link():
            # Bottle encodes returned text as strict UTF-8. ASCII JSON escapes
            # preserve browser UTF-16 surrogates in both values and object keys.
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
                                  ensure_ascii=True)
            cmd = data.get('cmd')
            if cmd == 'get_clients':
                return json.dumps({'r': [
                    {'client_id': cid, 'browser': entry.get('browser')}
                    for cid, entry in list(self.ext_clients.items())
                ]}, ensure_ascii=True)
            if cmd == 'get_all_sessions':
                try:
                    return json.dumps({'r': self.get_all_sessions()}, ensure_ascii=True)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='get_all_sessions failed')},
                                      ensure_ascii=True)
            if cmd == 'diagnose':
                try:
                    return json.dumps({'r': self.diagnose()}, ensure_ascii=True)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='diagnose failed')},
                                      ensure_ascii=True)
            if cmd == 'find_session':
                url_pattern = data.get('url_pattern', '')
                try:
                    return json.dumps({'r': self.find_session(url_pattern)}, ensure_ascii=True)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='find_session failed')},
                                      ensure_ascii=True)
            if cmd == 'resolve_session':
                try:
                    session_id = data.get('sessionId')
                    if session_id is None:
                        return json.dumps({'r': None}, ensure_ascii=True)
                    return json.dumps(
                        {'r': self._resolve_local_session_target(str(session_id))},
                        ensure_ascii=True,
                    )
                except Exception as e:
                    return json.dumps({'r': _error_payload(e, prefix='resolve_session failed')},
                                      ensure_ascii=True)
            if cmd == 'ext_cmd':
                try:
                    payload = data.get('payload')
                    if (
                        not isinstance(payload, dict)
                        or not isinstance(payload.get('cmd'), str)
                        or not payload['cmd'].strip()
                    ):
                        return json.dumps({'r': {
                            'error': (
                                'ext_cmd requires payload to be a JSON object with a non-empty string cmd field; '
                                'send {"cmd":"ext_cmd","payload":{"cmd":"tabs",...}}'
                            ),
                            'error_code': 'invalid_payload',
                            'hint': 'Do not put the extension command at the /link top level.',
                        }}, ensure_ascii=True)
                    timeout = _positive_timeout(data.get('timeout', 15.0))
                    caller_options = {}
                    if data.get('requesterId') is not None:
                        caller_options['requester_id'] = str(data['requesterId'])
                    if data.get('operationId') is not None:
                        caller_options['operation_id'] = str(data['operationId'])
                    result = self.ext_cmd(payload,
                                          client_id=self.select_client_id(data.get('clientId'), use_default=False),
                                          timeout=timeout, **caller_options)
                    return json.dumps({'r': result}, ensure_ascii=True)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e)}, ensure_ascii=True)
            if cmd == 'execute_js':
                session_id = data.get('sessionId')
                code = data.get('code')
                # Absent means False: an older client that doesn't send the flag
                # gets the safe behaviour (no substitute tab), not the old one.
                allow_failover = str(data.get('allowFailover', '0')) == '1'
                try:
                    timeout = _positive_timeout(data.get('timeout', 10.0))
                    if session_id is None:
                        self.select_client_id(use_default=False)
                    route_options: dict[str, Any] = {}
                    if str(data.get('allowRebind', '1')) == '0':
                        route_options['allow_rebind'] = False
                    if data.get('requesterId') is not None:
                        route_options['requester_id'] = str(data['requesterId'])
                    if data.get('operationId') is not None:
                        route_options['operation_id'] = str(data['operationId'])
                    if str(data.get('wait', '1')) == '0':
                        route_options['wait'] = False
                    if data.get('readOnlyProbe') is True:
                        route_options['read_only_probe'] = True
                    result = self.execute_js(code, timeout=timeout, session_id=session_id,
                                              allow_failover=allow_failover, **route_options)
                    logger.debug("Remote execute_js completed (session=%s)", _log_reference(session_id))
                    return json.dumps({'r': result}, ensure_ascii=True)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e)}, ensure_ascii=True)
            if cmd == 'get_execute_js_result':
                try:
                    result = self.get_execute_js_result(
                        str(data.get('operationId', '')),
                        timeout=float(data.get('timeout', 0)),
                        requester_id=data.get('requesterId'),
                    )
                    return json.dumps({'r': result}, ensure_ascii=True)
                except Exception as e:
                    return json.dumps({'r': _error_payload(e)}, ensure_ascii=True)
            # 未知 cmd 必须报错而不是含糊地回 "ok"：调用方会把裸 "ok" 当成功，
            # 而实际上什么都没执行（例如把 ext_cmd 的 payload 直接当顶层 cmd 发）。
            return json.dumps(
                {'r': {
                    'error': f'unknown cmd: {cmd!r}; extension commands require cmd=ext_cmd plus payload',
                    'error_code': 'unknown_command',
                }},
                ensure_ascii=True)
        from socketserver import TCPServer, ThreadingMixIn
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

        self.http_server: Optional[WSGIServer] = None

        def run():
            family, host = self._listener_address()
            class _T(ThreadingMixIn, WSGIServer):
                # A request thread must never outlive shutdown: server_close()
                # would otherwise join a long-poll that still has seconds to go.
                daemon_threads = True
                address_family = family

                def server_bind(self):
                    # HTTPServer reverse-resolves the bind address before
                    # listen(), which can stall on the macOS system resolver.
                    # This local JSON bridge only needs the bound address in
                    # its WSGI environment, not a DNS-derived server name.
                    TCPServer.server_bind(self)
                    self.server_name = str(self.server_address[0])
                    self.server_port = self.server_address[1]
                    self.setup_environ()

            class _H(WSGIRequestHandler):
                def log_request(self, *a): pass
            # Keep the listener reachable. The daemon never needs this — it dies
            # with its process — but a caller that owns the object rather than the
            # process (a test fixture) can then close the port instead of leaking
            # a listener that later steals requests aimed at its successor.
            server = make_server(
                host, self.port + 1, app, server_class=_T, handler_class=_H)
            self.http_server = server
            server.serve_forever()
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
        """Match the packaged extension ID, never just an extension scheme.

        A browser fixes its extension's Origin, so a different installed
        extension cannot publish a second clientId. A local non-browser process
        can still forge headers; this is not a local-process authentication
        boundary. Additional exact Origins and origin-less clients require
        their existing explicit operator overrides.
        """
        origin = self._ws_origin(sock)
        if not origin:
            return os.environ.get('BROWSERTAP_WS_ALLOW_NO_ORIGIN', '') == '1'
        return origin_is_allowed(origin)

    def clean_sessions(self):
        now = time.time()
        def expired(value):
            stamp = _timestamp(value)
            return stamp is None or now - stamp > 600

        # Snapshot keys, then re-fetch with .get: another thread (WS handler or a
        # concurrent clean_sessions from a parallel /link call) may delete a
        # session between the snapshot and access, so both the read and the
        # delete must tolerate a missing key instead of raising KeyError.
        for sid in list(self.sessions.keys()):
            session = self.sessions.get(sid)
            if session is None: continue
            if not session.is_active() and session.disconnect_at is not None \
                    and expired(session.disconnect_at):
                self.sessions.pop(sid, None)
        for old_sid, binding in list(getattr(self, '_rebindings', {}).items()):
            replacement = binding.get('replacement_session_id') if isinstance(binding, dict) else None
            session = self.sessions.get(replacement) if isinstance(replacement, str) else None
            if (session is None or not session.is_active()
                    or expired(binding.get('ts', now))):
                self._rebindings.pop(old_sid, None)
        # Results/acks that arrive after their caller already timed out would
        # otherwise accumulate forever in a long-lived daemon.
        for r_id in list(self.results.keys()):
            entry = self.results.get(r_id)
            if not isinstance(entry, dict) or expired(entry.get('ts', now)):
                self.results.pop(r_id, None)
        for a_id in list(self.acks.keys()):
            ts = self.acks.get(a_id)
            if expired(ts):
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
                    or expired(entry.get('ts', now))):
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
                except (ValueError, TypeError, RecursionError):
                    logger.warning('Rejected malformed WebSocket JSON')
                    return
                try:
                    if not isinstance(data, dict):
                        return
                    if data.get('type') == 'ready':
                        session_id = data.get('sessionId')
                        if not _valid_protocol_id(session_id) or not _valid_page_fields(data):
                            return
                        session_info = {'url': data.get('url'), 'title': data.get('title', ''),
                            'type': 'ws'}
                        driver._register_client(session_id, self, session_info)
                    elif data.get('type') in ['ext_ready', 'tabs_update']:
                        tabs = data.get('tabs', [])
                        # Namespace sessions per browser instance so Chrome/Edge (and
                        # multiple profiles) don't collide on identical small tab ids.
                        # Fall back to a per-connection uuid if an older extension
                        # doesn't send clientId — id(self) is unusable here because
                        # addresses get reused after GC, colliding namespaces.
                        client_id = data.get('clientId')
                        if client_id is None or client_id == '':
                            if not hasattr(self, '_fallback_cid'):
                                self._fallback_cid = f"conn_{uuid.uuid4().hex[:10]}"
                            client_id = self._fallback_cid
                        browser = data.get('browser', '')
                        # Validate the WHOLE snapshot before claiming a socket
                        # or publishing liveness; invalid input changes nothing.
                        if (not _valid_protocol_id(client_id) or not isinstance(browser, str)
                                or not _valid_tab_snapshot(tabs)):
                            return
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
                        # Publish the socket and its tab snapshot together. A
                        # failed send on another thread may retire the old socket
                        # while this reconnect is arriving; neither transition
                        # may act on a half-published replacement.
                        with _DRIVER_STATE_LOCK:
                            claimed = driver._claim_ext_client(client_id, browser, self)
                            if claimed:
                                driver.last_ext_seen = time.time()
                                driver.client_last_seen[client_id] = {'ts': time.time(), 'browser': browser}
                                driver._apply_extension_tabs(client_id, browser, tabs, self)
                        if not claimed:
                            try: self.close()
                            except Exception: pass  # noqa: S110 - closing a refused socket is best-effort
                            return
                    elif data.get('type') == 'ping':
                        # Liveness reply so the extension can tell a live socket
                        # from a half-open zombie (TCP ESTABLISHED but dead). No
                        # pong within a couple keepalive ticks => extension force-
                        # reconnects instead of pushing tabs into a black hole.
                        # It is also the only regular traffic on an idle browser,
                        # so it is what keeps a namespace owner's ts fresh.
                        driver._touch_ext_client(self)
                        try: self.send_message(json.dumps({'type': 'pong'}))
                        except Exception: pass  # noqa: S110 - peer may have disconnected
                    elif data.get('type') == 'ack':
                        driver._record_operation_reply(data, transport='ws', owner=self)
                    elif data.get('type') == 'result':
                        driver._record_operation_reply(data, transport='ws', owner=self)
                    elif data.get('type') == 'error':
                        driver._record_operation_reply(data, transport='ws', owner=self)
                except Exception as exc:
                    operation = data.get('type') if isinstance(data, dict) else None
                    if operation not in ('ready', 'ext_ready', 'tabs_update', 'ping', 'ack', 'result', 'error'):
                        operation = 'unknown'
                    logger.error("Error handling WebSocket message (operation=%s; error_type=%s)",
                                 operation, type(exc).__name__)
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
                    logger.warning("Rejected WS connection from %s, origin=%s",
                                   self.address, _log_reference(origin))
                    try: self.close()
                    except Exception: pass  # noqa: S110 - rejected socket is already unusable
                    return
                logger.info("New WS connection from %s", self.address)
            def handle_close(self):
                logger.info("WS connection closed: %s", self.address)
                driver._unregister_client(self)

        # First bind stays in the caller's thread so startup failures are loud.
        _, host = self._listener_address()
        self.server = WebSocketServer(host, self.port, JSExecutor)

        def run():
            # serve_forever has no internal guard: any exception that escapes
            # the poll loop silently kills the thread while the LISTEN socket
            # keeps accepting into the kernel backlog - clients show
            # ESTABLISHED but no handshake ever completes. Rebuild and go on.
            srv = self.server
            while True:
                try:
                    srv.serve_forever()
                except Exception as exc:
                    logger.error("WS server loop crashed; rebuilding in 1s (error_type=%s)",
                                 type(exc).__name__)
                try:
                    srv.close()
                except Exception:  # noqa: S110 - server close is best-effort before rebuild
                    pass
                time.sleep(1)
                try:
                    srv = self.server = WebSocketServer(host, self.port, JSExecutor)
                    logger.info("WS server rebuilt on ws://%s:%s", self.host, self.port)
                except Exception as exc:
                    logger.error("WS server rebuild failed (error_type=%s)", type(exc).__name__)
                    time.sleep(2)

        server_thread = threading.Thread(target=run)
        server_thread.daemon = True
        server_thread.start()
        logger.info("WebSocket server running on ws://%s:%s", self.host, self.port)

    def _apply_extension_tabs(self, client_id: str, browser: str,
                              tabs: list[dict], client: WebSocket) -> None:
        """Apply one extension tab snapshot, respecting tab lifecycle generations."""
        if not _valid_tab_snapshot(tabs):
            raise ValueError('Invalid extension tab snapshot')
        def _sid(tab_id): return f"{client_id}:{tab_id}"

        current_tab_ids = {_sid(tab['id']) for tab in tabs}
        logger.debug("Received tabs update from %s (browser=%s; count=%s)",
                     _log_reference(client_id), _log_reference(browser), len(current_tab_ids))
        rebindings = getattr(self, '_rebindings', None)
        if rebindings is None:
            rebindings = self._rebindings = {}
        prior_by_identity: dict[str, list[str]] = {}
        current_identity_counts: dict[str, int] = {}
        sess: Optional[Session]
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
                self._finish_tab_operations(sid, 'tab_removed')
        for tab in tabs:
            session_id = _sid(tab['id'])
            session_info = {
                'url': tab.get('url'),
                'title': tab.get('title', ''),
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
                    _log_reference(identity),
                    _log_reference(old_sid),
                    _log_reference(session_id),
                )
            old_generation = sess.info.get('generation') if sess else None
            new_generation = session_info.get('generation')
            if (sess and old_generation is not None
                    and new_generation is not None
                    and str(old_generation) != str(new_generation)):
                # Same client:tabId, different native tab lifetime. Replace the
                # session object so waiters cannot mistake the old registration
                # for the tab chrome.tabs.create just returned.
                logger.info(
                    "Tab generation changed for %s: %s -> %s",
                    _log_reference(session_id),
                    _log_reference(old_generation),
                    _log_reference(new_generation),
                )
                sess.mark_disconnected()
                self._finish_tab_operations(session_id, 'generation_changed')
                self.sessions.pop(session_id, None)
                sess = None
                lifecycle_changed = True
            if sess and sess.is_active():
                # A snapshot is activity on the existing connection unless its
                # socket changed. Keep both the channel and age in one update.
                sess.reconnect(client, session_info)
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
        with _DRIVER_STATE_LOCK:
            session = self.sessions.get(session_id)
            if session is None:
                session = self.sessions[session_id] = Session(session_id, session_info, client)
                logger.info("New tab connected: %s (session=%s)", redact_url(session.url), _log_reference(session_id))
            else:
                session.reconnect(client, session_info)
                logger.info("Tab reconnected: %s (session=%s)", redact_url(session.url), _log_reference(session_id))

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
        # Reconnect publication uses this lock too. Comparing the owner and then
        # mutating without it can disconnect a rebound Session or pop a client ID
        # that already belongs to a new socket.
        lifecycle_changed = False
        with _DRIVER_STATE_LOCK:
            for session in list(self.sessions.values()):
                if session.ws_client is client:
                    lifecycle_changed = lifecycle_changed or session.is_active()
                    session.mark_disconnected()
            # Losing the transport does not prove its page operations ended:
            # retain their reservations, capture ownership and reply admission.
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
            stamp = _timestamp(incumbent.get('ts'))
            # An entry with no usable stamp cannot prove it is alive, and the
            # lockout is the worse failure, so it counts as ancient.
            idle = now - stamp if stamp is not None else now
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
                    _log_reference(client_id), getattr(client, 'address', '?'), idle,
                    CLIENT_TAKEOVER_GRACE_SECONDS)
                return False
            logger.warning(
                "Handing client_id=%r to a new socket: the previous one went silent "
                "%.1fs ago", _log_reference(client_id), idle)
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
        if getattr(self, 'is_remote', False):
            if cur is not None:
                return cur
            client_id = self.select_client_id()
            sessions = [s for s in self.get_all_sessions()
                        if str(s.get('id', '')).rsplit(':', 1)[0] == client_id]
            if not sessions:
                return None
            candidates = [s for s in sessions if is_scriptable_url(s.get('url'))] or sessions
            chosen = next((s for s in candidates if s.get('active')), candidates[0])
            self.default_session_id = str(chosen['id'])
            return self.default_session_id
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
                    logger.info("Default session %s rebound to %s", _log_reference(cur), _log_reference(selected))
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
        if cur and ':' in str(cur):
            client_id = str(cur).rsplit(':', 1)[0]
            alive = [s for s in alive if str(s.id).rsplit(':', 1)[0] == client_id]
            if not alive:
                return cur
        elif len({str(s.id).rsplit(':', 1)[0] for s in alive if ':' in str(s.id)}) > 1:
            raise AmbiguousBrowserError([str(s.id).rsplit(':', 1)[0] for s in alive])
        alive = _prefer_scriptable(alive)
        latest = self.sessions.get(self.latest_session_id) if self.latest_session_id is not None else None
        chosen = latest if latest is not None and latest in alive else alive[-1]
        if cur:
            logger.info("Default session %s is stale; selected %s", _log_reference(cur), _log_reference(chosen.id))
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
        latest = self.sessions.get(self.latest_session_id) if self.latest_session_id is not None else None
        return latest if latest in candidates else candidates[-1]

    def execute_js(self, code, timeout=15, session_id=None, allow_failover=False, *,
                   allow_rebind=True, wait=True, requester_id=None, operation_id=None,
                   read_only_probe=False) -> Any:
        """Run JS in a tab.

        allow_failover=False by default: if the target session is gone we raise
        instead of running the script on some other live tab, because the script
        may have side effects the caller only meant for the tab it named.

        read_only_probe is an internal MCP-server/bridge flag for generated
        wait probes, never an execute_js tool parameter. Authenticated /link
        clients are trusted to construct those probes, as with raw JS dispatch.
        """
        if not isinstance(code, str):
            raise ValueError("code must be a string")
        timeout = _positive_timeout(timeout)
        requester_id = self._requester(requester_id)
        deadline = time.monotonic() + timeout

        def remaining():
            return max(0.0, deadline - time.monotonic())

        # Whether the caller actually named a tab. An explicitly named dead tab
        # must still be refused below; an implicit one the driver supplied from
        # its own memory must not be, since the caller never chose it.
        caller_named_target = session_id is not None
        rebound = None
        if session_id is not None and not self.is_remote and allow_rebind:
            rebound = self.resolve_session_target(str(session_id))
            if rebound and rebound.get('session_id'):
                session_id = str(rebound['session_id'])
        if session_id is None:
            session_id = self._live_default_session_id()
        scoped_remote = self.is_remote and has_command_scope()
        if scoped_remote and session_id is not None:
            rebound = self.resolve_session_target(str(session_id), timeout=max(0.001, remaining()))
            if rebound and rebound.get('session_id'):
                session_id = str(rebound['session_id'])
            elif not caller_named_target:
                client_id = str(session_id).rsplit(':', 1)[0]
                candidates = [s for s in self.get_all_sessions(timeout=max(0.001, remaining()))
                              if str(s.get('id', '')).rsplit(':', 1)[0] == client_id]
                if candidates:
                    candidates = [s for s in candidates if is_scriptable_url(s.get('url'))] or candidates
                    session_id = str(next((s for s in candidates if s.get('active')), candidates[0])['id'])
                    self.default_session_id = session_id
        if session_id is not None:
            guard_targets(f"{getattr(self, 'host', '127.0.0.1')}:{getattr(self, 'port', 18765)}", [str(session_id)])
        if self.is_remote:
            logger.debug("Dispatching remote execute_js (session=%s)", _log_reference(session_id))
            # HTTP timeout must outlast the JS timeout or long scripts die at
            # the transport layer before the bridge can answer. The daemon
            # answers *at* `timeout`, so an equal socket deadline is a coin flip
            # that usually lands on a transport TimeoutError and throws away the
            # structured delivery verdict; REMOTE_TRANSPORT_MARGIN buys the
            # daemon's reply the time it needs to arrive.
            mark_dispatched()
            operation_id = str(operation_id or uuid.uuid4())
            payload = {"cmd": "execute_js", "sessionId": session_id,
                       "code": code, "timeout": str(max(0.001, remaining())),
                       "allowFailover": "1" if allow_failover and not scoped_remote else "0",
                       "wait": "1" if wait else "0", "requesterId": requester_id,
                       "operationId": operation_id}
            if read_only_probe is True:
                payload['readOnlyProbe'] = True
            if scoped_remote:
                # The lock names this resolved tab. Rebinding again in the
                # daemon would dispatch outside the caller's acquired lock.
                payload['allowRebind'] = '0'
            try:
                envelope = self._remote_cmd(payload,
                                            timeout=max(0.001, remaining())
                                            + REMOTE_TRANSPORT_MARGIN)
                response = envelope.get('r')
                if not isinstance(response, dict):
                    raise BridgeNoResponseError(
                        "Bridge returned a malformed execute_js result",
                        error_code='transport_error', delivery_state='sent_unconfirmed', retry_safe=False,
                    )
            except (TimeoutError, BridgeNoResponseError) as remote_error:
                if getattr(remote_error, 'delivery_state', None) != 'undelivered':
                    _attach_operation(remote_error, operation_id, reservation_held=None)
                raise
            if response.get('error'):
                _raise_remote_error(response, script=code)
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
                # onReplaced can arrive after the initial resolution above.
                # Accept only the bridge's identity-backed binding, and check
                # the new target against any caller-held command scope before
                # dispatching. URL/title similarity is never identity proof.
                if allow_rebind and session_id is not None:
                    replacement = self.resolve_session_target(str(session_id))
                    replacement_id = replacement.get('session_id') if replacement else None
                    if replacement_id and replacement_id != session_id:
                        candidate = self.sessions.get(str(replacement_id))
                        if candidate and candidate.is_active():
                            guard_targets(
                                f"{getattr(self, 'host', '127.0.0.1')}:{getattr(self, 'port', 18765)}",
                                [str(replacement_id)],
                            )
                            rebound = replacement
                            session_id, session = str(replacement_id), candidate
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
                    pool = same_client or alive_sessions if allow_failover else (same_client if want_client else alive_sessions)
                    if pool:
                        session = self._pick_failover_session(pool)
                        logger.warning(
                            "Session %s is disconnected; switched to active session %s",
                            _log_reference(session_id),
                            _log_reference(session.id),
                        )
                        switched_from = session_id
                        session_id = self.default_session_id = session.id
                if not session or not session.is_active():
                    if alive_sessions:
                        cands = ', '.join(str(s.id) for s in alive_sessions[:8])
                        exc = SessionNotConnectedError(
                            f"Session {session_id} is not connected. BTAP refused to execute because "
                            "no live session could be verified for the same tab. No script was dispatched. "
                            f"Active sessions: {cands}. Run list_tabs, verify the intended target, "
                            "then select its live session_id with switch_tab and retry."
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
        guard_targets(f"{getattr(self, 'host', '127.0.0.1')}:{getattr(self, 'port', 18765)}", [str(session.id)])
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
        exec_id = str(operation_id or uuid.uuid4())
        payload_dict: dict[str, Any] = {'id': exec_id, 'code': code}
        # Forward what is left of the caller's budget. The extension's CSP/CDP
        # fallback used to bound Runtime.evaluate by a ceiling of its own, so a
        # caller-chosen timeout larger than that ceiling was truncated with a
        # cdp_timeout that named a deadline the caller never set. ext_cmd has
        # carried this field for the same reason; the exec path did not.
        # One reading serves both the wire field and the dispatch decision
        # below. Two separate reads would let the extension be told a budget
        # that the guard then measured differently, and the gap between them is
        # the cost of json.dumps -- not a boundary worth two clock samples.
        dispatch_budget = remaining()
        payload_dict['timeoutMs'] = max(1, int(dispatch_budget * 1000))
        if tp == 'ext_ws':
            # session.id is now "client_id:tab_id"; use the raw browser tab id.
            payload_dict['tabId'] = int(session.info.get('tab_id', str(session.id).rsplit(':', 1)[-1]))
        wire_payload = json.dumps(payload_dict)
        # Which tab this command names, derived from the session the driver
        # resolved — so every return path (success, reload, timeout) reports
        # the executor's target instead of a later guess from driver memory.
        exec_tab_id = int(session.info.get('tab_id', str(session.id).rsplit(':', 1)[-1])) if tp == 'ext_ws' else None

        # A disconnected tab may recover during the bounded grace sleep above.
        # Recovery does not renew the caller's budget: once the deadline is
        # spent, do not dispatch a side-effecting script merely because the
        # socket came back at the last instant.
        if dispatch_budget <= 0:
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

        self._sync_pending_operations()
        self._operation_state().reserve(
            exec_id, [str(session.id)], requester_id,
            kind='wait_probe' if read_only_probe is True else 'execute_js',
            metadata={**extra, 'executed_tab_id': exec_tab_id, 'session_id': str(session.id)},
            reply_transport='http' if tp == 'http' else 'ws',
            reply_owner=str(session.id) if tp == 'http' else session.ws_client,
            # The same budget the extension was just handed as timeoutMs. Once
            # this caller has given up, the only question left is how long the
            # tab stays held for a reply that may still arrive -- so the hold is
            # sized from the caller's own deadline plus a grace margin, not from
            # one ceiling shared with every other kind of command.
            caller_budget=dispatch_budget,
        )
        with self._operation_state().retain_for_waiter(exec_id, requester_id):
            mark_dispatched()
            if tp in ['ws', 'ext_ws']:
                try: session.ws_client.send_message(wire_payload)
                except Exception as e:
                    # Half-open socket (e.g. after system sleep): mark it dead so
                    # the next call fails over instead of hitting it again.
                    session.mark_disconnected()
                    self._notify_activity()
                    disconnected = SessionDisconnectedError(
                        f"Session {session_id} disconnected ({e}); the command outcome is unknown."
                    )
                    _attach_operation(disconnected, exec_id)
                    raise disconnected from e
            elif tp == 'http': session.http_queue.put(wire_payload)

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
                # Tab lifecycle cleanup is committed under the same state lock
                # and wakes this condition. Do not make a caller wait out its
                # original deadline after the target is already gone.
                try:
                    lifecycle = self._operation_state().read(exec_id, requester_id)
                except Exception:
                    lifecycle = None
                if isinstance(lifecycle, dict) and lifecycle.get('status') == 'lifecycle_ended':
                    self.results.pop(exec_id, None)
                    self.acks.pop(exec_id, None)
                    return _no_response_result(
                        'The target tab lifecycle ended before the execution result could be returned',
                        delivery_state='navigated', executed_tab_id=exec_tab_id,
                        extra={**extra, 'operation_id': exec_id, 'js_return_lost': True}, closed=True,
                    )
                if not acked and exec_id in self.acks:
                    acked = True
                    self._operation_state().acknowledge(exec_id)
                if acked and not wait:
                    return {
                        **extra,
                        'status': 'in_progress',
                        'operation_id': exec_id,
                        'executed_tab_id': exec_tab_id,
                        'session_id': str(session.id),
                        'delivery_state': 'delivered_no_result',
                        'retry_safe': False,
                        'reservation_held': True,
                    }
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
                            extra={**extra, 'operation_id': exec_id},
                            closed=True,
                        )
                if remaining() <= 0:
                    # Make the deadline decision with one atomic dict operation.
                    # A result present now is consumed; one arriving after this
                    # point stays available through get_execute_js_result.
                    result = self.results.pop(exec_id, missing_result)
                    if result is not missing_result:
                        break
                    if read_only_probe is True:
                        self._operation_state().release_wait_probe(exec_id, requester_id)
                    snapshot = self._operation_state().read(exec_id, requester_id)
                    pending_extra = {
                        **extra, 'operation_id': exec_id,
                        'reservation_held': snapshot['reservation_held'],
                        'poll_with': 'get_execute_js_result',
                    }
                    if tp in ['ws', 'ext_ws']:
                        if hasjump:
                            return _no_response_result(
                                f"Session {session_id} reloaded and new page is loading...",
                                delivery_state="navigated",
                                executed_tab_id=exec_tab_id,
                                extra={**extra, 'operation_id': exec_id},
                                closed=True,
                            )
                        if acked:
                            return _no_response_result(
                                f"No response data in {timeout}s (ACK received, script may still be running)",
                                delivery_state="delivered_no_result",
                                executed_tab_id=exec_tab_id,
                                extra=pending_extra,
                            )
                        return _no_response_result(
                            (
                                f"No response data in {timeout}s (sent but no ACK; "
                                "the script may still have executed. Query the operation "
                                "result before considering a retry)"
                            ),
                            # A successful send is not proof of receipt; a missing
                            # ACK is likewise not proof that execution never began.
                            delivery_state="sent_unconfirmed",
                            executed_tab_id=exec_tab_id,
                            extra=pending_extra,
                        )
                    elif tp == 'http':
                        if acked:
                            return _no_response_result(
                                f"Session {session_id} no response in {timeout}s (delivered but no result)",
                                delivery_state="delivered_no_result",
                                executed_tab_id=exec_tab_id,
                                extra=pending_extra,
                            )
                        return _no_response_result(
                            f"Session {session_id} no response in {timeout}s (queued; a later poll may still execute it)",
                            delivery_state="sent_unconfirmed",
                            executed_tab_id=exec_tab_id,
                            extra=pending_extra,
                        )

            if not isinstance(result, dict):
                raise RuntimeError("execute_js completed without a result")
            completed = self._complete_operation(exec_id, result)
            # A remote caller already holds this id. Keep its receipt even if
            # the HTTP response is lost after the browser finishes.
            snapshot = self._operation_state().read(
                exec_id, requester_id, consume=completed and operation_id is None and wait,
            )
            self.acks.pop(exec_id, None)
            if snapshot['status'] == 'lifecycle_ended':
                return _no_response_result(
                    'The target tab lifecycle ended before the execution result could be returned',
                    delivery_state='navigated', executed_tab_id=exec_tab_id,
                    extra={**extra, 'operation_id': exec_id, 'js_return_lost': True}, closed=True,
                )
            if not result['success']:
                execution_error = PageExecutionError(result.get('data'), script=code)
                if not completed:
                    _attach_operation(execution_error, exec_id, outcome_unknown=True)
                raise execution_error
            rr = {'data': result['data'], **extra}
            if not wait or not completed:
                rr['operation_id'] = exec_id
            if not completed:
                rr.update({
                    'reservation_held': True, 'operation_status': snapshot['status'],
                    'delivery_state': 'delivered_no_result', 'retry_safe': False,
                    'poll_with': 'get_execute_js_result',
                })
            # Prefer the tab the executor echoed back (covers failover and remote
            # transports); fall back to the session we resolved locally.
            rtab = result.get('tabId')
            rr['executed_tab_id'] = int(rtab) if rtab is not None else exec_tab_id
            newtabs = result.get('newTabs', [])
            for tab in newtabs:
                tab.pop('ts', None)
            if newtabs: rr['newTabs'] = newtabs
            return rr

    def get_execute_js_result(self, operation_id: str, timeout=0.0, *, requester_id=None):
        """Read a retained reply, repeatably, without dispatching a browser command."""
        operation_id = str(operation_id).strip()
        if not operation_id:
            raise ValueError('operation_id must be a non-empty string')
        timeout = float(timeout)
        if not math.isfinite(timeout) or not 0 <= timeout <= 120:
            raise ValueError('timeout must be a finite number between 0 and 120 seconds')
        requester_id = self._requester(requester_id)
        if self.is_remote:
            envelope = self._remote_cmd({
                'cmd': 'get_execute_js_result', 'operationId': operation_id,
                'timeout': timeout, 'requesterId': requester_id,
            }, timeout=max(1.0, timeout) + REMOTE_TRANSPORT_MARGIN)
            response = envelope.get('r')
            if not isinstance(response, dict):
                raise RuntimeError('Bridge returned a malformed execution result')
            abandoned_receipt = (
                response.get('status') == 'unknown'
                and response.get('operation_id') == operation_id
                and response.get('operation_status') == 'outcome_unknown'
                and response.get('abandoned_reason') == 'unknown_outcome_reservation_ttl'
                and response.get('reservation_released') is True
                and response.get('reservation_held') is False
                and response.get('retry_safe') is False
                and response.get('delivery_state') == 'delivered_no_result'
                and response.get('js_return_lost') is True
            )
            if (response.get('error') and response.get('status') not in {'failed', 'in_progress'}
                    and not abandoned_receipt):
                _raise_remote_error(response)
            return response

        deadline = time.monotonic() + timeout
        activity_serial = self._activity_snapshot()
        operations = self._operation_state()
        with operations.retain_for_waiter(operation_id, requester_id):
            while True:
                self._sync_pending_operations()
                snapshot = operations.read(operation_id, requester_id)
                if snapshot['status'] in {'outcome_unknown', 'blocked_by_dialog'}:
                    operation_status = snapshot['status']
                    result = snapshot.pop('wire_result', None)
                    snapshot.update({
                        'status': 'in_progress', 'operation_status': operation_status,
                        'delivery_state': 'delivered_no_result', 'retry_safe': False,
                        'poll_with': 'get_execute_js_result',
                    })
                    if isinstance(result, dict):
                        if operation_status == 'outcome_unknown':
                            snapshot.update(_error_payload(PageExecutionError(result.get('data'))))
                            snapshot['retry_safe'] = False
                        else:
                            snapshot['data'] = result.get('data')
                            snapshot['pending_execution'] = True
                    return snapshot
                if snapshot['status'] != 'in_progress':
                    # Reading cannot acknowledge receipt across HTTP/MCP. TTL
                    # and capacity bound storage; a lost query may be repeated.
                    # The wire caches may still have a synchronous waiter.
                    result = snapshot.pop('wire_result', None)
                    if snapshot['status'] == 'lifecycle_ended':
                        snapshot.update({
                            'status': 'navigated', 'delivery_state': 'navigated',
                            'retry_safe': False, 'js_return_lost': True,
                        })
                        return snapshot
                    if snapshot['status'] == 'completed_without_result':
                        snapshot.update({'retry_safe': False, 'js_return_lost': True})
                        return snapshot
                    if snapshot['status'] == 'abandoned':
                        # Keep the expiry receipt even if a late terminal reply
                        # is now available in late_result. It is evidence about
                        # the old execution, not permission to replay it.
                        snapshot.update({
                            'status': 'unknown', 'delivery_state': 'delivered_no_result',
                            'retry_safe': False, 'js_return_lost': True,
                        })
                        if isinstance(result, dict) and not result.get('success'):
                            snapshot['operation_status'] = 'outcome_unknown'
                            snapshot.update(_error_payload(PageExecutionError(result.get('data'))))
                        return snapshot
                    if not isinstance(result, dict):
                        raise RuntimeError('Completed operation has no browser result')
                    if not result.get('success'):
                        snapshot.update(_error_payload(PageExecutionError(result.get('data'))))
                        snapshot['status'] = 'failed'
                        return snapshot
                    snapshot.update({'status': 'success', 'data': result.get('data')})
                    if result.get('tabId') is not None:
                        snapshot['executed_tab_id'] = int(result['tabId'])
                    if result.get('newTabs'):
                        snapshot['newTabs'] = [
                            {key: value for key, value in tab.items() if key != 'ts'}
                            for tab in result['newTabs']
                        ]
                    return snapshot
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    snapshot.update({
                        'delivery_state': 'delivered_no_result' if snapshot['acknowledged'] else 'sent_unconfirmed',
                        'retry_safe': False,
                        'poll_with': 'get_execute_js_result',
                    })
                    return snapshot
                activity_serial = self._wait_for_activity(activity_serial, min(0.05, remaining))

    def select_client_id(self, client_id=None, *, use_default=True, timeout=5):
        if client_id is not None:
            return str(client_id)
        current = self.default_session_id if use_default else None
        if current and ':' in str(current):
            return str(current).rsplit(':', 1)[0]
        if self.is_remote:
            response = self._remote_cmd({'cmd': 'get_clients'}, timeout=timeout).get('r')
            if not isinstance(response, list):
                raise RuntimeError('Bridge cannot list browser clients; restart the bridge')
            clients = [str(item['client_id']) for item in response]
        else:
            clients = list(self.ext_clients)
        if len(clients) > 1:
            raise AmbiguousBrowserError(clients)
        if not clients:
            raise ExtensionNotConnectedError('No browser extension is connected')
        return clients[0]

    def ext_cmd(self, cmd: dict, client_id=None, timeout=15, *, requester_id=None,
                operation_id=None):
        """Send a command straight to an extension service worker.

        The extension's socket is per-browser, so this works with ZERO open
        tabs: background.js routes any payload carrying `.cmd` to
        handleExtMessage, which runs in the SW. Plain JS still needs a tab
        (handleWsExec requires tabId) — that asymmetry is the whole point.
        """
        if not isinstance(cmd, dict):
            raise ValueError("cmd must be a JSON object")
        timeout = _positive_timeout(timeout)
        requester_id = self._requester(requester_id)
        if not isinstance(cmd.get("cmd"), str) or not cmd["cmd"].strip():
            raise ValueError(
                'ext_cmd payload requires a non-empty string cmd field; '
                'for /link use {"cmd":"ext_cmd","payload":{"cmd":"tabs",...}}'
            )
        cmd, tab_ids = _normalize_ext_command(cmd)
        deadline = time.monotonic() + timeout

        def remaining():
            return max(0.0, deadline - time.monotonic())

        client_id = self.select_client_id(client_id, timeout=max(0.001, remaining()))
        targets = ([f"{client_id}:{tab_id}" for tab_id in tab_ids]
                   if tab_ids else [f"{client_id}/extension/{cmd['cmd']}"])
        guard_targets(f"{getattr(self, 'host', '127.0.0.1')}:{getattr(self, 'port', 18765)}", targets)
        if self.is_remote:
            # Same margin as execute_js: the daemon raises its own TimeoutError
            # at `timeout` and only then writes the response, so the socket has
            # to outlive that instant for the 'did not respond within' branch
            # below to be reachable at all.
            mark_dispatched()
            operation_id = str(operation_id or uuid.uuid4())
            try:
                envelope = self._remote_cmd({"cmd": "ext_cmd", "payload": cmd,
                                             "clientId": client_id, "timeout": str(max(0.001, remaining())),
                                             "requesterId": requester_id,
                                             "operationId": operation_id},
                                            timeout=max(0.001, remaining())
                                            + REMOTE_TRANSPORT_MARGIN)
                response = envelope.get('r')
                if not isinstance(response, dict):
                    raise BridgeNoResponseError(
                        "Bridge returned a malformed ext_cmd result",
                        error_code='transport_error', delivery_state='sent_unconfirmed', retry_safe=False,
                    )
            except (TimeoutError, BridgeNoResponseError) as remote_error:
                if getattr(remote_error, 'delivery_state', None) != 'undelivered':
                    _attach_operation(remote_error, operation_id, reservation_held=None)
                raise
            if response.get('error'):
                _raise_remote_error(response)
            return response

        entry = self.ext_clients.get(client_id)
        if not entry:
            raise ExtensionNotConnectedError(
                "No browser extension is connected; verify that the extension is loaded and the bridge is running"
            )

        exec_id = str(operation_id or uuid.uuid4())
        self._sync_pending_operations()
        closing_tabs = cmd['cmd'] == 'tabs' and cmd.get('method') in {'close', 'remove'}
        # Only these two routes are known reads in the extension. Keep unknown
        # methods and batches conservative even if a current router lists tabs
        # for an unrecognized method. The normal target/scope locks still apply
        # while this call waits; only its own read timeout can release the hold.
        read_only_tabs = cmd['cmd'] == 'tabs' and (
            'method' not in cmd or cmd.get('method') == 'create_status'
        )
        close_generations: dict[str, str | None] = {}
        if closing_tabs:
            for target in targets:
                session = self.sessions.get(target)
                close_generations[target] = (
                    str(session.info.get('generation'))
                    if session is not None and session.info.get('generation') is not None
                    else None
                )
        self._operation_state().reserve(
            exec_id, targets, requester_id,
            kind='tabs_read_probe' if read_only_tabs else str(cmd['cmd']),
            cleanup=cmd['cmd'] in {'clear_dialog_policy', 'handle_dialog'} or closing_tabs,
            close_targets=closing_tabs,
            recover_dead_owner=closing_tabs,
            metadata={'client_id': client_id},
            reply_transport='ws', reply_owner=entry['ws'],
            # What is left of this caller's timeout, not the whole of it:
            # select_client_id above may already have spent part of it, and the
            # hold should outlive the wait that is actually still running.
            caller_budget=remaining(),
        )
        with self._operation_state().retain_for_waiter(exec_id, requester_id):
            try:
                capture_operation = self._capture_state().prepare(client_id, cmd, requester_id)
            except BaseException:
                self._operation_state().forget(exec_id)
                raise
            if capture_operation is not None:
                with _DRIVER_STATE_LOCK:
                    self._capture_commands[exec_id] = capture_operation
            try:
                # Send commands via 'cmd' field (not 'code') to prevent JSON.parse
                # confusion: the extension can now route by field presence rather
                # than by trying to parse a JS string as JSON.
                mark_dispatched()
                entry['ws'].send_message(json.dumps({'id': exec_id, 'cmd': cmd}))
            except Exception as e:
                self._unregister_client(entry['ws'])
                disconnected = BridgeNoResponseError(
                    f"Extension client {client_id} disconnected during dispatch ({e}); outcome is unknown.",
                    error_code="extension_not_connected", delivery_state="sent_unconfirmed", retry_safe=False,
                )
                _attach_operation(disconnected, exec_id)
                raise disconnected from e

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
                    timeout_error: Any = TimeoutError(
                        f"extension {client_id} did not respond within {timeout}s; "
                        f"the command may not have reached the browser (service worker "
                        f"asleep, or no scriptable tab to wake it)")
                    timeout_error.delivery_state = "sent_unconfirmed"
                    timeout_error.retry_safe = False
                    if read_only_tabs:
                        operations = self._operation_state()
                        operations.release_tabs_probe(exec_id, requester_id)
                        timeout_error.reservation_held = operations.read(exec_id, requester_id)['reservation_held']
                    _attach_operation(
                        timeout_error, exec_id,
                        reservation_held=getattr(timeout_error, 'reservation_held', True),
                    )
                    raise timeout_error
            if not isinstance(result, dict):
                raise RuntimeError("ext_cmd completed without a result")
            completed = self._complete_operation(exec_id, result)
            if completed and operation_id is None:
                self._operation_state().forget(exec_id)
            self.acks.pop(exec_id, None)
            # `tabs.remove` resolves before the extension's follow-up tabs_update
            # reaches the bridge. Finish the removed target now so a caller that
            # immediately polls an older uncertain operation observes lifecycle
            # termination instead of the stale outcome_unknown state. Keep the
            # generation captured before dispatch to avoid touching a reused id.
            if closing_tabs and result.get('success') is True:
                data = result.get('data')
                closed = data.get('closed', []) if isinstance(data, dict) else []
                already_gone = data.get('alreadyGone', []) if isinstance(data, dict) else []
                for native_id in [*closed, *already_gone]:
                    try:
                        target = f"{client_id}:{int(native_id)}"
                    except (TypeError, ValueError):
                        continue
                    self._finish_tab_operations(
                        target, 'tab_removed', expected_generation=close_generations.get(target),
                    )
            if not result['success']:
                execution_error = PageExecutionError(result['data'])
                if not completed:
                    _attach_operation(execution_error, exec_id, outcome_unknown=True)
                raise execution_error
            reply = {'data': result['data'], 'client_id': client_id}
            if not completed:
                snapshot = self._operation_state().read(exec_id, requester_id)
                reply.update({
                    'operation_id': exec_id, 'reservation_held': True,
                    'operation_status': snapshot['status'], 'retry_safe': False,
                    'delivery_state': 'delivered_no_result', 'poll_with': 'get_execute_js_result',
                })
            return reply

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
            failure: Any = TimeoutError(
                f"bridge HTTP request timed out after {timeout}s"
            )
            failure.delivery_state = "sent_unconfirmed"
            failure.retry_safe = False
            raise failure from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            raise BridgeNoResponseError(
                'Bridge HTTP connection failed; the command outcome is unknown',
                error_code='transport_error', delivery_state='sent_unconfirmed', retry_safe=False,
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
            raise BridgeNoResponseError(
                f"Bridge returned HTTP {resp.status_code}: {snippet}",
                error_code='transport_error', delivery_state='sent_unconfirmed', retry_safe=False,
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise BridgeNoResponseError(
                "Bridge returned malformed JSON: response could not be decoded",
                error_code='transport_error', delivery_state='sent_unconfirmed', retry_safe=False,
            ) from exc
        if not isinstance(payload, dict):
            raise BridgeNoResponseError(
                "Bridge returned malformed JSON: expected an object",
                error_code='transport_error', delivery_state='sent_unconfirmed', retry_safe=False,
            )
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
            unavailable = {
                "cause": "bridge_unreachable",
                "ok": False,
                "advice": (
                    "The bridge daemon is unreachable or could not provide a diagnosis. "
                    "BTAP normally starts it automatically; run `browsertap bridge --restart` "
                    "if recovery does not occur."
                ),
            }
            try:
                envelope = self._remote_cmd(
                    {"cmd": "diagnose"}, timeout=request_timeout
                )
                result = envelope.get('r') if isinstance(envelope, dict) else None
                if isinstance(result, dict):
                    cause = result.get('cause')
                    if isinstance(cause, str) and cause.strip() and type(result.get('ok')) is bool:
                        return result
                    if isinstance(result.get('error'), str) and result['error'].strip():
                        # /link preserves failures as {error, error_code, ...}.
                        # Missing version fields on a failed query do not prove
                        # that the daemon is running an obsolete version.
                        return {**unavailable, **result, 'cause': 'bridge_unreachable', 'ok': False}
                return {
                    **unavailable,
                    "error": "Bridge returned a malformed diagnosis",
                    "error_code": "malformed_diagnosis",
                }
            except Exception as e:
                return {**unavailable, "error": str(e)}
        self.clean_sessions()
        now = time.time()
        active = [s for s in list(self.sessions.values()) if s.is_active()]
        last_seen = self.last_ext_seen
        seconds_since_seen = now - last_seen if last_seen is not None else None
        ever = seconds_since_seen is not None
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
        elif seconds_since_seen is not None and seconds_since_seen > 90:
            cause, ok, advice = "sw_slept_or_dropped", False, (
                f"The extension last checked in {round(seconds_since_seen)}s ago. A normal open page "
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
        loaded_python = loaded_source_identity()
        current_python = current_source_identity()
        python_verdict = compare_source_identities(loaded_python, current_python)
        result = {
            "cause": cause, "ok": ok, "advice": advice,
            "bridge_version": __version__,
            "bridge_source_identity": loaded_python,
            "bridge_expected_source_identity": current_python,
            "bridge_build_verdict": python_verdict,
            "bridge_build_enforced": python_verdict != "unverifiable",
            "active_tabs": len(active),
            "ever_registered": ever,
            "last_ext_seen_seconds_ago": round(seconds_since_seen, 1) if seconds_since_seen is not None else None,
            "bridge_started_at": started_at,
            "bridge_uptime_seconds": round(max(0.0, uptime), 1) if uptime is not None else None,
            "startup_grace_seconds": BRIDGE_STARTUP_GRACE_SECONDS,
            "clients": per_client,
            "ws_origin_policy": origin_policy_report(),
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
            session = self.sessions.get(self.latest_session_id) if self.latest_session_id is not None else None
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
            _log_reference(self.default_session_id),
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
