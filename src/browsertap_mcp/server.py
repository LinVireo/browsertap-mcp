from __future__ import annotations

import base64

# Imported explicitly rather than reached as `base64.binascii`. That attribute
# exists only because `base64` imports this module for its own use, which is an
# implementation detail and not part of its documented surface -- a lazy import
# upstream would turn the two `except` clauses that use it into AttributeError
# at the moment they were supposed to catch a malformed payload.
import binascii
import errno
import functools
import hashlib
import inspect
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Literal, Optional, Sequence
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage
from mcp.shared.exceptions import McpError, UrlElicitationRequiredError
from mcp.types import METHOD_NOT_FOUND, CallToolResult, TextContent
from pydantic import BaseModel, Field, StrictBool

# Only explicitly serialized process-wide tools take this lock. Page tools use
# request-local defaults and per-tab command locks instead.
# It must be a plain Lock, never an RLock: the async path in _threaded_tool
# acquires it on an anyio worker thread and releases it on the event-loop
# thread, and RLock refuses a release from a thread that does not own it
# ("cannot release un-acquired lock"). Ownership tracking would turn every
# async tool call into a RuntimeError.
_TOOL_LOCK = threading.Lock()
_DRIVER_LOCK = threading.Lock()

ROOT = Path(__file__).resolve().parent

from . import (
    __version__,  # noqa: E402
    bookmark_backup,  # noqa: E402
    native_dialog,  # noqa: E402
    physical_input,  # noqa: E402
    simphtml,  # noqa: E402
)
from . import bridge as bridge_module  # noqa: E402
from .browser_bridge import (  # noqa: E402
    AmbiguousBrowserError,
    BridgeNoResponseError,
    BrowserBridge,
    ExtensionNotConnectedError,
    PageExecutionError,
    is_scriptable_url,
    state_paths_report,
    tcp_port_open,
)
from .cdp_policy import validate_raw_cdp_batch, validate_raw_cdp_method  # noqa: E402
from .command_scope import command_scope  # noqa: E402
from .extension_build import (  # noqa: E402
    ExtensionStampError,
    compute_extension_stamp,
    read_extension_stamp,
)
from .page_frames import frame_locator_payload  # noqa: E402
from .page_input import (  # noqa: E402
    ChallengeAttemptTracker,
    InputValidationError,
    click_commands,
    drag_commands,
    locator_query_script,
    normalize_locator,
    press_commands,
    resolve_selector_script,
    structured_locator_script,
    type_commands,
    type_target_script,
)
from .paths import bridge_child_environment, configured_bridge_port, state_dir  # noqa: E402
from .runtime_identity import (  # noqa: E402
    compare_source_identities,
    current_source_identity,
    loaded_source_identity,
)
from .tool_annotations import annotations_for_tool  # noqa: E402

logger = logging.getLogger(__name__)

_RESULT_SERIALIZER_SOURCE = (ROOT / "chrome_extension" / "result_serialization.js").read_text(
    encoding="utf-8"
)
_GUARDED_EVAL_SOURCE = (ROOT / "chrome_extension" / "guarded_eval.js").read_text(
    encoding="utf-8"
)
_DIALOG_SCOPE_SOURCE = (ROOT / "chrome_extension" / "disable_dialogs.js").read_text(
    encoding="utf-8"
)


# Keep timeout validation at the public boundary.  A comparison such as
# ``timeout <= 0`` is not enough: ``NaN`` passes it, ``inf`` turns a deadline
# into an effectively unbounded wait, and a numeric string currently behaves
# differently depending on which tool received it.  Converting once here also
# means the hot path does not repeatedly coerce the same value.
def _positive_timeout(value: Any, *, name: str = "timeout") -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{name} must be a finite number greater than zero"
        ) from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")
    return normalized


def _validate_safe_path(
    save_path: str,
    *,
    allowed_base: Optional[Path] = None,
    description: str = "save_path",
) -> Path:
    """Validate that save_path stays within an allowed directory.

    Prevents path traversal attacks by ensuring user-supplied paths cannot
    write to arbitrary filesystem locations.

    Args:
        save_path: User-provided path (should be relative)
        allowed_base: Base directory to restrict writes to. Defaults to
                     ~/Downloads/browsertap if not specified.
        description: Parameter name for error messages

    Returns:
        Resolved absolute path within allowed_base

    Raises:
        ValueError: If path is empty, absolute, or traverses outside allowed_base

    Examples:
        >>> _validate_safe_path("report.pdf")
        PosixPath('/home/user/Downloads/browsertap/report.pdf')

        >>> _validate_safe_path("/etc/passwd")
        ValueError: save_path must be a relative path within ...

        >>> _validate_safe_path("../../etc/passwd")
        ValueError: ... path traversal detected
    """
    if not isinstance(save_path, (str, Path)) or not str(save_path).strip():
        raise ValueError(f"{description} must not be empty")

    # Default to a safe location in user's Downloads
    if allowed_base is None:
        allowed_base = Path.home() / "Downloads" / "browsertap"
    allowed_base = allowed_base.resolve()

    path = Path(save_path).expanduser()

    # Callers can supply either path syntax regardless of the host OS.
    # Windows anchors also include drive-relative and root-relative paths.
    if path.is_absolute() or PureWindowsPath(save_path).anchor:
        raise ValueError(
            f"{description} must be a relative path within {allowed_base}"
        )

    # Resolve relative to allowed_base and check traversal
    final_path = (allowed_base / path).resolve()
    if not final_path.is_relative_to(allowed_base):
        raise ValueError(
            f"{description} must stay within {allowed_base}, "
            f"attempted path traversal detected"
        )

    return final_path


def _atomic_write_bytes(path: Path, data: bytes, *, max_size: Optional[int] = None) -> None:
    """Atomically write binary data to a file, with size limits and cleanup on failure.

    Uses the temporary file + fsync + atomic rename pattern to ensure the target
    file is never left in a partially-written state. If the write fails, the
    temporary file is cleaned up and the original target (if any) remains unchanged.

    Args:
        path: Target file path (must already be validated with _validate_safe_path)
        data: Binary data to write
        max_size: Optional maximum file size in bytes. If data exceeds this,
                 ValueError is raised before any write occurs.

    Raises:
        ValueError: If data exceeds max_size
        RuntimeError: On write failures (disk full, permissions, etc.)

    Examples:
        >>> path = _validate_safe_path("screenshot.png")
        >>> _atomic_write_bytes(path, png_bytes, max_size=50*1024*1024)
    """
    if max_size is not None and len(data) > max_size:
        raise ValueError(
            f"File size {len(data)} bytes exceeds maximum {max_size} bytes"
        )

    # Ensure parent directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Create temporary file in same directory (ensures same filesystem for atomic rename)
    fd: Optional[int]
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".tmp_",
        suffix=path.suffix
    )

    try:
        # Write data in a loop to handle partial writes
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError(f"write returned {written}")
            remaining = remaining[written:]

        # Flush to disk before rename
        os.fsync(fd)
        closing_fd, fd = fd, None
        os.close(closing_fd)

        # Atomic rename
        os.replace(tmp_path, path)

    except OSError as exc:
        # Provide user-friendly error messages for common issues
        if exc.errno == errno.ENOSPC:
            raise RuntimeError(f"Failed to save {path.name}: disk full") from exc
        elif exc.errno == errno.EACCES:
            raise RuntimeError(
                f"Failed to save {path.name}: permission denied for {path.parent}"
            ) from exc
        else:
            raise RuntimeError(f"Failed to save {path.name}: {exc}") from exc
    finally:
        # A closed descriptor can already belong to another request.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# --- Stdio logging -----------------------------------------------------------
def configure_stdio_logging() -> None:
    """Route BTAP runtime diagnostics away from the MCP stdout transport."""
    package_logger = logging.getLogger("browsertap_mcp")
    if any(getattr(handler, "_btap_stdio_handler", False) for handler in package_logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler._btap_stdio_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.WARNING)
    package_logger.propagate = False

mcp = FastMCP(
    name="browsertap",
    instructions=(
        "Browser automation for the Chrome/Edge the user is already running. A Chrome extension holds the "
        "CDP connection from inside the browser, so the profile, logins and cookies are the user's own; no "
        "browser is launched for automation. Three capabilities follow from that: a selected tab is not a "
        "foreground tab, so page work runs in the named tab while the user keeps the screen; page_click, "
        "page_type, page_press and page_drag dispatch trusted input events inside that tab without moving "
        "the user's cursor or raising a window; the chrome.* surface is in "
        "scope, including extension management, bookmarks, scoped site-permission leases, and downloads "
        "through Chrome's own manager. "
        "Input goes through the page_* tools; there is no OS-level mouse or keyboard surface. When a "
        "page_* call fails, re-read the page with scan_page and fix the target - do not look for a "
        "screen-coordinate fallback, because none exists. "
        "Out of scope: headless, CI, containers, Firefox and WebKit. Report the mismatch rather than "
        "working around it. "
        "Bot checks are not solved here. A Cloudflare Turnstile verdict is decided before the widget "
        "renders, from IP reputation and browser fingerprint; a trusted profile passes without interaction, "
        "and where it does not, clicking does not change the verdict. Click it once in the same tab if "
        "present; on status='challenge_stalled', stop and return the tab to the user. Do not launch a "
        "second browser and do not route a challenge through a solver or token service. "
        "Supports page scanning, JS execution, CDP commands, screenshots, cookies, and page-level input. "
        "Page screenshots include MCP image content; a model that cannot process images must not claim to "
        "have seen the pixels and should use scan_page, execute_js, a page-specific API, or OCR instead. "
        "Several browsers can be connected at once; list_tabs shows a browser field per tab. "
        "Before acting, pick the target explicitly with switch_tab(browser='chrome'|'edge') or a full "
        "session_id (a 'client:tabId' string - pass it verbatim, never split it). "
        "Tabs that existed before the task are user-owned: do not close or navigate them by default. "
        "For mutating work use open_new_tab, keep its owner_id, and close that owned tab in cleanup. "
        "Read ok, error_code, delivery_state and retry_safe before recovery. Retrieve an operation_id "
        "from its original MCP session without redispatching. After a delivery failure, retry only with "
        "delivery_state='undelivered' and retry_safe=true, after fixing the target or connection. "
        "A page inspection or missing receipt "
        "does not prove a side effect never happened. Verify any switched_session before continuing. "
        "For exported results, first JSON-decode result_file only when result_file_encoding=json; "
        "otherwise use the path directly. Read that file as UTF-8 JSON and verify result_bytes/result_sha256, "
        "or parse result_json once if file writing failed. The corresponding result_file_scope or "
        "result_json_scope is js-value, envelope, or mcp-call-result (the complete normally adapted MCP "
        "result). JS descriptors are in data or legacy.late_result; envelope/MCP descriptors are at "
        "the decision-header root. Preserve the original ok/isError/retry verdicts. Error fields "
        "marked message_encoding=json, code_encoding=json or error_code_encoding=json, and "
        "result_file_error.message_json, must also be parsed once."
    ),
)

_driver: Optional[BrowserBridge] = None
_DRIVER_PORT: Optional[int] = None
_DRIVER_HOST: Optional[str] = None


def _get_driver_port() -> int:
    """Parse BROWSERTAP_BRIDGE_PORT lazily so invalid values fail inside
    get_driver() where cmd_doctor() can catch them, not at import time."""
    global _DRIVER_PORT
    if _DRIVER_PORT is not None:
        return _DRIVER_PORT
    _DRIVER_PORT = configured_bridge_port()
    return _DRIVER_PORT


def _get_driver_host() -> str:
    """Parse BROWSERTAP_BRIDGE_HOST lazily for consistency with port parsing."""
    global _DRIVER_HOST
    if _DRIVER_HOST is not None:
        return _DRIVER_HOST
    _DRIVER_HOST = os.environ.get("BROWSERTAP_BRIDGE_HOST", "127.0.0.1")
    return _DRIVER_HOST


# The local operator profile intentionally defaults to lab. Safe remains an
# explicit, process-wide override for sessions where every foreground action
# and permission grant must be confirmed separately.
_AUTOMATION_MODE_OVERRIDE: Optional[str] = None
_AUTOMATION_MODES = frozenset({"lab", "safe"})
_DEFAULT_AUTO_BEFOREUNLOAD_HOSTS = (
    "shell.", "ttyd", "code-server", "jupyter", "vscode-web"
)
_LAB_APPROVAL_OWNERS: dict[str, Any] = {}
_LAB_PHYSICAL_APPROVALS: set[str] = set()
_LAB_SITE_PERMISSION_APPROVALS: set[str] = set()
_XTERM_SUBMIT_DELAY_MS = 75


# --- MCP result envelope ----------------------------------------------------
# Python callers keep receiving the operation-specific dictionaries below.
# The registered FastMCP functions pass through this adapter, which gives MCP
# clients one stable shape without forcing a breaking rewrite of every tool.
_RESULT_ENVELOPE_VERSION = 1
_RESULT_COMPAT_TEXT_LIMIT = 256
_RESULT_RESERVED_KEYS = frozenset({
    "ok", "data", "error", "error_code", "retryable", "target",
    "diagnostics", "legacy", "result_contract", "version",
})
_RESULT_FAILURE_STATUSES = frozenset({
    "error", "failed", "failure", "timeout", "busy", "requires_user_action",
    "activation_failed", "input_activity_detected", "coordinates_off_screen",
    "obscured", "outside_viewport", "not_found", "cancelled", "rejected",
    "no_response", "navigation_timeout", "navigation_failed", "dialog_handle_failed",
    "blocked_by_beforeunload", "blocked_by_dialog", "challenge_stalled", "unknown",
    "unsupported", "unsupported_frame_transform", "partial", "bridge_unreachable", "stale_bridge", "stale_extension",
    "stale_package",
    "not_interactable", "focus_failed", "ambiguous", "invalid_selector",
    "cross_origin_frame", "closed_shadow_root",
})
_SETUP_DIAGNOSTIC_STATUSES = frozenset({
    "healthy", "starting", "stale_bridge", "stale_extension", "stale_package",
    "extension_unavailable",
})
_RESULT_RETRYABLE_CODES = frozenset({
    "bridge_unreachable", "extension_not_connected", "session_not_connected",
    "session_disconnected", "transport_error",
})


def _result_target(payload: Any) -> Optional[dict[str, Any]]:
    """Extract target identity without inventing a target for tab-less calls."""
    if not isinstance(payload, dict):
        return None
    session_id = (
        payload.get("session_id")
        or payload.get("executed_session_id")
        or payload.get("active_session_id")
        or payload.get("activated_session_id")
    )
    tab_id = payload.get("executed_tab_id")
    if tab_id is None:
        tab_id = payload.get("tab_id")
    client_id = payload.get("client_id")
    generation = payload.get("generation")
    if session_id is not None:
        session_text = str(session_id)
        if client_id is None and ":" in session_text:
            client_id = session_text.rsplit(":", 1)[0]
        if tab_id is None and ":" in session_text:
            maybe_tab = session_text.rsplit(":", 1)[-1]
            if maybe_tab.isdigit():
                tab_id = int(maybe_tab)
    if session_id is None and tab_id is None and client_id is None and generation is None:
        return None
    target: dict[str, Any] = {}
    if session_id is not None:
        target["session_id"] = str(session_id)
    if client_id is not None:
        target["client_id"] = str(client_id)
    if tab_id is not None:
        target["tab_id"] = tab_id
    if generation is not None:
        target["generation"] = str(generation)
    for key in ("browser", "url"):
        if payload.get(key) is not None:
            target[key] = payload[key]
    return target or None


def _result_diagnostics(payload: Any, *, tool: str) -> dict[str, Any]:
    """Keep actionable routing and delivery facts out of the operation data."""
    diagnostics: dict[str, Any] = {"tool": tool}
    if not isinstance(payload, dict):
        return diagnostics
    for key in (
        "delivery_state", "switched_session", "switched_from", "closed",
        "retry_safe", "dispatched", "may_have_executed", "operation_id",
        # An id is only actionable next to how to use it and whether the tab is
        # still held. The exception path already reports all three; a failure
        # returned as a dict used to keep the id and drop the other two.
        "poll_with", "reservation_held", "operation_status", "may_have_created", "on_screen",
        # Whether a timed-out page script is still pinning its tab. Decides the
        # caller's next move on that tab, so it belongs next to retry_safe.
        "zombie", "zombie_detail",
        "input_quiet", "input_reachability", "screen_bounds", "ownership", "desktop",
        "owner_id", "generation", "extension_build_verdict", "status", "code",
        "hint", "reason", "directory_applied", "requested_directory", "render_state",
        "content_ready", "render", "recheck_required", "recheck_action",
    ):
        if key in payload:
            diagnostics[key] = payload[key]
    if isinstance(payload.get("newTabs"), list):
        diagnostics["new_tabs_count"] = len(payload["newTabs"])
    return diagnostics


class SessionTargetNotFoundError(RuntimeError):
    """An explicitly named tab disappeared without authorising failover."""

    error_code = "session_not_connected"
    retry_safe = False

    def __init__(self, session_id: str, sessions: Optional[list[dict[str, Any]]] = None) -> None:
        sid = str(session_id)
        candidates: list[dict[str, Any]] = []
        if isinstance(sessions, list):
            native_id = sid.rsplit(":", 1)[-1]
            for item in sessions:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("id", ""))
                if candidate_id and candidate_id != sid and candidate_id.rsplit(":", 1)[-1] == native_id:
                    candidates.append({
                        key: item[key]
                        for key in ("id", "url", "title", "generation")
                        if item.get(key) is not None
                    })
        self.diagnostics = {
            "stale_session_id": sid,
            "replacement_candidates": candidates,
            "next_action": "list_tabs_then_retry",
        }
        suffix = ""
        if candidates:
            suffix = " Possible replacement session(s): " + ", ".join(
                str(item.get("id")) for item in candidates[:8]
            ) + "."
        super().__init__(
            f"Session {sid} not found: the explicitly requested tab session is stale; "
            "BTAP could not verify a live session for the same tab. "
            "Run list_tabs (or list_all_tabs), verify the intended target, "
            "then retry with its live session_id."
            + suffix
        )


def _session_target_not_found(
    session_id: str,
    sessions: Optional[list[dict[str, Any]]] = None,
) -> SessionTargetNotFoundError:
    """Build one actionable stale-target error for every server-side resolver."""
    if sessions is None:
        try:
            sessions = active_sessions(fresh=True)
        except Exception:
            sessions = []
    return SessionTargetNotFoundError(str(session_id), sessions)


def _resolve_session_target(driver: BrowserBridge, session_id: str) -> Optional[dict[str, Any]]:
    """Ask the bridge for exact or evidence-backed replacement resolution."""
    resolver = getattr(driver, "resolve_session_target", None)
    if not callable(resolver):
        return None
    result = resolver(str(session_id))
    if not isinstance(result, dict) or not result.get("session_id"):
        return None
    return result


def _result_retryable(*sources: dict[str, Any], default: bool = False) -> bool:
    """Delivery facts and explicit refusals override optimistic retry hints."""
    for source in sources:
        if any(source.get(key) is False for key in ("retry_safe", "retryable")):
            return False
        if any(source.get(key) is True for key in ("dispatched", "may_have_executed", "may_have_created")):
            return False
        delivery_state = source.get("delivery_state")
        if delivery_state is not None and delivery_state != "undelivered":
            return False
    if any(source.get(key) is True for source in sources for key in ("retry_safe", "retryable")):
        return True
    return default


def _legacy_failure(
    payload: Any, *, tool: Optional[str] = None,
) -> Optional[tuple[str, str, bool]]:
    """Classify explicit operation failures that did not raise an exception."""
    if not isinstance(payload, dict):
        return None
    diagnostics = payload.get("diagnostics")
    retryable = _result_retryable(
        payload, diagnostics if isinstance(diagnostics, dict) else {},
    )
    if payload.get("ok") is False:
        code = str(payload.get("code") or payload.get("error_code") or "operation_failed")
        message = str(payload.get("error") or payload.get("message") or f"{code} failed")
        return code, message, retryable
    status = str(payload.get("status") or "").strip().lower()
    # Setup diagnostics report the health of their dependencies in `status`.
    # A stale component is actionable data from a successful diagnostic call,
    # not a failure to produce the report. Keep this exception tool-local so
    # ordinary operations with the same status remain failures.
    if tool == "get_setup_status" and status in _SETUP_DIAGNOSTIC_STATUSES:
        return None
    if status in _RESULT_FAILURE_STATUSES:
        code = str(payload.get("code") or payload.get("error_code") or status)
        message = str(payload.get("error") or payload.get("message") or status)
        return code, message, retryable
    if payload.get("error") and status not in {"ok", "success", "completed", "stopped"}:
        code = str(payload.get("code") or payload.get("error_code") or "operation_failed")
        return code, str(payload["error"]), retryable
    return None


def _exception_result_metadata(exc: Exception) -> tuple[str, str, bool, dict[str, Any]]:
    code = str(getattr(exc, "error_code", "") or "")
    message = str(exc)
    if not code:
        if isinstance(exc, TimeoutError):
            code = "timeout"
        elif isinstance(exc, PermissionError):
            code = "permission_denied"
        elif isinstance(exc, ValueError):
            code = "invalid_request"
        elif isinstance(exc, FileNotFoundError):
            code = "not_found"
        elif isinstance(exc, (ConnectionError, OSError)):
            code = "transport_error"
        else:
            code = "internal_error"
    # A few server-side resolvers predate the bridge's typed session errors and
    # still raise RuntimeError("Session ... not found"). Normalize that message
    # so an explicitly dead target has the same stable code in local and remote
    # execution paths.
    lower_message = message.lower()
    if (
        code == "internal_error"
        and lower_message.startswith("session ")
        and lower_message.endswith(" not found")
    ):
        code = "session_not_connected"
    extra_diagnostics = getattr(exc, "diagnostics", None)
    diagnostics = dict(extra_diagnostics) if isinstance(extra_diagnostics, dict) else {}
    attributes: dict[str, Any] = {}
    for key in (
        "delivery_state", "retry_safe", "retryable", "operation_id",
        "reservation_held", "poll_with", "operation_status", "dispatched",
        "may_have_executed", "may_have_created",
    ):
        value = getattr(exc, key, None)
        if value is not None:
            attributes[key] = value
    retryable = _result_retryable(
        attributes, diagnostics, default=code in _RESULT_RETRYABLE_CODES,
    )
    diagnostics.update(attributes)
    for key in ("retry_safe", "retryable"):
        if key in diagnostics:
            diagnostics[key] = retryable
    return code, message, retryable, diagnostics


def _result_envelope(
    tool: str,
    payload: Any = None,
    *,
    exc: Optional[Exception] = None,
    target: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the v1 envelope and retain small scalar compatibility fields."""
    payload_target = _result_target(payload)
    resolved_target: Optional[dict[str, Any]]
    if target is not None:
        resolved_target = dict(payload_target or {})
        resolved_target.update(target)
    else:
        resolved_target = payload_target
    if exc is not None:
        code, message, retryable, error_diagnostics = _exception_result_metadata(exc)
        envelope: dict[str, Any] = {
            "ok": False,
            "result_contract": "btap.result.v1",
            "version": _RESULT_ENVELOPE_VERSION,
            "error": {"code": code, "message": message, "retryable": retryable},
            "error_code": code,
            "retryable": retryable,
            "target": resolved_target,
            "diagnostics": {"tool": tool, **error_diagnostics},
        }
        # These were part of the transport-specific failure shape before the
        # common envelope. Keep them readable for callers that have not yet
        # migrated while the canonical copy remains in diagnostics/error.
        for key in (
            "delivery_state", "retry_safe", "operation_id", "reservation_held",
            "poll_with", "operation_status",
        ):
            if key in error_diagnostics:
                envelope[key] = error_diagnostics[key]
        return envelope

    failure = _legacy_failure(payload, tool=tool)
    if failure is not None:
        code, message, retryable = failure
        diagnostics = _result_diagnostics(payload, tool=tool)
        if "retry_safe" in diagnostics:
            diagnostics["retry_safe"] = retryable
        envelope = {
            "ok": False,
            "result_contract": "btap.result.v1",
            "version": _RESULT_ENVELOPE_VERSION,
            "error": {"code": code, "message": message, "retryable": retryable},
            "error_code": code,
            "retryable": retryable,
            "target": resolved_target,
            "diagnostics": diagnostics,
            "legacy": payload,
        }
    else:
        envelope = {
            "ok": True,
            "result_contract": "btap.result.v1",
            "version": _RESULT_ENVELOPE_VERSION,
            "data": payload,
            "error": None,
            "error_code": None,
            "retryable": False,
            "target": resolved_target,
            "diagnostics": _result_diagnostics(payload, tool=tool),
        }

    # Collections and long bodies belong only in data/legacy. Copying HTML,
    # script results or tab lists here doubles the agent's context consumption.
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "retry_safe" and failure is not None:
                value = envelope["retryable"]
            compact_scalar = (
                value is None
                or isinstance(value, (bool, int, float))
                or isinstance(value, str) and len(value) <= _RESULT_COMPAT_TEXT_LIMIT
            )
            if compact_scalar and key not in _RESULT_RESERVED_KEYS and key not in envelope:
                envelope[key] = value
    return envelope


def _call_target(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Extract only caller-supplied target identity for exception envelopes."""
    try:
        bound = inspect.signature(fn, eval_str=False).bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        bound = None
    values = bound.arguments if bound is not None else kwargs
    target_values = {
        key: values[key]
        for key in ("session_id", "tab_id", "client_id", "generation", "browser", "url")
        if values.get(key) is not None
    }
    return _result_target(target_values)


_RESULT_PAYLOAD_FIELDS = frozenset({
    "result_externalized", "result_file", "result_file_encoding", "result_bytes", "result_sha256",
    "result_format", "result_file_scope", "result_inline_limit_bytes",
    "result_json", "result_json_scope", "result_file_error", "result_encoding_reason",
    "result_content_externalized", "result_meta_externalized",
})


def _export_json_result(payload: bytes, *, scope: str) -> dict[str, Any]:
    """Export JSON without losing an executed result when its file cannot be written."""
    try:
        path, size, digest = _write_execute_js_payload(payload)
        filename = str(path)
        metadata: dict[str, Any] = {
            "result_externalized": True,
            "result_file": filename,
            "result_bytes": size,
            "result_sha256": digest,
            "result_format": "utf-8-json",
            "result_file_scope": scope,
        }
        # Windows filenames can contain lone UTF-16 code units too. Keep the
        # exact path recoverable without reintroducing them into MCP metadata.
        if _contains_utf16_surrogate(filename):
            metadata["result_file"] = json.dumps(filename, ensure_ascii=True)
            metadata["result_file_encoding"] = "json"
        return metadata
    except OSError as write_error:
        # The operation already completed. A disk failure must not turn its
        # receipt into a new exception or imply that replay became safe.
        return {
            "result_externalized": False,
            "result_json": json.dumps(json.loads(payload), ensure_ascii=True),
            "result_json_scope": scope,
            "result_format": "utf-8-json",
            "result_encoding_reason": "result-file-write-failed",
            "result_file_error": {"message_json": json.dumps(str(write_error), ensure_ascii=True)},
        }


def _contains_utf16_surrogate(value: Any) -> bool:
    """Find non-scalar strings without confusing literal JSON escapes or cycles."""
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if re.search(r"[\ud800-\udfff]", current):
                return True
        elif isinstance(current, (dict, list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            if isinstance(current, dict):
                pending.extend(current.keys())
                pending.extend(current.values())
            else:
                pending.extend(current)
    return False


def _unicode_tool_result(
    envelope: dict[str, Any], *, native: Optional[CallToolResult] = None,
) -> Optional[CallToolResult]:
    """Keep the original JSON recoverable when MCP rejects UTF-16 code units."""
    original: dict[str, Any] = envelope
    scope = "envelope"
    if native is not None:
        original = {
            **native.model_dump(mode="python", by_alias=True),
            "structuredContent": envelope,
            "isError": bool(native.isError) or not envelope["ok"],
        }
        scope = "mcp-call-result"
    if not _contains_utf16_surrogate(original):
        return None

    metadata = _export_json_result(_serialize_execute_js_value(original), scope=scope)
    metadata["result_encoding_reason"] = "utf-16-surrogate"

    def safe_fields(mapping: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item for key, item in mapping.items()
            if not _contains_utf16_surrogate(key) and not _contains_utf16_surrogate(item)
        }

    # Keep usable routing and delivery facts in the decision header. Data and
    # legacy explicitly point to the complete original, rather than presenting
    # altered strings or colliding escaped dictionary keys as the caller's data.
    summary = safe_fields({
        key: item for key, item in envelope.items()
        if key not in _RESULT_PAYLOAD_FIELDS
    })
    summary.update(metadata)
    summary["target"] = safe_fields(envelope.get("target") or {}) or None
    summary["diagnostics"] = safe_fields(envelope["diagnostics"])
    for key in ("data", "legacy"):
        if key in envelope:
            summary[key] = (
                {"result_json_ref": "#/result_json"}
                if "result_json" in metadata else dict(metadata)
            )
    error = envelope.get("error")
    summary["error"] = None if error is None else dict(error)
    if error is not None:
        for key in ("code", "message"):
            if _contains_utf16_surrogate(error[key]):
                summary["error"][key] = json.dumps(error[key], ensure_ascii=True)
                summary["error"][key + "_encoding"] = "json"
        summary["error_code"] = summary["error"]["code"]
        if "code_encoding" in summary["error"]:
            summary["error_code_encoding"] = "json"

    content = []
    meta = None
    if native is not None:
        # Safe images/resources and metadata remain directly usable. Every
        # original block and metadata entry is also present in the archive.
        content = [
            item for item in native.content
            if not _contains_utf16_surrogate(item.model_dump(mode="python", by_alias=True))
        ]
        meta = None if native.meta is None else safe_fields(native.meta)
        if len(content) != len(native.content):
            summary["result_content_externalized"] = True
        if _contains_utf16_surrogate(native.meta):
            summary["result_meta_externalized"] = True
    return CallToolResult(
        _meta=meta,
        content=[TextContent(type="text", text=json.dumps(summary, ensure_ascii=False)), *content],
        structuredContent=summary,
        isError=bool(native and native.isError) or not envelope["ok"],
    )


def _adapt_tool_result(
    tool: str,
    value: Any = None,
    *,
    target: Optional[dict[str, Any]] = None,
    exc: Optional[Exception] = None,
) -> Any:
    if isinstance(value, CallToolResult):
        legacy = value.structuredContent
        envelope = _result_envelope(tool, legacy, target=target)
        unicode_result = _unicode_tool_result(envelope, native=value)
        if unicode_result is not None:
            return unicode_result
        return CallToolResult(
            _meta=value.meta,
            content=value.content,
            structuredContent=envelope,
            isError=bool(value.isError) or not envelope["ok"],
        )
    envelope = _result_envelope(tool, value, exc=exc, target=target)
    unicode_result = _unicode_tool_result(envelope)
    if unicode_result is not None:
        return unicode_result
    if envelope["ok"]:
        return envelope
    # FastMCP treats ordinary dictionaries as transport successes, regardless
    # of their fields. An explicit result carries the failure to MCP clients.
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))],
        structuredContent=envelope,
        isError=True,
    )


# --- Tab ownership: which tabs this process opened ---------------------------
class _TabOwnershipRegistry:
    """Process-local capabilities for tabs created by this MCP server.

    The shared bridge sees every browser tab, but each editor conversation gets
    its own MCP process and therefore its own registry.  The random owner_id is
    an additional capability check inside a process; the lifecycle generation
    prevents a reused native tab id from inheriting an earlier ownership claim.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, str]] = {}
        # Lifetime totals, never decremented. `outstanding()` alone cannot tell
        # "nothing was leaked" from "nothing was ever opened", and those two
        # readings are not interchangeable for anybody auditing a run.
        self._registered = 0
        self._released = 0

    @staticmethod
    def _new_owner_id() -> str:
        return f"btap_owner_{secrets.token_urlsafe(18)}"

    def register(
        self,
        session_id: str,
        generation: str,
        *,
        owner_id: Optional[str] = None,
    ) -> dict[str, str]:
        sid = str(session_id)
        gen = str(generation)
        capability = str(owner_id).strip() if owner_id is not None else self._new_owner_id()
        if not capability:
            raise ValueError("owner_id must not be empty")
        record = {
            "session_id": sid,
            "generation": gen,
            "owner_id": capability,
            "opener": "agent",
        }
        with self._lock:
            existing = self._records.get(sid)
            if existing is not None:
                # Recovery may be submitted more than once after a response
                # was lost.  The same lifecycle and capability are the same
                # ownership claim; return it without inflating the counters.
                # A different generation or owner must never overwrite the
                # capability that can still close the live tab.
                if (
                    existing["generation"] == gen
                    and existing["owner_id"] == capability
                ):
                    return dict(existing)
                raise ValueError(
                    f"tab ownership conflict for {sid}: "
                    "generation or owner_id does not match the existing claim"
                )
            self._records[sid] = record
            self._registered += 1
        return dict(record)

    def validate(
        self,
        session_ids: list[str],
        *,
        owner_id: Optional[str],
        live_sessions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, str]:
        capability = str(owner_id).strip() if owner_id is not None else ""
        if not capability:
            raise PermissionError(
                "close_tabs refused: owner_id is required when only_if_agent_owned=true; "
                "use the capability returned by open_new_tab"
            )
        expected: dict[str, str] = {}
        live_by_id = {
            str(item.get("id")): item for item in (live_sessions or [])
        }
        with self._lock:
            for sid in session_ids:
                record = self._records.get(sid)
                if record is None:
                    raise PermissionError(
                        f"close_tabs refused: {sid} is not owned by this MCP task"
                    )
                if record["owner_id"] != capability:
                    raise PermissionError(
                        f"close_tabs refused: owner_id does not match {sid}"
                    )
                # A positively registered live session can reject a reused id
                # early. Absence is not proof of closure: PDF, restricted URLs,
                # and a reconnecting content script may have no session while
                # the native Chrome tab still exists.
                live = live_by_id.get(sid)
                if live is not None and live.get("generation") is not None:
                    if str(live["generation"]) != record["generation"]:
                        raise PermissionError(
                            f"close_tabs refused: {sid} lifecycle generation changed"
                        )
                expected[str(_split_session_target(sid)[1])] = record["generation"]
        return expected

    def rebind(self, old_session_id: str, new_session_id: str) -> Optional[dict[str, str]]:
        """Move an ownership claim after bridge-confirmed tab replacement."""
        old_sid = str(old_session_id)
        new_sid = str(new_session_id)
        if old_sid == new_sid:
            return None
        with self._lock:
            record = self._records.get(old_sid)
            if record is None:
                return None
            existing = self._records.get(new_sid)
            if existing is not None and existing != record:
                raise PermissionError(
                    f"tab ownership conflict while rebinding {old_sid} to {new_sid}"
                )
            moved = dict(record)
            moved["session_id"] = new_sid
            self._records.pop(old_sid, None)
            self._records[new_sid] = moved
            return dict(moved)

    def release(self, session_ids: list[str], *, owner_id: str) -> None:
        with self._lock:
            for sid in session_ids:
                record = self._records.get(sid)
                if record and record["owner_id"] == owner_id:
                    self._records.pop(sid, None)
                    self._released += 1

    def outstanding(self) -> list[dict[str, Any]]:
        """Tabs this process opened and has not closed, without the capability.

        `release` runs only after a close actually succeeded, so whatever is
        left here is precisely "opened by this task, never cleaned up" -- the
        one claim about a browser that can be made without inspecting anybody
        else's tabs.  The owner_id is deliberately withheld: this is a read for
        reporting, and a capability that is never handed out cannot leak into a
        report or a published artifact.
        """
        with self._lock:
            records = [dict(record) for record in self._records.values()]
        out: list[dict[str, Any]] = []
        for record in records:
            try:
                tab_id: Optional[int] = _split_session_target(record["session_id"])[1]
            except ValueError:
                # A reporting path must not raise on a malformed key; naming the
                # session is already enough for the reader to act on.
                tab_id = None
            out.append(
                {
                    "session_id": record["session_id"],
                    "tab_id": tab_id,
                    "generation": record["generation"],
                    "opener": record.get("opener", "agent"),
                }
            )
        out.sort(key=lambda item: str(item["session_id"]))
        return out

    def counters(self) -> dict[str, int]:
        """How much ownership this process has handled, for reports.

        `outstanding()` returning nothing is the same shape as never having
        opened a tab at all, and a check that cannot distinguish those two says
        nothing when it measured nothing -- the failure mode `enforced` exists
        for on the quiet-input gate and `files_scanned` on the lint gate. These
        counters are what a reader compares against.
        """
        with self._lock:
            return {
                "registered": self._registered,
                "released": self._released,
                "outstanding": len(self._records),
            }


_TAB_OWNERSHIP = _TabOwnershipRegistry()


# --- Automation profile and approval policy ----------------------------------
def _env_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _automation_mode() -> str:
    mode = (_AUTOMATION_MODE_OVERRIDE or os.environ.get("BROWSERTAP_MODE", "lab"))
    mode = str(mode).strip().lower()
    return mode if mode in _AUTOMATION_MODES else "lab"


def _auto_beforeunload_hosts() -> list[str]:
    raw = os.environ.get("BROWSERTAP_AUTO_BEFOREUNLOAD_HOSTS")
    values = raw.split(",") if raw is not None else _DEFAULT_AUTO_BEFOREUNLOAD_HOSTS
    return [str(value).strip().lower() for value in values if str(value).strip()]


def _automation_profile() -> dict[str, Any]:
    mode = _automation_mode()
    no_elicit = mode == "lab" and _env_enabled(
        "BROWSERTAP_LAB_NO_ELICIT", default=True
    )
    return {
        "mode": mode,
        "no_elicit": no_elicit,
        "raw_cdp_policy": "allow_unsafe" if _raw_cdp_unsafe_enabled() else "guarded",
        "auto_beforeunload_hosts": _auto_beforeunload_hosts(),
        "physical_approval": (
            "every_action" if mode == "safe"
            else "not_required" if no_elicit
            else "once_per_session"
        ),
        "site_permission_approval": (
            "every_allow" if mode == "safe"
            else "not_required" if no_elicit
            else "once_per_session"
        ),
    }


def _raw_cdp_unsafe_enabled() -> bool:
    # lab is the shipped default, so mode alone must not authorize the bypass.
    return _automation_mode() == "lab" and _env_enabled("BROWSERTAP_ALLOW_UNSAFE_CDP")


def _approval_key(ctx: Context) -> str:
    request_context = getattr(ctx, "request_context", None)
    owner = getattr(request_context, "session", None) or ctx
    key = f"{type(owner).__module__}.{type(owner).__qualname__}:{id(owner)}"
    # Retain the owner for the MCP process lifetime so CPython cannot recycle an
    # id and accidentally treat a different session as already approved.
    _LAB_APPROVAL_OWNERS[key] = owner
    return key


# --- Keep blocking tools off the event loop ---------------------------------
# FastMCP calls a sync tool function directly in the coroutine that handles the
# request (func_metadata: `if fn_is_async: await fn(...) else: fn(...)`), with no
# thread offload. Every tool here blocks — the bridge polls results with
# time.sleep and does synchronous HTTP — and one execute_js can chain several
# roundtrips. Run them all in a worker thread so the server keeps answering
# pings and, critically, can still process notifications/cancelled while a
# slow scan_page is in flight.
_mcp_tool = mcp.tool


async def _acquire_tool_lock() -> None:
    """Take the process-wide tool lock so that cancellation cannot leak it.

    to_thread.run_sync does not abandon its worker on cancellation: the acquire
    always runs to completion and the Cancelled is delivered afterwards. Where it
    lands decides whether the lock comes back — inside the caller's try/finally,
    or at the `await` before it, in which case the gate stays held by a task that
    no longer exists and every serialized tool in this process blocks until it
    restarts. anyio currently lands it in the body (the worker wait is shielded,
    so delivery is deferred to the next checkpoint), which is an implementation
    detail of the backend, not a promise. Ask the worker whether it got in and
    hand the lock straight back if the await raises for any reason;
    threading.Lock has no owner to interrogate afterwards.
    """
    acquired: list[bool] = []

    def _acquire() -> None:
        _TOOL_LOCK.acquire()
        acquired.append(True)

    try:
        await anyio.to_thread.run_sync(_acquire)
    except BaseException:
        if acquired:
            _TOOL_LOCK.release()
        raise


def _default_target_snapshot(driver: Any) -> tuple[Any, bool]:
    """Temporary target changes belong to the current request's scope."""
    return driver.default_session_id, True


def _threaded_tool(*d_args: Any, **d_kwargs: Any):
    # Target locks protect the complete call; unrelated tabs can run together.
    serialize = bool(d_kwargs.pop("serialize", False))

    def wrap(fn):
        options = dict(d_kwargs)
        name = options.get("name") or (d_args[0] if d_args else None) or fn.__name__
        if options.get("annotations") is None:
            options["annotations"] = annotations_for_tool(name)
        decorator = _mcp_tool(*d_args, **options)

        def invoke_sync(*args: Any, **kwargs: Any) -> Any:
            target = _call_target(fn, args, kwargs)
            try:
                with command_scope(persist_defaults=target is None):
                    value = fn(*args, **kwargs)
            except UrlElicitationRequiredError:
                raise
            except Exception as exc:
                return _adapt_tool_result(fn.__name__, exc=exc, target=target)
            return _adapt_tool_result(fn.__name__, value, target=target)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_runner(*args: Any, **kwargs: Any):
                target = _call_target(fn, args, kwargs)
                if not serialize:
                    try:
                        with command_scope(persist_defaults=target is None):
                            value = await fn(*args, **kwargs)
                    except UrlElicitationRequiredError:
                        raise
                    except Exception as exc:
                        return _adapt_tool_result(fn.__name__, exc=exc, target=target)
                    return _adapt_tool_result(fn.__name__, value, target=target)
                await _acquire_tool_lock()
                try:
                    try:
                        with command_scope(persist_defaults=target is None):
                            value = await fn(*args, **kwargs)
                    except UrlElicitationRequiredError:
                        raise
                    except Exception as exc:
                        return _adapt_tool_result(fn.__name__, exc=exc, target=target)
                    return _adapt_tool_result(fn.__name__, value, target=target)
                finally:
                    _TOOL_LOCK.release()

            # Bound to a local and then attached, rather than attached and read
            # back: reading `__signature__` off the wrapper needs the same
            # suppression the write does, and one signature object shared by both
            # is what the annotations are actually derived from anyway.
            signature = inspect.signature(fn, eval_str=True)
            async_runner.__signature__ = signature  # type: ignore[attr-defined]
            async_runner.__annotations__ = {
                name: parameter.annotation
                for name, parameter in signature.parameters.items()
                if parameter.annotation is not inspect.Parameter.empty
            }
            async_runner.__module__ = fn.__module__
            decorator(async_runner)
            return fn

        @functools.wraps(fn)
        async def runner(*args: Any, **kwargs: Any):
            def _run():
                if not serialize:
                    return invoke_sync(*args, **kwargs)
                with _TOOL_LOCK:
                    return invoke_sync(*args, **kwargs)

            return await anyio.to_thread.run_sync(_run)

        # FastMCP builds the input schema from the signature, so runner must keep
        # the original one. `from __future__ import annotations` makes every
        # annotation a string, and pydantic resolves those against the
        # function's own globals — so carry the original signature AND the
        # defining module over, or `Optional` fails to resolve.
        # eval_str resolves the string annotations here, where Optional/Any are
        # in scope, so pydantic never has to look them up again.
        signature = inspect.signature(fn, eval_str=True)
        runner.__signature__ = signature  # type: ignore[attr-defined]
        runner.__annotations__ = {
            name: p.annotation
            for name, p in signature.parameters.items()
            if p.annotation is not inspect.Parameter.empty
        }
        runner.__module__ = fn.__module__
        decorator(runner)
        # Hand back the untouched sync function so in-process callers
        # (switch_session, compact_tabs, ...) don't have to await.
        return fn

    return wrap


mcp.tool = _threaded_tool  # type: ignore[method-assign]


# --- Package paths: extension and bundled skills -----------------------------
def chrome_extension_dir() -> Path:
    return ROOT / "chrome_extension"


def agent_skills_dir() -> Path:
    """Directory holding the shipped agent skills as ``<name>/SKILL.md``.

    Package data rather than a repository-only document, so the path resolves
    the same from a checkout and from a plain ``pip install``. A caller points
    its skill manager at this directory instead of copying the files, which is
    what keeps an installed copy from silently drifting from the release.
    """
    return ROOT / "skills"


# --- Bridge daemon: liveness, spawn lock, autostart --------------------------
def _port_open(host: str, port: int) -> bool:
    return tcp_port_open(host, port)


def _bridge_log_path() -> Path:
    """The daemon's log, rotated by rename if the *last* daemon left it oversized.

    This runs in the spawning process, where nothing holds the file open, so a
    rename is safe here. It is not the whole story: the handle opened below is
    inherited by the daemon and held for its entire life, so this check cannot
    fire again while a bridge is up. The long-lived half of the cap lives in
    ``bridge.rotate_own_log``, and the cap itself is shared so the two cannot
    drift apart.
    """
    path = state_dir(create=True) / "bridge.log"
    try:
        if path.exists() and path.stat().st_size > bridge_module.LOG_MAX_BYTES:
            path.replace(path.with_suffix(".log.old"))
    except OSError:
        pass
    return path


_SPAWN_LOCK_STALE = 30.0


@dataclass(frozen=True)
class _SpawnLockClaim:
    path: Path
    fingerprint: tuple[int, int, int, int, int]


def _spawn_lock_path() -> Path:
    return state_dir() / "spawn.lock"


def _spawn_lock_fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists.

    Used to recycle a spawn lock whose owner crashed mid-spawn instead of
    waiting the full _SPAWN_LOCK_STALE window — that window blocks a real
    recovery for 30s after a daemon that died seconds in.
    """
    return physical_input._pid_alive(pid)


def _acquire_spawn_lock(*, reset: bool = False) -> Optional[_SpawnLockClaim]:
    """Win the right to spawn the bridge, or return None if someone else has it.

    MCP instances start in parallel, and "is the port open? no -> spawn" is not
    atomic across processes: several instances check at the same moment, all see
    a closed port, and all spawn. The losers then sit there having lost the port
    bind. Observed for real — two daemons with identical creation timestamps.
    """
    lock = _spawn_lock_path()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        # Reuse only the OS file-lock primitive: this is spawn.lock.guard,
        # independent of input arbitration. Keep this sibling file in place so
        # every contender locks the same inode. The OS releases it after a crash.
        with physical_input._ArbitrationGuard(lock):
            if reset:
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass
            for _ in range(2):
                try:
                    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    recycle = time.time() - lock.stat().st_mtime > _SPAWN_LOCK_STALE
                    if not recycle:
                        # An empty/unreadable PID does not prove death. O_EXCL
                        # publishes the file before the PID write; treating an
                        # empty file as abandoned once produced 2 daemons for
                        # 12 concurrent callers. Unknown owners still expire.
                        try:
                            old_pid = int(lock.read_text(encoding="utf-8").strip())
                        except (ValueError, OSError):
                            old_pid = 0
                        recycle = old_pid > 0 and not _pid_alive(old_pid)
                    if not recycle:
                        return None
                    # Check, unlink and O_EXCL replacement share one guard. A
                    # second reclaimer cannot delete this caller's new claim.
                    lock.unlink(missing_ok=True)
                    continue
                try:
                    os.write(fd, str(os.getpid()).encode())
                except OSError:
                    pass
                finally:
                    os.close(fd)
                return _SpawnLockClaim(lock, _spawn_lock_fingerprint(lock))
    except (OSError, physical_input.PhysicalInputBusy):
        return None
    return None


def _release_spawn_lock(claim: _SpawnLockClaim) -> None:
    """Release only our failed launch, never a successor's replacement claim."""
    try:
        with physical_input._ArbitrationGuard(claim.path):
            if _spawn_lock_fingerprint(claim.path) == claim.fingerprint:
                claim.path.unlink(missing_ok=True)
    except (OSError, physical_input.PhysicalInputBusy):
        pass


def _handoff_spawn_lock(claim: _SpawnLockClaim, *, instance_id: Optional[str]) -> bool:
    """Keep the successful claim, with the verified daemon as its live owner."""
    record = bridge_module.read_bridge_record()
    if (
        record is None
        or record.get("host") != _get_driver_host()
        or record.get("ws_port") != _get_driver_port()
        or record.get("http_port") != _get_driver_port() + 1
        or (instance_id is not None and record.get("instance_id") != instance_id)
    ):
        return False
    try:
        identity = bridge_module.process_identity(record["pid"])
        if identity is None or not bridge_module._same_process(record, identity):
            return False
        with physical_input._ArbitrationGuard(claim.path):
            if _spawn_lock_fingerprint(claim.path) != claim.fingerprint:
                return False
            # Atomic publication preserves the original owner if writing fails;
            # readers can never interpret a partially written daemon PID.
            _atomic_write_bytes(claim.path, str(record["pid"]).encode())
        return True
    except (OSError, RuntimeError):
        # Identity-unavailable and write errors retain the original claim and
        # its bounded expiry. Neither authorizes dropping startup protection.
        return False


def _wait_for_spawned_bridge(
    claim: Optional[_SpawnLockClaim], *, instance_id: Optional[str],
) -> bool:
    deadline = time.monotonic() + 8
    ready = False
    while time.monotonic() < deadline:
        ready = _port_open(_get_driver_host(), _get_driver_port() + 1)
        if ready and (claim is None or _handoff_spawn_lock(claim, instance_id=instance_id)):
            return True
        # BrowserBridge starts HTTP before main publishes bridge.pid. Keep the
        # claim while waiting for the matching record, including on cold starts.
        time.sleep(0.25)
    # The public result still describes HTTP readiness. An unverifiable record
    # leaves the MCP-owned claim to expire instead of spawning a rival daemon.
    return ready


def spawn_bridge_daemon(*, reset_spawn_lock: bool = False) -> bool:
    """Start the bridge as a detached process so it outlives this MCP instance.

    Returns True once the bridge HTTP port answers. Self-hosting from an MCP
    instance is avoided because these instances are spawned per session and
    recycled, taking the bridge (and its bound ports) down with them.
    """
    _get_driver_port()
    claim = _acquire_spawn_lock(reset=reset_spawn_lock)
    if claim is None:
        # Another instance is spawning. Wait for its daemon rather than starting
        # a second one that will only lose the port bind.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _port_open(_get_driver_host(), _get_driver_port() + 1):
                return True
            time.sleep(0.25)
        return False
    ok = False
    try:
        ok = _spawn_bridge_daemon_locked(claim=claim)
        return ok
    finally:
        # Retain successful claims, but let a verified daemon's death recycle
        # them immediately even while the spawning MCP process remains alive.
        if not ok:
            _release_spawn_lock(claim)


def _spawn_bridge_daemon_locked(*, claim: Optional[_SpawnLockClaim] = None) -> bool:
    # Re-check under the lock: a daemon may have come up between the caller's
    # port check and our acquiring the lock, and spawning now would just create
    # the duplicate the lock exists to prevent.
    if _port_open(_get_driver_host(), _get_driver_port() + 1):
        return claim is None or _wait_for_spawned_bridge(claim, instance_id=None)
    # -u: unbuffered, so daemon tracebacks reach bridge.log immediately
    # instead of dying in a block buffer that never flushes.
    # Prefer pythonw.exe on Windows: it's the GUI-subsystem interpreter with no
    # console window at the binary level, so a stray console never flashes even
    # if the CREATE_NO_WINDOW flag is ignored under some launch environments.
    exe = sys.executable
    if sys.platform == "win32":
        cand = Path(exe).with_name("pythonw.exe")
        if cand.exists():
            exe = str(cand)
    instance_id = secrets.token_urlsafe(24)
    cmd = [
        exe,
        "-u",
        "-m",
        "browsertap_mcp.bridge",
        f"--instance-id={instance_id}",
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(Path.home()),
        "env": bridge_child_environment(),
    }
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        with open(_bridge_log_path(), "ab") as log:
            kwargs["stdout"] = log
            kwargs["stderr"] = log
            # noqa: S603 - `cmd` is built above from this interpreter's own path
            # plus fixed flags and a `secrets.token_urlsafe` id. No caller input
            # reaches it, and the list form means no shell.
            subprocess.Popen(cmd, **kwargs)  # noqa: S603
    except OSError:
        return False
    return _wait_for_spawned_bridge(claim, instance_id=instance_id)


# --- Driver handle and session cache -----------------------------------------
def get_driver() -> BrowserBridge:
    global _driver
    if _driver is not None:
        return _driver
    with _DRIVER_LOCK:
        if _driver is not None:
            return _driver
        if (
            os.environ.get("BROWSERTAP_NO_SPAWN") != "1"
            and not _port_open(_get_driver_host(), _get_driver_port() + 1)
        ):
            spawn_bridge_daemon()
        # If the spawn failed the constructor falls back to self-hosting,
        # which keeps the original single-process behavior working.
        _driver = BrowserBridge(host=_get_driver_host(), port=_get_driver_port())
    return _driver


def require_driver() -> BrowserBridge:
    driver = get_driver()
    # A remote driver outlives the bridge it points at. get_driver only spawns
    # on first construction, so a daemon that dies later would leave every
    # existing MCP instance erroring forever; this check lets any tool call
    # resurrect it.
    if (
        driver.is_remote
        and os.environ.get("BROWSERTAP_NO_SPAWN") != "1"
        and not _port_open(_get_driver_host(), _get_driver_port() + 1)
    ):
        spawn_bridge_daemon()
    return driver


# In remote mode every session listing is an HTTP roundtrip to the bridge; a
# single tool call may want it several times (precheck, response tabs, newTab
# detection). A tiny TTL cache collapses those into one roundtrip.
_sessions_cache: Optional[tuple[float, list[dict[str, Any]]]] = None
_SESSIONS_TTL = 2.0


def invalidate_sessions_cache() -> None:
    global _sessions_cache
    _sessions_cache = None


def active_sessions(timeout: Optional[float] = None, fresh: bool = False) -> list[dict[str, Any]]:
    global _sessions_cache
    cached = _sessions_cache
    if not fresh and cached and time.monotonic() - cached[0] < _SESSIONS_TTL:
        return cached[1]
    sessions = require_driver().get_all_sessions(timeout=timeout)
    _sessions_cache = (time.monotonic(), sessions)
    return sessions


def ensure_sessions(
    timeout: Optional[float] = None,
    fresh: bool = False,
    prune_default: bool = True,
) -> list[dict[str, Any]]:
    sessions = active_sessions(timeout=timeout, fresh=fresh)
    if not sessions:
        raise RuntimeError(
            "No connected browser tabs. Load the unpacked extension from the reported extension path, "
            "keep the bridge daemon running, and open a normal http/https page in Chrome."
        )
    # Every session-scoped tool passes through here before handing an implicit
    # target to the driver, so this is where a dead remembered tab has to be
    # dropped — otherwise the driver falls back to it and refuses the call.
    if prune_default:
        prune_stale_default()
    return sessions


# --- Session targeting: normalize, prune, switch -----------------------------
def normalize_session_id(session_id: Optional[str]) -> Optional[str]:
    if session_id is None:
        return None
    return str(session_id)


def prune_stale_default() -> Optional[str]:
    """Refresh a remembered tab while preserving its browser selection.

    Tab ids are not stable — they change on browser restart, extension reload,
    and whenever a tab is closed — so a default session id goes stale routinely.
    Left in place it poisons every later call with "session not connected, run
    switch_tab first", which is the caller's cue to redo list_tabs + switch_tab
    for something they never chose in the first place. Re-picking is safe here
    precisely because the caller did not name a tab; an *explicit* session_id
    that is dead still raises, since substituting a different page silently is
    the worse failure.
    """
    driver = get_driver()
    cur = driver.default_session_id
    if not cur:
        return None
    resolved = _resolve_session_target(driver, str(cur))
    if resolved:
        selected = str(resolved["session_id"])
        if selected != str(cur):
            _TAB_OWNERSHIP.rebind(str(cur), selected)
            driver.default_session_id = selected
        return selected
    if any(str(s.get("id")) == str(cur) for s in active_sessions()):
        return str(cur)
    # The session cache can simply be out of date; confirm against the bridge
    # before discarding a default that is actually fine.
    sessions = active_sessions(fresh=True)
    if any(str(s.get("id")) == str(cur) for s in sessions):
        return str(cur)
    if ":" in str(cur):
        client_id = str(cur).rsplit(":", 1)[0]
        candidates = [s for s in sessions if str(s.get("id", "")).rsplit(":", 1)[0] == client_id]
        if candidates:
            candidates = [s for s in candidates if is_scriptable_url(s.get("url"))] or candidates
            selected = str(next((s for s in candidates if s.get("active")), candidates[0])["id"])
            driver.default_session_id = selected
            return selected
        # Keep the client prefix even when it has no tabs. Browser-level
        # commands can still open one, and tab commands must not cross clients.
        return None
    driver.default_session_id = None
    return None


def _browser_candidates(
    sessions: list[dict[str, Any]], *, current: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Keep implicit tab selection inside one unambiguous browser client."""
    client_id = str(current).rsplit(":", 1)[0] if current and ":" in str(current) else None
    if client_id:
        candidates = [s for s in sessions if str(s.get("id", "")).rsplit(":", 1)[0] == client_id]
        if not candidates:
            raise ExtensionNotConnectedError(f"Selected browser {client_id} has no connected tabs; select a browser explicitly.")
        return candidates
    preferred = os.environ.get("BROWSERTAP_PREFERRED_BROWSER", "").strip().lower()
    candidates = [s for s in sessions if str(s.get("browser", "")).lower() == preferred] if preferred else []
    candidates = candidates or sessions
    clients = {str(s.get("id", "")).rsplit(":", 1)[0] for s in candidates if ":" in str(s.get("id", ""))}
    if len(clients) > 1:
        raise AmbiguousBrowserError(list(clients))
    return candidates


def switch_session(
    session_id: Optional[str] = None,
    url_pattern: Optional[str] = None,
    browser: Optional[str] = None,
) -> str:
    driver = require_driver()
    if session_id is not None:
        sid = str(session_id)
        resolved = _resolve_session_target(driver, sid)
        if resolved:
            current_sid = str(resolved["session_id"])
            if current_sid != sid:
                _TAB_OWNERSHIP.rebind(sid, current_sid)
            driver.default_session_id = current_sid
            return current_sid
        found = next((s for s in active_sessions() if str(s.get("id")) == sid), None)
        if not found:
            raise _session_target_not_found(sid)
        driver.default_session_id = sid
        return sid
    if browser is not None:
        # Pick a tab belonging to the named browser (chrome/edge/opera).
        # Prefer one matching url_pattern too, if given.
        want = browser.strip().lower()
        cands = [s for s in active_sessions() if str(s.get("browser", "")).lower() == want]
        if not cands:
            avail = sorted({str(s.get("browser", "?")) for s in active_sessions()})
            raise RuntimeError(f"No connected tab for browser '{want}'. Connected: {avail or 'none'}")
        if url_pattern:
            narrowed = [s for s in cands if url_pattern in str(s.get("url", ""))]
            if not narrowed:
                raise RuntimeError(
                    f"No connected tab for browser '{want}' matches URL pattern "
                    f"{url_pattern!r}."
                )
            cands = narrowed
            if len(cands) > 1:
                choices = ", ".join(
                    f"{item.get('id')} ({item.get('url', '')})" for item in cands[:8]
                )
                raise RuntimeError(
                    f"URL pattern {url_pattern!r} matched {len(cands)} tabs in browser "
                    f"'{want}': {choices}. Pass the full session_id to select one."
                )
        clients = {str(s["id"]).rsplit(":", 1)[0] for s in cands}
        if len(clients) > 1:
            raise AmbiguousBrowserError(list(clients))
        sid = str(cands[0]["id"])
        driver.default_session_id = sid
        return sid
    if url_pattern:
        # Its own name rather than reusing `sid`: `set_session` can return None,
        # while every other binding of `sid` in this function is a str.
        matched = driver.set_session(url_pattern)
        if not matched:
            raise RuntimeError(f"No session matching url pattern: {url_pattern}")
        return str(matched)
    if driver.default_session_id:
        return str(driver.default_session_id)
    sessions = ensure_sessions()
    # With several browsers connected, a blind default should land on the
    # user-preferred one (BROWSERTAP_PREFERRED_BROWSER=chrome|edge|opera).
    pref = os.environ.get("BROWSERTAP_PREFERRED_BROWSER", "").strip().lower()
    if pref:
        preferred = [s for s in sessions if str(s.get("browser", "")).lower() == pref]
        if preferred:
            sessions = preferred
    sessions = _browser_candidates(sessions)
    driver.default_session_id = str(sessions[0]["id"])
    return str(driver.default_session_id)


# --- exec_js: the one bridge roundtrip every tool goes through ---------------
_PENDING_OPERATION_HINT = (
    "The operation is still unresolved. Call get_execute_js_result with its "
    "operation_id from the same MCP session before another command on this tab."
)
_RELEASED_WAIT_HINT = (
    "The wait operation no longer holds the tab. "
    "Another command can use this tab. Call get_execute_js_result with its "
    "operation_id from the same MCP session to inspect its receipt or collect a delayed reply; "
    "do not replay the operation automatically."
)


def exec_js(
    script: str, session_id: Optional[str] = None, timeout: float = 15.0, *, read_only_probe: bool = False,
) -> dict[str, Any]:
    timeout = _positive_timeout(timeout)
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    driver = require_driver()
    # Pass the target session through per-call instead of mutating the global
    # default_session_id. A directed call ("run this on tab Y") must not steal
    # the shared default out from under a concurrent task working on tab X.
    # session_id=None falls back to the driver's default inside execute_js.
    sid = str(session_id) if session_id is not None else None
    first_budget, _reserved = simphtml.undelivered_retry_split(remaining())
    if first_budget <= 0:
        raise TimeoutError("bridge JS deadline exhausted before dispatch")
    probe_options = {"read_only_probe": True} if read_only_probe else {}
    response = driver.execute_js(script, timeout=first_budget, session_id=sid, **probe_options)
    if simphtml.undelivered_retry_safe(response):
        # Never reached the page; retrying is side-effect-free. The reserve
        # above is what makes this branch reachable — a first attempt handed the
        # whole budget can only report "undelivered" after spending it all.
        retry_budget = remaining()
        if retry_budget > 0:
            response = driver.execute_js(
                script,
                timeout=retry_budget,
                session_id=sid,
                **probe_options,
            )
    kind = simphtml.no_response_kind(response)
    if kind:
        # Tools built on this helper (CDP, cookies, screenshots) have nothing
        # useful to return without data; fail loudly instead of returning junk.
        delivery_state = response.get("delivery_state") or {
            "undelivered": "undelivered",
            "after_ack": "sent_unconfirmed",
            "navigated": "navigated",
        }[kind]
        message = response.get("result")
        if "delivery_state" not in response and kind == "after_ack" and isinstance(message, str):
            if "ACK received" in message or "delivered but no result" in message:
                delivery_state = "delivered_no_result"
        hint = (
            _PENDING_OPERATION_HINT
            if response.get("operation_id") and kind == "after_ack"
            else "Session may be asleep or disconnected; run list_tabs, switch_tab to a live session, then retry."
        )
        if kind == "undelivered" and not simphtml.undelivered_retry_safe(response):
            hint = "The bridge explicitly refused a retry; inspect its diagnostic details and the target before proceeding."
        if read_only_probe and response.get("reservation_held") is False and response.get("operation_id"):
            hint = _RELEASED_WAIT_HINT
        raise BridgeNoResponseError(
            f"Bridge no-response ({kind}): {response.get('result')}. {hint}",
            error_code=str(response.get("error_code") or "no_response"),
            delivery_state=delivery_state,
            retry_safe=simphtml.undelivered_retry_safe(response),
            operation_id=response.get("operation_id"),
            reservation_held=response.get("reservation_held"),
            poll_with=response.get("poll_with") or "get_execute_js_result",
            diagnostics=response.get("diagnostics"),
        )
    return response


def compact_tabs(timeout: Optional[float] = None, fresh: bool = False) -> list[dict[str, Any]]:
    tabs = []
    for sess in active_sessions(timeout=timeout, fresh=fresh):
        item = dict(sess)
        item.pop("connected_at", None)
        item.pop("type", None)
        url = str(item.get("url") or "").lower()
        if ("cloudflareaccess.com" in url or
                "/cdn-cgi/access/verify" in url or
                "/cdn-cgi/access/verify-code" in url):
            item["automation_attention"] = "authentication_required"
            item["hint"] = (
                "This looks like an authentication tab opened by another process or tunnel. "
                "Complete or close it before assuming the related service is ready."
            )
        tabs.append(item)
    return tabs


# Status/diagnostic tools must answer fast even when the bridge is half-dead;
# they use this short timeout and degrade instead of raising.
_STATUS_TIMEOUT = 5.0
_EXTENSION_PROTOCOL_VERSION = 3
_REQUIRED_EXTENSION_CAPABILITIES = {"content_command_channel_removed", "batch_result_guard"}


# The capability registry is deliberately kept beside the public tool surface.
# It is the server's agent-facing inventory: page work is the normal path,
# browser operations use the real profile, and desktop is an explicit escape
# hatch rather than an automatic fallback.  Keep every registered tool in
# exactly one group; the completeness check below turns a forgotten entry into
# a diagnostic/test failure instead of an undocumented capability.
_PAGE_TOOLS = frozenset({
    "open_url", "scan_page", "wait_for", "wait_for_url", "scroll_page",
    "execute_js", "get_execute_js_result", "handle_dialog",
    "resolve_leave_dialog", "page_click", "page_type", "page_press",
    "page_drag", "capture_page_screenshot", "upload_files",
})
_BROWSER_TOOLS = frozenset({
    "get_setup_status", "get_automation_profile", "set_automation_profile",
    "list_tabs", "list_all_tabs", "extension_path", "open_new_tab",
    "close_tabs", "switch_tab", "activate_tab", "list_extensions",
    "set_extension_enabled", "uninstall_extension", "get_bookmarks",
    "create_bookmark", "remove_bookmark", "call_extension", "download_file",
    "network_capture_start", "network_capture_stop", "console_capture_start",
    "get_console_messages", "console_capture_stop", "cdp_command",
    "cdp_batch", "debugger_targets", "get_cookies", "set_cookies",
    "delete_cookies", "storage_get", "storage_set", "set_site_permission",
    "reset_site_permissions", "save_pdf",
})
_DESKTOP_TOOLS = frozenset({
    "inspect_native_file_dialog", "cancel_native_file_dialog",
})
_CAPABILITY_GROUPS = ("page", "browser", "desktop")

# Targeting is intentionally coarse in S1: it describes whether a caller must
# provide a page/tab target or a native-dialog ticket, not the full resolution policy. The latter
# remains in the individual tool contracts and is covered by the target tests.
_NO_TARGET_TOOLS = frozenset({
    "get_setup_status", "get_automation_profile", "set_automation_profile",
    "list_tabs", "list_all_tabs", "extension_path", "list_extensions",
    "set_extension_enabled", "uninstall_extension", "get_bookmarks",
    "create_bookmark", "remove_bookmark", "call_extension",
    "inspect_native_file_dialog",
})
_REQUIRED_TARGET_TOOLS = frozenset({
    "close_tabs", "cancel_native_file_dialog",
})

_WRITE_TOOLS = frozenset({
    "open_url", "open_new_tab", "close_tabs", "switch_tab", "activate_tab",
    "set_automation_profile", "set_extension_enabled", "uninstall_extension",
    "create_bookmark", "remove_bookmark", "download_file",
    "network_capture_start", "network_capture_stop", "console_capture_start",
    "console_capture_stop", "set_cookies", "delete_cookies", "storage_set",
    "set_site_permission", "reset_site_permissions", "scroll_page", "page_click",
    "page_type", "page_press", "page_drag", "upload_files", "handle_dialog",
    "resolve_leave_dialog", "save_pdf",
    "cancel_native_file_dialog",
})
_MIXED_EFFECT_TOOLS = frozenset({
    "execute_js", "cdp_command", "cdp_batch", "call_extension",
    "inspect_native_file_dialog", "scan_page", "wait_for",
    "get_console_messages", "capture_page_screenshot", "get_execute_js_result",
    "get_setup_status",
})

def _build_tool_capabilities() -> dict[str, dict[str, Any]]:
    """Build the immutable-by-convention agent capability inventory."""
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(_PAGE_TOOLS):
        result[name] = {
            "capability": "page",
            "target": (
                "required" if name in _REQUIRED_TARGET_TOOLS
                else "none" if name in _NO_TARGET_TOOLS
                else "optional"
            ),
            "side_effect": "read",
            "result_contract": "btap.result.v1",
            "desktop_opt_in": False,
        }
    for name in sorted(_BROWSER_TOOLS):
        result[name] = {
            "capability": "browser",
            "target": (
                "required" if name in _REQUIRED_TARGET_TOOLS
                else "none" if name in _NO_TARGET_TOOLS
                else "optional"
            ),
            "side_effect": "read",
            "result_contract": "btap.result.v1",
            "desktop_opt_in": False,
        }
    for name in sorted(_DESKTOP_TOOLS):
        result[name] = {
            "capability": "desktop",
            "target": (
                "required" if name in _REQUIRED_TARGET_TOOLS
                else "none" if name in _NO_TARGET_TOOLS
                else "optional"
            ),
            "side_effect": "write",
            "result_contract": "btap.result.v1",
            "desktop_opt_in": True,
        }
    for name in _WRITE_TOOLS:
        if name in result:
            result[name]["side_effect"] = "write"
    for name in _MIXED_EFFECT_TOOLS:
        if name in result:
            result[name]["side_effect"] = "mixed"
    if "resolve_leave_dialog" in result:
        result["resolve_leave_dialog"]["desktop_fallback"] = True
        result["resolve_leave_dialog"]["desktop_opt_in"] = True
    return result


TOOL_CAPABILITIES = _build_tool_capabilities()


def _capability_registry_status() -> dict[str, Any]:
    """Return the registry and any mismatch with FastMCP's registered tools."""
    manager = getattr(mcp, "_tool_manager", None)
    registered_map = getattr(manager, "_tools", {})
    registered = set(registered_map) if isinstance(registered_map, dict) else set()
    declared = set(TOOL_CAPABILITIES)
    missing = sorted(registered - declared)
    unknown = sorted(declared - registered)
    groups = {
        group: sorted(
            name for name, metadata in TOOL_CAPABILITIES.items()
            if metadata.get("capability") == group
        )
        for group in _CAPABILITY_GROUPS
    }
    return {
        "version": 1,
        "groups": groups,
        "tool_count": len(registered),
        "declared_tool_count": len(declared),
        "missing_tools": missing,
        "unknown_tools": unknown,
        "complete": not missing and not unknown,
    }


# --- Version comparison for the setup report ---------------------------------
def _version_order(value: Any) -> tuple[int, ...] | None:
    """Parse a dotted version for ordering, or None when it cannot be ordered.

    Only the leading numeric run of each component is read, so `0.3.13rc1`
    orders as `(0, 3, 13)` -- enough to answer "older or newer" for the builds
    this project ships together. Anything unparseable returns None so callers
    fall back to plain inequality instead of inventing a direction.
    """
    if not isinstance(value, str):
        return None
    parts: list[int] = []
    for component in value.strip().split("."):
        digits = re.match(r"\d+", component)
        if digits is None:
            break
        parts.append(int(digits.group()))
    return tuple(parts) or None


def _component_is_newer(component: Any) -> bool:
    """True only when `component` is a strictly newer build than this process.

    The package, bridge, and extension ship as one version, so the mismatch a
    user normally hits is a component that is *older* -- restart it or reload
    it and the mismatch clears. The reverse direction means this process is the
    stale one: the files on disk are already new, so a restarted bridge or a
    reloaded extension just reports the new version again and the advice never
    converges. Distinguishing the two is the whole point of this helper; a bare
    `!=` reports "reload the extension" to someone who just reloaded it.
    """
    running = _version_order(__version__)
    reported = _version_order(component)
    return bool(running and reported and reported > running)


# --- Tools: automation profile -----------------------------------------------
@mcp.tool(
    description=(
        "Return the active safe/lab automation profile. Lab is the default and skips elicitation "
        "unless BROWSERTAP_LAB_NO_ELICIT is explicitly disabled; safe requires approval for "
        "every physical action and permission allow."
    ),
    serialize=False,
)
def get_automation_profile() -> dict[str, Any]:
    return _automation_profile()


@mcp.tool(
    description=(
        "Set the safe or lab automation profile for this MCP process. This does not persist or "
        "reload the extension; BROWSERTAP_MODE controls the next process."
    ),
    serialize=True,
)
def set_automation_profile(mode: str) -> dict[str, Any]:
    normalized = str(mode).strip().lower()
    if normalized not in _AUTOMATION_MODES:
        raise ValueError("mode must be one of: lab, safe")
    global _AUTOMATION_MODE_OVERRIDE
    _AUTOMATION_MODE_OVERRIDE = normalized
    _LAB_PHYSICAL_APPROVALS.clear()
    _LAB_SITE_PERMISSION_APPROVALS.clear()
    return _automation_profile()


# --- Tool: get_setup_status (what browsertap doctor reads) -------------------
@mcp.tool(
    description=(
        "Return component versions, stale-build actions, extension path, bridge ports, and "
        "connection status for setup/diagnostics. extension_build_verdict answers whether "
        "the browser worker is running this code (matches_tree / stale_worker), or says why "
        "it cannot tell (stamp_not_regenerated / unverifiable); it is decisive where "
        "version equality is not. extension_build_enforced=false means no comparison "
        "happened, so treat it as unknown rather than as a pass. "
        "extension_status_available=false means no runtime status was obtained: starting "
        "asks to wait_for_extension, then extension_unavailable asks to "
        "check_extension_connection; missing status alone never requests a reload. "
        "mcp_build_verdict and bridge_build_verdict compare each process's package-import "
        "Python and imported JavaScript source snapshot with expected_python_source_identity "
        "on disk: matches_tree, "
        "stale_process, or unverifiable. Same-version source changes require "
        "restart_mcp_session or restart_bridge; *_build_enforced=false is unknown. "
        "state_paths distinguishes missing/empty/ready/unreadable/invalid_encoding token files "
        "without exposing token content; unreadable metadata has unknown existence. "
        "On Windows, inspecting an existing token file may tighten its ACL to the current user. "
        "Bridge startup or authentication initialization may create a missing token file. "
        "A malformed remote diagnosis reports bridge_unreachable with malformed_diagnosis, "
        "not evidence of an old build. "
        "Answers while another tool "
        "is still running; default_session_id is isolated from other calls' temporary targets. "
        "capability_registry reports declared/registered tool counts, completeness and "
        "page/browser/desktop groups. Each tool's MCP annotations describe its potential effects."
    ),
    serialize=False,
)
def get_setup_status() -> dict[str, Any]:
    driver = get_driver()
    default_session_id, default_session_settled = _default_target_snapshot(driver)
    bridge_error = None
    diagnosis: dict[str, Any] = {}
    try:
        sessions = compact_tabs(timeout=_STATUS_TIMEOUT, fresh=True)
    except Exception as e:
        sessions = []
        bridge_error = str(e)
    try:
        raw_diagnosis = driver.diagnose(timeout=_STATUS_TIMEOUT)
        if isinstance(raw_diagnosis, dict):
            diagnosis = raw_diagnosis
    except Exception as e:
        diagnosis = {
            "cause": "bridge_unreachable",
            "ok": False,
            "error": str(e),
        }
        bridge_error = bridge_error or str(e)
    expected_python = current_source_identity()
    mcp_python = loaded_source_identity()
    bridge_python = diagnosis.get("bridge_source_identity")
    mcp_build_verdict = compare_source_identities(mcp_python, expected_python)
    bridge_build_verdict = compare_source_identities(bridge_python, expected_python)
    bridge_version = diagnosis.get("bridge_version")
    extension_version = diagnosis.get("extension_version")
    protocol_version = diagnosis.get("protocol_version")
    extension_capabilities = diagnosis.get("extension_capabilities") or {}
    extension_status_error = diagnosis.get("extension_status_error")
    reported_build_stamp = diagnosis.get("extension_build_stamp")
    # A missing handshake is not a compatibility failure. Older bridges forward
    # these fields only after a runtime reply; even an empty capability object is
    # meaningful evidence when a legacy extension actually answered.
    extension_status_available = any(
        diagnosis.get(field) is not None
        for field in (
            "extension_version", "protocol_version", "extension_capabilities",
            "extension_build_stamp",
        )
    )

    # An older bridge may not forward extension build data yet, while still
    # supporting the generic ext_cmd route. Probe it directly before deciding
    # that Chrome needs a manual extension reload. The build stamp joins the
    # condition rather than getting its own probe: a bridge that predates it
    # still reports both versions, so without this the stamp would be missing on
    # exactly the installs where the fallback exists to fill gaps in.
    if extension_version is None or protocol_version is None or reported_build_stamp is None:
        try:
            runtime_response = driver.ext_cmd({"cmd": "bridge_status"}, timeout=_STATUS_TIMEOUT)
            runtime = _extension_data(runtime_response)
            extension_status_available = extension_status_available or isinstance(
                runtime_response, dict
            )
            # Gap-filling, never overwriting. Every field keeps the bridge's answer
            # when it has one, because this branch now fires with all three version
            # fields present -- the stamp alone is enough to trigger it. A
            # `bridge_status` reply that omitted `capabilities` would otherwise empty
            # a populated set and demand a reload of an extension that was fine.
            if extension_version is None:
                extension_version = (
                    runtime.get("extension_version") or runtime.get("manifest_version")
                )
            if protocol_version is None:
                protocol_version = runtime.get("protocol_version")
            extension_capabilities = extension_capabilities or runtime.get("capabilities") or {}
            reported_build_stamp = reported_build_stamp or runtime.get("build_stamp")
        except Exception as e:
            extension_status_error = extension_status_error or str(e)

    # The one question the three version fields cannot answer: is the worker
    # running the code in this tree? `reported_build_stamp` is a literal read out of
    # the JavaScript the worker actually loaded, so comparing it to a fresh hash of
    # the directory is decisive in both directions -- which version equality never
    # was. Measured twice here: once the versions differed while the code matched,
    # once they agreed and every advertised capability was present and a reload was
    # still needed.
    tree_build_stamp: str | None = None
    recorded_build_stamp: str | None = None
    build_stamp_error = ""
    try:
        extension_directory = chrome_extension_dir()
        tree_build_stamp = compute_extension_stamp(extension_directory)
        recorded_build_stamp = read_extension_stamp(extension_directory)
    except (ExtensionStampError, OSError) as e:
        build_stamp_error = str(e)
    if recorded_build_stamp is not None and recorded_build_stamp != tree_build_stamp:
        # Someone edited an extension file without regenerating the stamp, so the
        # worker's value proves nothing either way: a *fresh* worker also reports the
        # old literal. Naming the developer's fix beats guessing at the browser's.
        extension_build_verdict = "stamp_not_regenerated"
    elif not isinstance(reported_build_stamp, str) or tree_build_stamp is None:
        extension_build_verdict = "unverifiable"
    elif reported_build_stamp == tree_build_stamp:
        extension_build_verdict = "matches_tree"
    else:
        extension_build_verdict = "stale_worker"
    # Reported rather than assumed, for the reason `on_screen`, `input_quiet.enforced`
    # and the lint rule floor are: a check that silently could not run must not read
    # as one that ran and passed.
    extension_build_enforced = extension_build_verdict in {"matches_tree", "stale_worker"}

    # Direction matters. A component that is *newer* than this process cannot be
    # repaired by restarting or reloading it, so those two flags stay false and
    # the verdict names the stale side instead: this MCP server process.
    bridge_is_newer = _component_is_newer(bridge_version)
    extension_is_newer = _component_is_newer(extension_version)
    protocol_is_newer = (
        isinstance(protocol_version, int)
        and not isinstance(protocol_version, bool)
        and protocol_version > _EXTENSION_PROTOCOL_VERSION
    )
    package_is_stale = (
        bridge_is_newer or extension_is_newer or protocol_is_newer
        or mcp_build_verdict == "stale_process"
    )

    restart_bridge_required = (
        (bridge_version != __version__ and not bridge_is_newer)
        or (bridge_build_verdict == "stale_process" and not bridge_is_newer)
    )
    missing_extension_capabilities = sorted(
        capability
        for capability in _REQUIRED_EXTENSION_CAPABILITIES
        if extension_status_available and extension_capabilities.get(capability) is not True
    )
    # The version number is the *weakest* of the four signals below and the only one
    # a reload cannot be needed for on its own, so it yields to the stamp rather than
    # being OR-ed in beside it. `matches_tree` means the worker's own JavaScript
    # hashes to this directory with two lines normalised away, one of which is the
    # manifest version -- so the only difference a reload could remove is the number
    # Chrome parsed at load time, and Chrome never re-parses it without one. Left as
    # a bare `or`, every release bump demanded a human click whose sole effect was to
    # stop this gate complaining, and `tests/live_preflight.py` reads the flag rather
    # than the verdict and has no override, so that click gated the whole live layer.
    # It stays authoritative whenever the stamp cannot judge (`unverifiable`,
    # `stamp_not_regenerated`): a weak signal beats none.
    version_skew = extension_version != __version__ and not extension_is_newer
    version_skew_is_only_the_release_number = extension_build_verdict == "matches_tree"
    reload_extension_required = extension_status_available and (
        (version_skew and not version_skew_is_only_the_release_number)
        or (protocol_version != _EXTENSION_PROTOCOL_VERSION and not protocol_is_newer)
        # A newer extension that no longer advertises a capability this build
        # requires is also a stale-package problem: reloading cannot add back
        # something the newer extension deliberately dropped.
        or (bool(missing_extension_capabilities) and not extension_is_newer)
        # The strongest evidence of the three, and the only one that has ever been
        # right when the others were wrong. Same `extension_is_newer` guard: a
        # worker ahead of this process is compared against this process's own copy
        # of the tree, so the difference is the stale server, not the worker.
        or (extension_build_verdict == "stale_worker" and not extension_is_newer)
    )
    # The 401 this catches has no other symptom: the rejection body names no
    # path, so "which token file did each side read?" is unanswerable from the
    # error alone. Compare only the fields that decide the check, and only when
    # the daemon actually reported its own -- an older bridge does not.
    local_state_paths = state_paths_report()
    bridge_state_paths = diagnosis.get("state_paths")
    state_paths_disagreement: dict[str, Any] = {}
    state_paths_advice = ""
    if isinstance(bridge_state_paths, dict):
        for field in ("state_dir", "token_file", "token_fingerprint", "auth_enabled"):
            mine, theirs = local_state_paths.get(field), bridge_state_paths.get(field)
            if mine != theirs:
                state_paths_disagreement[field] = {"this_process": mine, "bridge": theirs}
        if bridge_state_paths.get("token_matches_file") is False:
            state_paths_disagreement["bridge_token_is_from_before_the_file_changed"] = True
    if state_paths_disagreement:
        state_paths_advice = (
            "The bridge daemon and this process disagree about their state directory, "
            "token file or token (see state_paths_disagreement). A token file that both "
            "sides name identically but the daemon no longer matches means the daemon "
            "predates the file: run `browsertap bridge --restart`. Different paths mean "
            "the two processes were started with different environments; fix that first, "
            "because a restart will not."
        )
    if bridge_error or diagnosis.get("cause") == "bridge_unreachable":
        component_status = "bridge_unreachable"
        component_action = "restart_bridge"
    elif package_is_stale:
        component_status = "stale_package"
        component_action = "restart_mcp_session"
    elif restart_bridge_required:
        component_status = "stale_bridge"
        component_action = "restart_bridge"
    elif not extension_status_available:
        if diagnosis.get("cause") == "starting":
            component_status = "starting"
            component_action = "wait_for_extension"
        else:
            component_status = "extension_unavailable"
            component_action = "check_extension_connection"
    elif reload_extension_required:
        component_status = "stale_extension"
        component_action = "reload_extension"
    else:
        component_status = "healthy"
        component_action = "none"
    status: dict[str, Any] = {
        "status": component_status,
        "action": component_action,
        "capability_registry": _capability_registry_status(),
        "package_version": __version__,
        "bridge_version": bridge_version,
        "mcp_source_identity": mcp_python,
        "bridge_source_identity": bridge_python,
        "expected_python_source_identity": expected_python,
        "mcp_build_verdict": mcp_build_verdict,
        "bridge_build_verdict": bridge_build_verdict,
        "mcp_build_enforced": mcp_build_verdict != "unverifiable",
        "bridge_build_enforced": bridge_build_verdict != "unverifiable",
        "extension_version": extension_version,
        "extension_status_available": extension_status_available,
        "protocol_version": protocol_version,
        "expected_protocol_version": _EXTENSION_PROTOCOL_VERSION,
        "extension_capabilities": extension_capabilities,
        "missing_extension_capabilities": missing_extension_capabilities,
        "extension_build_stamp": reported_build_stamp,
        "expected_extension_build_stamp": tree_build_stamp,
        "extension_build_verdict": extension_build_verdict,
        "extension_build_enforced": extension_build_enforced,
        "restart_bridge_required": restart_bridge_required,
        "reload_extension_required": reload_extension_required,
        "restart_mcp_session_required": package_is_stale,
        "extension_name": "BrowserTap Bridge",
        "extension_path": str(chrome_extension_dir()),
        "bridge_host": _get_driver_host(),
        "bridge_ws_port": _get_driver_port(),
        "bridge_http_port": _get_driver_port() + 1,
        # Where *this* process keeps state and which token file it reads. The
        # daemon answers the same question inside `diagnosis.state_paths`, and
        # the two can disagree -- see the note added below.
        "state_paths": local_state_paths,
        "remote_mode": driver.is_remote,
        "connected_tabs": len(sessions),
        "default_session_id": default_session_id,
        "default_session_settled": default_session_settled,
        "tabs": sessions,
        "diagnosis": diagnosis,
        "notes": [
            "Load the unpacked extension from extension_path in chrome://extensions with Developer Mode enabled.",
            "Keep a normal http/https page open in Chrome; about:blank is not enough.",
            "The bridge runs as a detached daemon; this MCP server auto-starts it when missing.",
        ],
    }
    if component_status == "starting":
        status["notes"].insert(
            0,
            "The bridge is up but the extension handshake has not completed yet. "
            "Wait a few seconds and retry doctor or the browser tool; do not reload "
            "the extension unless the next diagnosis says stale_extension.",
        )
    elif component_status == "extension_unavailable":
        status["notes"].insert(
            0,
            "The bridge is reachable but extension runtime status is unavailable. "
            "Retry doctor; if it persists, check that BrowserTap Bridge is enabled "
            "and connected in the intended browser. See diagnosis for the connection "
            "cause. Missing runtime status does not establish an outdated extension.",
        )
    if (
        extension_status_available
        and extension_build_verdict == "unverifiable"
        and recorded_build_stamp is not None
    ):
        # This tree can be verified -- it carries a stamp the sources agree with --
        # and the worker still reported none, so the strongest of the four checks was
        # skipped while the other three passed. Measured here: versions equal,
        # protocol matched, every capability present, `action: none`, and the browser
        # running code from before the stamp existed. `status` stays `healthy` on
        # purpose, because an unknown is not a failure and a reload demanded on one
        # would fire at every pre-stamp install; this note is the disclosure half of
        # `enforced: false`. It names both causes because they need different fixes
        # and leave identical evidence -- absence cannot be narrowed to the worker
        # without trusting the bridge's version number, which is the inference the
        # stamp exists to replace.
        status["notes"].insert(
            0,
            "This tree carries a build stamp but the extension reported none, so "
            "extension_build_verdict could not check whether the browser is running "
            "this code: extension_build_enforced is false, which means unknown rather "
            "than a pass. A worker loaded before the stamp existed reads this way, and "
            "so does a bridge daemon too old to forward the field. Reload the unpacked "
            "extension once, and `browsertap bridge --restart` covers the other half.",
        )
    if version_skew and version_skew_is_only_the_release_number:
        # The disclosure half of letting the stamp overrule the version number. Without
        # it a reader sees `healthy` next to two different version numbers and has to
        # guess which one this process believed, and the honest answer -- neither, it
        # compared the code instead -- is not derivable from the other fields.
        status["notes"].insert(
            0,
            f"The extension reports version {extension_version} against this "
            f"build's {__version__}, and no reload is needed: extension_build_verdict "
            "is matches_tree, so the worker's own JavaScript hashes to this tree and "
            "the only difference is the release number Chrome parsed when the "
            "extension was loaded. Chrome does not re-parse it without a reload, so "
            "this gap persists for the life of the install and means nothing.",
        )
    if package_is_stale:
        # Lead with the only action that can clear this, because the two flags a
        # reader reaches for first are both false here. Inserted after the note
        # above so a genuinely stale package still leads.
        status["notes"].insert(
            0,
            (
                "This MCP process's package-import source snapshot (Python and imported JavaScript) differs "
                "from the installed sources now on disk. "
                if mcp_build_verdict == "stale_process"
                else "A component reports a newer version than this MCP server process. "
            ) +
            "Restart the MCP session or client so it loads the installed build; "
            "restarting the bridge or reloading the extension cannot clear it.",
        )
    if bridge_build_verdict == "stale_process" and not bridge_is_newer:
        status["notes"].insert(
            1 if package_is_stale else 0,
            "The bridge daemon's package-import source snapshot (Python and imported JavaScript) differs from "
            "this installation's current sources. Run `browsertap bridge --restart` "
            "using this installation. If the daemon uses a different installation, "
            "align its package path before restarting it.",
        )
    if "unverifiable" in (mcp_build_verdict, bridge_build_verdict):
        status["notes"].append(
            "A package source identity could not be compared: the process may use an older "
            "identity schema, or Python sources or required imported JavaScript could not be read. See "
            "mcp_build_verdict and bridge_build_verdict; *_build_enforced=false means "
            "unknown, even if versions match. A fresh process with readable installed "
            "Python sources and imported JavaScript is required to verify the running build.",
        )
    if extension_build_verdict == "stamp_not_regenerated":
        # A stale MCP process is the component verdict and its restart is the
        # only action that can clear a newer bridge/extension mismatch. Keep
        # that instruction first when both conditions are present; the stamp
        # repair remains immediately after it instead of sending the operator
        # to reload an extension that is not the primary fault.
        note_index = 1 if package_is_stale else 0
        status["notes"].insert(
            note_index,
            "An extension file was edited without regenerating the build stamp "
            f"(background.js says {recorded_build_stamp}, the sources hash to "
            f"{tree_build_stamp}), so extension_build_verdict cannot tell a stale "
            "worker from a fresh one. Run `python -m scripts.extension_stamp --write`, "
            "then reload the unpacked extension.",
        )
    elif build_stamp_error:
        status["extension_build_error"] = build_stamp_error
    if bridge_error:
        status["bridge_error"] = bridge_error
    if extension_status_error:
        status["extension_status_error"] = extension_status_error
    if state_paths_disagreement:
        status["state_paths_disagreement"] = state_paths_disagreement
        status["notes"].insert(0, state_paths_advice)
    return status


# --- Tools: tab inventory and closing ----------------------------------------
@mcp.tool(
    description=(
        "List connected tabs across all connected browsers; each tab has a browser field "
        "(chrome/edge/opera) and a session id to pass verbatim. Answers while another tool is "
        "still running. The default_session_id snapshot is isolated from other calls' "
        "temporary targets. Pass session_id explicitly when agents share one MCP process. "
        "A timed-out inventory releases only that read's bridge reservation; pending "
        "mutations keep their reservations."
    ),
    serialize=False,
)
def list_tabs() -> dict[str, Any]:
    default_session_id, settled = _default_target_snapshot(require_driver())
    try:
        sessions = compact_tabs(timeout=_STATUS_TIMEOUT, fresh=True)
    except Exception as e:
        return {
            "default_session_id": default_session_id,
            "default_session_settled": settled,
            "tabs": [],
            "bridge_error": str(e),
        }
    return {
        "default_session_id": default_session_id,
        "default_session_settled": settled,
        "tabs": sessions,
    }


@mcp.tool(
    description=(
        "List every open tab, including chrome-extension:// pages that list_tabs hides. "
        "Those never become sessions (content scripts can't run there), so they have no "
        "session id — drive them with cdp_command(tab_id=...) instead. Works with no tabs open."
    ),
    serialize=False,
)
def list_all_tabs(session_id: Optional[str] = None) -> dict[str, Any]:
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    return driver.ext_cmd({"cmd": "tabs", "all": True},
                          client_id=client_id, timeout=20.0)


@mcp.tool(
    description=(
        "Close one or more tabs by native tab id or composite session_id. Accepts a single "
        "identifier or a list; identifiers in one call must belong to the same browser. "
        "By default it closes only tabs created by this MCP task and requires the owner_id "
        "returned by open_new_tab; lifecycle generations are checked before removal. Set "
        "only_if_agent_owned=false only for an explicit operator request to close a user tab."
    )
)
def close_tabs(
    tab_id: int | str | list[int | str],
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    only_if_agent_owned: bool = True,
) -> dict[str, Any]:
    driver = require_driver()
    rebound: Optional[dict[str, Any]] = None
    if session_id is not None:
        requested_session_id = str(session_id)
        rebound = _resolve_session_target(driver, requested_session_id)
        if rebound and str(rebound.get("session_id")) != requested_session_id:
            replacement_session_id = str(rebound["session_id"])
            _TAB_OWNERSHIP.rebind(requested_session_id, replacement_session_id)
            tab_id = _replace_rebound_tab_target(
                tab_id,
                old_session_id=requested_session_id,
                new_session_id=replacement_session_id,
            )
            session_id = replacement_session_id
    native_ids, client_id = _normalize_tab_targets(tab_id, session_id=session_id)
    session_ids = [f"{client_id}:{native_id}" for native_id in native_ids]
    expected_generations: dict[str, str] = {}
    if only_if_agent_owned:
        capability = str(owner_id).strip() if owner_id is not None else ""
        if not capability:
            raise PermissionError(
                "close_tabs refused: owner_id is required when only_if_agent_owned=true; "
                "use the capability returned by open_new_tab"
            )
        expected_generations = _TAB_OWNERSHIP.validate(
            session_ids,
            owner_id=capability,
            live_sessions=active_sessions(fresh=True),
        )
    requested: int | list[int] = native_ids[0] if not isinstance(tab_id, list) else native_ids
    command_ids: int | list[int] = native_ids[0] if not isinstance(tab_id, list) else native_ids
    payload: dict[str, Any] = {
        "cmd": "tabs",
        "method": "close",
        "tabId": command_ids,
    }
    if expected_generations:
        payload["expectedGenerations"] = expected_generations
    result = driver.ext_cmd(
        payload, client_id=client_id, timeout=20.0
    )
    info = _extension_data(result)
    if info.get("ok") is False:
        raise RuntimeError(info.get("error") or "the browser refused to close the tab")
    raw_closed = info.get("closed")
    raw_already_gone = info.get("alreadyGone", info.get("already_gone"))
    if only_if_agent_owned and (
        not isinstance(raw_closed, list)
        or not isinstance(raw_already_gone, list)
    ):
        raise RuntimeError(
            "close_tabs refused: the extension response omitted explicit closed and "
            "alreadyGone lists; ownership was retained"
        )
    closed_ids = (
        [int(value) for value in raw_closed]
        if isinstance(raw_closed, list)
        else list(native_ids)
    )
    already_gone_ids = (
        [int(value) for value in raw_already_gone]
        if isinstance(raw_already_gone, list)
        else []
    )
    if only_if_agent_owned and owner_id is not None:
        _TAB_OWNERSHIP.release(session_ids, owner_id=str(owner_id))
    invalidate_sessions_cache()
    single = not isinstance(tab_id, list)
    closed: int | list[int] = (
        closed_ids[0] if single and closed_ids else [] if single else closed_ids
    )
    already_gone: int | list[int] = (
        already_gone_ids[0]
        if single and already_gone_ids
        else [] if single else already_gone_ids
    )
    status = "already_gone" if only_if_agent_owned and not closed_ids and already_gone_ids else "ok"
    closed_by = (
        "user" if status == "already_gone"
        else "agent" if only_if_agent_owned and closed_ids
        else "none"
    )
    out = {
        "status": status,
        "requested": requested,
        "closed": closed,
        "already_gone": already_gone,
        "closed_by": closed_by,
        "owner_id": str(owner_id) if only_if_agent_owned else None,
        "only_if_agent_owned": bool(only_if_agent_owned),
        "result": result,
    }
    if rebound and rebound.get("rebound_from"):
        out.update({
            "rebound_from": rebound.get("rebound_from"),
            "replacement_session_id": rebound.get("replacement_session_id"),
            "tab_identity": rebound.get("tab_identity"),
            "rebind_reason": rebound.get("reason"),
        })
    if already_gone_ids:
        # A generation-safe close can prove only that the owned native tab is
        # gone. It must not infer that a newly registered tab is its successor
        # and close that tab on the agent's behalf.
        out.update({
            "recheck_required": True,
            "recheck_action": "list_all_tabs",
            "recheck_hint": (
                "The owned native tab disappeared before close. Re-run list_all_tabs and "
                "verify URL/title before deciding whether a replacement should be closed; "
                "ownership is not transferred automatically."
            ),
        })
    return out


# --- Tools: switch and activate a tab ----------------------------------------
@mcp.tool(
    description=(
        "Set the target tab for later calls by session id, URL substring, or browser name "
        "('chrome'/'edge'/'opera') without focusing the browser. A URL substring must match "
        "exactly one tab; pass its full session_id when several tabs match. Use activate=true "
        "or activate_tab when foreground work is required."
    )
)
def switch_tab(
    session_id: Optional[str] = None,
    url_pattern: Optional[str] = None,
    browser: Optional[str] = None,
    activate: bool = False,
) -> dict[str, Any]:
    requested_sid = str(session_id) if session_id is not None else None
    sid = switch_session(session_id=session_id, url_pattern=url_pattern, browser=browser)
    publish_default = getattr(require_driver(), "publish_default_session_id", None)
    if callable(publish_default):
        publish_default(sid)
    out: dict[str, Any] = {"active_session_id": sid}
    if requested_sid is not None and requested_sid != sid:
        current = next(
            (item for item in active_sessions(fresh=True) if str(item.get("id")) == sid),
            {},
        )
        out.update({
            "rebound_from": requested_sid,
            "replacement_session_id": sid,
            "tab_identity": current.get("tab_identity"),
            "rebind_reason": "chrome.tabs.onReplaced",
        })
    if activate:
        try:
            out["activated"] = _activate(sid)
            time.sleep(0.3)  # let the window manager finish raising the window
        except Exception as e:
            out["activation_failed"] = str(e)
    out["tabs"] = compact_tabs()
    return out


def _activate(session_id: Optional[str] = None) -> dict[str, Any]:
    """Bring a tab to the front for real (foreground tab + focused window).

    Reports whether the tab genuinely ended up on screen. Making the tab active
    always "succeeds" even when its window is minimized, and physical input then
    lands somewhere else entirely, so the honest answer needs the window state
    rather than just the absence of an error.
    """
    driver = require_driver()
    sid = str(session_id) if session_id else driver.default_session_id
    if not sid:
        raise RuntimeError("no target session; run list_tabs then switch_tab first")
    client_id = sid.rsplit(":", 1)[0] if ":" in sid else None
    tab_id = int(sid.rsplit(":", 1)[-1])
    reply = driver.ext_cmd({"cmd": "tabs", "method": "switch", "tabId": tab_id},
                           client_id=client_id, timeout=15.0)
    out: dict[str, Any] = {"activated_session_id": sid, "tab_id": tab_id}
    # ext_cmd returns {'data': <handler reply>} both locally and over HTTP
    # (the bridge's ext_cmd wraps its own result the same way), so 'data' is
    # where onScreen actually lives. 'r' is a legacy unwrap some callers did
    # on the remote path; accept it as a fallback, never as the primary key.
    info = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(info, dict):
        info = reply.get("r") if isinstance(reply, dict) else None
    if not isinstance(info, dict):
        info = reply if isinstance(reply, dict) else {}
    # Older extension builds answer a bare {ok:true} and cannot tell us; say so
    # rather than implying the tab is on screen.
    if "onScreen" in info:
        out["on_screen"] = bool(info["onScreen"])
        out["window_state"] = info.get("windowState")
        if info.get("wasMinimized"):
            out["was_minimized"] = True
        if not info["onScreen"]:
            out["warning"] = ("window is still not on screen; screen-coordinate clicks and "
                              "desktop screenshots will not hit this tab")
    else:
        out["on_screen"] = None
        out["note"] = "extension predates window-state reporting; reload it to get this"
    return out


@mcp.tool(
    description=(
        "Bring a tab to the foreground and focus its window. Use this explicitly after "
        "switch_tab when foreground work is required, or to re-raise a tab the user has "
        "since clicked away from."
    )
)
def activate_tab(session_id: Optional[str] = None) -> dict[str, Any]:
    # Do NOT switch_session here: _activate resolves the tab from session_id
    # directly, and switching would leave the shared default parked on this tab
    # (stealing a concurrent task's target) for no benefit.
    out = _activate(session_id)
    time.sleep(0.3)  # let the window manager finish raising the window
    return {"status": "ok", **out}


# --- Navigation: open_url and its dialog policy ------------------------------
def _session_url(session_id: str) -> str:
    for session in active_sessions():
        if str(session.get("id")) == str(session_id):
            return str(session.get("url") or "")
    return ""


def _lab_auto_accepts_beforeunload(
    session_id: str,
    session_url: Optional[str] = None,
) -> bool:
    if _automation_mode() != "lab":
        return False
    try:
        current_url = _session_url(session_id) if session_url is None else session_url
        host = (urlsplit(current_url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host and any(marker in host for marker in _auto_beforeunload_hosts()))


@mcp.tool(
    description=(
        "Navigate the current real-browser tab through CDP without raising its window. "
        "beforeunload defaults to dismiss, except lab mode auto-accepts configured shell/IDE "
        "hosts. Use accept to leave explicitly, manual to inspect, or intent_leave=false to "
        "force the conservative dismiss behavior even on a lab auto host."
    )
)
def open_url(
    url: str,
    session_id: Optional[str] = None,
    timeout: float = 15.0,
    beforeunload: str = "dismiss",
    intent_leave: Optional[bool] = None,
) -> dict[str, Any]:
    timeout = _positive_timeout(timeout)
    deadline = time.monotonic() + timeout
    policy = _validate_dialog_policy(beforeunload)
    driver = require_driver()
    prev_default = driver.default_session_id
    session_budget = deadline - time.monotonic()
    if session_budget <= 0:
        raise TimeoutError("open_url total deadline exhausted before session resolution")
    sessions = ensure_sessions(
        timeout=session_budget,
        fresh=True,
        prune_default=False,
    )
    if deadline - time.monotonic() <= 0:
        raise TimeoutError("open_url total deadline exhausted during session resolution")
    if session_id is not None:
        requested_sid = str(session_id)
        target_session = next(
            (item for item in sessions if str(item.get("id")) == requested_sid),
            None,
        )
        if target_session is None:
            raise _session_target_not_found(requested_sid, sessions)
        target_sid = requested_sid
        driver.default_session_id = target_sid
    else:
        current_sid = str(prev_default) if prev_default is not None else None
        target_session = next(
            (item for item in sessions if str(item.get("id")) == current_sid),
            None,
        )
        if target_session is None:
            candidates = sessions
            preferred_browser = os.environ.get(
                "BROWSERTAP_PREFERRED_BROWSER", ""
            ).strip().lower()
            if preferred_browser:
                preferred = [
                    item for item in sessions
                    if str(item.get("browser", "")).lower() == preferred_browser
                ]
                if preferred:
                    candidates = preferred
            candidates = _browser_candidates(candidates, current=current_sid)
            target_session = candidates[0]
        target_sid = str(target_session["id"])
        driver.default_session_id = target_sid
    client_id, tab_id = _split_session_target(target_sid)
    auto_policy = bool(
        policy == "dismiss" and intent_leave is not False
        and _lab_auto_accepts_beforeunload(
            target_sid, str(target_session.get("url") or "")
        )
    )
    effective_policy = "accept" if auto_policy else policy
    fallback_used = False
    try:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("open_url total deadline exhausted before navigation")
            response = driver.ext_cmd(
                {
                    "cmd": "navigate",
                    "tabId": tab_id,
                    "url": url,
                    "beforeunload": effective_policy,
                    "timeoutMs": max(1, int(remaining * 1000)),
                },
                client_id=client_id,
                timeout=remaining,
            )
            result = _extension_data(response)
        except Exception as route_error:
            # A timed-out navigation has unknown outcome and may already have
            # changed the page. Only an explicit unsupported-route response is
            # safe to resend through CDP.
            if not _unknown_command_error(route_error):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "open_url total deadline exhausted before CDP fallback"
                ) from route_error
            navigation = _direct_cdp(
                "Page.navigate", {"url": url}, session_id=target_sid,
                client_id=client_id, tab_id=tab_id, timeout=remaining,
                deadline=deadline,
            )
            fallback_used = True
            landing: dict[str, Any] = {}
            landing_error: Optional[str] = None
            landing_observed = False
            before_url = str(target_session.get("url") or "")
            while deadline - time.monotonic() > 0:
                remaining = deadline - time.monotonic()
                try:
                    evaluation = _direct_cdp(
                        "Runtime.evaluate",
                        {
                            "expression": "({url: location.href, title: document.title})",
                            "returnByValue": True,
                        },
                        session_id=target_sid,
                        client_id=client_id,
                        tab_id=tab_id,
                        timeout=remaining,
                        deadline=deadline,
                    )
                    if isinstance(evaluation, dict):
                        remote = evaluation.get("result")
                        if isinstance(remote, dict) and isinstance(remote.get("value"), dict):
                            landing = dict(remote["value"])
                except Exception as exc:
                    landing_error = str(exc)
                landed_url = str(landing.get("url") or "")
                if landed_url and (
                    not before_url
                    or landed_url.rstrip("/") != before_url.rstrip("/")
                    or landed_url.rstrip("/") == url.rstrip("/")
                ):
                    landing_observed = True
                    break
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            if landing_observed:
                result = {
                    "status": "ok",
                    "url": str(landing["url"]),
                    "title": str(landing.get("title") or ""),
                    "navigation": navigation,
                    "bridge_route_error": str(route_error),
                }
            else:
                result = {
                    "status": "navigation_timeout",
                    "url": "",
                    "title": "",
                    "navigation": navigation,
                    "bridge_route_error": str(route_error),
                    "landing_error": landing_error or (
                        "navigation completed but the final URL/title could not be read "
                        "within the total deadline"
                    ),
                }
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
        invalidate_sessions_cache()
    out = _classify_navigation_result(result, requested_url=url)
    out["active_session_id"] = target_sid
    if auto_policy:
        out["beforeunload_auto"] = True
        out["beforeunload_policy"] = "accept"
    if fallback_used:
        out["navigation_mode"] = "cdp_fallback"
    if out.get("status") in {
        "blocked_by_beforeunload", "blocked_by_dialog", "dialog_handle_failed",
        "navigation_timeout", "navigation_failed",
    }:
        leave_intended = effective_policy == "accept"
        out.setdefault(
            "hint",
            (
                "Leave intent was detected. Call resolve_leave_dialog for the bounded "
                "protocol retry and lab physical fallback."
                if leave_intended else
                "Navigation was deliberately dismissed to preserve the current page. "
                "Retry with beforeunload='accept' when leaving is intended."
            ),
        )
    return out


_DIALOG_POLICIES = frozenset({"dismiss", "accept", "manual"})

#: Stamped onto every caller-supplied script before it reaches the extension.
#:
#: `background.js` coerces a string `code` field into an object whenever it
#: parses as JSON, and routes anything carrying `cmd` to the internal command
#: router. So a caller passing `{"cmd": "site_permission", ...}` as its *script*
#: reached `setSitePermission` directly -- skipping the `ctx.elicit` approval
#: that `set_site_permission` enforces in `safe` mode, which `SECURITY.md`
#: promises will be asked for on every site-allow action. Nineteen internal
#: commands were reachable that way; `site_permission` is the one with a gate to
#: skip.
#:
#: A leading comment makes `JSON.parse` fail, so a script can only ever take the
#: plain-JS path. `dismiss`/`accept` already got this for free from the
#: dialog-scope comment; `manual` has no token, so it had no prefix and was the
#: reachable path. This marker does not depend on a token, so it covers every
#: policy and also the no-token case where the extension route is unavailable.
#:
#: The extension's scope-token regex is unanchored, so this may precede it.
_JS_WIRE_MARKER = "/*__btap_js*/"


def _mark_js_wire(script: str) -> str:
    """Return *script* prefixed so the extension cannot read it as a command."""
    return f"{_JS_WIRE_MARKER}\n{script}"


# --- Navigation helpers: targets, direct CDP, result classification ----------
def _validate_dialog_policy(policy: Any) -> str:
    if not isinstance(policy, str) or policy not in _DIALOG_POLICIES:
        raise ValueError("dialog policy/action must be one of: dismiss, accept, manual")
    return policy


def _split_session_target(session_id: str) -> tuple[str, int]:
    sid = str(session_id)
    if ":" not in sid:
        raise ValueError(f"invalid composite session id: {sid!r}")
    client_id, raw_tab_id = sid.rsplit(":", 1)
    if not client_id:
        raise ValueError(f"invalid composite session id: {sid!r}")
    try:
        return client_id, int(raw_tab_id)
    except ValueError as exc:
        raise ValueError(f"invalid composite session id: {sid!r}") from exc


def _implicit_client_id(session_id: Optional[str] = None) -> Optional[str]:
    if session_id is not None and ":" in str(session_id):
        return str(session_id).rsplit(":", 1)[0]
    driver = require_driver()
    current = driver.default_session_id
    if current and ":" in str(current):
        return str(current).rsplit(":", 1)[0]
    try:
        sessions = active_sessions()
    except Exception:
        sessions = []
    if sessions:
        sessions = _browser_candidates(sessions)
        sid = str(sessions[0].get("id") or "")
        if ":" in sid:
            return sid.rsplit(":", 1)[0]
    return None


def _normalize_tab_targets(
    value: int | str | list[int | str],
    *,
    session_id: Optional[str] = None,
) -> tuple[list[int], Optional[str]]:
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError("at least one tab identifier is required")
    # An explicit composite target owns the browser choice. Seeding this from
    # the shared default first made close_tabs("chrome:7") fail whenever the
    # current default happened to belong to Edge. A supplied session_id remains
    # an explicit constraint; otherwise infer the client only after parsing all
    # target values.
    client_id = _implicit_client_id(session_id) if session_id is not None else None
    native_ids: list[int] = []
    for raw in values:
        item_client: Optional[str] = None
        if isinstance(raw, bool):
            raise ValueError(f"invalid tab identifier: {raw!r}")
        if isinstance(raw, str) and ":" in raw:
            item_client, tab = _split_session_target(raw)
            resolved = _resolve_session_target(require_driver(), str(raw))
            if resolved and str(resolved.get("session_id")) != str(raw):
                replacement = str(resolved["session_id"])
                _TAB_OWNERSHIP.rebind(str(raw), replacement)
                item_client, tab = _split_session_target(replacement)
        else:
            try:
                tab = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid tab identifier {raw!r}; use a numeric tab id or client:tabId session_id"
                ) from exc
        if tab < 0:
            raise ValueError(f"invalid tab identifier: {raw!r}")
        if item_client:
            if client_id and client_id != item_client:
                raise ValueError("all tab identifiers in one call must belong to the same browser client")
            client_id = item_client
        native_ids.append(tab)
    if client_id is None:
        client_id = _implicit_client_id()
    return native_ids, client_id


def _replace_rebound_tab_target(
    value: int | str | list[int | str],
    *,
    old_session_id: str,
    new_session_id: str,
) -> int | str | list[int | str]:
    """Carry a close request across an evidence-backed native tab replacement.

    A caller may supply either the composite session handle or its numeric tab
    id.  Only the old handle/native id belonging to this exact replacement is
    rewritten; unrelated numeric targets remain untouched and are validated by
    ``_normalize_tab_targets`` afterward.
    """
    old_native = _split_session_target(str(old_session_id))[1]
    new_native = _split_session_target(str(new_session_id))[1]

    def replace_one(raw: int | str) -> int | str:
        if isinstance(raw, str) and raw == old_session_id:
            return new_session_id
        if isinstance(raw, bool):
            return raw
        try:
            numeric = int(raw)
        except (TypeError, ValueError):
            return raw
        if numeric == old_native:
            return new_native
        return raw

    if isinstance(value, list):
        return [replace_one(item) for item in value]
    return replace_one(value)


def _unknown_command_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    return "unknown cmd" in message or "unknown command" in message


def _direct_cdp(
    method: str,
    params: dict[str, Any],
    *,
    session_id: str,
    client_id: str,
    tab_id: int,
    timeout: float,
    deadline: Optional[float] = None,
) -> Any:
    driver = require_driver()
    try:
        timeout = _positive_timeout(timeout)
    except ValueError as exc:
        raise TimeoutError("CDP deadline exhausted before dispatch") from exc
    deadline = deadline if deadline is not None else time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    first_budget = remaining()
    if first_budget <= 0:
        raise TimeoutError("CDP deadline exhausted before dispatch")
    payload = {
        "cmd": "cdp",
        "method": method,
        "params": params,
        "tabId": tab_id,
        "timeoutMs": max(1, int(first_budget * 1000)),
    }
    try:
        response = driver.ext_cmd(
            payload, client_id=client_id, timeout=first_budget
        )
    except BaseException as exc:
        fallback_budget = remaining()
        # A timed-out mutation is ambiguous: it may already be running in the
        # extension. Never send the same CDP command through a second route,
        # and never dispatch anything after the shared deadline.
        if isinstance(exc, TimeoutError) or fallback_budget <= 0:
            # Keep the operation handle and reservation facts on the original
            # error so a timed-out command remains observable and recoverable.
            raise
        if not _unknown_command_error(exc):
            raise
        execute = getattr(driver, "execute_js", None)
        if not callable(execute):
            raise
        fallback_payload = dict(payload)
        fallback_payload["timeoutMs"] = max(1, int(fallback_budget * 1000))
        response = execute(
            json.dumps(fallback_payload),
            timeout=fallback_budget,
            session_id=session_id,
        )
    result = _extension_data(response)
    if result.get("ok") is False:
        code = result.get("code") or "cdp_error"
        hint = result.get("hint") or (
            "A cdp_timeout detaches the debugger but may leave page code running; "
            "inspect the operation result before considering a retry. "
            "A debugger_conflict requires closing DevTools or the competing debugger."
        )
        raise RuntimeError(f"{code}: {result.get('error') or 'CDP command failed'}. {hint}")
    # The real extension WS route sends res.data as the result. Therefore a
    # native CDP response such as Page.printToPDF's {data: "<base64>"} arrives
    # here as result={data:"..."}. Only unwrap an explicit extension envelope;
    # blindly unwrapping every data key destroys valid CDP payloads.
    if result.get("ok") is True and "data" in result:
        return result["data"]
    return result


def _extension_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return dict(data) if isinstance(data, dict) else dict(response)


def _response_client_id(response: Any, fallback: Optional[str]) -> Optional[str]:
    """Keep the client namespace when an extension result is unwrapped."""
    if isinstance(response, dict) and response.get("client_id") is not None:
        return str(response["client_id"])
    if isinstance(response, dict) and response.get("clientId") is not None:
        return str(response["clientId"])
    if isinstance(response, dict) and isinstance(response.get("data"), dict):
        nested = response["data"]
        if nested.get("client_id") is not None:
            return str(nested["client_id"])
        if nested.get("clientId") is not None:
            return str(nested["clientId"])
    return fallback


def _classify_navigation_result(
    result: dict[str, Any], requested_url: str
) -> dict[str, Any]:
    out = dict(result)
    out.setdefault("requested_url", requested_url)
    out.setdefault("url", requested_url)
    navigation = out.get("navigation")
    is_download = bool(
        out.get("is_download") is True
        or out.get("isDownload") is True
        or (isinstance(navigation, dict) and navigation.get("isDownload") is True)
    )
    if is_download:
        out.update({
            "type": "download",
            "status": "triggered",
            "is_download": True,
        })
        out.setdefault(
            "hint",
            "The browser accepted this as a download. net::ERR_ABORTED can be normal "
            "when Page.navigate reports isDownload=true; use download_file for completion "
            "status and the final local path.",
        )
        return out
    dialog = out.get("dialog")
    action = out.get("dialog_action")
    terminal_statuses = {
        "navigation_timeout",
        "navigation_failed",
        "dialog_handle_failed",
        "blocked_by_beforeunload",
        "blocked_by_dialog",
    }
    if out.get("status") in terminal_statuses:
        return out
    if (out.get("handle_error") or
            (isinstance(dialog, dict) and action != "manual"
             and out.get("handled") is False)):
        out["status"] = "dialog_handle_failed"
        return out
    if isinstance(dialog, dict):
        if action == "manual":
            out["status"] = "blocked_by_dialog"
        elif dialog.get("type") == "beforeunload" and action == "dismiss":
            out["status"] = "blocked_by_beforeunload"
        else:
            out["status"] = "ok"
    else:
        landed = out.get("url")
        if isinstance(landed, str) and landed.rstrip("/") != requested_url.rstrip("/"):
            out["status"] = "redirected"
            out.setdefault(
                "note",
                "The final URL differs from the request; verify that the redirect or sign-in destination is expected.",
            )
        else:
            out.setdefault("status", "ok")
    return out


# --- Tools: dialogs (native prompts and leave confirmations) -----------------
@mcp.tool(
    description=(
        "Inspect or handle a JavaScript dialog on the requested real-browser tab. "
        "action is dismiss, accept, or manual; manual reports the dialog without choosing."
    )
)
def handle_dialog(
    action: str,
    prompt_text: str = "",
    session_id: Optional[str] = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    policy = _validate_dialog_policy(action)
    driver = require_driver()
    prev_default = driver.default_session_id
    target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
    client_id, tab_id = _split_session_target(target_sid)
    try:
        response = driver.ext_cmd(
            {
                "cmd": "handle_dialog",
                "tabId": tab_id,
                "action": policy,
                "promptText": prompt_text,
            },
            client_id=client_id,
            timeout=max(0.5, min(float(timeout), 3.0)),
        )
        result = _extension_data(response)
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    if result.get("ok") is False:
        message = str(result.get("error") or "")
        normalized = message.lower()
        try:
            decoded_error = json.loads(message)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded_error = None
        if isinstance(decoded_error, dict):
            nested_message = decoded_error.get("message")
            if nested_message:
                message = f"{message} ({nested_message})"
                normalized = message.lower()
        if (
            "no dialog" in normalized
            or "no javascript dialog" in normalized
            or "dialog is not showing" in normalized
        ):
            result["status"] = "no_dialog"
            result.setdefault("handled", False)
            result.setdefault("dialog", None)
        else:
            result["status"] = "dialog_handle_failed"
    result.setdefault("status", "blocked_by_dialog" if policy == "manual" else "ok")
    result["active_session_id"] = target_sid
    if result.get("status") in {"blocked_by_dialog", "dialog_handle_failed"}:
        result.setdefault(
            "hint",
            "The dialog is still open. For an intended page leave, call resolve_leave_dialog; otherwise choose accept or dismiss explicitly.",
        )
    elif result.get("status") == "no_dialog":
        result.setdefault("url", _session_url(target_sid))
    return result


@mcp.tool(
    description=(
        "Resolve an intended beforeunload leave in one bounded workflow: protocol accept twice, "
        "return immediately when no dialog exists, then use a lab-only foreground Enter fallback "
        "after the normal physical-input approval gate only when protocol handling actually fails. "
        "Approval failures expose reason in the result and diagnostics: elicitation_unsupported, "
        "declined, timeout, cancelled or error."
    )
)
async def resolve_leave_dialog(
    ctx: Context,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    target_sid = await anyio.to_thread.run_sync(
        lambda: switch_session(session_id=session_id)
        if session_id is not None
        else switch_session()
    )
    attempts: list[dict[str, Any]] = []
    for _ in range(2):
        try:
            result = await anyio.to_thread.run_sync(
                lambda: handle_dialog("accept", session_id=target_sid, timeout=3.0)
            )
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        if result.get("status") == "error":
            message = str(result.get("error") or "")
            normalized = message.lower()
            try:
                decoded_error = json.loads(message)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded_error = None
            if isinstance(decoded_error, dict) and decoded_error.get("message"):
                normalized = f"{normalized} {decoded_error['message']}".lower()
            if (
                "no dialog" in normalized
                or "no javascript dialog" in normalized
                or "dialog is not showing" in normalized
            ):
                result["status"] = "no_dialog"
                result.setdefault("handled", False)
                result.setdefault("dialog", None)
        attempts.append(result)
        if result.get("status") == "no_dialog":
            return {
                **result,
                "status": "no_dialog",
                "resolution": "none",
                "session_id": target_sid,
                "attempts": attempts,
            }
        if result.get("handled") is True or result.get("status") == "ok":
            return {
                "status": "ok",
                "resolution": "protocol",
                "session_id": target_sid,
                "attempts": attempts,
            }
        await anyio.sleep(0.1)

    if attempts and all(
        str(item.get("status") or "") in {"error", "dialog_handle_failed"}
        and (
            isinstance(item.get("error"), str)
            and (
                "timeout" in item["error"].lower()
                or "no response" in item["error"].lower()
                or "did not respond" in item["error"].lower()
            )
        )
        for item in attempts
    ):
        return {
            "status": "no_response",
            "session_id": target_sid,
            "attempts": attempts,
            "retryable": True,
            "hint": "The dialog probe did not answer; no physical Enter was sent. Retry after list_tabs confirms the same owned session.",
        }

    if _automation_mode() != "lab":
        return {
            "status": "requires_user_action",
            "session_id": target_sid,
            "attempts": attempts,
            "hint": "Safe mode does not send a physical leave fallback. Accept the browser dialog manually or switch to lab explicitly.",
        }

    def press_default_leave() -> dict[str, Any]:
        _pyautogui().hotkey("enter")
        return {"status": "ok", "input": "enter"}

    physical = await _run_approved_physical_action(
        ctx,
        "confirm the intended browser beforeunload leave with Enter",
        press_default_leave,
        session_id=target_sid,
        activate_session="current",
    )
    if physical.get("status") == "ok":
        return {
            "status": "ok",
            "resolution": "physical_fallback",
            "session_id": target_sid,
            "attempts": attempts,
            "physical": physical,
            "hint": "The default browser-dialog action was sent; verify the destination URL before continuing.",
        }
    result = {
        "status": "requires_user_action",
        "session_id": target_sid,
        "attempts": attempts,
        "physical": physical,
        "hint": "Click Leave in the browser dialog, then retry the intended navigation.",
    }
    if "reason" in physical:
        result["reason"] = physical["reason"]
    return result


# --- Tool: open_new_tab (owned tabs) -----------------------------------------
@mcp.tool(description=(
    "Create one background browser tab, deduplicated by operation_id. Uses this MCP task's "
    "selected browser; select one with switch_tab or session_id/client_id when several are "
    "connected. Pass active=true for foreground work. Save owner_id for cleanup; ownership "
    "requires a completed record with exact client_id, tab_id and generation. "
    "Before dispatch, an unresolved probe returns unknown, may_have_created=false. "
    "When retry_safe=true, resolve its cause and retry with no operation_id. After uncertain "
    "dispatch, may_have_created=true,retry_safe=false: pass the returned operation_id, "
    "client_id and owner_id to this tool to read the same creation record without replaying. "
    "Failed recovery preserves uncertainty and ownership. A failed probe's separate "
    "reconciliation.bridge_operation.operation_id can be read with get_execute_js_result; "
    "its reservation_held=false does not prove non-creation or permit replay. After worker "
    "restart pending creates become terminal unknown; retired IDs retain replay guards. "
    "If reconciliation.resume_required=false, including initial not_found, follow its "
    "list_tabs() inspection guidance and stop repeating recovery. Matching URLs, unchanged "
    "tab counts, missing records and retired IDs prove neither non-creation nor ownership."
))
def open_new_tab(
    url: str,
    timeout: float = 15.0,
    active: bool = False,
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    operation_id: Optional[str] = None,
    client_id: Optional[str] = None,
) -> dict[str, Any]:
    driver = require_driver()
    timeout = _positive_timeout(timeout)
    deadline = time.monotonic() + timeout
    resuming = operation_id is not None
    if resuming:
        operation_id = str(operation_id).strip()
        if not operation_id:
            raise ValueError("operation_id must not be empty when resuming a tab create")
    else:
        operation_id = f"open-tab-{secrets.token_urlsafe(18)}"

    requested_client_id = str(client_id).strip() if client_id is not None else None
    if client_id is not None and not requested_client_id:
        raise ValueError("client_id must not be empty")
    if session_id is not None:
        session_client_id = _split_session_target(str(session_id))[0]
        if requested_client_id is not None and requested_client_id != session_client_id:
            raise ValueError("client_id and session_id must name the same browser client")
        client_id = session_client_id
    else:
        client_id = requested_client_id
        if client_id is None and driver.default_session_id is not None:
            client_id = _split_session_target(str(driver.default_session_id))[0]

    capability_owner_id = str(owner_id).strip() if owner_id is not None else None
    if owner_id is not None and not capability_owner_id:
        raise ValueError("owner_id must not be empty")
    if capability_owner_id is None:
        # Generate the close capability before dispatch. If the ACK is lost,
        # returning it with the operation id gives the caller a complete,
        # resumable capability instead of an unowned native tab.
        capability_owner_id = _TabOwnershipRegistry._new_owner_id()

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def create_call() -> Any:
        left = remaining()
        if left <= 0:
            raise TimeoutError("open_new_tab total deadline exhausted before create")
        # Leave a meaningful reconciliation window after a lost ACK. The
        # extension itself remains the exactly-once authority for this id.
        ack_timeout = min(left, max(0.001, min(2.0, left * 0.5)))
        return driver.newtab(
            url=url, client_id=client_id, timeout=ack_timeout, active=active,
            operation_id=operation_id,
        )

    def response_info(response: Any) -> tuple[dict[str, Any], Optional[str]]:
        routed_client = _response_client_id(response, client_id)
        info = _extension_data(response)
        nested = info.get("data")
        if isinstance(nested, dict) and (
            "operation_id" in nested or "operation_status" in nested
        ):
            nested_info = dict(nested)
            if info.get("error") and not nested_info.get("error"):
                nested_info["error"] = info["error"]
            info = nested_info
        return info, routed_client

    def operation_state(info: dict[str, Any]) -> str:
        if str(info.get("operation_id") or "") != operation_id:
            return "unknown"
        return str(info.get("operation_status") or "unknown").lower()

    def unknown_result(
        info: Optional[dict[str, Any]] = None,
        *,
        may_have_created: bool,
        retry_safe: bool,
        terminal: bool = False,
    ) -> dict[str, Any]:
        # Reconciliation cannot prove that an earlier call never created a tab.
        # Keep its cleanup capability even when the first status read fails.
        if resuming:
            may_have_created = True
            retry_safe = False
        detail = dict(info or {})
        terminal = terminal or detail.get("resume_required") is False
        detail.setdefault("status", "unknown")
        detail.setdefault("operation_status", "unknown")
        detail.update({
            "operation_id": operation_id,
            "client_id": client_id,
            "may_have_created": may_have_created,
            "retry_safe": retry_safe,
            "resume_required": may_have_created and not terminal,
        })
        out = {
            "status": "unknown",
            "operation_id": operation_id,
            "client_id": client_id,
            "tab_id": detail.get("id", detail.get("tab_id")),
            "generation": detail.get("generation"),
            "session_id": None,
            "ready": False,
            "owned": False,
            "may_have_created": may_have_created,
            "retry_safe": retry_safe,
            "reconciliation": detail,
            "resume_required": detail["resume_required"],
        }
        if may_have_created:
            detail["owner_id"] = capability_owner_id
            out["owner_id"] = capability_owner_id
            out["recovery"] = {
                "operation_id": operation_id,
                "client_id": client_id,
                "owner_id": capability_owner_id,
                "url": url,
                "instruction": (
                    (
                        "The operation record is missing; "
                        if operation_state(detail) == "not_found" else
                        "The operation has a terminal unknown outcome; "
                    ) + "stop repeating this recovery read. "
                    "Call list_tabs() and inspect tabs for the returned client_id. "
                    "A matching URL, an unchanged tab count, or no matching tab does not prove "
                    "ownership or that no tab was created. For cleanup, use "
                    "close_tabs(tab_id=<exact session_id>, owner_id=<returned owner_id>) "
                    "only for an exact client_id/tab_id/generation already registered as owned "
                    "by this MCP task; owner_id alone grants no ownership. If identity or "
                    "ownership cannot be established, leave those tabs untouched and report "
                    "the unresolved outcome; a new create may duplicate the original."
                    if terminal else
                    "Call open_new_tab again with operation_id, client_id and owner_id; "
                    "the recovery call only reads the durable operation record."
                ),
            }
        else:
            out["owner_id"] = None
        return out

    def not_created_result(info: dict[str, Any]) -> dict[str, Any]:
        detail = dict(info)
        detail.update({
            "operation_id": operation_id,
            "client_id": client_id,
            "may_have_created": False,
            "retry_safe": True,
        })
        return {
            "status": "error",
            "operation_id": operation_id,
            "client_id": client_id,
            "tab_id": None,
            "generation": None,
            "session_id": None,
            "ready": False,
            "owned": False,
            "owner_id": None,
            "may_have_created": False,
            "retry_safe": True,
            "error": detail.get("error") or "the browser did not create the tab",
            "reconciliation": detail,
        }

    def status_call() -> Any:
        left = remaining()
        if left <= 0:
            raise TimeoutError("open_new_tab total deadline exhausted before reconciliation")
        # A sleeping MV3 worker or a briefly busy bridge can take more than one
        # second to answer even though the operation registry is healthy. Give
        # each read a useful budget while retaining at least half of the total
        # deadline for create/reconciliation work that follows.
        status_timeout = min(left, max(0.001, min(3.0, left * 0.5)))
        return driver.ext_cmd(
            {"cmd": "tabs", "method": "create_status", "operation_id": operation_id},
            client_id=client_id,
            timeout=status_timeout,
        )

    def probe_error(exc: Exception, phase: str) -> dict[str, Any]:
        detail: dict[str, Any] = {"error": str(exc), "phase": phase}
        _, _, _, diagnostics = _exception_result_metadata(exc)
        if diagnostics.get("operation_id"):
            # This is the bridge's status-query receipt, not the durable tab
            # creation handle. Poll it without replaying a create; its release
            # says nothing about whether an earlier create reached the browser.
            detail["bridge_operation"] = {
                key: diagnostics[key] for key in (
                    "operation_id", "poll_with", "reservation_held", "delivery_state", "retry_safe",
                ) if key in diagnostics
            }
        return detail

    # Pin one concrete extension before the first mutation. This works with no
    # content sessions and prevents create/status from drifting across browsers.
    try:
        probe_response = status_call()
        probe_info, routed_client = response_info(probe_response)
    except AmbiguousBrowserError:
        # Several browser clients with no explicit target is a caller routing
        # error, not an uncertain tab create. Preserve the structured choice
        # prompt instead of hiding it inside an `unknown` reconciliation result.
        raise
    except Exception as exc:
        return unknown_result(
            probe_error(exc, "client_discovery"),
            may_have_created=False,
            retry_safe=True,
        )
    if client_id is not None and routed_client is not None and routed_client != client_id:
        return unknown_result(
            {"error": "client discovery reached a different browser client"},
            may_have_created=False,
            retry_safe=False,
        )
    client_id = routed_client
    if not client_id:
        return unknown_result(
            {"error": "the bridge did not identify the extension client"},
            may_have_created=False,
            retry_safe=True,
        )
    result: Any = None
    last_status = dict(probe_info)
    probe_state = operation_state(probe_info)
    create_attempts = 0 if resuming else 1
    create_timed_out = False

    if resuming:
        # A caller supplied an operation id because a previous create may have
        # happened.  Recovery is deliberately read-only: never replay create,
        # even when the durable record is missing or the first status read is
        # inconclusive.
        if probe_state == "completed":
            result = probe_response
        elif probe_state == "not_found":
            # A readable but missing record gives this recovery call no result
            # to poll. It is not proof of absence: completed records expire and
            # browser restarts clear the store while tabs may be restored.
            return unknown_result(
                {
                    **probe_info,
                    "error": (
                        "operation_id was not found in this browser's operation store; "
                        "it may have expired or belong to another client; inspect tabs "
                        "without replaying tab creation"
                    ),
                    "resume_required": False,
                },
                may_have_created=True,
                retry_safe=False,
                terminal=True,
            )
        elif probe_state != "pending":
            return unknown_result(
                probe_info,
                may_have_created=True,
                retry_safe=False,
            )
    else:
        if probe_state != "not_found":
            return unknown_result(
                probe_info,
                # This operation id was generated locally and no mutation has
                # been dispatched yet, so a structured storage/registry
                # uncertainty in the read-only probe cannot mean that this call
                # created a tab.
                may_have_created=False,
                retry_safe=True,
            )
        try:
            create_response = create_call()
            create_info, routed_client = response_info(create_response)
            if routed_client is not None and routed_client != client_id:
                return unknown_result(
                    {"error": "the create ACK came from a different browser client"},
                    may_have_created=True,
                    retry_safe=False,
                )
            last_status = create_info
            create_state = operation_state(create_info)
            if create_state == "completed":
                result = create_response
            elif create_state == "not_found":
                return not_created_result(create_info)
            elif create_state == "unknown":
                return unknown_result(
                    create_info,
                    may_have_created=bool(create_info.get("may_have_created")),
                    retry_safe=bool(create_info.get("retry_safe", False)),
                )
        except TimeoutError:
            create_timed_out = True
        except Exception as exc:
            last_status = {
                "error": str(exc),
                "phase": "create",
                "operation_id": operation_id,
                "operation_status": "unknown",
            }

    # A direct pending ACK and a lost ACK share the same reconciliation path.
    # Only a timed-out create may have been undelivered, so only that case may
    # resend create once, always with the same operation and pinned client ids.
    while result is None and remaining() > 0:
        try:
            status_response = status_call()
            status_info, routed_client = response_info(status_response)
            if routed_client is not None and routed_client != client_id:
                return unknown_result(
                    {"error": "reconciliation reached a different browser client"},
                    may_have_created=True,
                    retry_safe=False,
                )
            last_status = status_info
        except TimeoutError as exc:
            last_status = {**last_status, **probe_error(exc, "reconciliation")}
            continue
        except Exception as exc:
            last_status = {
                **probe_error(exc, "reconciliation"),
                "operation_id": operation_id,
                "operation_status": "unknown",
            }
            break

        state = operation_state(last_status)
        if state == "completed":
            result = status_response
            break
        if state == "not_found" and resuming:
            return unknown_result(
                {
                    **last_status,
                    "error": (
                        "operation_id disappeared during recovery; do not replay tab creation"
                    ),
                    "resume_required": True,
                },
                may_have_created=True,
                retry_safe=False,
            )
        if state == "not_found" and create_timed_out and create_attempts < 2:
            create_attempts += 1
            try:
                retry_response = create_call()
                create_timed_out = False
                retry_info, routed_client = response_info(retry_response)
                if routed_client is not None and routed_client != client_id:
                    return unknown_result(
                        {"error": "the create retry ACK came from a different browser client"},
                        may_have_created=True,
                        retry_safe=False,
                    )
                last_status = retry_info
                retry_state = operation_state(retry_info)
                if retry_state == "completed":
                    result = retry_response
                    break
                if retry_state == "not_found":
                    return not_created_result(retry_info)
                if retry_state == "unknown":
                    return unknown_result(
                        retry_info,
                        may_have_created=bool(retry_info.get("may_have_created", True)),
                        retry_safe=bool(retry_info.get("retry_safe", False)),
                    )
            except TimeoutError:
                create_timed_out = True
            except Exception as exc:
                last_status = {
                    "error": str(exc),
                    "phase": "create_retry",
                    "operation_id": operation_id,
                    "operation_status": "unknown",
                }
            continue
        if state not in {"pending", "not_found"}:
            break
        if state == "not_found" and create_timed_out and create_attempts >= 2:
            return not_created_result(last_status)
        time.sleep(min(0.05, remaining()))

    if result is None:
        return unknown_result(last_status, may_have_created=True, retry_safe=False)

    info, routed_client = response_info(result)
    if routed_client is not None and routed_client != client_id:
        return unknown_result(
            {"error": "the completed operation belongs to a different browser client"},
            may_have_created=True,
            retry_safe=False,
        )
    if operation_state(info) != "completed":
        return unknown_result(info, may_have_created=True, retry_safe=False)
    record_client = info.get("client_id")
    if record_client is not None and str(record_client) != client_id:
        return unknown_result(
            {**info, "error": "the completed operation record has a different client_id"},
            may_have_created=True,
            retry_safe=False,
        )
    raw_tab_id = info.get("id", info.get("tab_id"))
    generation = str(info.get("generation") or "")
    if raw_tab_id is None or not generation:
        return unknown_result(
            {**info, "error": "completed create result lacks an exact tab_id or generation"},
            may_have_created=True,
            retry_safe=False,
        )
    try:
        tab_id = int(raw_tab_id)
    except (TypeError, ValueError):
        return unknown_result(
            {**info, "error": "completed create result has an invalid tab_id"},
            may_have_created=True,
            retry_safe=False,
        )
    invalidate_sessions_cache()
    expected_sid = f"{client_id}:{tab_id}"
    found_sid: Optional[str] = None
    found_url = str(info.get("url") or url)
    while remaining() > 0:
        left = remaining()
        try:
            sessions = active_sessions(timeout=min(2.0, left), fresh=True)
        except Exception:
            sessions = []
        match = next(
            (
                session
                for session in sessions
                if str(session.get("id")) == expected_sid
                and str(session.get("generation") or "") == generation
            ),
            None,
        )
        if match:
            found_sid = expected_sid
            found_url = str(match.get("url") or found_url)
            break
        time.sleep(min(0.1, remaining()))
    # Exact session+generation registration is the readiness barrier for
    # session-scoped tools.  The extension now acknowledges chrome.tabs.create
    # immediately, so its initial tab.status is commonly "loading"; retaining
    # that snapshot as a second gate would leave a permanently pending result
    # even after the content session has registered and is executable.
    ready = found_sid is not None
    # Restricted pages may never register a content session. The exact native
    # client/id/generation tuple is nevertheless sufficient for safe cleanup.
    ownership = _TAB_OWNERSHIP.register(
        expected_sid,
        generation,
        owner_id=capability_owner_id,
    )
    out: dict[str, Any] = {
        "status": "ok" if ready else "pending",
        "operation_id": operation_id,
        "client_id": client_id,
        "tab_id": tab_id,
        "session_id": found_sid or expected_sid,
        "generation": generation,
        "ready": ready,
        "url": found_url,
        "load_status": info.get("status"),
        "owned": True,
        "opener": "agent",
        "owner_id": ownership["owner_id"],
        "result": result,
    }
    if not ready:
        out["hint"] = (
            "The native tab exists but its exact content session did not register before the "
            "bounded timeout. The returned owner_id can safely close it."
        )
    return out


# --- Tools: extension inventory and enable/disable ---------------------------
@mcp.tool(
    description="Get absolute path to the unpacked Chrome extension directory for manual installation.",
    serialize=False,
)
def extension_path() -> dict[str, Any]:
    return {"extension_path": str(chrome_extension_dir())}


@mcp.tool(
    description="List installed browser extensions (id, name, enabled, type, version). Works with no tabs open.",
    serialize=False,
)
def list_extensions(session_id: Optional[str] = None) -> dict[str, Any]:
    # Addressed to the extension itself, so this answers even with zero tabs;
    # session_id only picks which browser when several are connected.
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    return driver.ext_cmd({"cmd": "management", "method": "list"},
                          client_id=client_id, timeout=20.0)


@mcp.tool(
    description=(
        "Enable or disable an installed extension by id. Chrome exposes no API to INSTALL "
        "an extension, so this only toggles ones already present; use list_extensions for ids. "
        "The BTAP bridge refuses to disable itself -- nothing would be left to re-enable it -- "
        "so ask a human to press Reload on chrome://extensions to pick up a new build."
    )
)
def set_extension_enabled(extension_id: str, enabled: bool,
                          session_id: Optional[str] = None) -> dict[str, Any]:
    response = require_driver().ext_cmd(
        {"cmd": "management", "method": "enable" if enabled else "disable",
         "extId": extension_id},
        client_id=_extension_client_id(session_id), timeout=20.0)
    # This used to return `status: ok` unconditionally with the extension's
    # answer tucked into `result`, so a refusal -- the self-disable guard
    # above all -- read as a completed toggle in the one field a caller
    # checks before moving on. `uninstall_extension` already routed through
    # this helper; the toggle did not.
    return _extension_operation_result(
        response, operation="set_extension_enabled",
        extension_id=extension_id, enabled=enabled)


def _extension_client_id(session_id: Optional[str]) -> Optional[str]:
    return (str(session_id).rsplit(":", 1)[0]
            if session_id and ":" in str(session_id) else None)


def _extension_operation_result(
    response: Any, *, operation: str, **context: Any,
) -> dict[str, Any]:
    raw = response.get("data") if isinstance(response, dict) and "data" in response else response
    result = dict(raw) if isinstance(raw, dict) else {}
    explicit_reply = isinstance(response, dict) and (
        "data" in response or isinstance(response.get("ok"), bool)
    )
    if not explicit_reply and _legacy_failure(result) is None:
        return {
            "status": "error", "code": "malformed_extension_result",
            "error": "The extension returned no recognizable completion receipt; inspect state before retrying.",
            "retry_safe": False, "operation": operation, **context,
        }
    if result.get("ok") is False:
        failure = dict(result)
        failure.setdefault("status", "error")
        failure.setdefault("code", result.get("code") or "extension_operation_failed")
        failure.setdefault("error", result.get("error") or f"{operation} failed")
        failure["operation"] = operation
        failure.update(context)
        return failure
    # The in-process/fake route commonly returns an explicit extension
    # envelope: {data: {ok: true, data: <payload>}}.  The real remote bridge,
    # however, has already removed that inner envelope in BrowserBridge.ext_cmd
    # and returns {data: <payload>} instead.  Preserve both forms.  Treating a
    # direct payload as an envelope used to discard capture snapshots such
    # as {status: "capturing", messages: [...]}, so live Network/Console tools
    # misleadingly returned only the generic operation status.
    if result.get("ok") is True:
        payload = result.get("data") if "data" in result else {
            key: value for key, value in result.items() if key != "ok"
        }
    else:
        payload = raw
    legacy_failure = _legacy_failure(payload)
    if legacy_failure is not None:
        code, message, retryable = legacy_failure
        failed = dict(payload) if isinstance(payload, dict) else {}
        failed.setdefault("status", "error")
        failed.setdefault("code", code)
        failed.setdefault("error", message)
        if retryable and "retryable" not in failed:
            failed["retryable"] = True
        failed["operation"] = operation
        failed.update(context)
        return failed
    return {
        "status": "ok",
        "operation": operation,
        **context,
        **({"data": payload} if payload not in ({}, None) else {}),
    }


# --- Tools: downloads --------------------------------------------------------
def _move_download(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        temporary: Optional[Path] = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".download",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as temporary_file, source.open(
                "rb"
            ) as source_file:
                shutil.copyfileobj(source_file, temporary_file)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            shutil.copystat(source, temporary)
            os.link(temporary, destination)
            temporary.unlink()
            temporary = None
            source.unlink()
            return
        except FileExistsError as exc:
            raise FileExistsError(
                f"download destination already exists: {destination}; "
                "pass overwrite=true to replace it"
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    try:
        os.replace(source, destination)
        return
    except OSError:
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{threading.get_ident()}.download"
        )
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
            source.unlink()
        finally:
            temporary.unlink(missing_ok=True)


@mcp.tool(
    description=(
        "Download an http(s) URL through the real browser's native download manager, so "
        "the current browser profile's cookies and authenticated session are used. Waits "
        "for completion by default and returns the final absolute local path. directory may "
        "be any absolute local directory; completed files are moved there without replacing an "
        "existing file unless overwrite=true. A directory timeout reports directory_applied=false "
        "because Chrome may finish in its default download directory. An explicit session_id "
        "must still be live and is never replaced with another profile. Use this for attachments "
        "instead of page fetch."
    ),
    serialize=False,
)
def download_file(
    url: str,
    filename: Optional[str] = None,
    directory: Optional[str] = None,
    wait: bool = True,
    timeout: float = 60.0,
    session_id: Optional[str] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    parsed = urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    timeout = float(timeout)
    if not 0 < timeout <= 1800:
        raise ValueError("timeout must be between 0 and 1800 seconds")

    relative_name: Optional[Path] = None
    wire_name: Optional[str] = None
    if filename is not None:
        filename = str(filename).strip()
        # Validate what actually goes on the wire, not what came in: the payload
        # below rewrites backslashes to "/", so a native check on POSIX passed
        # "\escape.bin" (one ordinary part) and Chrome received the absolute
        # path "/escape.bin".
        wire_name = filename.replace("\\", "/")
        # Both flavours, so the answer cannot depend on the OS the server happens
        # to run on. PurePosixPath calls "C:escape.bin" an ordinary name while
        # Chrome on Windows reads it as drive-relative; PureWindowsPath is the
        # half that objects. The other direction is "//host/share".
        candidates = (PurePosixPath(wire_name), PureWindowsPath(wire_name))
        if not wire_name or any(
            not candidate.parts
            or candidate.anchor
            or candidate.is_absolute()
            or ".." in candidate.parts
            for candidate in candidates
        ):
            raise ValueError(
                "filename must be a non-empty relative download name without '..'"
            )
        relative_name = Path(wire_name)

    target_directory: Optional[Path] = None
    if directory is not None:
        target_directory = Path(str(directory)).expanduser()
        if not target_directory.is_absolute():
            raise ValueError("directory must be an absolute path")
        target_directory = target_directory.resolve()
        if not wait:
            raise ValueError("directory requires wait=true")

    client_id: Optional[str] = None
    if session_id is not None:
        explicit_session_id = str(session_id)
        client_id, _ = _split_session_target(explicit_session_id)
        sessions = active_sessions(fresh=True)
        if not any(str(item.get("id")) == explicit_session_id for item in sessions):
            raise _session_target_not_found(explicit_session_id, sessions)
    else:
        client_id = _implicit_client_id()

    payload: dict[str, Any] = {
        "cmd": "downloads",
        "method": "download",
        "url": parsed.geturl(),
        "conflictAction": "overwrite" if overwrite else "uniquify",
        "wait": bool(wait),
        "timeoutMs": max(1, int(timeout * 1000)),
    }
    if wire_name is not None:
        payload["filename"] = wire_name
    response = require_driver().ext_cmd(
        payload,
        client_id=client_id,
        timeout=timeout + 1.0,
    )
    result = _extension_data(response)
    if result.get("ok") is False:
        return {
            "type": "download",
            "status": "failed",
            **({"download_id": result["download_id"]} if result.get("download_id") is not None else {}),
            "error": result.get("error") or "download failed",
            **({"code": result["code"]} if result.get("code") else {}),
        }
    info = result.get("data") if result.get("ok") is True else result
    if not isinstance(info, dict):
        raise RuntimeError("download_file received an invalid extension response")
    status = str(info.get("status") or "failed")
    out: dict[str, Any] = {
        "type": "download",
        "status": status,
        **({"download_id": info["download_id"]} if info.get("download_id") is not None else {}),
        **({"bytes_received": info["bytes_received"]} if info.get("bytes_received") is not None else {}),
        **({"total_bytes": info["total_bytes"]} if info.get("total_bytes") is not None else {}),
    }
    if status == "failed":
        out["error"] = info.get("error") or "download interrupted"
        if info.get("code"):
            out["code"] = info["code"]
        if info.get("hint"):
            out["hint"] = info["hint"]
        return out
    if status != "completed":
        if target_directory is not None:
            out["directory_applied"] = False
            out["requested_directory"] = str(target_directory)
            out["hint"] = (
                "The requested directory move was not applied because the download did not "
                "finish before this call returned. The file may continue downloading into "
                "the browser's default download directory, and this call no longer tracks it."
            )
        else:
            out.setdefault(
                "hint",
                "The browser accepted the download but it did not reach a terminal state before this call returned.",
            )
        return out

    raw_path = info.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("download completed but the browser returned no local path")
    source = Path(raw_path).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(
            f"download completed but the reported local file does not exist: {source}"
        )
    final_path = source
    if target_directory is not None:
        destination_name = relative_name if relative_name is not None else Path(source.name)
        final_path = (target_directory / destination_name).resolve()
        if not final_path.is_relative_to(target_directory):
            raise ValueError("filename must stay within directory")
        _move_download(source, final_path, overwrite=bool(overwrite))
    out["path"] = str(final_path)
    out["size"] = final_path.stat().st_size
    return out


# --- Tools: uninstall extension, bookmarks, raw extension calls --------------
@mcp.tool(
    description=(
        "Uninstall another installed extension by id. show_confirm_dialog defaults to true; "
        "set it false only for an explicitly selected disposable/test extension. The BTAP bridge "
        "cannot uninstall itself through its active connection."
    )
)
def uninstall_extension(
    extension_id: str,
    show_confirm_dialog: bool = True,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    extension_id = str(extension_id).strip()
    if not extension_id:
        raise ValueError("extension_id must not be empty")
    response = require_driver().ext_cmd(
        {
            "cmd": "management",
            "method": "uninstall",
            "extId": extension_id,
            "showConfirmDialog": bool(show_confirm_dialog),
        },
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(
        response,
        operation="uninstall_extension",
        extension_id=extension_id,
        confirmation_requested=bool(show_confirm_dialog),
    )


@mcp.tool(description="Return the browser bookmark tree. Works with no tabs open.")
def get_bookmarks(session_id: Optional[str] = None) -> dict[str, Any]:
    response = require_driver().ext_cmd(
        {"cmd": "bookmarks", "method": "tree"},
        client_id=_extension_client_id(session_id),
        timeout=30.0,
    )
    return _extension_operation_result(response, operation="get_bookmarks")


@mcp.tool(
    description=(
        "Create a bookmark or folder. Supply url for a bookmark; omit url to create a folder. "
        "parent_id is optional and uses Chrome's default bookmark location when omitted."
    )
)
def create_bookmark(
    title: str,
    url: Optional[str] = None,
    parent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    title = str(title).strip()
    if not title:
        raise ValueError("title must not be empty")
    node: dict[str, Any] = {"title": title}
    if url is not None:
        url = str(url).strip()
        if not url:
            raise ValueError("url must not be empty when supplied")
        node["url"] = url
    if parent_id is not None:
        parent_id = str(parent_id).strip()
        if not parent_id:
            raise ValueError("parent_id must not be empty when supplied")
        node["parentId"] = parent_id
    response = require_driver().ext_cmd(
        {"cmd": "bookmarks", "method": "create", "node": node},
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(response, operation="create_bookmark")


@mcp.tool(
    description=(
        "Remove a bookmark by id. Set recursive=true only for a folder whose full subtree "
        "should be removed. First saves the complete subtree to an atomic local JSON backup; "
        "nothing is removed if the snapshot or backup fails. The managed backup subdirectory "
        "must not be a symlink or reparse point. Returns backup_path even if the "
        "deletion outcome is unknown. Backups stay in the state directory for up to 30 days, "
        "100 files or 64 MiB; each subtree is limited to 16 MiB. The snapshot and browser "
        "removal are separate operations, so concurrent edits by the user are not transactional."
    )
)
def remove_bookmark(
    bookmark_id: str,
    recursive: bool = False,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    bookmark_id = str(bookmark_id).strip()
    if not bookmark_id:
        raise ValueError("bookmark_id must not be empty")
    driver = require_driver()
    client_id = _extension_client_id(session_id)
    if session_id is not None and client_id is None:
        raise ValueError("session_id must identify a browser tab as client_id:tab_id")
    with command_scope():
        if client_id is None:
            client_id = driver.select_client_id(timeout=5)
        context: dict[str, Any] = {
            "operation": "remove_bookmark", "bookmark_id": bookmark_id,
            "recursive": bool(recursive), "client_id": client_id,
        }
        try:
            snapshot = driver.ext_cmd(
                {"cmd": "bookmarks", "method": "tree"}, client_id=client_id, timeout=30.0,
            )
            tree_result = _extension_operation_result(snapshot, operation="get_bookmarks")
            if tree_result.get("status") != "ok":
                raise RuntimeError(tree_result.get("error") or "bookmark snapshot failed")
            subtree = bookmark_backup.bookmark_subtree(tree_result.get("data"), bookmark_id)
            backup = bookmark_backup.save_bookmark_backup(
                subtree, client_id=client_id, recursive=bool(recursive),
            )
        except Exception as exc:
            return {
                **context, "status": "error", "code": "bookmark_backup_failed",
                "error": str(exc), "phase": "backup", "dispatched": False,
                "retry_safe": True,
            }
        try:
            response = driver.ext_cmd(
                {"cmd": "bookmarks", "method": "removeTree" if recursive else "remove", "id": bookmark_id},
                client_id=client_id, timeout=20.0,
            )
        except Exception as exc:
            code, message, retryable, diagnostics = _exception_result_metadata(exc)
            retry_safe = retryable and diagnostics.get("delivery_state") == "undelivered"
            return {
                **context, **backup, **diagnostics, "status": "error", "code": code,
                "error": message, "phase": "remove", "retry_safe": retry_safe,
                "hint": "Inspect the bookmark tree and the saved backup before retrying deletion.",
            }
        return _extension_operation_result(response, **context, **backup)


@mcp.tool(
    description=(
        "Send a JSON message from the BTAP extension service worker to another installed "
        "extension. The target must be enabled and list this BTAP extension in "
        "externally_connectable. Works with no tabs open."
    )
)
def call_extension(
    extension_id: str,
    message_json: str,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    extension_id = str(extension_id).strip()
    if not extension_id:
        raise ValueError("extension_id must not be empty")
    try:
        message = json.loads(message_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"message_json must be valid JSON: {exc}") from exc
    response = require_driver().ext_cmd(
        {"cmd": "call_extension", "extId": extension_id, "message": message},
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(
        response,
        operation="call_extension",
        extension_id=extension_id,
    )


def _tab_extension_operation(
    payload: dict[str, Any],
    *,
    operation: str,
    session_id: Optional[str],
    timeout: float,
) -> dict[str, Any]:
    driver = require_driver()
    previous_default = driver.default_session_id
    try:
        if session_id is not None and ":" not in str(session_id):
            tab_ids, client_id = _normalize_tab_targets(str(session_id))
            if client_id is None:
                raise ValueError(
                    "cannot infer browser client for numeric session_id; run switch_tab first"
                )
            target_sid = f"{client_id}:{tab_ids[0]}"
        else:
            target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
            client_id, _ = _split_session_target(target_sid)
        client_id, tab_id = _split_session_target(target_sid)
        command = {**payload, "tabId": tab_id}
        response = driver.ext_cmd(command, client_id=client_id, timeout=timeout)
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default
    result = _extension_operation_result(
        response,
        operation=operation,
        session_id=target_sid,
        tab_id=tab_id,
    )
    data = result.pop("data", None)
    if result.get("status") == "ok" and isinstance(data, dict):
        result.update(data)
    elif data is not None:
        result["data"] = data
    return result


# --- Tools: network and console capture --------------------------------------
@mcp.tool(
    description=(
        "Start bounded CDP Network capture on a real-browser tab. Captures requests, responses, "
        "and optionally response bodies without foregrounding the tab. Call network_capture_stop "
        "from this same MCP session to return the buffer and release the debugger lease. "
        "Another session's capture returns capture_busy; use a separate tab per agent."
    )
)
def network_capture_start(
    session_id: Optional[str] = None,
    include_bodies: bool = True,
    max_entries: int = 500,
    max_body_bytes: int = 262144,
    body_timeout: float = 5.0,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if not 10 <= int(max_entries) <= 2000:
        raise ValueError("max_entries must be between 10 and 2000")
    if not 1024 <= int(max_body_bytes) <= 2097152:
        raise ValueError("max_body_bytes must be between 1024 and 2097152")
    if not 0.1 <= float(body_timeout) <= 10.0:
        raise ValueError("body_timeout must be between 0.1 and 10 seconds")
    return _tab_extension_operation(
        {
            "cmd": "network_capture",
            "method": "start",
            "includeBodies": bool(include_bodies),
            "maxEntries": int(max_entries),
            "maxBodyBytes": int(max_body_bytes),
            "bodyTimeoutMs": int(float(body_timeout) * 1000),
            "timeoutMs": int(float(timeout) * 1000),
        },
        operation="network_capture_start",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Stop Network capture on a real-browser tab, optionally filter returned records by URL, "
        "resource type, HTTP status range, or response-body inclusion, and release its debugger lease. "
        "url_pattern uses the browser's JavaScript RegExp syntax and invalid patterns return a structured error. "
        "Only the MCP session that started the capture may stop it; another session gets capture_busy."
    )
)
def network_capture_stop(
    session_id: Optional[str] = None,
    url_pattern: str = "",
    resource_type: str = "",
    status_min: Optional[int] = None,
    status_max: Optional[int] = None,
    include_response_bodies: bool = True,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if status_min is not None and not 100 <= int(status_min) <= 599:
        raise ValueError("status_min must be between 100 and 599")
    if status_max is not None and not 100 <= int(status_max) <= 599:
        raise ValueError("status_max must be between 100 and 599")
    if status_min is not None and status_max is not None and int(status_min) > int(status_max):
        raise ValueError("status_min must not exceed status_max")
    return _tab_extension_operation(
        {
            "cmd": "network_capture",
            "method": "stop",
            "urlPattern": url_pattern,
            "resourceType": resource_type,
            "statusMin": int(status_min) if status_min is not None else None,
            "statusMax": int(status_max) if status_max is not None else None,
            "includeResponseBodies": bool(include_response_bodies),
        },
        operation="network_capture_stop",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Start a bounded Runtime console and exception capture on a real-browser tab without "
        "foregrounding it. Use get_console_messages while running and console_capture_stop when done, "
        "from this same MCP session. Another session's capture returns capture_busy; use a separate tab per agent."
    )
)
def console_capture_start(
    session_id: Optional[str] = None,
    max_entries: int = 500,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if not 10 <= int(max_entries) <= 5000:
        raise ValueError("max_entries must be between 10 and 5000")
    return _tab_extension_operation(
        {
            "cmd": "console",
            "method": "start",
            "maxEntries": int(max_entries),
            "timeoutMs": int(float(timeout) * 1000),
        },
        operation="console_capture_start",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Read a page of captured console messages and exceptions from a real-browser tab. "
        "Set clear=true to clear the full buffer after reading; only the capture's originating MCP session "
        "may clear it, otherwise capture_busy is returned. Non-clearing reads are shared. "
        "Set filter='user' to exclude extension service-worker / content-script logs "
        "and keep only the page's own main-world console output."
    )
)
def get_console_messages(
    session_id: Optional[str] = None,
    offset: int = 0,
    max_items: int = 200,
    clear: bool = False,
    filter: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    if int(offset) < 0:
        raise ValueError("offset must be non-negative")
    if not 1 <= int(max_items) <= 1000:
        raise ValueError("max_items must be between 1 and 1000")
    normalized_filter = str(filter).strip().lower() if filter else ""
    if normalized_filter and normalized_filter not in {"user", "all"}:
        raise ValueError("filter must be 'user' or 'all'")
    payload: dict[str, Any] = {
        "cmd": "console",
        "method": "get",
        "offset": int(offset),
        "maxItems": int(max_items),
        "clear": bool(clear),
    }
    if normalized_filter == "user":
        payload["filter"] = "user"
    return _tab_extension_operation(
        payload,
        operation="get_console_messages",
        session_id=session_id,
        timeout=timeout,
    )


@mcp.tool(
    description=(
        "Stop console capture on a real-browser tab, return the remaining bounded message "
        "buffer, and release its debugger lease. Only the MCP session that started the capture "
        "may stop it; another session gets capture_busy."
    )
)
def console_capture_stop(
    session_id: Optional[str] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return _tab_extension_operation(
        {"cmd": "console", "method": "stop"},
        operation="console_capture_stop",
        session_id=session_id,
        timeout=timeout,
    )


# A scan is intentionally a read-only operation, but a SPA can answer with a
# fully formed HTML shell while its application is still hydrating. Keep that
# distinction visible to the agent without waiting, polling, or foregrounding
# the tab. The probe is small enough to be safe on every ordinary page.
_RENDER_PROBE_JS = """
(() => {
  const body = document.body;
  let text = body ? String(body.innerText || '').trim() : '';
  // The extension's own visible badge is not application content. Count
  // without mutating the live DOM or subtracting a hidden badge's label.
  const indicator = body ? body.querySelector('#btap-indicator') : null;
  if (indicator && indicator.checkVisibility({visibilityProperty: true})) {
    const label = String(indicator.innerText || '').trim();
    if (label) text = text.replace(label, '').trim();
  }
  const html = body ? String(body.innerHTML || '') : '';
  const loading = !!document.querySelector(
    '[aria-busy="true"], [data-loading="true"], [data-testid*="loading" i]'
  );
  return {
    ready_state: document.readyState,
    has_body: !!body,
    text_chars: text.length,
    html_chars: html.length,
    loading,
    fonts: document.fonts ? document.fonts.status : null,
  };
})()
"""


def _page_render_state(driver: Any, session_id: Optional[str], timeout: float) -> Optional[dict[str, Any]]:
    """Read a best-effort render readiness snapshot without changing the page."""
    execute = getattr(driver, "execute_js", None)
    if not callable(execute):
        return None
    try:
        # This optional built-in read must not keep the tab reserved after
        # scan_page has returned its content without a readiness verdict.
        response = execute(
            _RENDER_PROBE_JS, timeout=timeout, session_id=session_id, read_only_probe=True,
        )
    except Exception:
        # scan_page's primary contract is the page content. An optional probe
        # must never turn a successful read into a transport failure.
        return None
    value = response.get("data") if isinstance(response, dict) else response
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    ready_state = str(value.get("ready_state") or "").lower()
    has_body = bool(value.get("has_body"))
    text_chars = int(value.get("text_chars") or 0)
    html_chars = int(value.get("html_chars") or 0)
    loading = bool(value.get("loading"))
    if not has_body:
        state, content_ready = "no_body", False
    elif ready_state != "complete":
        state, content_ready = "loading", False
    elif loading:
        state, content_ready = "hydrating", False
    elif (
        html_chars >= 10_000
        and text_chars <= 16
    ):
        # A hydrated SPA shell can expose a large React/Vue tree while its
        # actual content is still absent.  The observed failure mode was
        # innerText=8 with hundreds of kilobytes of HTML, so zero text alone
        # is too weak a readiness test.
        state, content_ready = "shell_only", False
    else:
        state, content_ready = "content", True
    return {
        "state": state,
        "content_ready": content_ready,
        "ready_state": ready_state or None,
        "has_body": has_body,
        "text_chars": text_chars,
        "html_chars": html_chars,
        "loading": loading,
        "fonts": value.get("fonts"),
    }


# --- Tool: scan_page ---------------------------------------------------------
@mcp.tool(
    description=(
        "Read the current page as simplified HTML/text, preserving login state from the real "
        "browser. cutlist collapses long repeated lists and reports a CSS selector for each "
        "container it collapsed, derived from that container's own structure. The built-in scan "
        "does not write page attributes, ids, or window globals. Optional extra_js runs caller "
        "code and can modify the page or send requests. "
        "Background tabs may report viewport height zero; ordinary DOM/text/API work still works "
        "there, and only visual/layout fidelity requires explicit activate_tab. "
        "The result also includes render_state/content_ready when the page can be probed: "
        "shell_only or hydrating means the SPA has not produced reliable content yet; retry "
        "scan_page or wait_for before treating an empty result as a real empty page. "
        "A timed-out built-in readiness probe releases its tab reservation; missing render "
        "fields mean readiness is unknown. "
        "Defaults: cutlist=true, maxchars=35000, timeout=15 seconds."
    )
)
def scan_page(
    session_id: Optional[str] = None,
    text_only: bool = False,
    cutlist: bool = True,
    maxchars: int = 35000,
    instruction: str = "",
    extra_js: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    driver = require_driver()
    ensure_sessions()
    # Target a specific tab without permanently clobbering the shared default:
    # the monitor pipeline (get_html) does several driver.execute_js roundtrips
    # that read default_session_id, so we point it at the target for the call's
    # duration and restore it in finally. Otherwise a session_id-scoped scan_page
    # would leave the global default on this tab and hijack other tasks' work.
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    else:
        # Pin down which tab this is about before reading it. In remote mode the
        # bridge would otherwise resolve an unset target on its own, and the
        # answer came back saying active_session_id: null next to content
        # scraped from a real page — leaving the caller unable to say which tab
        # it just read, or to aim a follow-up call at the same one.
        switch_session()
    link_refs: dict[str, str] = {}
    try:
        content = simphtml.get_html(
            driver,
            cutlist=cutlist,
            maxchars=maxchars,
            instruction=instruction,
            extra_js=extra_js,
            text_only=text_only,
            timeout=timeout,
            link_refs=None if text_only else link_refs,
        )
        active = driver.default_session_id
    except simphtml.PageUnavailable as e:
        # The tab never answered. Report that as a failure with the bridge's
        # own diagnosis, instead of an empty page the agent would read as
        # "this site is blank".
        #
        # The operation handle travels with it. A scan that times out may still
        # have a page script running, and dropping the id left the caller with
        # a reserved tab it could neither poll nor release -- the next call on
        # that tab answered target_busy with nothing to name.
        failed: dict[str, Any] = {
            "status": "no_response",
            "active_session_id": driver.default_session_id,
            "tabs": compact_tabs(),
            "error": str(e),
        }
        payload = getattr(e, "payload", None)
        if isinstance(payload, dict):
            failed.update(payload)
        return failed
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    out: dict[str, Any] = {
        "status": "success",
        "active_session_id": active,
        "tabs": compact_tabs(),
        "content": content,
    }
    try:
        probe_timeout = min(1.5, max(0.25, _positive_timeout(timeout) / 4.0))
    except (TypeError, ValueError):
        probe_timeout = 1.0
    render = _page_render_state(driver, active, probe_timeout)
    if render is not None:
        out["render"] = render
        out["render_state"] = render["state"]
        out["content_ready"] = render["content_ready"]
        if not render["content_ready"]:
            render_hint = (
                f"The page currently reports render_state={render['state']} "
                "(ready content is not confirmed). Retry scan_page or wait_for before "
                "acting on an empty/shell response."
            )
            out["hint"] = f"{out['hint']} {render_hint}" if out.get("hint") else render_hint
    # Long hrefs in `content` were shortened to '#r1'-style refs; hand back the
    # real URLs so links stay usable (open_url) instead of being unreachable.
    if link_refs:
        out["links"] = {ref: url for url, ref in link_refs.items()}
    off = _offscreen_note(content)
    if off:
        out["offscreen"] = off
        if off["viewport_height"] == 0:
            # A background tab measures as zero-height, so the whole page counts
            # as "offscreen" and the numbers mean nothing. Say so rather than
            # reporting a bogus count as fact.
            out["hint"] = (
                "This tab is in the background (viewport height is zero), so visibility/layout measurements are "
                "unreliable. Ordinary DOM/text/API work can continue without foregrounding it; use text_only=true "
                "or execute_js for content. Only call activate_tab when you explicitly need visual/layout fidelity."
            )
        else:
            out["hint"] = (
                f"{off['elements']} rendered element(s) more than 5000px outside the viewport were omitted "
                f"(scrollY={off['scroll_y']}, viewport height={off['viewport_height']}, "
                f"document height={off['doc_height']}). "
                "If the target is missing, run scroll_page and scan_page again."
            )
    return out


_OFFSCREEN_RE = re.compile(
    r"<!--btap-offscreen:(\d+) scrollY:(-?\d+) viewH:(\d+) docH:(\d+)-->")


def _offscreen_note(content: Any) -> Optional[dict[str, int]]:
    """Pull the pageOutline offscreen marker out of the page HTML, if present."""
    if not isinstance(content, str):
        return None
    m = _OFFSCREEN_RE.search(content)
    if not m:
        return None
    return {
        "elements": int(m.group(1)),
        "scroll_y": int(m.group(2)),
        "viewport_height": int(m.group(3)),
        "doc_height": int(m.group(4)),
    }


# --- Tools: wait_for, wait_for_url, scroll_page ------------------------------
_WAIT_CALL_TIMEOUT_SECONDS = 6.0
_WAIT_RESULT_MARGIN_SECONDS = 0.5
_WAIT_POLL_INTERVAL_SECONDS = 0.1


def _poll_wait_condition(
    script: str, target_session: Optional[str], timeout: float, *, read_only_probe: bool = False,
    probe: Optional[Callable[[float], dict[str, Any]]] = None,
) -> tuple[dict[str, Any], Optional[str], dict[str, Any], int]:
    """Poll synchronously; a delayed reply is collected without replaying JS."""
    started = time.monotonic()
    deadline = started + timeout
    info: dict[str, Any] = {}
    pending: dict[str, Any] = {}
    last_error: Optional[str] = None
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        delay = _WAIT_POLL_INTERVAL_SECONDS
        try:
            response = None
            if pending:
                collect = getattr(require_driver(), "get_execute_js_result", None)
                if not callable(collect):
                    break
                snapshot = collect(
                    pending["operation_id"], timeout=min(remaining, _WAIT_CALL_TIMEOUT_SECONDS),
                )
                for key in ("reservation_held", "delivery_state"):
                    if key in snapshot:
                        pending[key] = snapshot[key]
                if snapshot.get("status") == "success":
                    pending = {}
                    response = snapshot
                elif snapshot.get("status") != "in_progress":
                    # Expiry or a lost/failed result is not a false condition.
                    # Return the original receipt instead of dispatching again.
                    pending["operation_status"] = snapshot.get("operation_status") or snapshot.get("status")
                    last_error = str(snapshot.get("error") or snapshot.get("status"))
                    break
            else:
                call_timeout = min(remaining, _WAIT_CALL_TIMEOUT_SECONDS)
                first_budget, _reserved = simphtml.undelivered_retry_split(call_timeout)
                # The worker needs time to publish the result after evaluation.
                # A tiny final probe would leave a reservation past this timeout.
                if first_budget <= _WAIT_RESULT_MARGIN_SECONDS:
                    time.sleep(remaining)
                    break
                response = probe(call_timeout) if probe is not None else exec_js(
                    script, session_id=target_session, timeout=call_timeout, read_only_probe=read_only_probe,
                )
            if response is not None:
                raw = response.get("data")
                decoded = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(decoded, dict):
                    raise ValueError("wait probe returned no condition snapshot")
                info = decoded
                last_error = None
        except Exception as exc:
            last_error = str(exc)
            info = {}
            delay = 0.3
            _code, _message, _retryable, details = _exception_result_metadata(exc)
            if pending:
                break
            if details.get("operation_id") and details.get("delivery_state") != "undelivered":
                pending = {
                    "operation_id": details["operation_id"],
                    "delivery_state": details.get("delivery_state", "sent_unconfirmed"),
                    "reservation_held": details.get("reservation_held"),
                    "retry_safe": False,
                    "poll_with": "get_execute_js_result",
                }
        if info.get("met") or info.get("terminal"):
            break
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            time.sleep(min(delay, remaining))
    if pending.get("reservation_held") is False and last_error:
        last_error = last_error.replace(_PENDING_OPERATION_HINT, _RELEASED_WAIT_HINT)
    return info, last_error, pending, int((time.monotonic() - started) * 1000)


@mcp.tool(
    description=(
        "Wait until a condition holds on the page, then return. Use this instead of "
        "polling scan_page (each scan re-serializes the whole DOM). Exactly one of "
        "selector / text / url_pattern / js must be given: selector waits for a CSS or "
        "structured-locator match, including nested same-origin/cross-origin iframe paths. "
        "Role locators exclude hidden and inert controls; "
        "disabled visible controls can still be observed. Text waits for a substring "
        "in body text, url_pattern for a regex on the URL, "
        "js for a JS expression to become truthy. Caller js is evaluated repeatedly and can "
        "have side effects; use a read-only predicate. The server schedules short synchronous "
        "page checks under one deadline. A delayed reply returns its operation_id for "
        "get_execute_js_result; it is never replayed while pending. Timed-out "
        "top-document selector/text/URL checks can release the tab while keeping that receipt: "
        "reservation_held=false permits another command. Framed checks and caller-provided js may stay reserved; "
        "when reservation_held is true or unknown, collect the original operation first."
    )
)
def wait_for(
    selector: Optional[str | dict[str, Any]] = None,
    text: Optional[str] = None,
    url_pattern: Optional[str] = None,
    js: Optional[str] = None,
    timeout: float = 15.0,
    gone: bool = False,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    timeout = _positive_timeout(timeout)
    given = [n for n, v in (("selector", selector), ("text", text),
                            ("url_pattern", url_pattern), ("js", js)) if v]
    if len(given) != 1:
        raise ValueError(
            f"pass exactly one of selector/text/url_pattern/js (got {given or 'none'})")
    kind = given[0]
    # The `is not None` is redundant at runtime -- `kind == "selector"` already
    # implies `selector` was truthy, since `given` only lists truthy arguments --
    # but that proof lives in the comprehension above, out of reach here. Kept
    # alongside the `kind` test rather than replacing it: `selector=""` with
    # another locator set makes `kind` something else while `selector` is still
    # not None, so the value test alone would normalise an empty selector.
    normalized_selector = (
        normalize_locator(selector) if kind == "selector" and selector is not None else None
    )
    framed = isinstance(normalized_selector, dict) and bool(normalized_selector.get("frame"))
    deadline = time.monotonic() + timeout
    driver = require_driver()
    if not framed:
        ensure_sessions()
    prev_default = driver.default_session_id
    target_session = None
    if framed:
        target_session = _resolve_page_input_session(
            driver, session_id, deadline=deadline, operation="wait_for",
        )
    elif session_id is not None:
        target_session = switch_session(session_id=session_id)
    # Only condition evaluation runs in-page; scheduling belongs to the server
    # so background-tab timer throttling cannot strand a page promise.
    probe = {
        "selector": "!!document.querySelector(SEL)",
        "text": "(document.body ? document.body.innerText : '').includes(SEL)",
        "url_pattern": "new RegExp(SEL).test(location.href)",
        "js": "(SEL)",
    }[kind]
    expr = probe.replace("SEL", json.dumps(selector or text or url_pattern)
                         if kind != "js" else (js or "false"))
    structured_probe = None
    if kind == "selector" and isinstance(normalized_selector, dict):
        structured_probe = locator_query_script(normalized_selector)
    if gone and structured_probe is None:
        expr = f"!({expr})"
    structured_check = ""
    detail_fields = ""
    if structured_probe is not None:
        located_condition = "located.status === 'not_found'" if gone else "!!located.found"
        structured_check = (
            f"const located = ({structured_probe}); "
            f"ok = {located_condition}; detail = located;"
        )
        detail_fields = (
            ", locator_status: detail && detail.status, "
            "matches: detail && detail.matches, stage: detail && detail.stage"
        )
    script = f"""
    return (() => {{
      let ok = false, err = null, detail = null;
      try {{ {structured_check or f'ok = !!({expr});'} }} catch (e) {{ err = String(e && e.message || e); }}
      return JSON.stringify({{met: ok, error: err, url: location.href,
        title: document.title, ready: document.readyState{detail_fields}}});
    }})()
    """
    try:
        frame_probe = None
        if framed and isinstance(normalized_selector, dict) and target_session is not None:
            frame_payload = frame_locator_payload(normalized_selector, action="query", gone=gone)

            def frame_probe(call_timeout: float) -> dict[str, Any]:
                data = _call_frame_locator(
                    frame_payload, str(target_session),
                    min(deadline, time.monotonic() + call_timeout),
                )
                if data.get("status") == "stale_extension":
                    data = {"met": False, "locator_status": "stale_extension",
                            "error": data.get("next_action"), "terminal": True}
                return {"data": data}

        info, last_error, pending, waited_ms = _poll_wait_condition(
            script, target_session,
            max(0.0, deadline - time.monotonic()) if framed else timeout,
            read_only_probe=kind != "js",
            **({"probe": frame_probe} if framed else {}),
        )
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    met = bool(info.get("met"))
    out: dict[str, Any] = {
        "status": "success" if met else "timeout",
        "condition": f"{kind}{' gone' if gone else ''}",
        "waited_ms": waited_ms,
        "url": info.get("url"),
        "title": info.get("title"),
        **pending,
    }
    if not met:
        if info.get("locator_status"):
            out["locator_status"] = info["locator_status"]
        if info.get("matches") is not None:
            out["matches"] = info["matches"]
        if info.get("stage"):
            out["stage"] = info["stage"]
        if info.get("error"):
            out["error"] = info["error"]
        elif last_error:
            out["error"] = f"The page was repeatedly unavailable while waiting: {last_error}"
        out["hint"] = (
            (_RELEASED_WAIT_HINT if pending.get("reservation_held") is False else _PENDING_OPERATION_HINT)
            if pending else
            "The condition was not met before timeout. Verify the selector or text, or inspect the page with scan_page."
        )
    return out


@mcp.tool(
    description=(
        "Wait for navigation to settle: blocks until the tab's URL matches url_pattern "
        "(regex, or plain substring) and — unless wait_ready=false — document.readyState is "
        "'complete', then returns the final url, title and readyState. Use this after a click "
        "or open_url that navigates; wait_for(url_pattern=...) only checks the URL and can "
        "return while the new document is still blank. The server schedules short synchronous "
        "page checks. Delayed replies retain their operation_id for get_execute_js_result. "
        "A timed-out probe can release the tab without losing its receipt: "
        "reservation_held=false permits another command while the original reply remains collectible. "
        "When reservation_held is true or unknown, collect the original operation first."
    )
)
def wait_for_url(
    url_pattern: str,
    timeout: float = 15.0,
    session_id: Optional[str] = None,
    wait_ready: bool = True,
) -> dict[str, Any]:
    pattern = str(url_pattern or "")
    if not pattern.strip():
        raise ValueError("url_pattern must not be empty")
    timeout = _positive_timeout(timeout)
    # The condition is evaluated by the browser, so JavaScript RegExp syntax is
    # authoritative here. Python's ``re`` accepts a different language (and
    # rejects valid JS features such as named groups). An invalid JavaScript
    # pattern is still a valid literal substring under this tool's contract.
    driver = require_driver()
    ensure_sessions()
    prev_default = driver.default_session_id
    target_session = None
    if session_id is not None:
        target_session = switch_session(session_id=session_id)
    # 正则匹配不上时退一步按子串匹配：调用方多半直接贴了一个 URL 进来（'?'、'.'
    # 在正则里另有含义），静默等不到不如两种都试。
    pattern_json = json.dumps(pattern)
    probe = (
        "(() => { const pattern = " + pattern_json + "; "
        "try { return new RegExp(pattern).test(location.href) || location.href.includes(pattern); } "
        "catch (_) { return location.href.includes(pattern); } })()"
    )
    if wait_ready:
        probe = f"({probe} && document.readyState === 'complete')"
    script = f"""
    return (() => {{
      let ok = false, err = null;
      try {{ ok = !!{probe}; }} catch (e) {{ err = String(e && e.message || e); }}
      return JSON.stringify({{met: ok, error: err, url: location.href,
        title: document.title, ready: document.readyState}});
    }})()
    """
    try:
        info, last_error, pending, waited_ms = _poll_wait_condition(
            script, target_session, timeout, read_only_probe=True,
        )
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    met = bool(info.get("met"))
    out: dict[str, Any] = {
        "status": "success" if met else "timeout",
        "url_pattern": pattern,
        "waited_ms": waited_ms,
        "url": info.get("url"),
        "title": info.get("title"),
        "ready_state": info.get("ready"),
        "waited_for_ready": bool(wait_ready),
        **pending,
    }
    if not met:
        if info.get("error"):
            out["error"] = info["error"]
        elif last_error:
            out["error"] = f"The page was repeatedly unavailable while waiting: {last_error}"
        landed = info.get("url")
        out["hint"] = (
            (_RELEASED_WAIT_HINT if pending.get("reservation_held") is False else _PENDING_OPERATION_HINT)
            if pending else
            f"Timed out: current URL {landed} (readyState={info.get('ready')}) does not match url_pattern"
            if landed else
            "Timed out and could not read the current URL. The tab may be suspended or disconnected; confirm it with list_tabs first.")
    return out


@mcp.tool(
    description=(
        "Scroll the page and report the new position. scan_page omits anything past "
        "±5000px from the current scroll offset, so on a long page: scan, then scroll, "
        "then scan again. Pass to='bottom'/'top', a pixel offset, or a CSS selector to "
        "bring into view. Defaults: to='bottom', timeout=15 seconds."
    )
)
def scroll_page(
    to: str = "bottom",
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    target = str(to).strip()
    is_selector = False
    if target.lower() in ("bottom", "end"):
        move = "window.scrollTo(0, document.documentElement.scrollHeight)"
    elif target.lower() in ("top", "start"):
        move = "window.scrollTo(0, 0)"
    elif re.fullmatch(r"[-+]?\d+(\.\d+)?", target):
        move = f"window.scrollTo(0, {float(target)})"
    else:
        is_selector = True
        move = (f"const __el = document.querySelector({json.dumps(target)});"
                f" if (!__el) return JSON.stringify({{__not_found: true}});"
                f" __el.scrollIntoView({{block: 'center'}});")
    driver = require_driver()
    ensure_sessions()
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    script = f"""
    const before = window.scrollY;
    {move}
    return new Promise(r => setTimeout(() => {{
      const de = document.documentElement;
      r(JSON.stringify({{
        before, after: Math.round(window.scrollY),
        viewH: window.innerHeight,
        docH: Math.max(de.scrollHeight, document.body.scrollHeight),
        atBottom: Math.ceil(window.scrollY + window.innerHeight) >=
                  Math.max(de.scrollHeight, document.body.scrollHeight) - 2
      }}));
    }}, 400))
    """
    try:
        resp = exec_js(script, session_id=None, timeout=timeout)
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    raw = resp.get("data")
    info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    if is_selector and info.get("__not_found"):
        return {"status": "not_found", "selector": target,
                "note": f"Selector {target!r} did not match the page; verify it or use 'top', 'bottom', or a pixel offset"}
    return {
        "status": "success",
        "scrolled_from": info.get("before"),
        "scroll_y": info.get("after"),
        "viewport_height": info.get("viewH"),
        "doc_height": info.get("docH"),
        "at_bottom": info.get("atBottom"),
        "moved": info.get("before") != info.get("after"),
    }


# --- Tools: execute_js (with CDP fallback) and cdp_command -------------------

# The MCP host may render a large TextContent result through a bounded context
# channel.  Keep the inline payload comfortably below the observed truncation
# boundary; larger JS return values are handed back by path and digest instead
# of being silently clipped.
EXECUTE_JS_INLINE_MAX_BYTES = 24 * 1024


def _serialize_execute_js_value(value: Any) -> bytes:
    """Encode a JS return value exactly once for size checks and file output."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError, OverflowError):
        # A hostile/cyclic host object should still be exportable as a useful
        # diagnostic string rather than making the whole execute_js call fail.
        encoded = json.dumps(str(value), ensure_ascii=False)
    # UTF-8 rejects lone UTF-16 surrogates admitted by JavaScript strings. At
    # this point they occur inside JSON strings, so backslashreplace produces
    # valid \uXXXX escapes while ordinary Unicode remains readable UTF-8.
    return encoded.encode("utf-8", errors="backslashreplace")


def _write_execute_js_payload(payload: bytes) -> tuple[Path, int, str]:
    """Write a completed, private JSON payload to a unique temporary file."""
    descriptor: Optional[int]
    descriptor, filename = tempfile.mkstemp(
        prefix="browsertap-execute-js-",
        suffix=".json",
    )
    path = Path(filename)
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream as output:
            output.write(payload)
            output.flush()
    except BaseException:
        # fdopen owns the descriptor once it returns, even if writing fails.
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path, len(payload), hashlib.sha256(payload).hexdigest()


def _externalize_execute_js_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep large or non-scalar JS strings lossless outside the MCP result."""
    if not isinstance(result, dict):
        return result
    value = result.get("js_return")
    if value is None:
        return result
    if result.get("status") in {
        "failed",
        "error",
        "no_response",
        "navigated",
        "busy",
        "blocked_by_dialog",
    }:
        return result

    payload = _serialize_execute_js_value(value)
    has_surrogate = _contains_utf16_surrogate(value)
    if len(payload) <= EXECUTE_JS_INLINE_MAX_BYTES and not has_surrogate:
        return result

    metadata = _export_json_result(payload, scope="js-value")
    if has_surrogate:
        metadata.setdefault("result_encoding_reason", "utf-16-surrogate")
    externalized = {key: item for key, item in result.items() if key not in _RESULT_PAYLOAD_FIELDS}
    externalized["js_return"] = None
    externalized.update(metadata)
    externalized["result_inline_limit_bytes"] = EXECUTE_JS_INLINE_MAX_BYTES
    return externalized


def _normalize_execute_js_dialog_result(result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the extension's dialog envelope on sync and claimed results."""
    wrapped = result.get("js_return")
    if not isinstance(wrapped, dict) or wrapped.get("__btap_dialog_result") is not True:
        return result

    normalized = dict(result)
    normalized["js_return"] = wrapped.get("value")
    wrapped_status = wrapped.get("status")
    normalized["manual_blocked"] = bool(
        wrapped.get("manual_blocked")
        or wrapped_status == "blocked_by_dialog"
    )
    if isinstance(wrapped_status, str):
        normalized["status"] = wrapped_status
    if "handled" in wrapped:
        normalized["handled"] = bool(wrapped.get("handled"))
    if "pending_execution" in wrapped:
        normalized["pending_execution"] = bool(wrapped.get("pending_execution"))
        if normalized["pending_execution"] and normalized.get("operation_id"):
            normalized["reservation_held"] = True
            normalized["poll_with"] = "get_execute_js_result"
    if isinstance(wrapped.get("error"), dict):
        normalized["error"] = dict(wrapped["error"])
    dialogs = wrapped.get("dialogs")
    if isinstance(dialogs, list) and dialogs:
        normalized["dialogs"] = dialogs
        normalized["dialog"] = dialogs[-1]
        normalized["status"] = (
            "blocked_by_dialog" if normalized["manual_blocked"] else "ok"
        )
    elif isinstance(wrapped.get("dialog"), dict):
        normalized["dialog"] = dict(wrapped["dialog"])
    return normalized


def _build_cdp_fallback_expression(script: str, policy: str, timeout: float) -> str:
    token = f"cdp-{time.monotonic_ns()}"
    # Fix the page deadline before transport/debugger work. A late CDP delivery
    # must not start a fresh timeout or install a new scope after the caller's
    # remaining budget expired. The browser and Python run on the same host.
    deadline_ms = int(time.time() * 1000) + max(1, min(120000, int(float(timeout) * 1000)))
    return f"""
    (async () => {{
      {_RESULT_SERIALIZER_SOURCE}
      {_GUARDED_EVAL_SOURCE}
      {_DIALOG_SCOPE_SOURCE}
      const rawJsCode = {json.dumps(script)}.trim();
      const policy = {json.dumps(policy)};
      const token = {json.dumps(token)};
      const deadline = {deadline_ms};
      const scoped = policy === 'accept' || policy === 'dismiss';
      let dialogLease = null;
      try {{
        if (Date.now() >= deadline) {{
          return {{ok: false, error: {{
            name: 'TimeoutError', message: 'Command deadline expired before caller execution',
            code: 'exec_timeout', dispatched: false, may_have_executed: false, retryable: false,
          }}}};
        }}
        if (scoped) {{
          dialogLease = manageDialogScope('enter', {{token, policy, deadline}});
          if (!dialogLease) {{
            return {{ok: false, error: {{
              name: 'TimeoutError', message: 'Dialog scope expired before caller execution',
              code: 'exec_timeout', dispatched: false, may_have_executed: false, retryable: false,
            }}}};
          }}
        }}
        const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
        let value;
        const _btapEvalGuard = (() => {{
          try {{ return prepareGuardedEval(rawJsCode, this); }}
          catch (error) {{ return {{ error }}; }}
        }})();
        try {{
          // Scope installation and compile-only probes can consume the last
          // budget. Recheck after preparation, immediately before each caller.
          if (Date.now() >= deadline) {{
            return {{ok: false, error: {{
              name: 'TimeoutError', message: 'Command deadline expired before caller execution',
              code: 'exec_timeout', dispatched: false, may_have_executed: false, retryable: false,
            }}}};
          }}
          if (_btapEvalGuard.error) throw _btapEvalGuard.error;
          value = eval(_btapEvalGuard.source);
          if (value instanceof Promise) value = await value;
        }} catch (error) {{
          const parseFailed = _btapEvalGuard.error || !_btapEvalGuard.started();
          if (parseFailed && error instanceof SyntaxError &&
              (_btapEvalGuard.error || /return/i.test(error.message))) {{
            const asyncCaller = new AsyncFunction(prepareGuardedEval.asyncBody(rawJsCode));
            if (Date.now() >= deadline) {{
              return {{ok: false, error: {{
                name: 'TimeoutError', message: 'Command deadline expired before caller execution',
                code: 'exec_timeout', dispatched: false, may_have_executed: false, retryable: false,
              }}}};
            }}
            value = await asyncCaller();
          }} else throw error;
        }} finally {{
          if (_btapEvalGuard.cleanup) _btapEvalGuard.cleanup();
        }}
        const processed = smartProcessResult(value);
        const dialogs = dialogLease ? dialogLease.records() : [];
        return {{ok: true, data: dialogs.length ? {{
          __btap_dialog_result: true, value: processed, dialogs,
        }} : processed}};
      }} catch (error) {{
        return {{ok: false, error: {{name: error?.name || 'Error',
          message: error?.message || String(error), stack: error?.stack || ''}}}};
      }} finally {{
        if (dialogLease) dialogLease.release();
      }}
    }})()
    """


def _execute_js_cdp_fallback(
    script: str,
    *,
    policy: str,
    target_sid: str,
    client_id: str,
    tab_id: int,
    deadline: float,
    route_error: BaseException,
) -> dict[str, Any]:
    timeout = max(0.0, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError(
            "execute_js total deadline exhausted before CDP fallback dispatch"
        ) from route_error
    evaluation = _direct_cdp(
        "Runtime.evaluate",
        {
            "expression": _build_cdp_fallback_expression(script, policy, timeout),
            "awaitPromise": True,
            "returnByValue": True,
        },
        session_id=target_sid,
        client_id=client_id,
        tab_id=tab_id,
        timeout=timeout,
        deadline=deadline,
    )
    if not isinstance(evaluation, dict):
        raise RuntimeError(f"CDP fallback returned an unexpected result: {evaluation!r}")
    if evaluation.get("exceptionDetails"):
        details = evaluation["exceptionDetails"]
        raise RuntimeError(
            str(details.get("exception", {}).get("description") or details.get("text") or details)
        )
    # Fetched once and then tested, rather than `.get()` twice in a ternary: two
    # calls are two separate reads of a dict another thread can be writing, and
    # the pair cannot be narrowed to "not None" by anything a reader or a checker
    # can see.
    remote_result = evaluation.get("result")
    remote = remote_result if isinstance(remote_result, dict) else {}
    wrapped = remote.get("value")
    if not isinstance(wrapped, dict) or "ok" not in wrapped:
        raise RuntimeError(f"CDP fallback returned no serializable value: {evaluation!r}")
    if wrapped.get("ok") is not True:
        error = wrapped.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        return {
            "status": "failed",
            "js_return": None,
            "tab_id": tab_id,
            "execution_mode": "cdp_fallback",
            "bridge_route_error": str(route_error),
            "error": str(message or "CDP evaluation failed"),
        }
    return _normalize_execute_js_dialog_result({
        "status": "success",
        "js_return": wrapped.get("data"),
        "tab_id": tab_id,
        "execution_mode": "cdp_fallback",
        "bridge_route_error": str(route_error),
    })


@mcp.tool(description=(
    "Execute arbitrary JS, which can cause side effects, in session_id's browser tab under one "
    "total deadline. Pin an explicit session_id; fallbacks keep that target and never replay "
    "an already-started script. For complex async bodies use an explicit return in an async IIFE. "
    "accept/dismiss prepare current injectable frames on extension routes; the legacy CDP "
    "fallback covers only its current evaluation context. manual keeps native dialogs. "
    "wait=false returns operation_id after delivery acknowledgement; collect with "
    "get_execute_js_result in the same MCP session, without replay. partial/unknown results "
    "or an expired handle do not prove non-execution; inspect retry_safe before retrying. "
    "A dispatched exec_timeout retains an outcome_unknown reservation for the bounded "
    "recovery window; the deadline does not cancel page JS. "
    "Use wait_for/wait_for_url for page state instead of sleep Promises. "
    "Conversion on all routes: undefined/non-finite numbers become null; BigInt/symbol become "
    "strings; DOM/Error/functions become readable values; cycles/depth 6/iterables above 200 "
    "items have markers. JSON UTF-8 over 24 KiB or any unpaired UTF-16 uses a private JSON "
    "result_file with result_bytes, result_sha256, result_format and result_file_scope=js-value. "
    "result_file_encoding=json means JSON-decode the path once; otherwise use it directly. "
    "Parse the UTF-8 file once; it preserves the full converted value and markers. "
    "If file writing fails, parse the complete ASCII result_json once "
    "(result_json_scope=js-value); this fallback may exceed the inline limit and preserves "
    "original result/retry verdicts."
))
def execute_js(
    script: str,
    session_id: Optional[str] = None,
    no_monitor: bool = False,
    timeout: float = 15.0,
    dialog_policy: str = "dismiss",
    wait: bool = True,
) -> dict[str, Any]:
    timeout = _positive_timeout(timeout)
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    policy = _validate_dialog_policy(dialog_policy)
    if not wait and policy == "manual":
        raise ValueError(
            "execute_js wait=false does not support dialog_policy='manual'; "
            "use dismiss or accept, or run synchronously"
        )
    driver = require_driver()
    session_budget = remaining()
    if session_budget <= 0:
        raise TimeoutError("execute_js total deadline exhausted before session resolution")
    sessions = ensure_sessions(
        timeout=session_budget,
        fresh=True,
        prune_default=False,
    )
    if remaining() <= 0:
        raise TimeoutError("execute_js total deadline exhausted during session resolution")
    before_sids = {str(s.get("id")) for s in sessions}
    # Point the shared default at the target only for this call's roundtrips
    # (execute_js_rich does baseline/diff/transient snapshots that read the
    # global default), then restore — a session_id-scoped call must not leave
    # the default parked on this tab and steal another task's session.
    prev_default = driver.default_session_id
    target_sid: Optional[str] = None
    dispatch_sid: Optional[str] = None
    if session_id is not None:
        requested_sid = str(session_id)
        resolved = _resolve_session_target(driver, requested_sid)
        if resolved:
            current_sid = str(resolved["session_id"])
            if current_sid != requested_sid:
                _TAB_OWNERSHIP.rebind(requested_sid, current_sid)
            # Keep the caller's old handle in the per-call target. The bridge
            # then returns rebound_from/replacement_session_id, so the agent
            # learns the new handle instead of silently losing the transition.
            target_sid = requested_sid
            dispatch_sid = current_sid
            driver.default_session_id = current_sid
        elif not any(str(session.get("id")) == requested_sid for session in sessions):
            raise _session_target_not_found(requested_sid, sessions)
        else:
            target_sid = requested_sid
            dispatch_sid = requested_sid
    else:
        # Resolve once from the bounded snapshot. A stale implicit default may
        # be repicked; a caller-named dead session above is still refused.
        current = str(prev_default) if prev_default is not None else None
        if current and any(str(session.get("id")) == current for session in sessions):
            target_sid = current
            dispatch_sid = current
        else:
            candidates = sessions
            preferred_browser = os.environ.get(
                "BROWSERTAP_PREFERRED_BROWSER", ""
            ).strip().lower()
            if preferred_browser:
                preferred = [
                    session for session in sessions
                    if str(session.get("browser", "")).lower() == preferred_browser
                ]
                if preferred:
                    candidates = preferred
            candidates = _browser_candidates(candidates, current=prev_default)
            target_sid = str(candidates[0]["id"])
            dispatch_sid = target_sid
            driver.default_session_id = target_sid
    scope_token: Optional[str] = None
    client_id: Optional[str] = None
    tab_id: Optional[int] = None
    ext_cmd = getattr(driver, "ext_cmd", None)
    primary_error: Optional[BaseException] = None
    execution_pending = False
    try:
        if dispatch_sid is not None and callable(ext_cmd):
            client_id, tab_id = _split_session_target(dispatch_sid)
            policy_timeout = remaining()
            policy_request: dict[str, Any] = {
                "cmd": "set_dialog_policy",
                "tabId": tab_id,
                "policy": policy,
                # Keep the wire contract stable (15.0 -> exactly 15000); the
                # ext_cmd transport below still receives only the remaining
                # end-to-end budget.
                "timeoutMs": max(1, int(float(timeout) * 1000)),
            }
            if policy == "manual":
                # `claimManualExecDialogPolicy` matches this against the raw
                # `data.code` it receives, so it has to be the exact text that
                # goes on the wire below -- marker included, or a manual policy
                # silently never matches its own execution.
                policy_request["source"] = _mark_js_wire(script)
            try:
                if policy_timeout <= 0:
                    raise TimeoutError("execute_js total deadline exhausted during policy setup")
                scope = _extension_data(ext_cmd(
                    policy_request,
                    client_id=client_id,
                    timeout=min(policy_timeout, 15.0),
                ))
            except Exception as route_error:
                if target_sid is None or client_id is None or tab_id is None:
                    raise
                if isinstance(route_error, TimeoutError) or remaining() <= 0:
                    raise TimeoutError(
                        "execute_js total deadline exhausted during policy setup"
                    ) from route_error
                if not _unknown_command_error(route_error):
                    raise
                if not wait:
                    raise RuntimeError(
                        "execute_js wait=false requires a current BrowserTap extension; "
                        "CDP fallback cannot expose a durable operation handle"
                    ) from route_error
                return _externalize_execute_js_result(
                    _execute_js_cdp_fallback(
                        script,
                        policy=policy,
                        target_sid=target_sid,
                        client_id=client_id,
                        tab_id=tab_id,
                        deadline=deadline,
                        route_error=route_error,
                    )
                )
            raw_token = scope.get("token")
            if raw_token is None:
                # Deliberately not named `route_error`: that is the `except ... as`
                # target above, and Python unbinds an except target when its
                # handler ends. Reusing the name here worked only because this
                # assignment happens before every read of it -- and any later
                # edit that reads it between the handler and this line would be a
                # NameError, not a type error. This path is not an exception at
                # all: the call succeeded and simply came back without a token.
                stale_router_error = RuntimeError(
                    "extension did not return a dialog scope token; command router may be stale"
                )
                if remaining() <= 0:
                    raise TimeoutError(
                        "execute_js total deadline exhausted before CDP fallback dispatch"
                    ) from stale_router_error
                if not wait:
                    raise RuntimeError(
                        "execute_js wait=false requires a current BrowserTap extension; "
                        "CDP fallback cannot expose a durable operation handle"
                    ) from stale_router_error
                return _externalize_execute_js_result(
                    _execute_js_cdp_fallback(
                        script,
                        policy=policy,
                        target_sid=target_sid,
                        client_id=client_id,
                        tab_id=tab_id,
                        deadline=deadline,
                        route_error=stale_router_error,
                    )
                )
            scope_token = str(raw_token)
            if not re.fullmatch(r"[A-Za-z0-9._-]+", scope_token):
                raise RuntimeError("extension returned an invalid dialog scope token")
        # Every branch here is marked, so "a caller script on the wire starts
        # with _JS_WIRE_MARKER" holds unconditionally -- including the no-token
        # case, which used to send the script bare exactly like `manual` did.
        marked_script = _mark_js_wire(script)
        scoped_script = (
            marked_script
            if policy == "manual" else
            f"/*__btap_dialog_scope:{scope_token}*/\n{marked_script}"
            if scope_token is not None else marked_script
        )
        if not wait:
            raw = driver.execute_js(
                scoped_script,
                timeout=max(0.001, remaining()),
                session_id=target_sid,
                wait=False,
            )
            kind = simphtml.no_response_kind(raw)
            execution_pending = (
                bool(raw.get("reservation_held")) or raw.get("status") == "in_progress"
                or kind == "after_ack"
            )
            if raw.get("status") == "in_progress":
                result = dict(raw)
                result["tab_id"] = result.get("executed_tab_id")
                result["poll_with"] = "get_execute_js_result"
                result["monitoring"] = False
                return result
            if "data" in raw:
                result = {key: value for key, value in raw.items()
                          if key not in {"data", "executed_tab_id"}}
                result.update({"status": "success", "js_return": raw.get("data"),
                               "tab_id": raw.get("executed_tab_id")})
                execution_pending = execution_pending or bool(
                    isinstance(raw["data"], dict) and raw["data"].get("pending_execution")
                )
                return _externalize_execute_js_result(
                    _normalize_execute_js_dialog_result(result)
                )
            result = dict(raw)
            result["status"] = "navigated" if kind == "navigated" else "no_response"
            result["retry_safe"] = kind == "undelivered" and raw.get("retry_safe") is not False
            result["delivery_state"] = raw.get("delivery_state") or {
                "undelivered": "undelivered", "navigated": "navigated",
            }.get(kind, "sent_unconfirmed")
            result["tab_id"] = result.get("executed_tab_id")
            result["poll_with"] = "get_execute_js_result"
            result["monitoring"] = False
            return result
        result = simphtml.execute_js_rich(
            scoped_script,
            driver,
            no_monitor=no_monitor,
            timeout=max(0.001, remaining()),
            before_sids=before_sids,
            session_id=target_sid,
            deadline=deadline,
        )
        execution_pending = bool(result.get("reservation_held")) or result.get("delivery_state") in (
            "sent_unconfirmed", "delivered_no_result",
        ) or (result.get("status") == "no_response" and result.get("retry_safe") is not True)
        return _externalize_execute_js_result(
            _normalize_execute_js_dialog_result(result)
        )
    except BaseException as exc:
        primary_error = exc
        error_diagnostics = getattr(exc, "diagnostics", None) or {}
        execution_pending = execution_pending or bool(
            getattr(exc, "reservation_held", False) or error_diagnostics.get("reservation_held")
        ) or (getattr(exc, "delivery_state", None) or error_diagnostics.get("delivery_state")) in (
            "sent_unconfirmed", "delivered_no_result",
        )
        raise
    finally:
        try:
            if (not execution_pending and scope_token is not None and tab_id is not None
                    and client_id is not None and callable(ext_cmd)):
                clear: dict[str, Any] = {"cmd": "clear_dialog_policy", "tabId": tab_id}
                if scope_token is not None:
                    clear["token"] = scope_token
                try:
                    cleanup_timeout = remaining()
                    if cleanup_timeout > 0.001:
                        ext_cmd(
                            clear,
                            client_id=client_id,
                            timeout=min(cleanup_timeout, 15.0),
                        )
                    else:
                        logger.warning(
                            "execute_js deadline exhausted; dialog scope will expire naturally"
                        )
                except Exception as cleanup_error:
                    if primary_error is None:
                        raise
                    logger.warning(
                        "execute_js dialog policy cleanup failed: error_type=%s",
                        type(cleanup_error).__name__,
                    )
        finally:
            if session_id is not None:
                driver.default_session_id = prev_default


@mcp.tool(description=(
    "Read or wait for an operation_id from execute_js or another timed-out bridge command, "
    "including open_new_tab's nested reconciliation.bridge_operation.operation_id. "
    "The outer tab-creation ID uses open_new_tab recovery. Call from the submitting MCP "
    "session; this tool never replays operations. timeout is 0-120 seconds. Completed "
    "results are repeatable for up to 10 minutes and 512 records, with earlier capacity "
    "eviction. Unknown/expired does not prove non-execution. After reservation expiry, "
    "the first valid terminal reply is late_result (success,data), with late_reply_age "
    "in seconds; unknown and retry_safe=false remain. Late replies neither renew retention "
    "nor reserve targets again. Known read-only wait, inventory and creation-status probes "
    "can release on timeout; reservation_held describes that probe, not a prior mutation. "
    "Large or unpaired-UTF-16 values use execute_js result_file metadata or fallback "
    "result_json, with scope=js-value. Late descriptors are inside late_result with "
    "data=null. JSON-decode the path only when result_file_encoding=json, then parse the "
    "UTF-8 file or result_json once as JSON. Export failure preserves the complete value "
    "and original retry verdict."
))
def get_execute_js_result(
    operation_id: str,
    timeout: float = 0.0,
) -> dict[str, Any]:
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("operation_id must be a non-empty string")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise ValueError(
            "timeout must be a finite number between 0 and 120 seconds"
        ) from None
    if not math.isfinite(timeout) or timeout < 0 or timeout > 120:
        raise ValueError("timeout must be a finite number between 0 and 120 seconds")

    raw = require_driver().get_execute_js_result(
        operation_id.strip(), timeout=timeout,
    )
    late = raw.get("late_result")
    if isinstance(late, dict) and late.get("success") is True:
        exported = _externalize_execute_js_result({"js_return": late.get("data")})
        if exported.get("result_externalized") or "result_json" in exported:
            raw = {
                **raw,
                "late_result": {
                    **late, "data": exported["js_return"],
                    **{key: value for key, value in exported.items() if key != "js_return"},
                },
            }
    if raw.get("status") != "success":
        result = dict(raw)
        if result.get("executed_tab_id") is not None:
            result["tab_id"] = result.get("executed_tab_id")
        if result.get("status") == "in_progress":
            result["poll_with"] = "get_execute_js_result"
        return result

    result = {
        key: value
        for key, value in raw.items()
        if key not in {"data", "executed_tab_id"}
    }
    result["js_return"] = raw.get("data")
    result["tab_id"] = raw.get("executed_tab_id")
    return _externalize_execute_js_result(
        _normalize_execute_js_dialog_result(result)
    )


@mcp.tool(description=(
    "Raw CDP can cause side effects in a tab, extension target or the entire browser profile. "
    "Choose session_id (client:tabId), tab_id (number or composite), extension_id or target_id "
    "deliberately. A cross-process iframe can use target_id from DOM.describeNode.frameId "
    "of its exact parent-page element; verify the frame origin and control before input. "
    "Listed high-risk methods are blocked before dispatch unless mode=lab "
    "AND operator env BROWSERTAP_ALLOW_UNSAFE_CDP=1; safe always blocks them "
    "(raw_cdp_blocked, delivery_state=undelivered, retry_safe=false, retryable=false). This guard prevents "
    "common destructive calls; allowed CDP/JavaScript can still change pages or profile state. Inspect state "
    "after uncertain delivery before retrying."
))
def cdp_command(
    method: str,
    params_json: str = "{}",
    session_id: Optional[str] = None,
    tab_id: Optional[int | str] = None,
    extension_id: Optional[str] = None,
    target_id: Optional[str] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    validate_raw_cdp_method(method, allow_unsafe=_raw_cdp_unsafe_enabled())
    params = json.loads(params_json or "{}")
    if not isinstance(params, dict):
        raise ValueError("params_json must be a JSON object")
    payload: dict[str, Any] = {"cmd": "cdp", "method": method, "params": params}
    if extension_id is not None or target_id is not None:
        # Non-tab debuggee. Routed via ext_cmd so it works with no tabs open.
        # NOTE both forms are refused for OTHER extensions unless Chrome runs
        # with --silent-debugger-extension-api: extensionId reports "No
        # background page with given id", targetId hits the same-extension URL
        # check. Useful for this extension's own targets and for diagnosis.
        if extension_id is not None:
            payload["extensionId"] = extension_id
        if target_id is not None:
            payload["targetId"] = target_id
        driver = require_driver()
        client_id = _implicit_client_id(session_id)
        return driver.ext_cmd(payload, client_id=client_id, timeout=timeout)

    driver = require_driver()
    previous_default = driver.default_session_id
    directed_sid: Optional[str] = None
    try:
        tab_id_is_composite = (
            session_id is None and isinstance(tab_id, str) and ":" in tab_id
        )
        if tab_id_is_composite:
            client_id, session_tab_id = _split_session_target(str(tab_id))
            directed_sid = str(tab_id)
        elif session_id is not None and ":" in str(session_id):
            directed_sid = switch_session(session_id=str(session_id))
            client_id, session_tab_id = _split_session_target(directed_sid)
        elif session_id is not None:
            raw_ids, client_id = _normalize_tab_targets(str(session_id))
            session_tab_id = raw_ids[0]
            if client_id is None:
                raise ValueError("cannot infer browser client for numeric session_id; run switch_tab first")
            directed_sid = f"{client_id}:{session_tab_id}"
        else:
            directed_sid = switch_session()
            client_id, session_tab_id = _split_session_target(directed_sid)

        if tab_id is not None and not tab_id_is_composite:
            tab_ids, tab_client = _normalize_tab_targets(tab_id, session_id=directed_sid)
            target_tab_id = tab_ids[0]
            if tab_client and tab_client != client_id:
                raise ValueError("tab_id and session_id identify different browser clients")
            if session_id is not None and target_tab_id != session_tab_id:
                raise ValueError("tab_id does not match the directed session_id")
        else:
            target_tab_id = session_tab_id
        data = _direct_cdp(
            method,
            params,
            session_id=directed_sid,
            client_id=client_id,
            tab_id=target_tab_id,
            timeout=timeout,
        )
        return {
            "status": "ok",
            "data": data,
            "session_id": f"{client_id}:{target_tab_id}",
            "tab_id": target_tab_id,
        }
    finally:
        if session_id is not None or tab_id is not None:
            driver.default_session_id = previous_default


# --- Tools: save_pdf, debugger_targets, cdp_batch ----------------------------
@mcp.tool(
    description=(
        "Print a real-browser tab to a validated PDF file through bounded CDP. save_path is "
        "RELATIVE and resolves under ~/Downloads/browsertap; an absolute path or a '..' escape "
        "is refused. The file is "
        "written atomically only after valid non-empty PDF bytes are returned; a CDP timeout "
        "invalidates and detaches the debugger lease."
    )
)
def save_pdf(
    save_path: str,
    session_id: Optional[str] = None,
    landscape: bool = False,
    print_background: bool = True,
    prefer_css_page_size: bool = True,
    scale: float = 1.0,
    page_ranges: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    path = _validate_safe_path(save_path, description="save_path")
    if not 0.1 <= float(scale) <= 2.0:
        raise ValueError("scale must be between 0.1 and 2.0")
    if not 0.1 <= float(timeout) <= 120.0:
        raise ValueError("timeout must be between 0.1 and 120 seconds")
    params: dict[str, Any] = {
        "landscape": bool(landscape),
        "printBackground": bool(print_background),
        "preferCSSPageSize": bool(prefer_css_page_size),
        "scale": float(scale),
    }
    if page_ranges.strip():
        params["pageRanges"] = page_ranges.strip()
    result = cdp_command(
        "Page.printToPDF",
        params_json=json.dumps(params),
        session_id=session_id,
        timeout=float(timeout),
    )
    payload = result.get("data") if isinstance(result, dict) else None
    encoded = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("save_pdf failed: Page.printToPDF returned no PDF data")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("save_pdf failed: Page.printToPDF returned invalid base64") from exc
    if len(raw) < 8 or not raw.startswith(b"%PDF-"):
        raise RuntimeError("save_pdf failed: decoded data is not a valid PDF document")

    _atomic_write_bytes(path, raw)
    return {
        "status": "success",
        "saved_to": str(path),
        "size": len(raw),
        "session_id": result.get("session_id"),
        "tab_id": result.get("tab_id"),
        "format": "pdf",
    }


@mcp.tool(
    description=(
        "List every CDP-attachable target, including service workers and extension "
        "background pages that list_tabs never shows. Works with no tabs open."
    ),
    serialize=False,
)
def debugger_targets(session_id: Optional[str] = None) -> dict[str, Any]:
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    return driver.ext_cmd({"cmd": "debugger_targets"}, client_id=client_id, timeout=20.0)


@mcp.tool(description=(
    "Raw CDP batches can cause side effects across multiple targets or the entire browser profile. "
    "Pass the full JSON object with cmd='batch'; child targets may inherit or override top-level "
    "tabId. Policy checks complete for the whole batch before dispatch. Listed high-risk "
    "methods require mode=lab AND operator env BROWSERTAP_ALLOW_UNSAFE_CDP=1; safe always "
    "blocks them (raw_cdp_blocked, delivery_state=undelivered, retry_safe=false, retryable=false). "
    "Allowed CDP/JavaScript can still change pages or profile state. Inspect state after uncertain delivery "
    "before retrying a batch."
))
def cdp_batch(batch_json: str, session_id: Optional[str] = None) -> dict[str, Any]:
    payload = json.loads(batch_json)
    if not isinstance(payload, dict):
        raise ValueError("batch_json must be a JSON object with cmd='batch'")
    if payload.get("cmd") != "batch":
        raise RuntimeError("batch_json must be a JSON object with cmd='batch'")
    validate_raw_cdp_batch(payload, allow_unsafe=_raw_cdp_unsafe_enabled())
    return _extension_batch(payload, session_id=session_id, timeout=30.0)


_PAGE_CHALLENGES = ChallengeAttemptTracker(max_attempts=3, window_seconds=120)
_PAGE_CHALLENGE_ATTEMPTS: dict[str, tuple[str, float, int]] = {}
_PAGE_CHALLENGE_LOCK = threading.Lock()


# --- Page input: focus proof, challenge tracking, dispatch -------------------
def _clear_page_challenge(session_id: str) -> None:
    with _PAGE_CHALLENGE_LOCK:
        _PAGE_CHALLENGES.clear(session_id)
        _PAGE_CHALLENGE_ATTEMPTS.pop(session_id, None)


def _prime_page_challenge(session_id: str, marker: str) -> None:
    """Install a marker baseline without counting the click as unchanged."""
    with _PAGE_CHALLENGE_LOCK:
        _PAGE_CHALLENGES.clear(session_id)
        _PAGE_CHALLENGE_ATTEMPTS[session_id] = (marker, 0.0, 0)


def _record_unchanged_page_challenge(session_id: str, marker: str) -> tuple[bool, int]:
    with _PAGE_CHALLENGE_LOCK:
        now = time.monotonic()
        # The middle field is the LAST attempt, not the first. It has to agree with
        # `ChallengeAttemptTracker`, which decides `stalled` below: while this copy
        # measured total age and the tracker measured the same thing, both stopped
        # counting at a slow cadence together, so the disagreement stayed invisible.
        # With the tracker fixed to expire on idleness, an age-anchored counter here
        # would reset at `window_seconds` while the tracker reported a stall -- the
        # caller would receive `challenge_stalled` alongside `attempts=1`.
        previous_marker, last_at, previous_attempts = _PAGE_CHALLENGE_ATTEMPTS.get(
            session_id, (marker, 0.0, 0)
        )
        if (previous_marker != marker or previous_attempts == 0
                or now - last_at >= _PAGE_CHALLENGES.window_seconds):
            previous_attempts = 0
        stalled = _PAGE_CHALLENGES.record(session_id, marker, now=now)
        attempts = previous_attempts + 1
        _PAGE_CHALLENGE_ATTEMPTS[session_id] = (marker, now, attempts)
        return stalled, attempts


def _blocked_page_challenge_attempts(session_id: str, marker: str) -> int | None:
    """Return the stalled attempt count while the identical marker window is live."""
    with _PAGE_CHALLENGE_LOCK:
        state = _PAGE_CHALLENGE_ATTEMPTS.get(session_id)
        if state is None:
            return None
        previous_marker, last_at, attempts = state
        now = time.monotonic()
        # Same idleness anchor as the writer above: "has nothing happened for a
        # whole window?" rather than "is the challenge older than a window?".
        if now - last_at >= _PAGE_CHALLENGES.window_seconds:
            _PAGE_CHALLENGES.clear(session_id)
            _PAGE_CHALLENGE_ATTEMPTS.pop(session_id, None)
            return None
        if previous_marker != marker or attempts < _PAGE_CHALLENGES.max_attempts:
            return None
        return attempts


_FOCUS_EMULATION_COMMAND = {
    "cmd": "cdp",
    "method": "Emulation.setFocusEmulationEnabled",
    "params": {"enabled": True},
}
# A renderer round trip, and its answer is the proof.  document.hasFocus() is
# the only thing that separates "the renderer will route this input" from
# "Chrome will drop it and report success anyway".
_FOCUS_PROOF_COMMAND = {
    "cmd": "cdp",
    "method": "Runtime.evaluate",
    "params": {"expression": "document.hasFocus()", "returnByValue": True},
}


def _focus_proof_value(result: Any) -> Optional[bool]:
    """The probed focus state, or None when the route did not really answer.

    Older extensions and the embedded test doubles return a shorter or synthetic
    result list.  Those must not be read as "unfocused", which would turn a
    compatible route into a hard failure.
    """
    if not isinstance(result, list) or len(result) < 2:
        return None
    probe = result[1]
    if not isinstance(probe, dict):
        return None
    value = probe.get("result")
    if not isinstance(value, dict) or "value" not in value:
        return None
    return bool(value["value"])


def _resolve_page_input_session(
    driver: BrowserBridge,
    session_id: Optional[str],
    *,
    deadline: float,
    operation: str,
) -> str:
    """Resolve one page-input target without letting discovery outlive the call.

    The common path uses the short-lived session cache. A cache miss for a
    caller-named tab, or a remembered implicit tab that disappeared, gets one
    bounded fresh snapshot before the target is rejected or replaced.
    """

    def fetch(*, fresh: bool) -> list[dict[str, Any]]:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError(
                f"{operation} deadline exhausted before session resolution"
            )
        sessions = active_sessions(timeout=remaining, fresh=fresh)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{operation} deadline exhausted during session resolution"
            )
        return sessions

    sessions = fetch(fresh=False)
    if session_id is not None:
        requested = str(session_id)
        if any(str(item.get("id")) == requested for item in sessions):
            return requested
        # The two-second cache may predate a just-opened tab. Confirm once
        # before refusing an explicit target; never substitute another tab.
        sessions = fetch(fresh=True)
        if any(str(item.get("id")) == requested for item in sessions):
            return requested
        raise _session_target_not_found(requested, sessions)

    current = (
        str(driver.default_session_id)
        if driver.default_session_id is not None
        else None
    )
    if current and any(str(item.get("id")) == current for item in sessions):
        return current
    if current or not sessions:
        # A missing remembered default may be a stale cache rather than a dead
        # tab. Re-read before repicking so implicit calls do not jump tabs.
        sessions = fetch(fresh=True)
        if current and any(str(item.get("id")) == current for item in sessions):
            return current
    if not sessions:
        raise RuntimeError(
            "No connected browser tabs. Load the unpacked extension from the "
            "reported extension path, keep the bridge daemon running, "
            "and open a normal http/https page in Chrome."
        )

    # A caller that named no tab expressed no preference, so avoid pages Chrome
    # cannot script when another live target exists. Preserve the old fallback
    # when every connected page is restricted.
    candidates = _browser_candidates(sessions, current=current)
    candidates = [
        item for item in candidates if is_scriptable_url(item.get("url"))
    ] or candidates
    target = str(candidates[0]["id"])
    driver.default_session_id = target
    return target


def _run_page_input(
    commands: list[dict[str, Any]],
    session_id: Optional[str],
    timeout: float,
    *,
    session_validated: bool = False,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Dispatch one uninterrupted CDP input sequence to one resolved tab."""
    if not commands:
        raise InputValidationError("page input commands must not be empty")
    timeout = _positive_timeout(timeout)
    # Chrome drops Input.* events sent to a tab that has never received focus,
    # even though the CDP commands return success.  Focus emulation makes the
    # renderer input-capable without activating the tab or changing the user's
    # foreground page.  Keep it in this same batch so attach/lease ordering is
    # atomic and do not expose the internal setup result to callers.
    #
    # Enabling it is not enough: the command ACKs before the renderer has
    # applied it, and an Input.* event that arrives inside that window is
    # discarded with nothing reported anywhere -- measured on a freshly opened
    # tab as a ~1-in-8 silent miss, exactly the failure this file must never
    # produce.  The probe supplies the renderer round trip the flag needs and
    # its answer proves the state.  It belongs in this same batch: a separate
    # one would cost a second debugger attach per input, which is slower and one
    # more thing that can time out mid-sequence.
    input_commands = [_FOCUS_EMULATION_COMMAND, _FOCUS_PROOF_COMMAND, *commands]
    if deadline is None:
        deadline = time.monotonic() + timeout
    driver = require_driver()
    prev_default = driver.default_session_id
    directed = session_id is not None
    try:
        # switch_session validates a caller-named tab before any input is sent.
        # An explicit dead target must never fall back to a different live tab.
        if session_validated:
            if session_id is None:
                raise InputValidationError(
                    "session_validated requires an explicit target session"
                )
            target_session = str(session_id)
        else:
            target_session = _resolve_page_input_session(
                driver,
                session_id,
                deadline=deadline,
                operation="page input",
            )
        ext_cmd = getattr(driver, "ext_cmd", None)

        def dispatch(batch_commands: list[dict[str, Any]]) -> Any:
            """Send one batch to the resolved tab and unwrap the reply."""
            wall_now_ms = time.time() * 1000
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError(
                    "page input deadline exhausted before batch dispatch"
                )
            payload = {
                "cmd": "batch",
                "commands": batch_commands,
                # The extension uses the absolute deadline to stop a batch whose
                # transport ACK or an earlier CDP command consumed the budget.
                "deadlineEpochMs": int(wall_now_ms + remaining * 1000),
                "timeoutMs": max(1, int(remaining * 1000)),
            }
            if callable(ext_cmd):
                client_id, tab_id = _split_session_target(target_session)
                payload["tabId"] = tab_id
                try:
                    # A batch is an extension command, not page JavaScript. Sending
                    # it over the browser-level socket avoids relying on a
                    # background tab's content-script ACK while retaining the exact
                    # target tab.
                    response = ext_cmd(
                        payload,
                        client_id=client_id,
                        timeout=remaining,
                    )
                except BaseException as exc:
                    fallback_budget = max(0.0, deadline - time.monotonic())
                    # Only an explicit old-router rejection proves the mutation did
                    # not run. A timeout or transport failure is ambiguous and must
                    # never replay clicks, keys, or drags through another route.
                    if not _unknown_command_error(exc) or fallback_budget <= 0:
                        raise
                    response = exec_js(
                        json.dumps(payload),
                        session_id=target_session,
                        timeout=fallback_budget,
                    )
            else:  # Compatibility for older embedded/fake drivers.
                response = exec_js(
                    json.dumps(payload), session_id=target_session, timeout=remaining
                )
            unwrapped = response.get("data") if isinstance(response, dict) else response
            if isinstance(unwrapped, dict) and unwrapped.get("ok") is False:
                raise PageExecutionError(unwrapped)
            return unwrapped

        result = dispatch(input_commands)
        if _focus_proof_value(result) is False:
            # The probe ran between enabling focus emulation and the first
            # Input.* command, so a false answer means Chrome had nowhere to
            # route these events.  Say so: the alternative is the silent
            # "reported success, nothing happened" miss.  Do not replay them --
            # like the timeout path above, a repeat could double the action.
            raise RuntimeError(
                "page input may not have landed: the target page did not hold "
                "focus when the events were dispatched, so Chrome can drop them "
                "without reporting anything. Check the page before retrying."
            )
        # The focus prelude is internal.  Current extensions return one result
        # per command; retain compatibility with older/fake routes that return a
        # shorter synthetic list.
        if (
            isinstance(result, list)
            and len(result) == len(input_commands)
            and result
            and result[0] == {}
        ):
            result = result[2:]
        return {
            "status": "success",
            "session_id": target_session,
            "input_mode": "cdp",
            "foreground_changed": False,
            "result": result,
        }
    finally:
        if directed:
            driver.default_session_id = prev_default


def _page_selector_info(
    selector: str | dict[str, Any],
    offset_x: float,
    offset_y: float,
    session_id: str,
    timeout: float,
    *,
    verify_hit: bool = False,
    center_x: bool = False,
    center_y: bool = False,
) -> dict[str, Any]:
    """Resolve a locator to a click point, optionally proving the point is hittable.

    ``verify_hit`` costs nothing extra: the proof runs inside the resolver's own
    round trip, before any ``Input.*`` command exists. ``center_x``/``center_y``
    say that the caller omitted that offset, so the point under test is the
    element centre. Structured locators return top-document ``x``/``y``
    coordinates. For same-origin frames the resolver accumulates each frame's
    client offset. Selector click verification refuses a non-identity CSS
    transform on any traversed iframe instead of pretending its axis-aligned
    rectangle proves a safe dispatch point; query/type resolution is unaffected.
    """
    normalized = normalize_locator(selector)
    point_mode = isinstance(normalized, dict) and "x" in normalized and "y" in normalized
    script = (
        resolve_selector_script(
            normalized,
            offset_x,
            offset_y,
            require_interactable=True,
            verify_hit=verify_hit,
            center_x=center_x,
            center_y=center_y,
        )
        if isinstance(normalized, str)
        else structured_locator_script(
            normalized,
            purpose="click",
            offset_x=offset_x,
            offset_y=offset_y,
            verify_hit=False if point_mode else verify_hit,
            center_x=False if point_mode else center_x,
            center_y=False if point_mode else center_y,
        )
    )
    response = exec_js(
        script,
        session_id=session_id,
        timeout=timeout,
    )
    raw = response.get("data") if isinstance(response, dict) else response
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"selector resolver returned invalid JSON: {raw!r}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"selector resolver returned an unexpected result: {raw!r}")
    return raw


def _page_type_target_script(selector: str | dict[str, Any], clear: bool) -> str:
    normalized = selector if selector == "" else normalize_locator(selector)
    return (
        type_target_script(normalized, select_all=clear)
        if isinstance(normalized, str)
        else structured_locator_script(
            normalized, purpose="type", select_all=clear
        )
    )


def _page_type_target_info(
    selector: str | dict[str, Any],
    clear: bool,
    session_id: str,
    timeout: float,
) -> dict[str, Any]:
    # An empty selector is the legacy focused-element mode. Structured locator
    # validation only applies when the caller actually supplied a selector.
    response = exec_js(
        _page_type_target_script(selector, clear),
        session_id=session_id,
        timeout=timeout,
    )
    raw = response.get("data") if isinstance(response, dict) else response
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"page_type target resolver returned invalid JSON: {raw!r}"
            ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"page_type target resolver returned an unexpected result: {raw!r}"
        )
    return raw


def _call_frame_locator(
    payload: dict[str, Any], session_id: str, deadline: float,
) -> dict[str, Any]:
    """One parent-tab reservation covers every renderer visited by the locator."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("iframe operation deadline exhausted before dispatch")
    client_id, tab_id = _split_session_target(session_id)
    ext_cmd = getattr(require_driver(), "ext_cmd", None)
    try:
        if not callable(ext_cmd):
            raise RuntimeError("Unknown command: frame_locator")
        response = ext_cmd(
            {**payload, "tabId": tab_id, "timeoutMs": max(1, int(remaining * 1000)),
             "deadlineEpochMs": int(time.time() * 1000 + remaining * 1000)},
            client_id=client_id, timeout=remaining,
        )
        data = _extension_data(response)
        if data.get("ok") is False:
            raise PageExecutionError(data)
        return data
    except Exception as exc:
        if not _unknown_command_error(exc):
            raise
        return {
            "status": "stale_extension", "input_dispatched": False,
            "required_capability": "frame_locators", "next_action": "reload_extension",
        }


def _run_frame_input(
    selector: dict[str, Any], action: str, session_id: str, deadline: float,
    commands: list[dict[str, Any]], *, text: str = "", clear: bool = False,
    offset_x: Optional[float] = None, offset_y: Optional[float] = None,
) -> dict[str, Any]:
    point = "x" in selector
    payload = frame_locator_payload(
        selector, action=action, clear=clear,
        offset_x=0 if offset_x is None else offset_x,
        offset_y=0 if offset_y is None else offset_y,
        center_x=not point and action == "click" and offset_x is None,
        center_y=not point and action == "click" and offset_y is None,
    )
    payload["commands"] = commands
    payload["submitDelayMs"] = _XTERM_SUBMIT_DELAY_MS
    if action == "click":
        with _PAGE_CHALLENGE_LOCK:
            prior = _PAGE_CHALLENGE_ATTEMPTS.get(session_id)
        if prior and _blocked_page_challenge_attempts(session_id, prior[0]) is not None:
            payload["blockedMarker"] = prior[0]
    raw = _call_frame_locator(payload, session_id, deadline)
    out: dict[str, Any] = {
        "status": raw.get("status", "error"), "session_id": session_id,
        "input_mode": "cdp", "foreground_changed": False,
        "target": {"selector": selector},
        "input_dispatched": raw.get("input_dispatched", False),
    }
    for key in ("result", "matches", "stage", "next_action", "required_capability",
                "input_commands_dispatched"):
        if key in raw:
            out[key] = raw[key]
    for source, destination in (
        ("frameTransform", "frame_transform"), ("occludedBy", "occluded_by"),
        ("activeElement", "active_element"), ("focusConfirmed", "focus_confirmed"),
        ("previousActiveElement", "previous_active_element"),
    ):
        if source in raw:
            out[destination] = raw[source]
    if action == "type":
        out["target_kind"] = raw.get("targetKind", "element")
        # A partial sequence (e.g. navigation after insertText) is not proof of
        # how many characters landed. Never invite replay by reporting zero.
        out["typed_chars"] = len(text) if out["status"] == "success" else (
            None if out["input_dispatched"] else 0
        )
        return out
    out["target"].update({
        **({"x": raw["x"], "y": raw["y"]} if "x" in raw and "y" in raw else {}),
        "offset_x": offset_x, "offset_y": offset_y,
        "hit_verified": bool(raw.get("hitVerified")), "scrolled_into_view": False,
    })
    marker = raw.get("beforeMarker")
    if out["status"] == "challenge_stalled":
        out.update(challenge_detected=True, attempts=_blocked_page_challenge_attempts(session_id, str(marker)))
    elif out["status"] != "success":
        out.update(challenge_detected=False, attempts=0)
    elif raw.get("challenge_check", {}).get("enforced"):
        after = raw.get("afterMarker")
        out["challenge_check"] = raw["challenge_check"]
        if after and after == marker:
            stalled, attempts = _record_unchanged_page_challenge(session_id, str(after))
            out.update(challenge_detected=True, attempts=attempts)
            if stalled:
                out["status"] = "challenge_stalled"
        elif after:
            _prime_page_challenge(session_id, str(after))
            out.update(challenge_detected=True, attempts=0)
        else:
            _clear_page_challenge(session_id)
            out.update(challenge_detected=False, attempts=0)
    else:
        out.update(challenge_detected=None, attempts=None,
                   challenge_check=raw.get("challenge_check", {"enforced": False}))
    if out["status"] == "challenge_stalled":
        out["next_action"] = "Stop automatic attempts and let the user take over the same tab."
    return out


# --- Tools: page input (click, type, press, drag, upload) --------------------
@mcp.tool(
    description=(
        "Click a CSS/structured locator or viewport coordinates in a specific real browser tab "
        "using background CDP input. Coordinates are viewport-relative CSS pixels (the space "
        "getBoundingClientRect reports), NOT the device pixels capture_page_screenshot returns "
        "-- on a scaled display divide a screenshot pixel by devicePixelRatio first. Ambiguous or unreachable targets dispatch "
        "nothing; the tab "
        "is not activated and the desktop cursor does not move. Selector offsets are measured "
        "from the element's top-left corner; an omitted axis uses the element centre. In selector "
        "mode, duplicate CSS/structured matches are reduced to visible, interactable candidates "
        "so hidden modal templates do not win, then the point is hit-tested before anything is "
        "dispatched: an element below the fold is "
        "scrolled into view, and a point owned by another element returns status 'obscured' (with "
        "occluded_by) or 'outside_viewport' having clicked nothing. Coordinate mode is not "
        "hit-tested -- coordinates name a pixel, not an element. A selector click crossing a "
        "non-identity CSS-transformed iframe returns status 'unsupported_frame_transform' without "
        "dispatch; query/type paths remain available. A structured selector may use "
        "{'selector': '#pay', 'frame': [...]} as a CSS alias, or {'frame': [...], 'x': 20, 'y': 30} "
        "to click a point inside the final same-origin or cross-origin frame; frame-point mode is not hit-tested. "
        "Nested frame locators also support OOPIFs. Framed clicks do not scroll automatically and "
        "check every parent for obstruction. A binding invalidated during the call returns "
        "stale_frame; inspect input_dispatched before recovery and never replay partial or unknown input."
    )
)
def page_click(
    selector: str | dict[str, Any] = "",
    x: Optional[float] = None,
    y: Optional[float] = None,
    offset_x: Optional[float] = None,
    offset_y: Optional[float] = None,
    button: str = "left",
    clicks: int = 1,
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    selector_mode = (isinstance(selector, str) and bool(selector)) or isinstance(selector, dict)
    any_coordinate = x is not None or y is not None
    both_coordinates = x is not None and y is not None
    if any_coordinate and not both_coordinates:
        raise InputValidationError("coordinate mode requires both x and y")
    point_mode = isinstance(selector, dict) and ("x" in selector or "y" in selector)
    if point_mode and (x is not None or y is not None):
        raise InputValidationError(
            "frame-relative point locators carry x and y inside selector; do not also pass top-level coordinates"
        )
    if point_mode and (offset_x is not None or offset_y is not None):
        raise InputValidationError(
            "frame-relative point locators cannot use offset_x or offset_y"
        )
    if selector_mode == both_coordinates:
        raise InputValidationError(
            "page_click requires exactly one targeting mode: selector, or both x and y"
        )
    timeout = _positive_timeout(timeout)
    deadline = time.monotonic() + timeout

    if not selector_mode:
        out = _run_page_input(
            click_commands(x, y, button=button, clicks=clicks),  # type: ignore[arg-type]
            session_id,
            timeout,
            deadline=deadline,
        )
        out["target"] = {"x": x, "y": y}
        return out

    driver = require_driver()
    prev_default = driver.default_session_id
    directed = session_id is not None
    try:
        target_session = _resolve_page_input_session(
            driver,
            session_id,
            deadline=deadline,
            operation="page_click",
        )
        normalized = normalize_locator(selector)
        if isinstance(normalized, dict) and normalized.get("frame"):
            return _run_frame_input(
                normalized, "click", target_session, deadline,
                click_commands(0, 0, button=button, clicks=clicks),
                offset_x=offset_x, offset_y=offset_y,
            )
        resolver_x = 0 if offset_x is None else offset_x
        resolver_y = 0 if offset_y is None else offset_y
        resolver_budget = max(0.0, deadline - time.monotonic())
        if resolver_budget <= 0:
            raise TimeoutError(
                "page_click deadline exhausted before target resolution"
            )
        before = _page_selector_info(
            selector,
            resolver_x,
            resolver_y,
            target_session,
            resolver_budget,
            verify_hit=True,
            center_x=offset_x is None,
            center_y=offset_y is None,
        )
        if not before.get("found"):
            _clear_page_challenge(target_session)
            return {
                "status": before.get("status", "not_found"),
                "session_id": target_session,
                "input_mode": "cdp",
                "foreground_changed": False,
                "challenge_detected": False,
                "attempts": 0,
                "target": {"selector": selector},
                **({"matches": before["matches"]} if before.get("matches") is not None else {}),
                **({"stage": before["stage"]} if before.get("stage") else {}),
                **(
                    {"frame_transform": before["frameTransform"]}
                    if before.get("frameTransform")
                    else {}
                ),
                **({"occluded_by": before["occludedBy"]} if before.get("occludedBy") else {}),
                **(
                    {"scrolled_into_view": True}
                    if before.get("scrolledIntoView")
                    else {}
                ),
                **(
                    {
                        "next_action": (
                            "Nothing was dispatched: another element owns that pixel. "
                            "Dismiss the overlay, then call "
                            "page_click again."
                        )
                    }
                    if before.get("status") == "obscured"
                    else {}
                ),
                **(
                    {
                        "next_action": (
                            "Nothing was dispatched: the target point is outside the viewport. "
                            "Scroll the target into view, adjust the click offset, or resize "
                            "the tab, then call page_click again."
                        )
                    }
                    if before.get("status") == "outside_viewport"
                    else {}
                ),
                **(
                    {
                        "next_action": (
                            "Nothing was dispatched: the locator crosses an iframe with a CSS transform. "
                            "Use an untransformed same-origin frame or target the transformed frame explicitly."
                        )
                    }
                    if before.get("status") == "unsupported_frame_transform"
                    else {}
                ),
            }

        geometry: dict[str, float] = {}
        for name in ("x", "y", "width", "height"):
            value = before.get(name, 0) if name in ("width", "height") else before.get(name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or (name in ("width", "height") and value < 0)):
                raise RuntimeError(f"selector resolver returned invalid geometry: {name}")
            geometry[name] = value
        resolved_x = geometry["x"]
        resolved_y = geometry["y"]
        if offset_x is None:
            resolved_x += geometry["width"] / 2
        if offset_y is None:
            resolved_y += geometry["height"] / 2
        before_marker = before.get("challengeMarker")
        if before_marker is not None:
            blocked_attempts = _blocked_page_challenge_attempts(
                target_session, str(before_marker)
            )
            if blocked_attempts is not None:
                return {
                    "status": "challenge_stalled",
                    "session_id": target_session,
                    "input_mode": "cdp",
                    "foreground_changed": False,
                    "challenge_detected": True,
                    "attempts": blocked_attempts,
                    "target": {
                        "selector": selector,
                        "x": resolved_x,
                        "y": resolved_y,
                        "offset_x": offset_x,
                        "offset_y": offset_y,
                    },
                    "next_action": (
                        "Stop automatic attempts and let the user take over the same tab; "
                        f"resume with session_id={target_session!r} after the challenge clears."
                    ),
                }
        input_budget = max(0.0, deadline - time.monotonic())
        if input_budget <= 0:
            raise TimeoutError("page_click deadline exhausted before input dispatch")
        out = _run_page_input(
            click_commands(resolved_x, resolved_y, button=button, clicks=clicks),
            target_session,
            input_budget,
            session_validated=True,
            deadline=deadline,
        )
        out["target"] = {
            "selector": selector,
            "x": resolved_x,
            "y": resolved_y,
            "offset_x": offset_x,
            "offset_y": offset_y,
            # Proven before dispatch: this point resolved to the target element,
            # not to whatever is drawn on top of it. False only where the page
            # denied the browser its own hit test.
            "hit_verified": bool(before.get("hitVerified")),
            "scrolled_into_view": bool(before.get("scrolledIntoView")),
        }

        post_budget = max(0.0, deadline - time.monotonic())
        if post_budget <= 0:
            out["challenge_detected"] = None
            out["attempts"] = None
            out["challenge_check"] = {
                "enforced": False,
                "reason": "deadline_exhausted",
                "before_click_detected": before_marker is not None,
            }
            return out
        try:
            after = _page_selector_info(
                selector, resolver_x, resolver_y, target_session, post_budget
            )
        except Exception as exc:
            # The click batch already completed. A post-click observation is
            # allowed to become unknown, but it must never turn that mutation
            # into a reported failure or trigger an automatic replay.
            out["challenge_detected"] = None
            out["attempts"] = None
            out["challenge_check"] = {
                "enforced": False,
                "reason": (
                    "probe_timed_out"
                    if isinstance(exc, TimeoutError)
                    else "probe_failed"
                ),
                "before_click_detected": before_marker is not None,
                "error_type": type(exc).__name__,
            }
            return out
        out["challenge_check"] = {"enforced": True}
        after_marker = after.get("challengeMarker") if after.get("found") else None
        if after_marker:
            after_marker = str(after_marker)
            out["challenge_detected"] = True
            if before_marker is None or str(before_marker) != after_marker:
                _prime_page_challenge(target_session, after_marker)
                out["attempts"] = 0
                return out
            stalled, attempts = _record_unchanged_page_challenge(
                target_session, after_marker
            )
            out["attempts"] = attempts
            if stalled:
                out.update({
                    "status": "challenge_stalled",
                    "next_action": (
                        "Stop automatic attempts and let the user take over the same tab; "
                        f"resume with session_id={target_session!r} after the challenge clears."
                    ),
                })
        else:
            _clear_page_challenge(target_session)
            out["challenge_detected"] = False
            out["attempts"] = 0
        return out
    finally:
        if directed:
            driver.default_session_id = prev_default


@mcp.tool(
    description=(
        "Insert text into the focused element or a CSS/structured-locator field in a specific tab "
        "using background CDP input; xterm containers automatically retarget their helper textarea. "
        "Optionally clear and submit a key. Missing, ambiguous, or unusable targets dispatch nothing. "
        "CSS/structured matches are reduced to visible, interactable candidates so hidden templates "
        "do not win; the resulting input event is trusted in the page. "
        "The result includes active_element and focus_confirmed so omitted-selector input is auditable. "
        "Nested frame locators support same-origin, cross-origin and OOPIF targets. A call retains "
        "the selected document and element, then checks the focus chain before each input. "
        "Navigation or replacement returns stale_frame; inspect input_dispatched and do not replay "
        "partial or unknown input. A new independent call can locate the new document."
    )
)
def page_type(
    text: str,
    selector: str | dict[str, Any] = "",
    clear: bool = False,
    submit_key: str = "",
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    if not isinstance(text, str):
        raise InputValidationError("text must be a string")
    if not isinstance(selector, (str, dict)):
        raise InputValidationError("selector must be a CSS string or locator object")
    if isinstance(selector, dict):
        normalize_locator(selector)
    if not isinstance(clear, bool):
        raise InputValidationError("clear must be a boolean")
    if not isinstance(submit_key, str):
        raise InputValidationError("submit_key must be a string")
    timeout = _positive_timeout(timeout)
    deadline = time.monotonic() + timeout
    driver = require_driver()
    previous_default = driver.default_session_id
    directed = session_id is not None
    try:
        session_budget = max(0.0, deadline - time.monotonic())
        if session_budget <= 0:
            raise TimeoutError("page_type deadline exhausted before session resolution")
        sessions = ensure_sessions(
            timeout=session_budget,
            fresh=True,
            prune_default=False,
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("page_type deadline exhausted during session resolution")
        if directed:
            requested_sid = str(session_id)
            if not any(
                str(session.get("id")) == requested_sid for session in sessions
            ):
                raise _session_target_not_found(requested_sid, sessions)
            target_session = requested_sid
        else:
            current = (
                str(previous_default) if previous_default is not None else None
            )
            if current and any(
                str(session.get("id")) == current for session in sessions
            ):
                target_session = current
            else:
                candidates = _browser_candidates(sessions, current=previous_default)
                candidates = [
                    item for item in candidates if is_scriptable_url(item.get("url"))
                ] or candidates
                target_session = str(candidates[0]["id"])
                driver.default_session_id = target_session
        resolution_budget = max(0.0, deadline - time.monotonic())
        if resolution_budget <= 0:
            raise TimeoutError("page_type deadline exhausted before target resolution")
        if isinstance(selector, dict) and selector.get("frame"):
            return _run_frame_input(
                selector, "type", target_session, deadline,
                type_commands("", text, select_all=clear, submit_key=submit_key or None)[1:],
                text=text, clear=clear,
            )
        target_info = _page_type_target_info(
            selector,
            clear,
            target_session,
            resolution_budget,
        )
        target = {"selector": selector} if selector else {"focused_element": True}
        focus_info = {}
        if "activeElement" in target_info:
            focus_info["active_element"] = target_info.get("activeElement")
        if "focusConfirmed" in target_info:
            focus_info["focus_confirmed"] = bool(target_info.get("focusConfirmed"))
        if "previousActiveElement" in target_info:
            focus_info["previous_active_element"] = target_info.get("previousActiveElement")
        if not target_info.get("found") or target_info.get("focusConfirmed") is False:
            return {
                "status": (
                    "focus_failed" if target_info.get("found")
                    else target_info.get("status", "not_found")
                ),
                "session_id": target_session,
                "input_mode": "cdp",
                "foreground_changed": False,
                "target": target,
                "target_kind": target_info.get("targetKind", "missing"),
                "typed_chars": 0,
                "input_dispatched": False,
                "retryable": False,
                **focus_info,
                **({"matches": target_info["matches"]} if target_info.get("matches") is not None else {}),
                **({"stage": target_info["stage"]} if target_info.get("stage") else {}),
            }
        target_kind = target_info.get("targetKind", "element")
        submit_delay_ms = (
            _XTERM_SUBMIT_DELAY_MS
            if target_kind == "xterm" and submit_key
            else 0
        )
        input_budget = max(0.0, deadline - time.monotonic())
        if input_budget <= 0:
            raise TimeoutError("page_type deadline exhausted before input dispatch")
        if submit_delay_ms and input_budget <= submit_delay_ms / 1000:
            raise TimeoutError(
                "page_type deadline cannot fit the xterm submit delay"
            )
        ext_cmd = getattr(driver, "ext_cmd", None)
        runtime = {}
        if callable(ext_cmd):
            client_id, _ = _split_session_target(target_session)
            runtime = _extension_data(ext_cmd(
                {"cmd": "bridge_status"}, client_id=client_id, timeout=input_budget
            ))
        capabilities = runtime.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities.get("batch_result_guard") is not True:
            return {
                "status": "stale_extension",
                "error_code": "batch_result_guard_required",
                "session_id": target_session,
                "input_mode": "cdp",
                "foreground_changed": False,
                "target": target,
                "target_kind": target_kind,
                "typed_chars": 0,
                "input_dispatched": False,
                "required_capability": "batch_result_guard",
                "next_action": "reload_extension",
            }
        input_budget = max(0.0, deadline - time.monotonic())
        if input_budget <= 0:
            raise TimeoutError("page_type deadline exhausted during capability check")
        # Re-resolve immediately before input in the same attached CDP batch.
        # The extension must stop the batch if focus or editability was lost.
        # Xterm forwards insertText to its backend asynchronously, so yield once
        # before Enter without breaking the single attached CDP batch.
        target_script = _page_type_target_script(selector, clear)
        # `cmd` is what handleBatch dispatches on. Without it the guard was
        # recorded as `unknown cmd: undefined` and the batch carried on typing,
        # so the re-resolution below protected nothing -- measured live: result
        # [{ok:false, error:"unknown cmd: undefined"}, {}, {}, {}].
        guard = {
            "cmd": "cdp",
            "method": "Runtime.evaluate",
            "params": {
                "expression": (
                    f"(() => {{ const target = {target_script}; "
                    "return target.found === true && target.focusConfirmed === true; })()"
                ),
                "returnByValue": True,
            },
            "assertTruthy": True,
        }
        commands = [guard, *type_commands(
            selector if isinstance(selector, str) else "",
            text,
            select_all=clear,
            submit_key=submit_key or None,
            submit_delay_ms=submit_delay_ms,
        )[1:]]
        try:
            out = _run_page_input(
                commands,
                target_session,
                input_budget,
                session_validated=True,
                deadline=deadline,
            )
        except PageExecutionError as exc:
            if exc.error_code != "batch_guard_failed":
                raise
            return {
                "status": "focus_failed",
                "error_code": exc.error_code,
                "session_id": target_session,
                "input_mode": "cdp",
                "foreground_changed": False,
                "target": target,
                "target_kind": target_kind,
                "typed_chars": 0,
                "input_dispatched": False,
                "focus_confirmed": False,
                "retryable": False,
                "diagnostics": exc.diagnostics,
            }
        out["target"] = target
        out["target_kind"] = target_kind
        out["typed_chars"] = len(text)
        out.update(focus_info)
        return out
    finally:
        if directed:
            driver.default_session_id = previous_default


@mcp.tool(
    description=(
        "Press a key or comma-delimited modifier chord in a specific tab using background CDP "
        "input, without activating the tab."
    )
)
def page_press(
    keys_csv: str,
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    out = _run_page_input(press_commands(keys_csv), session_id, timeout)
    out["target"] = {"keys_csv": keys_csv}
    return out


@mcp.tool(
    description=(
        "Drag between viewport coordinates in a specific tab using one background CDP input "
        "sequence, without activating the tab or moving the desktop cursor. Both endpoints are "
        "viewport-relative CSS pixels, like page_click's coordinate mode, and neither is hit-tested."
    )
)
def page_drag(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    duration: float = 0.3,
    button: str = "left",
    session_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    out = _run_page_input(
        drag_commands(x1, y1, x2, y2, duration=duration, button=button),
        session_id,
        timeout,
    )
    out["target"] = {"from": [x1, y1], "to": [x2, y2]}
    return out


@mcp.tool(
    description=(
        "Set files on a file input, which JS cannot do (input.files is read-only). "
        "Give a CSS selector for the <input type=file> and absolute local paths. Runs as a "
        "single CDP batch so the DOM node ids stay valid across the sequence."
    )
)
def upload_files(
    selector: str,
    paths: str | list[str],
    session_id: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if not isinstance(selector, str) or not selector.strip():
        raise InputValidationError("selector must be a non-empty CSS string")
    if isinstance(paths, str):
        files = [paths]
    elif isinstance(paths, list):
        files = list(paths)
    else:
        raise InputValidationError("paths must be a path string or a list of path strings")
    if not files:
        raise InputValidationError("paths must contain at least one file")
    if any(not isinstance(path, str) or not path for path in files):
        raise InputValidationError("every upload path must be a non-empty string")
    timeout = _positive_timeout(timeout)
    missing = [p for p in files if not Path(p).is_file()]
    if missing:
        raise RuntimeError(f"file(s) not found: {missing}")
    files = [str(Path(p).resolve()) for p in files]
    # One batch, one attach: DOM.getDocument's nodeId is only valid while the
    # debugger stays attached, so this cannot be split across cdp_command calls.
    batch = {
        "cmd": "batch",
        # The outer bridge wait and the debugger batch share the same public
        # budget. Without this, a timed-out caller can leave the extension
        # attaching a debugger and setting files after the result was abandoned.
        "deadlineEpochMs": int(time.time() * 1000 + timeout * 1000),
        "timeoutMs": max(1, int(timeout * 1000)),
        "commands": [
            {"cmd": "cdp", "method": "DOM.getDocument", "params": {"depth": -1}},
            {"cmd": "cdp", "method": "DOM.querySelector",
             "params": {"nodeId": "$0.root.nodeId", "selector": selector}},
            {"cmd": "cdp", "method": "DOM.setFileInputFiles",
             "params": {"nodeId": "$1.nodeId", "files": files}},
        ],
    }
    result = _extension_batch(batch, session_id=session_id, timeout=timeout)
    # The extension's batch reply arrives as a BARE ARRAY — handleBatch returns
    # {ok, results} and both the WS envelope and the bridge's ext_cmd unwrap it
    # to the results list, so `data` here is the list, not a wrapper dict. Only
    # the error path (handleBatch catch) surfaces as {ok:false, error, results}.
    data = result.get("data")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(f"upload failed: {data.get('error')}")
    results = data if isinstance(data, list) else (
        data.get("results") if isinstance(data, dict) else None)
    if not isinstance(results, list) or len(results) < 2 or not isinstance(results[1], dict):
        raise RuntimeError(
            "upload returned an incomplete batch result; the file-input state "
            "could not be verified, so inspect the page before retrying"
        )
    node = results[1].get("nodeId")
    if isinstance(node, bool) or not isinstance(node, int) or node <= 0:
        raise RuntimeError(
            f"selector {selector!r} matched no element (DOM.querySelector returned "
            f"{results[1]}); check the selector and that the input is in the top frame")
    if len(results) < 3 or not isinstance(results[2], dict):
        raise RuntimeError(
            "upload batch did not confirm DOM.setFileInputFiles; the file-input "
            "state is unknown, so inspect the page before retrying"
        )
    return {"status": "ok", "selector": selector, "files": files, "node_id": node}


# --- Cookies: read through the extension, write through CDP ------------------
@mcp.tool(description="Get cookies for the current page or specified tab via the Chrome extension bridge.")
def get_cookies(session_id: Optional[str] = None, tab_id: Optional[int] = None) -> dict[str, Any]:
    client_id = _extension_client_id(session_id)
    payload: dict[str, Any] = {"cmd": "cookies"}
    # `session_id` names both the extension namespace and its native tab. Keep
    # that identity when the caller did not provide a separate tab_id; sending
    # the JSON command through execute_js would make the page evaluator parse
    # {"cmd": ...} as JavaScript, which is precisely the protocol split this
    # command channel exists to avoid.
    if session_id is not None and tab_id is None:
        client_id, tab_id = _split_session_target(session_id)
    if tab_id is not None:
        payload["tabId"] = tab_id
    response = require_driver().ext_cmd(
        payload,
        client_id=client_id,
        timeout=15.0,
    )
    result = _extension_data(response)
    if result.get("ok") is False:
        raise RuntimeError(result.get("error") or "the browser refused to read cookies")
    return result


# --- cookie 写入 -------------------------------------------------------------
# 读走扩展的 chrome.cookies（get_cookies），写走 CDP Network.setCookie：CDP 能写
# HttpOnly / 跨路径 / 指定 domain，页面内的 document.cookie 一样都做不到。CDP 不可
# 用时（debugger 被占、页面禁止 attach）才退到 document.cookie，且必须说清降级后
# 哪些字段丢了 —— 静默丢掉 HttpOnly 会让调用方以为写进去了。
_SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None",
             "no_restriction": "None", "unspecified": None}


def _parse_cookies_arg(cookies: Any) -> list[dict[str, Any]]:
    """接受 JSON 文本 / 单个 dict / dict 列表，统一成列表。"""
    if isinstance(cookies, str):
        text = cookies.strip()
        if not text:
            raise ValueError("cookies must not be empty")
        try:
            cookies = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"cookies is not valid JSON: {e}") from None
    if isinstance(cookies, dict):
        cookies = [cookies]
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("cookies must be a non-empty cookie object or list of cookie objects")
    return cookies


def _normalize_cookie(raw: Any, index: int) -> dict[str, Any]:
    """校验并转成 CDP Network.setCookie 的参数形状。"""
    where = f"cookies[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object, got {type(raw).__name__}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"{where} is missing name")
    # 名字里带 '=' 或 ';' 会把 Cookie 头拆坏，CDP 也不会替你挡。
    if any(ch in name for ch in "=;,\r\n \t"):
        raise ValueError(f"{where} name contains an invalid separator or whitespace: {name!r}")
    out: dict[str, Any] = {"name": name, "value": str(raw.get("value", ""))}
    if any(ch in out["value"] for ch in ";\r\n"):
        raise ValueError(f"{where} value contains a semicolon or newline; encode it before calling set_cookies")
    for key in ("url", "domain", "path"):
        val = raw.get(key)
        if val not in (None, ""):
            out[key] = str(val)
    # 同时收 httponly / http_only 这些写法：agent 常混着传，静默忽略等于静默丢标志。
    for key, aliases in (("httpOnly", ("httpOnly", "httponly", "http_only")),
                         ("secure", ("secure",))):
        val = next((raw[a] for a in aliases if a in raw), None)
        if val is not None:
            out[key] = bool(val)
    expires = raw.get("expires", raw.get("expirationDate"))
    # Same outcome as `not in (None, "")`, spelled so that the `None` case is a
    # separate test: that is what lets a type checker see `float()` below can
    # never receive None, and it is the one exclusion a reader needs to find.
    if expires is not None and expires != "":
        try:
            out["expires"] = float(expires)
        except (TypeError, ValueError):
            raise ValueError(f"{where} expires must be a Unix timestamp in seconds, got {expires!r}") from None
    same = raw.get("sameSite", raw.get("same_site"))
    if same not in (None, ""):
        key = str(same).strip().lower()
        if key not in _SAMESITE:
            raise ValueError(
                f"{where} sameSite must be Strict, Lax, or None; got {same!r}")
        norm = _SAMESITE[key]
        if norm is not None:
            out["sameSite"] = norm
    # SameSite=None 没有 Secure 会被浏览器整条丢掉，且不报错 —— 提前挡住。
    if out.get("sameSite") == "None" and not out.get("secure"):
        raise ValueError(f"{where} sameSite='None' requires secure=true or the browser will reject the cookie")
    return out


def _page_location(session_id: Optional[str] = None,
                   timeout: float = 10.0) -> dict[str, Any]:
    """当前页的 location，用于给没写 url/domain 的 cookie 补上作用域。"""
    resp = exec_js(
        "JSON.stringify({url: location.href, origin: location.origin,"
        " host: location.hostname, protocol: location.protocol,"
        " path: location.pathname})",
        session_id=session_id, timeout=timeout)
    raw = resp.get("data")
    info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return info if isinstance(info, dict) else {}


_SITE_PERMISSION_CONTENT_SETTINGS = {
    "notifications": "notifications",
    "geolocation": "location",
    "location": "location",
    "camera": "camera",
    "microphone": "microphone",
}
_SITE_PERMISSION_SETTINGS = {"allow", "block", "ask"}


# --- Tools: site permissions (operator approval required) --------------------
def _normalize_site_permission_origin(raw_origin: Any) -> str:
    if not isinstance(raw_origin, str) or not raw_origin.strip():
        raise ValueError("origin must be an http or https origin")
    try:
        parsed = urlsplit(raw_origin.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin must be an http or https origin") from exc
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("origin must be an http or https origin")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _validate_site_permission_duration(duration_seconds: Any) -> int:
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 60 <= duration_seconds <= 600:
        raise ValueError("duration_seconds must be an integer between 60 and 600")
    return duration_seconds


def _site_permission_spec(permission: Any) -> dict[str, str]:
    if not isinstance(permission, str):
        raise ValueError("unsupported permission")
    name = permission.strip().lower()
    if name == "clipboard":
        return {"kind": "clipboard", "setting": "clipboard"}
    content_setting = _SITE_PERMISSION_CONTENT_SETTINGS.get(name)
    if content_setting is None:
        raise ValueError("unsupported permission")
    return {"kind": "content", "setting": content_setting}


def _validate_site_permission_setting(setting: Any) -> str:
    if not isinstance(setting, str) or setting not in _SITE_PERMISSION_SETTINGS:
        raise ValueError("setting must be one of: allow, block, ask")
    return setting


class SitePermissionApproval(BaseModel):
    approve: StrictBool = Field(description="Approve this temporary site permission")


# An elicitation is answered by a human, and the global tool lock is held for the
# whole await (see _threaded_tool), so a prompt nobody answers does not just
# stall its own tool — it wedges every other BTAP tool in this process until the
# client cancels the request. Bound the wait: an unanswered approval is exactly
# the "declined, cancelled, or unavailable" case both callers already report as
# requires_user_action.
_APPROVAL_TIMEOUT_ENV = "BROWSERTAP_APPROVAL_TIMEOUT"
_DEFAULT_APPROVAL_TIMEOUT = 120.0


def _approval_timeout() -> float:
    raw = (os.environ.get(_APPROVAL_TIMEOUT_ENV) or "").strip()
    if not raw:
        return _DEFAULT_APPROVAL_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s is not a number; using %ss",
                       _APPROVAL_TIMEOUT_ENV, _DEFAULT_APPROVAL_TIMEOUT)
        return _DEFAULT_APPROVAL_TIMEOUT
    return value if math.isfinite(value) and value > 0 else _DEFAULT_APPROVAL_TIMEOUT


async def _request_site_permission_approval(
    ctx: Context, permission: str, origin: str, duration_seconds: int
) -> _ApprovalDecision:
    return await _request_approval(
        ctx,
        message=("BTAP requests temporary site permission: "
                 f"allow {permission} for {origin} for {duration_seconds} seconds"),
        schema=SitePermissionApproval,
        approvals=_LAB_SITE_PERMISSION_APPROVALS,
        category="Site-permission",
    )


def _site_permission_requires_user_action(reason: _ApprovalReason | None) -> dict[str, Any]:
    return {
        "status": "requires_user_action",
        "message": "Temporary site-permission approval was declined, cancelled, or unavailable.",
        "reason": reason,
    }


def _site_permission_extension_result(response: Any) -> dict[str, Any]:
    result = _extension_data(response)
    classified = dict(result)
    if result.get("unsupported"):
        classified["status"] = "unsupported"
        classified["message"] = str(result.get("error") or "browser API unavailable")
        return classified
    if result.get("ok") is False:
        classified["status"] = "error"
        classified["message"] = str(result.get("error") or "site permission failed")
        return classified
    return classified


@mcp.tool(
    description=(
        "Temporarily set an origin-scoped browser site permission for 60-600 seconds. "
        "Only http/https origins and notifications, geolocation/location, camera or microphone are supported; "
        "clipboard returns unsupported because its prior state cannot be restored. "
        "safe asks on every allow; lab skips prompts by default and restores session approval only "
        "when BROWSERTAP_LAB_NO_ELICIT is explicitly disabled. Leases attempt to restore their prior setting. "
        "requires_user_action.reason distinguishes elicitation_unsupported, declined, timeout, cancelled "
        "and error; an unsuccessful approval never sends a permission grant. "
        "If restoration becomes unsupported, manual_recovery retains that setting and recovery guidance "
        "without automatic retries; an explicit reset can retry after the cause is resolved."
    )
)
async def set_site_permission(
    ctx: Context,
    permission: str,
    setting: str,
    origin: str = "",
    duration_seconds: int = 300,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    spec = _site_permission_spec(permission)
    normalized_setting = _validate_site_permission_setting(setting)
    duration = _validate_site_permission_duration(duration_seconds)
    def prepare() -> tuple[BrowserBridge, Optional[str], str, str, int, str]:
        driver = require_driver()
        previous_default = driver.default_session_id
        target_sid = (
            switch_session(session_id=session_id)
            if session_id is not None
            else switch_session()
        )
        try:
            selected_origin = origin or str(_page_location(target_sid).get("url") or "")
            normalized_origin = _normalize_site_permission_origin(selected_origin)
        except Exception:
            if session_id is not None:
                driver.default_session_id = previous_default
            raise
        client_id, tab_id = _split_session_target(target_sid)
        return driver, previous_default, target_sid, client_id, tab_id, normalized_origin

    (
        driver,
        previous_default,
        target_sid,
        client_id,
        tab_id,
        normalized_origin,
    ) = await anyio.to_thread.run_sync(prepare)
    try:
        if normalized_setting == "allow":
            decision = await _request_site_permission_approval(
                ctx, spec["setting"], normalized_origin, duration
            )
            if not decision.approved:
                return _site_permission_requires_user_action(decision.reason)
        response = await anyio.to_thread.run_sync(
            lambda: driver.ext_cmd(
                {
                    "cmd": "site_permission",
                    "action": "set",
                    "tabId": tab_id,
                    "permission": spec["setting"],
                    "setting": normalized_setting,
                    "origin": normalized_origin,
                    "durationSeconds": duration,
                },
                client_id=client_id,
                timeout=20.0,
            )
        )
        result = _site_permission_extension_result(response)
        result.setdefault("origin", normalized_origin)
        result.setdefault("permission", spec["setting"])
        result.setdefault("duration_seconds", duration)
        if result.get("status") not in {"unsupported", "error"}:
            result.setdefault("status", "ok")
        return result
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default


@mcp.tool(
    description=(
        "Attempt to restore matching temporary site-permission leases now, including manual_recovery records. "
        "Omit origin and permission to reset every lease for the selected browser; origin accepts only http/https. "
        "Unsupported restoration preserves the prior setting and recovery guidance as manual_recovery "
        "and stops automatic retries. Resolve that cause before another explicit reset."
    )
)
def reset_site_permissions(
    origin: str = "",
    permission: str = "",
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    normalized_origin = _normalize_site_permission_origin(origin) if origin else ""
    spec = _site_permission_spec(permission) if permission else None
    driver = require_driver()
    previous_default = driver.default_session_id
    target_sid = switch_session(session_id=session_id) if session_id is not None else switch_session()
    client_id, tab_id = _split_session_target(target_sid)
    try:
        response = driver.ext_cmd(
            {
                "cmd": "site_permission",
                "action": "reset",
                "tabId": tab_id,
                "origin": normalized_origin,
                "permission": spec["setting"] if spec else "",
            },
            client_id=client_id,
            timeout=20.0,
        )
        result = _site_permission_extension_result(response)
        result.setdefault("origin", normalized_origin)
        if spec:
            result.setdefault("permission", spec["setting"])
        if result.get("status") not in {"unsupported", "error"}:
            result.setdefault("status", "ok")
        return result
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default


# --- Cookie writes: CDP path with a document.cookie fallback -----------------
def _resolve_cdp_target(
    session_id: Optional[str], tab_id: Optional[int],
) -> tuple[str, str, int]:
    """Resolve ``(session_id, client_id, tab_id)`` for an extension-routed CDP call.

    An explicit composite session names both the browser client and the tab; a
    bare ``tab_id`` borrows the client of the current default (or the only
    connected browser); neither means the remembered default, re-picked when
    stale exactly like every other implicit page call.
    """
    if session_id is not None:
        client_id, session_tab = _split_session_target(str(session_id))
    else:
        client_id, session_tab = _split_session_target(switch_session())
    target_tab = int(tab_id) if tab_id is not None else session_tab
    return f"{client_id}:{target_tab}", client_id, target_tab


def _cdp(method: str, params: dict[str, Any], session_id: Optional[str],
         tab_id: Optional[int], timeout: float) -> Any:
    # Routed through ext_cmd, never as a text script: since the cmd/code split
    # the extension evaluates whatever arrives in `code` as page JavaScript, so
    # a JSON envelope sent that way died with `SyntaxError: Unexpected token ':'`
    # and every cookie write silently took the document.cookie fallback --
    # HttpOnly dropped, `status: ok` reported. `_direct_cdp` keeps the text
    # fallback for the one case it is safe in: an old router that answers
    # `unknown cmd`.
    target_sid, client_id, target_tab = _resolve_cdp_target(session_id, tab_id)
    return _direct_cdp(
        method, params, session_id=target_sid, client_id=client_id,
        tab_id=target_tab, timeout=timeout,
    )


def _extension_batch(
    payload: dict[str, Any], *, session_id: Optional[str], timeout: float,
) -> dict[str, Any]:
    """Send one ``batch`` envelope to the extension's command router.

    Same reason as ``_cdp``: a batch is an extension command, so it travels on
    the ``cmd`` field. ``exec_js`` remains only for a driver without ``ext_cmd``
    (embedded/fake drivers) or a router that explicitly reports ``unknown cmd``;
    a timeout or transport failure is never replayed through the text route,
    because the batch may already have run. The reply keeps the historical
    shape: ``{"data": <results list | {ok: false, ...}>}``.
    """
    timeout = _positive_timeout(timeout)
    deadline = time.monotonic() + timeout
    driver = require_driver()
    ext_cmd = getattr(driver, "ext_cmd", None)
    if not callable(ext_cmd):
        return exec_js(json.dumps(payload), session_id=session_id, timeout=timeout)
    target_sid, client_id, target_tab = _resolve_cdp_target(session_id, None)
    wire = dict(payload)
    wire.setdefault("tabId", target_tab)
    try:
        return ext_cmd(wire, client_id=client_id, timeout=timeout)
    except BaseException as exc:
        fallback_budget = max(0.0, deadline - time.monotonic())
        if isinstance(exc, TimeoutError) or fallback_budget <= 0:
            raise
        if not _unknown_command_error(exc):
            raise
        return exec_js(json.dumps(payload), session_id=target_sid, timeout=fallback_budget)


def _cookie_via_document(cookie: dict[str, Any], session_id: Optional[str],
                         timeout: float) -> dict[str, Any]:
    """降级路径：页面内 document.cookie，写完立刻回读确认。

    document.cookie 写不了 HttpOnly，也写不了别的域，所以这里只报"写没写进去"
    这一件事实，剩下的差异原样交回给调用方。
    """
    script = f"""
    const c = {json.dumps(cookie)};
    // 写和回读用同一个 encode 后的名字：非 ASCII 名字写进去是 %XX，拿原名去
    // 匹配会读不到，然后把一次成功的写入报成失败。
    const n = encodeURIComponent(c.name);
    const parts = [n + '=' + encodeURIComponent(c.value || '')];
    parts.push('path=' + (c.path || '/'));
    if (c.domain) parts.push('domain=' + c.domain);
    if (c.expires) parts.push('expires=' + new Date(c.expires * 1000).toUTCString());
    if (c.secure) parts.push('secure');
    if (c.sameSite) parts.push('samesite=' + c.sameSite);
    try {{ document.cookie = parts.join('; '); }}
    catch (e) {{ return JSON.stringify({{ok: false, error: String(e && e.message || e)}}); }}
    const re = new RegExp('(?:^|; )' + n.replace(/([.*+?^${{}}()|[\\]\\\\])/g, '\\\\$1') + '=');
    return JSON.stringify({{ok: re.test(document.cookie), cookie_header_len: document.cookie.length}});
    """
    resp = exec_js(script, session_id=session_id, timeout=timeout)
    raw = resp.get("data")
    info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return info if isinstance(info, dict) else {}


@mcp.tool(
    description=(
        "Write cookies into the real browser profile. Takes one cookie object or a list "
        "(JSON text is accepted): name is required, plus optional value/url/domain/path/"
        "expires (Unix seconds)/httpOnly/secure/sameSite. Uses CDP Network.setCookie so "
        "HttpOnly and cross-path cookies work; falls back to document.cookie only if CDP "
        "is unavailable, and then says which cookies could not carry HttpOnly. Cookies "
        "with neither url nor domain are scoped to the current page."
    )
)
def set_cookies(
    cookies: str | list | dict,
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    items = [_normalize_cookie(c, i) for i, c in enumerate(_parse_cookies_arg(cookies))]
    page: dict[str, Any] = {}
    if any("url" not in c and "domain" not in c for c in items):
        page = _page_location(session_id=session_id, timeout=min(timeout, 10.0))
        if not page.get("url"):
            raise RuntimeError(
                "Could not read the current page URL to determine cookie scope; provide url or domain for every cookie.")
    results: list[dict[str, Any]] = []
    for cookie in items:
        params = dict(cookie)
        scoped_to_page = False
        if "url" not in params and "domain" not in params:
            params["url"] = page["url"]
            scoped_to_page = True
        entry: dict[str, Any] = {"name": params["name"], "method": "cdp"}
        if scoped_to_page:
            entry["scoped_to"] = params["url"]
        try:
            data = _cdp("Network.setCookie", params, session_id, tab_id, timeout)
            # 老版本协议回 {success: bool}，新版本回 {} —— 没有该字段就按成功算，
            # 真失败时 handleCDP 会抛 error，走不到这里。
            ok = data.get("success", True) if isinstance(data, dict) else True
            entry["status"] = "ok" if ok else "failed"
            if not ok:
                entry["error"] = "CDP Network.setCookie returned success=false; domain or secure likely conflicts with the current page"
        except Exception as e:
            entry["cdp_error"] = str(e)
            entry["method"] = "document.cookie"
            if cookie.get("httpOnly"):
                entry["httpOnly_dropped"] = True
            if tab_id is not None:
                # document.cookie runs in the DEFAULT tab, not the named one —
                # writing there would report ok for a cookie that never reached
                # the target. Fail loudly instead of lying.
                entry["status"] = "failed"
                entry["error"] = (
                    f"CDP is unavailable ({e}) and tab_id={tab_id} was explicit. The document.cookie fallback "
                    "can only target the default tab; remove tab_id or retry with session_id."
                )
            else:
                try:
                    fb = _cookie_via_document(params, session_id, timeout)
                    entry["status"] = "ok" if fb.get("ok") else "failed"
                    if not fb.get("ok"):
                        entry["error"] = (
                            fb.get("error")
                            or "The cookie was not readable after document.cookie wrote it; domain/secure rules or the browser may have rejected it")
                    elif cookie.get("httpOnly"):
                        entry["note"] = "Used the document.cookie fallback. Page JavaScript cannot set HttpOnly, so this cookie is not HttpOnly."
                except Exception as e2:
                    entry["status"] = "failed"
                    entry["error"] = f"Both CDP and document.cookie failed: {e2}"
        results.append(entry)
    ok_count = sum(1 for r in results if r.get("status") == "ok")
    status = "ok" if ok_count == len(results) else ("partial" if ok_count else "failed")
    out: dict[str, Any] = {
        "status": status,
        "set": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }
    if status != "ok":
        out["hint"] = "Some cookies were not written. Verify with get_cookies and confirm domain, secure, and sameSite match the target site."
    return out


@mcp.tool(
    description=(
        "Delete a cookie by name from the real browser profile. Scope defaults to the "
        "current page (url), or pass domain/path/url to target another scope. Uses CDP "
        "Network.deleteCookies, falling back to expiring it via document.cookie."
    )
)
def delete_cookies(
    name: str,
    domain: Optional[str] = None,
    path: Optional[str] = None,
    url: Optional[str] = None,
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    cookie_name = str(name or "").strip()
    if not cookie_name:
        raise ValueError("name must not be empty")
    params: dict[str, Any] = {"name": cookie_name}
    if url:
        params["url"] = str(url)
    if domain:
        params["domain"] = str(domain)
    if path:
        params["path"] = str(path)
    scoped_to_page = "url" not in params and "domain" not in params
    if scoped_to_page:
        page = _page_location(session_id=session_id, timeout=min(timeout, 10.0))
        if not page.get("url"):
            raise RuntimeError("Could not read the current page URL to determine deletion scope; provide url or domain.")
        params["url"] = page["url"]
    out: dict[str, Any] = {"name": cookie_name, "scope": {k: v for k, v in params.items() if k != "name"},
                           "method": "cdp"}
    try:
        _cdp("Network.deleteCookies", params, session_id, tab_id, timeout)
        out["status"] = "ok"
    except Exception as e:
        out["cdp_error"] = str(e)
        out["method"] = "document.cookie"
        if tab_id is not None:
            # The expiration script runs in the default tab; expiring cookies
            # there lies about the named tab's cookies. Fail loudly instead.
            out["status"] = "failed"
            out["error"] = (
                f"CDP is unavailable ({e}) and tab_id={tab_id} was explicit. The document.cookie expiry "
                "fallback can only target the default tab; remove tab_id or retry with session_id."
            )
            return out
        script = f"""
        const c = {json.dumps({'name': cookie_name, 'domain': params.get('domain'), 'path': params.get('path')})};
        const n = encodeURIComponent(c.name);
        // 删除必须 path/domain 全中才生效，调用方常常两个都没给：把当前路径和
        // 裸/点两种 domain 写法都试一遍，比让它"删了但还在"好。
        const paths = c.path ? [c.path] : ['/', location.pathname];
        const domains = c.domain ? [c.domain] : [null, location.hostname, '.' + location.hostname];
        for (const p of paths) for (const d of domains) {{
          let s = n + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=' + p;
          if (d) s += '; domain=' + d;
          try {{ document.cookie = s; }} catch (_) {{}}
        }}
        const re = new RegExp('(?:^|; )' + n.replace(/([.*+?^${{}}()|[\\]\\\\])/g, '\\\\$1') + '=');
        return JSON.stringify({{gone: !re.test(document.cookie)}});
        """
        try:
            resp = exec_js(script, session_id=session_id, timeout=timeout)
            raw = resp.get("data")
            info = json.loads(raw) if isinstance(raw, str) else (raw or {})
            out["status"] = "ok" if info.get("gone") else "failed"
            if not info.get("gone"):
                out["error"] = "The cookie remained after the expiry fallback; it may be HttpOnly or scoped differently. Provide domain and path."
        except Exception as e2:
            out["status"] = "failed"
            out["error"] = f"Both CDP and document.cookie failed: {e2}"
    return out


# --- localStorage / sessionStorage -------------------------------------------
_STORAGE_AREAS = {"local": "localStorage", "session": "sessionStorage",
                  "localstorage": "localStorage", "sessionstorage": "sessionStorage"}
# 整个 localStorage 可能有几 MB，原样回给 agent 会把上下文烧光。
_STORAGE_DUMP_LIMIT = 20000


def _storage_area(area: str) -> str:
    key = str(area or "").strip().lower()
    if key not in _STORAGE_AREAS:
        raise ValueError(f"area must be 'local' or 'session', got {area!r}")
    return _STORAGE_AREAS[key]


def _storage_transport_error(exc: Exception, *, read_only: bool) -> dict[str, Any]:
    if isinstance(exc, BridgeNoResponseError):
        error_code = exc.error_code
        delivery_state = exc.delivery_state
        retry_safe = read_only or exc.retry_safe
    elif isinstance(exc, TimeoutError):
        error_code = "timeout"
        delivery_state = "unknown"
        retry_safe = read_only
    else:
        error_code = "bridge_error"
        delivery_state = "unknown"
        retry_safe = read_only
    return {
        "error_code": error_code,
        "delivery_state": delivery_state,
        "retry_safe": retry_safe,
        "retryable": retry_safe,
    }


@mcp.tool(
    description=(
        "Read localStorage or sessionStorage. Give a key for one value, or omit it to dump "
        "every key (values are truncated past ~20k chars and truncated is reported). "
        "area='local' (default) or 'session'."
    )
)
def storage_get(
    key: Optional[str] = None,
    area: str = "local",
    session_id: Optional[str] = None,
    timeout: float = 30.0,
    offset: int = 0,
    max_items: int = 500,
    max_bytes: int = _STORAGE_DUMP_LIMIT,
) -> dict[str, Any]:
    store = _storage_area(area)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 5000:
        raise ValueError("max_items must be an integer between 1 and 5000")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 5_000_000:
        raise ValueError("max_bytes must be an integer between 1 and 5000000")
    script = f"""
    try {{
      const s = window.{store};
      if (!s) return JSON.stringify({{ok: false, error: '{store} is unavailable'}});
      const key = {json.dumps(key)};
      if (key !== null) {{
        const v = s.getItem(key);
        return JSON.stringify({{ok: true, found: v !== null, value: v}});
      }}
      const items = {{}};
      const offset = {offset}, maxItems = {max_items}, maxBytes = {max_bytes};
      let bytes = 0, truncated = false, nextOffset = null, emitted = 0;
      for (let i = offset; i < s.length; i++) {{
        const k = s.key(i);
        const v = s.getItem(k) || '';
        const itemBytes = new Blob([k, v]).size;
        if (emitted >= maxItems || bytes + itemBytes > maxBytes) {{
          truncated = true; nextOffset = i; break;
        }}
        items[k] = v;
        bytes += itemBytes;
        emitted += 1;
      }}
      return JSON.stringify({{ok: true, items, total_keys: s.length, truncated,
        next_offset: nextOffset, bytes}});
    }} catch (e) {{
      return JSON.stringify({{ok: false, error: String(e && e.message || e)}});
    }}
    """
    try:
        resp = exec_js(script, session_id=session_id, timeout=timeout)
        raw = resp.get("data")
        info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception as exc:
        message = str(exc)
        return {
            "status": "error",
            "area": store,
            "key": key,
            "error": message,
            **_storage_transport_error(exc, read_only=True),
            "hint": "The storage call failed in-page; the MCP connection remains usable. Run list_tabs, then retry the same directed session once.",
        }
    if not isinstance(info, dict):
        return {"status": "error", "area": store, "error_code": "invalid_response",
                "error": f"The page returned an unexpected storage result: {info!r}", "retryable": True}
    if not info.get("ok"):
        return {"status": "error", "area": store, "error_code": "storage_unavailable",
                "error": info.get("error") or "Storage is inaccessible",
                "retryable": False,
                "hint": "This can occur when third-party storage is blocked or the page uses a sandbox/data context. Retry on a normal http(s) page."}
    out: dict[str, Any] = {"status": "success", "area": store}
    if key is not None:
        out["key"] = key
        out["found"] = bool(info.get("found"))
        out["value"] = info.get("value")
        if not out["found"]:
            out["note"] = "The key does not exist (value is null, which differs from a stored empty string)."
        return out
    out["items"] = info.get("items") or {}
    out["count"] = len(out["items"])
    out["total_keys"] = info.get("total_keys")
    out["offset"] = offset
    out["bytes"] = info.get("bytes", 0)
    if info.get("truncated"):
        out["truncated"] = True
        out["next_offset"] = info.get("next_offset")
        out["hint"] = "Storage output hit max_items or max_bytes; continue with next_offset or read a single key."
    else:
        out["truncated"] = False
    return out


@mcp.tool(
    description=(
        "Write one key into localStorage or sessionStorage and read it back to confirm. "
        "area='local' (default) or 'session'. Values are strings; non-string values are "
        "JSON-encoded first."
    )
)
def storage_set(
    key: str,
    value: str,
    area: str = "local",
    session_id: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    store = _storage_area(area)
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    encoded = False
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
        encoded = True
    script = f"""
    try {{
      const s = window.{store};
      if (!s) return JSON.stringify({{ok: false, error: '{store} is unavailable'}});
      const k = {json.dumps(key)}, v = {json.dumps(value)};
      const existed = s.getItem(k) !== null;
      s.setItem(k, v);
      // 回读确认：配额满 / 隐私模式下 setItem 可能抛，也可能静默不落盘。
      return JSON.stringify({{ok: s.getItem(k) === v, existed, keys: s.length}});
    }} catch (e) {{
      return JSON.stringify({{ok: false, error: String(e && e.message || e)}});
    }}
    """
    try:
        resp = exec_js(script, session_id=session_id, timeout=timeout)
        raw = resp.get("data")
        info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception as exc:
        message = str(exc)
        return {
            "status": "error",
            "area": store,
            "key": key,
            "error": message,
            **_storage_transport_error(exc, read_only=False),
            "hint": "The write failed without closing MCP. Confirm with storage_get before retrying because a timed-out write may have landed.",
        }
    if not isinstance(info, dict) or not info.get("ok"):
        err = info.get("error") if isinstance(info, dict) else None
        return {
            "status": "failed", "area": store, "key": key,
            "error": err or "The read-back value differs after writing; storage quota or browser policy may have blocked it",
            "hint": "Confirm this is a normal http(s) page with site data enabled; remove unused keys if the quota is full.",
        }
    out: dict[str, Any] = {
        "status": "success", "area": store, "key": key,
        "bytes": len(value), "replaced": bool(info.get("existed")),
        "total_keys": info.get("keys"),
    }
    if encoded:
        out["note"] = "The non-string value was JSON-serialized before storage."
    return out


# --- Tool: capture_page_screenshot -------------------------------------------
def _image_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Read pixel dimensions out of an encoded image header.

    Only the three formats this server will ask CDP for, and deliberately by
    hand rather than through Pillow: page screenshots have no imaging dependency
    today, and taking one would make them fail on an install without the
    optional desktop extra. Reading the bytes already in memory also avoids a
    second CDP round trip, which would mean a second debugger attach/detach on
    the tab -- the thing AGENTS.md section 4 measured at 15s instead of 0.16s.

    Returns None rather than raising: a screenshot whose header cannot be parsed
    is still a screenshot, and the caller reports the gap instead of losing the
    pixels over it. A truncated header parses to zeroes rather than failing, so
    the result is validated here instead of at each format's return -- reporting
    a 0x0 image would be worse than reporting nothing.
    """
    parsed = _parse_image_header(raw)
    if parsed is None:
        return None
    width, height = parsed
    if width <= 0 or height <= 0:
        return None
    return width, height


def _parse_image_header(raw: bytes) -> tuple[int, int] | None:
    if not isinstance(raw, bytes):
        return None
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            if len(raw) < 24:
                return None
            return (
                int.from_bytes(raw[16:20], "big"),
                int.from_bytes(raw[20:24], "big"),
            )
        if raw[:2] == b"\xff\xd8":
            # Walk the segment chain to the frame header. Chrome emits baseline
            # JPEG, but progressive (SOF2) costs nothing to accept.
            offset = 2
            while offset + 4 <= len(raw):
                if raw[offset] != 0xFF:
                    return None
                marker = raw[offset + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    offset += 2
                    continue
                length = int.from_bytes(raw[offset + 2:offset + 4], "big")
                # SOFn carries the frame size; 0xC4/0xC8/0xCC share the range
                # but are Huffman/arithmetic tables, not frame headers.
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    if len(raw) < offset + 9:
                        return None
                    return (
                        int.from_bytes(raw[offset + 7:offset + 9], "big"),
                        int.from_bytes(raw[offset + 5:offset + 7], "big"),
                    )
                if length < 2:
                    return None
                offset += 2 + length
            return None
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            chunk = raw[12:16]
            if chunk == b"VP8X":
                if len(raw) < 30:
                    return None
                # Canvas size is stored minus one, 24 bits little-endian each.
                return (
                    int.from_bytes(raw[24:27], "little") + 1,
                    int.from_bytes(raw[27:30], "little") + 1,
                )
            if chunk == b"VP8 ":
                # Lossy keyframe: 3-byte start code, then 14-bit dimensions with
                # a 2-bit scale field in the high bits.
                if len(raw) < 30 or raw[23:26] != b"\x9d\x01\x2a":
                    return None
                return (
                    int.from_bytes(raw[26:28], "little") & 0x3FFF,
                    int.from_bytes(raw[28:30], "little") & 0x3FFF,
                )
            if chunk == b"VP8L":
                if len(raw) < 25 or raw[20] != 0x2F:
                    return None
                bits = int.from_bytes(raw[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            return None
    except (IndexError, ValueError):
        return None
    return None


_SCREENSHOT_PIXEL_NOTE = (
    "Pixel dimensions are DEVICE pixels: CSS pixels x the page's devicePixelRatio, so on a "
    "scaled display they do not match the coordinates page_click and page_drag take. Divide by "
    "devicePixelRatio (read it with execute_js) before turning a point in this image into a "
    "page_click coordinate, or avoid the conversion entirely by clicking a selector from "
    "scan_page, which is hit-tested. Some clients also downscale an attached image before the "
    "model sees it, so rescale any point read off the picture to image_width x image_height first."
)


@mcp.tool(
    description=(
        "Capture a viewport, full-page, or clipped screenshot of a page/tab via CDP with optional "
        "JPEG/WebP quality. Returns text metadata plus an "
        "attached MCP image even when save_path is set; save_path only controls disk output. "
        "image_width/image_height are DEVICE pixels (CSS x devicePixelRatio), not the CSS pixels "
        "page_click takes, and `size` is the byte count. "
        "If the current model cannot consume images, it has not seen the pixels and must use "
        "scan_page, execute_js, a page-specific API, or OCR instead. Base64 is included only "
        "when return_base64=true."
    )
)
def capture_page_screenshot(
    session_id: Optional[str] = None,
    tab_id: Optional[int] = None,
    format: str = "png",
    full_page: bool = False,
    clip: Optional[dict[str, float]] = None,
    quality: Optional[int] = None,
    save_path: str = "",
    return_base64: bool = False,
    timeout: float = 20.0,
) -> CallToolResult:
    normalized_format = str(format).strip().lower()
    if normalized_format == "jpg":
        normalized_format = "jpeg"
    if normalized_format not in {"png", "jpeg", "webp"}:
        raise ValueError("format must be png, jpeg, or webp")
    if full_page and clip is not None:
        raise ValueError("full_page and clip are mutually exclusive")
    if quality is not None:
        if normalized_format == "png":
            raise ValueError("quality is only valid for jpeg or webp")
        if isinstance(quality, bool) or not 0 <= int(quality) <= 100:
            raise ValueError("quality must be between 0 and 100")
    normalized_clip = None
    if clip is not None:
        if not isinstance(clip, dict):
            raise ValueError("clip must be an object with x, y, width, and height")
        if set(clip) - {"x", "y", "width", "height", "scale"}:
            raise ValueError("clip accepts only x, y, width, height, and scale")
        if not {"x", "y", "width", "height"} <= set(clip):
            raise ValueError("clip requires x, y, width, and height")
        try:
            normalized_clip = {key: float(value) for key, value in clip.items()}
        except (TypeError, ValueError, OverflowError):
            raise ValueError("clip values must be finite numbers") from None
        if any(not math.isfinite(value) for value in normalized_clip.values()):
            raise ValueError("clip values must be finite numbers")
        if normalized_clip["width"] <= 0 or normalized_clip["height"] <= 0:
            raise ValueError("clip width and height must be greater than zero")
        if normalized_clip.get("scale", 1.0) <= 0:
            raise ValueError("clip scale must be greater than zero")
    driver = require_driver()
    previous_default = driver.default_session_id
    try:
        if session_id is not None:
            target_sid = switch_session(session_id=session_id)
        else:
            # Match the implicit execute_js path: a stale remembered default may
            # be replaced, while a caller-directed dead session must hard-fail.
            ensure_sessions()
            target_sid = switch_session()
        client_id, session_tab_id = _split_session_target(target_sid)
        target_tab_id = int(tab_id) if tab_id is not None else session_tab_id
        if session_id is not None and target_tab_id != session_tab_id:
            raise ValueError(
                f"tab_id {target_tab_id} does not match directed session_id {target_sid!r}"
            )

        params: dict[str, Any] = {"format": normalized_format}
        if quality is not None:
            params["quality"] = int(quality)
        if normalized_clip is not None:
            params["clip"] = {**normalized_clip, "scale": normalized_clip.get("scale", 1.0)}
        payload: dict[str, Any] = {
            "cmd": "cdp",
            "method": "Page.captureScreenshot",
            "params": params,
            "tabId": target_tab_id,
        }
        if full_page:
            payload["fullPage"] = True
        response = driver.ext_cmd(payload, client_id=client_id, timeout=float(timeout))
        result = _extension_data(response)
    finally:
        if session_id is not None:
            driver.default_session_id = previous_default
    data = result.get("data")
    if isinstance(data, dict) and "data" in data:
        b64 = data["data"]
    else:
        b64 = data
    if not isinstance(b64, str) or not b64:
        raise RuntimeError(
            f"Screenshot failed because the bridge returned no image data (data={data!r}). "
            "Confirm the target is a normal page and no other debugger owns it, or inspect it with list_tabs first.")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("Screenshot failed because the bridge returned invalid base64 image data.") from exc

    dimensions = _image_dimensions(raw)
    out: dict[str, Any] = {
        "status": "success",
        "format": normalized_format,
        "full_page": bool(full_page),
        "image_width": dimensions[0] if dimensions else None,
        "image_height": dimensions[1] if dimensions else None,
        "pixel_space": "device",
        "size": len(raw),
        "image_attached": True,
        "model_note": (
            "Screenshot pixels are attached as MCP image content. If the current model does not "
            "support images, it has not seen those pixels and must not infer page state from this "
            "result; use scan_page, execute_js, a page-specific API, or OCR. "
            + _SCREENSHOT_PIXEL_NOTE
        ),
    }
    if dimensions is None:
        # Reported rather than dropped: without dimensions a caller has no way to
        # rescale a point read off the picture, and `size` is a byte count that
        # reads like one if nothing says otherwise.
        out["dimensions_note"] = (
            "the image header could not be parsed, so its pixel dimensions are unknown; "
            "do not treat `size` as a dimension, it is the byte count"
        )
    if save_path:
        # Validate path to prevent traversal outside allowed directory.
        path = _validate_safe_path(save_path, description="save_path")
        # Atomic write with size limit (50MB for screenshots)
        _atomic_write_bytes(path, raw, max_size=50 * 1024 * 1024)
        out["saved_to"] = str(path)
    if return_base64:
        out["base64"] = b64

    # Keep base64 out of the text block even when explicitly requested. It
    # remains available in structuredContent for machine consumers, while the
    # model receives the actual pixels through ImageContent.
    text_metadata = {key: value for key, value in out.items() if key != "base64"}
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(text_metadata, ensure_ascii=False)),
            MCPImage(data=raw, format=normalized_format).to_image_content(),
        ],
        structuredContent=out,
    )


# --- Desktop automation: pyautogui loader and screen capture -----------------
def _pyautogui():
    # pyautogui reads no env vars; the failsafe (corner abort raising
    # FailSafeException mid-automation) must be disabled on the module itself.
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError(
            "Physical input requires the optional desktop dependencies. "
            "Install `browsertap-mcp[desktop]`."
        ) from exc
    except Exception as exc:
        # pyautogui binds to a display while importing, so a headless or
        # otherwise unusable session fails here with whatever its platform
        # backend raises -- KeyError('DISPLAY'), an Xlib error, an OSError. None
        # of those are ImportError, so catching only that reported a backend
        # detail instead of "this machine has no usable desktop".
        raise RuntimeError(
            "Physical input is unavailable because the desktop session could not be "
            f"initialised ({type(exc).__name__}: {exc}). Use the page_* tools, which "
            "do not need a desktop."
        ) from exc

    pyautogui.FAILSAFE = False
    return pyautogui



# --- Site permissions and physical input: operator approval gate -------------

class PhysicalInputApproval(BaseModel):
    approve: StrictBool = Field(description="Approve this one physical input action")


_ApprovalReason = Literal[
    "elicitation_unsupported", "declined", "timeout", "cancelled", "error",
]


@dataclass(frozen=True)
class _ApprovalDecision:
    approved: bool
    reason: _ApprovalReason | None = None


def _supports_form_elicitation(ctx: Context) -> bool:
    if not callable(getattr(ctx, "elicit", None)):
        return False
    request_context = getattr(ctx, "request_context", None)
    session = getattr(request_context, "session", None)
    # Context doubles / in-process callers can implement elicit themselves.
    # Real MCP sessions expose their initialized client capabilities.
    if session is None or not hasattr(session, "client_params"):
        return True
    client_params = session.client_params
    capability = (
        getattr(client_params.capabilities, "elicitation", None)
        if client_params is not None else None
    )
    if capability is None:
        return False
    # Legacy elicitation={} means form support. A URL-only client cannot
    # answer this primitive boolean form; newer clients advertise form={}.
    return capability.form is not None or capability.url is None


async def _request_approval(
    ctx: Context,
    *,
    message: str,
    schema: type[SitePermissionApproval] | type[PhysicalInputApproval],
    approvals: set[str],
    category: Literal["Site-permission", "Physical-input"],
) -> _ApprovalDecision:
    """Ask for a strict boolean form without performing the approved action."""
    profile = _automation_profile()
    if profile["mode"] == "lab" and profile["no_elicit"]:
        return _ApprovalDecision(True)
    try:
        approval_key = _approval_key(ctx)
        if profile["mode"] == "lab" and approval_key in approvals:
            return _ApprovalDecision(True)
        if not _supports_form_elicitation(ctx):
            return _ApprovalDecision(False, "elicitation_unsupported")
        with anyio.fail_after(_approval_timeout()):
            result = await ctx.elicit(message=message, schema=schema)
        if result.action == "decline":
            return _ApprovalDecision(False, "declined")
        if result.action == "cancel":
            return _ApprovalDecision(False, "cancelled")
        if (
            result.action != "accept" or result.data is None
            or type(result.data.approve) is not bool
        ):
            return _ApprovalDecision(False, "error")
        if result.data.approve is not True:
            return _ApprovalDecision(False, "declined")
        if profile["mode"] == "lab":
            approvals.add(approval_key)
        return _ApprovalDecision(True)
    except Exception as exc:
        # Task cancellation inherits BaseException and must still propagate,
        # allowing each caller's finally block to clean up its claimed state.
        reason: _ApprovalReason = "error"
        if isinstance(exc, TimeoutError):
            reason = "timeout"
        elif isinstance(exc, NotImplementedError) or (
            isinstance(exc, McpError) and exc.error.code == METHOD_NOT_FOUND
        ):
            reason = "elicitation_unsupported"
        logger.warning(
            "%s approval: reason=%s error_type=%s",
            category, reason, type(exc).__name__,
        )
        return _ApprovalDecision(False, reason)


async def _request_physical_approval(ctx: Context, summary: str) -> _ApprovalDecision:
    return await _request_approval(
        ctx,
        message=f"BTAP requests one physical input action: {summary}",
        schema=PhysicalInputApproval,
        approvals=_LAB_PHYSICAL_APPROVALS,
        category="Physical-input",
    )


def _requires_user_action(reason: _ApprovalReason | None) -> dict[str, Any]:
    return {
        "status": "requires_user_action",
        "message": "One-action physical-input approval was declined, cancelled, or unavailable.",
        "reason": reason,
    }


def _physical_error_result(status: str, message: str) -> dict[str, Any]:
    return {"status": status, "message": message}


@mcp.tool(description=(
    "Explicit desktop capability: inspect the current foreground Windows native file dialog "
    "owned by a registered Chrome or Edge process. Requires desktop_opt_in=true and the desktop "
    "extra. Verifies the OS owner/process identity, standard Shell file controls and visible "
    "Cancel button, then installs a temporary per-ticket window identity marker. Returns a "
    "15-second ticket for cancel_native_file_dialog plus desktop/on_screen/input_quiet diagnostics. "
    "This inspection has a temporary marker side effect; it does not activate a window. "
    "Use page/CDP tools for ordinary pages. Unsupported or unverifiable native layouts are refused."
))
def inspect_native_file_dialog(desktop_opt_in: StrictBool = False) -> dict[str, Any]:
    return native_dialog.inspect_native_file_dialog(desktop_opt_in=desktop_opt_in)


@mcp.tool(description=(
    "Explicit desktop capability: cancel the Windows file dialog identified by a fresh "
    "inspect_native_file_dialog ticket. Requires desktop_opt_in=true and follows the current "
    "safe/lab physical-approval policy. Each opted-in attempt consumes its ticket. After the "
    "physical-input lease, enforced quiet gate, identity, foreground and Cancel hit checks, "
    "sends one bounded message to that Cancel control without activating a window. Reports "
    "cancelled only after observing the dialog HWND gone; uncertain delivery or closure is "
    "unknown with retry_safe=false. Inspect state before another action. Returns desktop, "
    "on_screen and input_quiet diagnostics; unsupported native layouts are refused. "
    "Approval failures expose reason in the result and diagnostics: elicitation_unsupported, "
    "declined, timeout, cancelled or error."
))
async def cancel_native_file_dialog(
    ticket: str, ctx: Context, desktop_opt_in: StrictBool = False,
) -> dict[str, Any]:
    with native_dialog._claim_cancellation(ticket, desktop_opt_in=desktop_opt_in) as cancellation:
        decision = _ApprovalDecision(True)
        if cancellation.needs_approval():
            decision = await _request_physical_approval(ctx, "cancel the inspected native file dialog")
        result = await anyio.to_thread.run_sync(functools.partial(
            cancellation.cancel, approved=decision.approved,
        ))
        if not decision.approved:
            # Ticket expiry/identity errors keep their original error code.
            result["reason"] = decision.reason
        return result


async def _run_approved_physical_action(
    ctx: Context,
    summary: str,
    action: Callable[[], dict[str, Any]],
    *,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = None,
    points: Optional[Sequence[tuple[Any, Any]]] = None,
) -> dict[str, Any]:
    decision = await _request_physical_approval(ctx, summary)
    if not decision.approved:
        return _requires_user_action(decision.reason)

    should_activate = session_id is not None or activate_session not in (None, "none")
    action_started = False

    def worker() -> dict[str, Any]:
        nonlocal action_started
        def gated_action() -> dict[str, Any]:
            nonlocal action_started
            # Activation is itself foreground work and must wait until the
            # lease and quiet-input check have passed.
            action_started = False
            bounds = None
            if points:
                # Deliberately inside the lease rather than in front of the
                # approval prompt: refusing here still precedes every dispatch,
                # which is the property that matters, and BTAP touches no
                # desktop API before the human consents. It is also ahead of
                # activation on purpose -- display geometry does not depend on
                # which window is raised, so a request that cannot work should
                # not foreground someone's tab first. The probe loads no input
                # backend, so this adds nothing to the ordering below.
                bounds = physical_input.check_screen_bounds(points)
            activated = None
            if should_activate:
                activated = _maybe_activate(activate_session, session_id)
                if not isinstance(activated, dict) or activated.get("on_screen") is not True:
                    return {
                        "status": "activation_failed",
                        "message": (
                            "The requested browser target could not be confirmed on screen; "
                            "no physical input was sent."
                        ),
                        "activated": activated,
                        "screen_bounds": bounds,
                    }
            # Read after activation, and that is the mirror image of the bounds
            # check above: display geometry does not depend on which window is
            # raised, but whether input can reach the target depends on exactly
            # that. Asking before activation would report the integrity level of
            # whatever the human was last looking at.
            reachability = physical_input.delivery_reachability(
                tuple(points[0]) if points else None
            )
            action_started = True
            result = action()
            if activated:
                result["activated"] = activated
            if bounds is not None:
                result["screen_bounds"] = bounds
            if reachability is not None:
                # Always attached, not only when blocked: "checked, looks fine" and
                # "never checked" are different facts, and a field that appears only
                # on failure teaches a caller to read its absence as success.
                result["input_reachability"] = reachability
            return result

        return physical_input.run_physical_action(summary, gated_action)

    try:
        return await anyio.to_thread.run_sync(worker)
    except physical_input.PhysicalInputBusy as exc:
        if action_started:
            raise
        return _physical_error_result("busy", str(exc))
    except physical_input.InputActivityDetected as exc:
        if action_started:
            raise
        return _physical_error_result("input_activity_detected", str(exc))
    except physical_input.CoordinatesOffScreen as exc:
        # Raised above `action_started = True` by construction, so unlike the two
        # gates before it this one never has to decide whether input already
        # went out: it did not.
        return _physical_error_result("coordinates_off_screen", str(exc))


def _maybe_activate(activate_session: Optional[str],
                    session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Raise the target tab before a screen-coordinate action.

    Physical input lands on whatever is actually on screen, so skipping this
    would send resolve_leave_dialog's Enter to the previously visible tab —
    silently, since pyautogui reports success either way. That is why raising
    the tab is the default and opting out is explicit.

    ``session_id`` wins when given, and is the parameter to reach for: every
    other tool here takes one, and the shared global default is not a safe
    stand-in for it. Session-scoped tools save and restore that default, so an
    agent that carefully passes session_id to scan_page and then calls this
    without one gets whatever tab some *other* task last selected — the more
    disciplined the caller, the more surprising the miss.

    Otherwise ``None``/``"current"`` raise the current target tab, a session id
    in ``activate_session`` raises that tab, and ``"none"`` skips activation for
    genuine desktop clicks outside the browser.
    """
    if session_id is None and activate_session == "none":
        return None
    target = session_id
    if target is None and activate_session not in (None, "current"):
        target = activate_session
    try:
        info = _activate(target)
        time.sleep(0.3)
        return info
    except Exception as e:
        # No target tab yet is normal for a desktop click; don't fail the action.
        return {"activation_skipped": str(e)}


if __name__ == "__main__":
    configure_stdio_logging()
    get_driver()
    mcp.run(transport="stdio")
