from __future__ import annotations

import base64
import functools
import inspect
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field, StrictBool

# All tools share one BrowserBridge whose target (default_session_id) is mutable
# state. When the MCP lowlevel server dispatches concurrent requests, two tools
# running in parallel both save/restore that global and race each other — a
# scan_page and an execute_js in the same turn can read/write different tabs
# than the ones they named. Serialize tool execution with a single lock; the
# cost is lost parallelism, the win is that directed calls stay directed.
#
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
    physical_input,  # noqa: E402
    simphtml,  # noqa: E402
)
from . import bridge as bridge_module  # noqa: E402
from .browser_bridge import (  # noqa: E402
    BridgeNoResponseError,
    BrowserBridge,
    state_paths_report,
)
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
from .paths import state_dir  # noqa: E402

logger = logging.getLogger(__name__)


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
        "Browser automation tools for the user's real Chrome/Edge session via BrowserBridge/CDP. "
        "Supports page scanning, JS execution, CDP commands, screenshots, cookies, and desktop physical input. "
        "Page screenshots include MCP image content; a model that cannot process images must not claim to "
        "have seen the pixels and should use scan_page, execute_js, a page-specific API, or OCR instead. "
        "Several browsers can be connected at once; list_tabs shows a browser field per tab. "
        "Before acting, pick the target explicitly with switch_tab(browser='chrome'|'edge') or a full "
        "session_id (a 'client:tabId' string - pass it verbatim, never split it). "
        "Tabs that existed before the task are user-owned: do not close or navigate them by default. "
        "For mutating work use open_new_tab, keep its owner_id, and close that owned tab in cleanup. "
        "If a result carries status='no_response', switched_session, or bridge_error: the tab slept, "
        "reconnected, or the bridge blipped - run list_tabs, switch_tab to the right target, then retry; "
        "re-run scripts with side effects only after scan_page confirms they did not land."
    ),
)

_driver: Optional[BrowserBridge] = None
_DRIVER_PORT = int(os.environ.get("BROWSERTAP_BRIDGE_PORT", "18765"))
_DRIVER_HOST = os.environ.get("BROWSERTAP_BRIDGE_HOST", "127.0.0.1")

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
            self._records[sid] = record
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

    def release(self, session_ids: list[str], *, owner_id: str) -> None:
        with self._lock:
            for sid in session_ids:
                record = self._records.get(sid)
                if record and record["owner_id"] == owner_id:
                    self._records.pop(sid, None)


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


def _threaded_tool(*d_args: Any, **d_kwargs: Any):
    serialize = bool(d_kwargs.pop("serialize", True))
    decorator = _mcp_tool(*d_args, **d_kwargs)

    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_runner(*args: Any, **kwargs: Any):
                if not serialize:
                    return await fn(*args, **kwargs)
                await _acquire_tool_lock()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    _TOOL_LOCK.release()

            async_runner.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
            async_runner.__annotations__ = {
                name: parameter.annotation
                for name, parameter in async_runner.__signature__.parameters.items()
                if parameter.annotation is not inspect.Parameter.empty
            }
            async_runner.__module__ = fn.__module__
            decorator(async_runner)
            return fn

        @functools.wraps(fn)
        async def runner(*args: Any, **kwargs: Any):
            def _run():
                if not serialize:
                    return fn(*args, **kwargs)
                with _TOOL_LOCK:
                    return fn(*args, **kwargs)

            return await anyio.to_thread.run_sync(_run)

        # FastMCP builds the input schema from the signature, so runner must keep
        # the original one. `from __future__ import annotations` makes every
        # annotation a string, and pydantic resolves those against the
        # function's own globals — so carry the original signature AND the
        # defining module over, or `Optional` fails to resolve.
        # eval_str resolves the string annotations here, where Optional/Any are
        # in scope, so pydantic never has to look them up again.
        runner.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
        runner.__annotations__ = {
            name: p.annotation
            for name, p in runner.__signature__.parameters.items()
            if p.annotation is not inspect.Parameter.empty
        }
        runner.__module__ = fn.__module__
        decorator(runner)
        # Hand back the untouched sync function so in-process callers
        # (switch_session, compact_tabs, ...) don't have to await.
        return fn

    return wrap


mcp.tool = _threaded_tool  # type: ignore[assignment]


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
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


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


def _spawn_lock_path() -> Path:
    return state_dir() / "spawn.lock"


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists.

    Used to recycle a spawn lock whose owner crashed mid-spawn instead of
    waiting the full _SPAWN_LOCK_STALE window — that window blocks a real
    recovery for 30s after a daemon that died seconds in.
    """
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is not ours; treat it as alive so we don't
        # steal a lock another instance legitimately holds.
        return True
    except OSError:
        return False


def _acquire_spawn_lock() -> Optional[Path]:
    """Win the right to spawn the bridge, or return None if someone else has it.

    MCP instances start in parallel, and "is the port open? no -> spawn" is not
    atomic across processes: several instances check at the same moment, all see
    a closed port, and all spawn. The losers then sit there having lost the port
    bind. Observed for real — two daemons with identical creation timestamps.
    """
    lock = _spawn_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL is the atomic part: exactly one process creates the file.
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # A crashed spawner would otherwise block every later attempt forever.
        # Two recovery paths: if the lock is older than _SPAWN_LOCK_STALE it is
        # definitely stale; otherwise read the pid and check liveness so a
        # daemon that died seconds after spawn frees the lock immediately
        # instead of holding recovery hostage for 30s.
        try:
            recycle = False
            if time.time() - lock.stat().st_mtime > _SPAWN_LOCK_STALE:
                recycle = True
            else:
                try:
                    old_pid = int(lock.read_text(encoding="utf-8").strip())
                except (ValueError, OSError):
                    old_pid = 0
                if not _pid_alive(old_pid):
                    recycle = True
            if recycle:
                lock.unlink(missing_ok=True)
                return _acquire_spawn_lock()
        except OSError:
            pass
        return None
    except OSError:
        return None
    try:
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    finally:
        os.close(fd)
    return lock


def spawn_bridge_daemon(*, reset_spawn_lock: bool = False) -> bool:
    """Start the bridge as a detached process so it outlives this MCP instance.

    Returns True once the bridge HTTP port answers. Self-hosting from an MCP
    instance is avoided because these instances are spawned per session and
    recycled, taking the bridge (and its bound ports) down with them.
    """
    if reset_spawn_lock:
        try:
            _spawn_lock_path().unlink(missing_ok=True)
        except OSError:
            pass
    lock = _acquire_spawn_lock()
    if lock is None:
        # Another instance is spawning. Wait for its daemon rather than starting
        # a second one that will only lose the port bind.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _port_open(_DRIVER_HOST, _DRIVER_PORT + 1):
                return True
            time.sleep(0.25)
        return False
    ok = False
    try:
        ok = _spawn_bridge_daemon_locked()
        return ok
    finally:
        # Only release on failure. Releasing after a success would let the next
        # caller — whose own port check can still be failing, since a freshly
        # spawned daemon needs a moment to bind — win the lock and spawn a
        # duplicate. On success the lock is left to expire via _SPAWN_LOCK_STALE,
        # by which point the port is up and nobody needs to spawn at all.
        if not ok:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass


def _spawn_bridge_daemon_locked() -> bool:
    # Re-check under the lock: a daemon may have come up between the caller's
    # port check and our acquiring the lock, and spawning now would just create
    # the duplicate the lock exists to prevent.
    if _port_open(_DRIVER_HOST, _DRIVER_PORT + 1):
        return True
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
            subprocess.Popen(cmd, **kwargs)
    except OSError:
        return False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if _port_open(_DRIVER_HOST, _DRIVER_PORT + 1):
            return True
        time.sleep(0.25)
    return False


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
            and not _port_open(_DRIVER_HOST, _DRIVER_PORT + 1)
        ):
            spawn_bridge_daemon()
        # If the spawn failed the constructor falls back to self-hosting,
        # which keeps the original single-process behavior working.
        _driver = BrowserBridge(host=_DRIVER_HOST, port=_DRIVER_PORT)
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
        and not _port_open(_DRIVER_HOST, _DRIVER_PORT + 1)
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
    if not fresh and _sessions_cache and time.monotonic() - _sessions_cache[0] < _SESSIONS_TTL:
        return _sessions_cache[1]
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
            "keep this MCP server running via Hermes, and open a normal http/https page in Chrome."
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
    """Forget a remembered target tab that no longer exists.

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
    if any(str(s.get("id")) == str(cur) for s in active_sessions()):
        return str(cur)
    # The session cache can simply be out of date; confirm against the bridge
    # before discarding a default that is actually fine.
    if any(str(s.get("id")) == str(cur) for s in active_sessions(fresh=True)):
        return str(cur)
    driver.default_session_id = None
    return None


def switch_session(
    session_id: Optional[str] = None,
    url_pattern: Optional[str] = None,
    browser: Optional[str] = None,
) -> str:
    driver = require_driver()
    if session_id is not None:
        sid = str(session_id)
        found = next((s for s in active_sessions() if str(s.get("id")) == sid), None)
        if not found:
            raise RuntimeError(f"Session {sid} not found")
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
        sid = str(cands[0]["id"])
        driver.default_session_id = sid
        return sid
    if url_pattern:
        sid = driver.set_session(url_pattern)
        if not sid:
            raise RuntimeError(f"No session matching url pattern: {url_pattern}")
        return str(sid)
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
    driver.default_session_id = str(sessions[0]["id"])
    return str(driver.default_session_id)


# --- exec_js: the one bridge roundtrip every tool goes through ---------------
def exec_js(script: str, session_id: Optional[str] = None, timeout: float = 15.0) -> dict[str, Any]:
    timeout = float(timeout)
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
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
    response = driver.execute_js(script, timeout=first_budget, session_id=sid)
    if simphtml.no_response_kind(response) == "undelivered":
        # Never reached the page; retrying is side-effect-free. The reserve
        # above is what makes this branch reachable — a first attempt handed the
        # whole budget can only report "undelivered" after spending it all.
        retry_budget = remaining()
        if retry_budget > 0:
            response = driver.execute_js(
                script,
                timeout=retry_budget,
                session_id=sid,
            )
    kind = simphtml.no_response_kind(response)
    if kind:
        # Tools built on this helper (CDP, cookies, screenshots) have nothing
        # useful to return without data; fail loudly instead of returning junk.
        delivery_state = response.get("delivery_state") or {
            "undelivered": "undelivered",
            "after_ack": "delivered_no_result",
            "navigated": "navigated",
        }[kind]
        raise BridgeNoResponseError(
            f"Bridge no-response ({kind}): {response.get('result')}. "
            "Session may be asleep or disconnected; run list_tabs, switch_tab to a live session, then retry.",
            error_code=str(response.get("error_code") or "no_response"),
            delivery_state=delivery_state,
            retry_safe=bool(response.get("retry_safe", kind == "undelivered")),
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
_REQUIRED_EXTENSION_CAPABILITIES = {"content_command_channel_removed"}


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
    )
)
def get_automation_profile() -> dict[str, Any]:
    return _automation_profile()


@mcp.tool(
    description=(
        "Set the safe or lab automation profile for this MCP process. This does not persist or "
        "reload the extension; BROWSERTAP_MODE controls the next process."
    )
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
@mcp.tool(description="Return component versions, stale-build actions, extension path, bridge ports, and connection status for setup/diagnostics.")
def get_setup_status() -> dict[str, Any]:
    driver = get_driver()
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
    bridge_version = diagnosis.get("bridge_version")
    extension_version = diagnosis.get("extension_version")
    protocol_version = diagnosis.get("protocol_version")
    extension_capabilities = diagnosis.get("extension_capabilities") or {}
    extension_status_error = diagnosis.get("extension_status_error")

    # An older bridge may not forward extension build data yet, while still
    # supporting the generic ext_cmd route. Probe it directly before deciding
    # that Chrome needs a manual extension reload.
    if extension_version is None or protocol_version is None:
        try:
            runtime = _extension_data(
                driver.ext_cmd({"cmd": "bridge_status"}, timeout=_STATUS_TIMEOUT)
            )
            extension_version = (
                runtime.get("extension_version") or runtime.get("manifest_version")
            )
            protocol_version = runtime.get("protocol_version")
            extension_capabilities = runtime.get("capabilities") or {}
        except Exception as e:
            extension_status_error = extension_status_error or str(e)

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
    package_is_stale = bridge_is_newer or extension_is_newer or protocol_is_newer

    restart_bridge_required = bridge_version != __version__ and not bridge_is_newer
    missing_extension_capabilities = sorted(
        capability
        for capability in _REQUIRED_EXTENSION_CAPABILITIES
        if extension_capabilities.get(capability) is not True
    )
    reload_extension_required = (
        (extension_version != __version__ and not extension_is_newer)
        or (protocol_version != _EXTENSION_PROTOCOL_VERSION and not protocol_is_newer)
        # A newer extension that no longer advertises a capability this build
        # requires is also a stale-package problem: reloading cannot add back
        # something the newer extension deliberately dropped.
        or (bool(missing_extension_capabilities) and not extension_is_newer)
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
    elif reload_extension_required:
        component_status = "stale_extension"
        component_action = "reload_extension"
    else:
        component_status = "healthy"
        component_action = "none"
    status: dict[str, Any] = {
        "status": component_status,
        "action": component_action,
        "package_version": __version__,
        "bridge_version": bridge_version,
        "extension_version": extension_version,
        "protocol_version": protocol_version,
        "expected_protocol_version": _EXTENSION_PROTOCOL_VERSION,
        "extension_capabilities": extension_capabilities,
        "missing_extension_capabilities": missing_extension_capabilities,
        "restart_bridge_required": restart_bridge_required,
        "reload_extension_required": reload_extension_required,
        "restart_mcp_session_required": package_is_stale,
        "extension_name": "BrowserTap Bridge",
        "extension_path": str(chrome_extension_dir()),
        "bridge_host": _DRIVER_HOST,
        "bridge_ws_port": _DRIVER_PORT,
        "bridge_http_port": _DRIVER_PORT + 1,
        # Where *this* process keeps state and which token file it reads. The
        # daemon answers the same question inside `diagnosis.state_paths`, and
        # the two can disagree -- see the note added below.
        "state_paths": local_state_paths,
        "remote_mode": driver.is_remote,
        "connected_tabs": len(sessions),
        "default_session_id": driver.default_session_id,
        "tabs": sessions,
        "diagnosis": diagnosis,
        "notes": [
            "Load the unpacked extension from extension_path in chrome://extensions with Developer Mode enabled.",
            "Keep a normal http/https page open in Chrome; about:blank is not enough.",
            "The bridge runs as a detached daemon; this MCP server auto-starts it when missing.",
        ],
    }
    if package_is_stale:
        # Lead with the only action that can clear this, because the two flags a
        # reader reaches for first are both false here.
        status["notes"].insert(
            0,
            "A component reports a newer version than this MCP server process. "
            "Restart the MCP session or client so it loads the installed build; "
            "restarting the bridge or reloading the extension cannot clear it.",
        )
    if bridge_error:
        status["bridge_error"] = bridge_error
    if extension_status_error:
        status["extension_status_error"] = extension_status_error
    if state_paths_disagreement:
        status["state_paths_disagreement"] = state_paths_disagreement
        status["notes"].insert(0, state_paths_advice)
    return status


# --- Tools: tab inventory and closing ----------------------------------------
@mcp.tool(description="List connected tabs across all connected browsers; each tab has a browser field (chrome/edge/opera) and a session id to pass verbatim.")
def list_tabs() -> dict[str, Any]:
    try:
        sessions = compact_tabs(timeout=_STATUS_TIMEOUT, fresh=True)
    except Exception as e:
        return {
            "default_session_id": require_driver().default_session_id,
            "tabs": [],
            "bridge_error": str(e),
        }
    return {
        "default_session_id": require_driver().default_session_id,
        "tabs": sessions,
    }


@mcp.tool(
    description=(
        "List every open tab, including chrome-extension:// pages that list_tabs hides. "
        "Those never become sessions (content scripts can't run there), so they have no "
        "session id — drive them with cdp_command(tab_id=...) instead. Works with no tabs open."
    )
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
    return {
        "status": status,
        "requested": requested,
        "closed": closed,
        "already_gone": already_gone,
        "closed_by": closed_by,
        "owner_id": str(owner_id) if only_if_agent_owned else None,
        "only_if_agent_owned": bool(only_if_agent_owned),
        "result": result,
    }


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
    sid = switch_session(session_id=session_id, url_pattern=url_pattern, browser=browser)
    out: dict[str, Any] = {"active_session_id": sid}
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
    timeout = float(timeout)
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
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
            raise RuntimeError(f"Session {requested_sid} not found")
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
    timeout = float(timeout)
    if timeout <= 0:
        raise TimeoutError("CDP deadline exhausted before dispatch")
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
    first_error: Optional[BaseException] = None
    try:
        response = driver.ext_cmd(
            payload, client_id=client_id, timeout=first_budget
        )
    except BaseException as exc:
        first_error = exc
        fallback_budget = remaining()
        # A timed-out mutation is ambiguous: it may already be running in the
        # extension. Never send the same CDP command through a second route,
        # and never dispatch anything after the shared deadline.
        if isinstance(exc, TimeoutError) or fallback_budget <= 0:
            raise TimeoutError(
                f"CDP command did not complete within its total deadline: {exc}"
            ) from exc
        execute = getattr(driver, "execute_js", None)
        if not callable(execute):
            raise
        fallback_payload = dict(payload)
        fallback_payload["timeoutMs"] = max(1, int(fallback_budget * 1000))
        try:
            response = execute(
                json.dumps(fallback_payload),
                timeout=fallback_budget,
                session_id=session_id,
            )
        except BaseException as fallback_error:
            raise RuntimeError(
                f"CDP fallback failed after extension command error ({first_error}): "
                f"{fallback_error}. Run get_setup_status/list_tabs and restart the bridge "
                "if it reports an older command router."
            ) from fallback_error
    result = _extension_data(response)
    if result.get("ok") is False:
        code = result.get("code") or "cdp_error"
        hint = result.get("hint") or (
            "A cdp_timeout forces BTAP to detach; retry once after list_tabs. "
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
        "after the normal physical-input approval gate only when protocol handling actually fails."
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
    return {
        "status": "requires_user_action",
        "session_id": target_sid,
        "attempts": attempts,
        "physical": physical,
        "hint": "Click Leave in the browser dialog, then retry the intended navigation.",
    }


# --- Tool: open_new_tab (owned tabs) -----------------------------------------
@mcp.tool(description="Open one real-browser tab in the background by default with an operation_id-backed exactly-once create. Pass active=true only when foreground work is genuinely required. If the create ACK is lost, the same operation_id is reconciled within one total deadline; a completed result is registered only with its exact client_id, tab_id, and generation. Before create is dispatched, an unresolved probe returns status=unknown, may_have_created=false, retry_safe=true; after dispatch, an unresolved operation returns status=unknown, may_have_created=true, retry_safe=false and the operation_id. Never use a URL-based guess or an unmarked create retry.")
def open_new_tab(
    url: str,
    timeout: float = 15.0,
    active: bool = False,
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> dict[str, Any]:
    driver = require_driver()
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    deadline = time.monotonic() + float(timeout)
    operation_id = f"open-tab-{secrets.token_urlsafe(18)}"
    client_id = _split_session_target(str(session_id))[0] if session_id is not None else None

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
    ) -> dict[str, Any]:
        detail = dict(info or {})
        detail.setdefault("status", "unknown")
        detail.setdefault("operation_status", "unknown")
        detail.update({
            "operation_id": operation_id,
            "client_id": client_id,
            "may_have_created": may_have_created,
            "retry_safe": retry_safe,
        })
        return {
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
        }

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

    # Pin one concrete extension before the first mutation. This works with no
    # content sessions and prevents create/status from drifting across browsers.
    try:
        probe_response = status_call()
        probe_info, routed_client = response_info(probe_response)
    except Exception as exc:
        return unknown_result(
            {"error": str(exc), "phase": "client_discovery"},
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
    if operation_state(probe_info) != "not_found":
        return unknown_result(
            probe_info,
            # This operation id was generated locally and no mutation has been
            # dispatched yet, so a structured storage/registry uncertainty in
            # the read-only probe cannot mean that this call created a tab.
            may_have_created=False,
            retry_safe=True,
        )

    result: Any = None
    last_status = dict(probe_info)
    create_attempts = 1
    create_timed_out = False
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
        except TimeoutError:
            continue
        except Exception as exc:
            last_status = {
                "error": str(exc),
                "phase": "reconciliation",
                "operation_id": operation_id,
                "operation_status": "unknown",
            }
            break

        state = operation_state(last_status)
        if state == "completed":
            result = status_response
            break
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
        owner_id=owner_id,
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
@mcp.tool(description="Get absolute path to the unpacked Chrome extension directory for manual installation.")
def extension_path() -> dict[str, Any]:
    return {"extension_path": str(chrome_extension_dir())}


@mcp.tool(description="List installed browser extensions (id, name, enabled, type, version). Works with no tabs open.")
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
    result = _extension_data(response)
    if result.get("ok") is False:
        return {
            "status": "error",
            "operation": operation,
            "code": result.get("code") or "extension_operation_failed",
            "error": result.get("error") or f"{operation} failed",
            **({"hint": result["hint"]} if result.get("hint") else {}),
            **context,
        }
    # The in-process/fake route commonly returns an explicit extension
    # envelope: {data: {ok: true, data: <payload>}}.  The real remote bridge,
    # however, has already removed that inner envelope in BrowserBridge.ext_cmd
    # and returns {data: <payload>} instead.  Preserve both forms.  Treating a
    # direct dict payload as an envelope used to discard capture snapshots such
    # as {status: "capturing", messages: [...]}, so live Network/Console tools
    # misleadingly returned only the generic operation status.
    if result.get("ok") is True:
        payload = result.get("data") if "data" in result else {
            key: value for key, value in result.items() if key != "ok"
        }
    else:
        payload = result
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
    if filename is not None:
        filename = str(filename).strip()
        relative_name = Path(filename)
        if (not filename or not relative_name.parts or relative_name.anchor
                or relative_name.is_absolute()
                or ".." in relative_name.parts):
            raise ValueError(
                "filename must be a non-empty relative download name without '..'"
            )

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
            raise RuntimeError(f"Session {explicit_session_id} not found")
    else:
        # Pin the implicit browser while other serialized tools have their
        # temporary default-session mutations restored. Release immediately:
        # the potentially long native download wait must not block other tools.
        with _TOOL_LOCK:
            client_id = _implicit_client_id()

    payload: dict[str, Any] = {
        "cmd": "downloads",
        "method": "download",
        "url": parsed.geturl(),
        "conflictAction": "overwrite" if overwrite else "uniquify",
        "wait": bool(wait),
        "timeoutMs": max(1, int(timeout * 1000)),
    }
    if filename is not None:
        payload["filename"] = filename.replace("\\", "/")
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
        "should be removed."
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
    response = require_driver().ext_cmd(
        {
            "cmd": "bookmarks",
            "method": "removeTree" if recursive else "remove",
            "id": bookmark_id,
        },
        client_id=_extension_client_id(session_id),
        timeout=20.0,
    )
    return _extension_operation_result(
        response,
        operation="remove_bookmark",
        bookmark_id=bookmark_id,
        recursive=bool(recursive),
    )


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
        "to return the buffer and release the debugger lease."
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
        "url_pattern uses the browser's JavaScript RegExp syntax and invalid patterns return a structured error."
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
        "foregrounding it. Use get_console_messages while running and console_capture_stop when done."
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
        "Set clear=true to clear the full buffer after reading. "
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
        "buffer, and release its debugger lease."
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


# --- Tool: scan_page ---------------------------------------------------------
@mcp.tool(
    description=(
        "Read the current page as simplified HTML/text, preserving login state from the real "
        "browser. Defaults: cutlist=true, maxchars=35000, timeout=15 seconds."
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
        return {
            "status": "no_response",
            "active_session_id": driver.default_session_id,
            "tabs": compact_tabs(),
            "error": str(e),
        }
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    out: dict[str, Any] = {
        "status": "success",
        "active_session_id": active,
        "tabs": compact_tabs(),
        "content": content,
    }
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
                "This tab is not currently visible (viewport height is zero), so visibility results are unreliable. "
                "Run activate_tab before scan_page."
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
    """Pull the optHTML offscreen marker out of the page HTML, if present."""
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
@mcp.tool(
    description=(
        "Wait until a condition holds on the page, then return. Use this instead of "
        "polling scan_page (each scan re-serializes the whole DOM). Exactly one of "
        "selector / text / url_pattern / js must be given: selector waits for a CSS "
        "match, text for a substring in body text, url_pattern for a regex on the URL, "
        "js for a JS expression to become truthy. Polls inside the page, so it costs "
        "one bridge roundtrip regardless of how long the wait takes."
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
    given = [n for n, v in (("selector", selector), ("text", text),
                            ("url_pattern", url_pattern), ("js", js)) if v]
    if len(given) != 1:
        raise ValueError(
            f"pass exactly one of selector/text/url_pattern/js (got {given or 'none'})")
    kind = given[0]
    normalized_selector = normalize_locator(selector) if kind == "selector" else None
    driver = require_driver()
    ensure_sessions()
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    # The condition is evaluated in-page on a 100ms interval, so a 30s wait is
    # still one roundtrip. Deadline is enforced on both sides: the page resolves
    # with timedOut, and the bridge call gets a few seconds of slack on top.
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
    # Wait in short in-page chunks rather than one long promise. A promise that
    # outlives its page dies with it: injected while the tab is still navigating,
    # it never resolves and the bridge reports ACK-but-no-result. Chunking means
    # an unload costs one chunk, and the next chunk lands on the new document.
    CHUNK = 4.0
    deadline = time.monotonic() + float(timeout)
    started = time.monotonic()
    info: dict[str, Any] = {}
    last_error = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = min(CHUNK, remaining)
            structured_check = ""
            detail_fields = ""
            if structured_probe is not None:
                located_condition = "located.status === 'not_found'" if gone else "!!located.found"
                structured_check = (
                    f"const located = ({structured_probe}); "
                    f"ok = {located_condition}; "
                    "detail = located;"
                )
                detail_fields = (
                    ", locator_status: detail && detail.status, "
                    "matches: detail && detail.matches, stage: detail && detail.stage"
                )
            script = f"""
            return new Promise(resolve => {{
              const start = Date.now();
              const deadline = start + {chunk * 1000};
              const check = () => {{
                let ok = false, err = null, detail = null;
                try {{ {structured_check or f'ok = !!({expr});'} }} catch (e) {{ err = String(e && e.message || e); }}
                if (ok) return resolve(JSON.stringify({{met: true,
                  url: location.href, title: document.title{detail_fields}}}));
                if (Date.now() >= deadline) return resolve(JSON.stringify({{met: false,
                  error: err, url: location.href, title: document.title,
                  ready: document.readyState{detail_fields}}}));
                setTimeout(check, 100);
              }};
              check();
            }})
            """
            try:
                resp = exec_js(script, session_id=None, timeout=chunk + 8)
                raw = resp.get("data")
                info = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception as e:
                # Page unloaded mid-wait, or the session blinked. Waiting is
                # side-effect-free, so just try the next chunk.
                last_error = str(e)
                info = {}
                time.sleep(0.3)
                continue
            if info.get("met"):
                break
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    waited_ms = int((time.monotonic() - started) * 1000)
    met = bool(info.get("met"))
    out: dict[str, Any] = {
        "status": "success" if met else "timeout",
        "condition": f"{kind}{' gone' if gone else ''}",
        "waited_ms": waited_ms,
        "url": info.get("url"),
        "title": info.get("title"),
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
        out["hint"] = "The condition was not met before timeout. Verify the selector or text, or inspect the page with scan_page."
    return out


@mcp.tool(
    description=(
        "Wait for navigation to settle: blocks until the tab's URL matches url_pattern "
        "(regex, or plain substring) and — unless wait_ready=false — document.readyState is "
        "'complete', then returns the final url, title and readyState. Use this after a click "
        "or open_url that navigates; wait_for(url_pattern=...) only checks the URL and can "
        "return while the new document is still blank. Polls in-page, so a long wait is "
        "still cheap."
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
    # The condition is evaluated by the browser, so JavaScript RegExp syntax is
    # authoritative here. Python's ``re`` accepts a different language (and
    # rejects valid JS features such as named groups). An invalid JavaScript
    # pattern is still a valid literal substring under this tool's contract.
    driver = require_driver()
    ensure_sessions()
    prev_default = driver.default_session_id
    if session_id is not None:
        switch_session(session_id=session_id)
    # 与 wait_for 同样的分块策略：一个 promise 活不过它所在的 document，导航中注入的
    # 等待会随页面卸载一起死掉、永不 resolve。分块后卸载只损失一块，下一块落在新
    # 文档里 —— 这对"等导航落定"尤其重要，因为这里本来就预期页面会换。
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
    CHUNK = 4.0
    deadline = time.monotonic() + float(timeout)
    started = time.monotonic()
    info: dict[str, Any] = {}
    last_error = None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = min(CHUNK, remaining)
            script = f"""
            return new Promise(resolve => {{
              const deadline = Date.now() + {chunk * 1000};
              const snap = (met) => JSON.stringify({{met, url: location.href,
                title: document.title, ready: document.readyState}});
              const check = () => {{
                let ok = false, err = null;
                try {{ ok = !!{probe}; }} catch (e) {{ err = String(e && e.message || e); }}
                if (ok) return resolve(snap(true));
                if (Date.now() >= deadline) {{
                  const out = JSON.parse(snap(false));
                  if (err) out.error = err;
                  return resolve(JSON.stringify(out));
                }}
                setTimeout(check, 100);
              }};
              check();
            }})
            """
            try:
                resp = exec_js(script, session_id=None, timeout=chunk + 8)
                raw = resp.get("data")
                info = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception as e:
                # 页面在等待中卸载，或会话眨了一下眼。等待本身没有副作用，下一块重试。
                last_error = str(e)
                info = {}
                time.sleep(0.3)
                continue
            if info.get("met"):
                break
    finally:
        if session_id is not None:
            driver.default_session_id = prev_default
    met = bool(info.get("met"))
    out: dict[str, Any] = {
        "status": "success" if met else "timeout",
        "url_pattern": pattern,
        "waited_ms": int((time.monotonic() - started) * 1000),
        "url": info.get("url"),
        "title": info.get("title"),
        "ready_state": info.get("ready"),
        "waited_for_ready": bool(wait_ready),
    }
    if not met:
        if info.get("error"):
            out["error"] = info["error"]
        elif last_error:
            out["error"] = f"The page was repeatedly unavailable while waiting: {last_error}"
        landed = info.get("url")
        out["hint"] = (
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
def _build_cdp_fallback_expression(script: str, policy: str, timeout: float) -> str:
    token = f"cdp-{time.monotonic_ns()}"
    deadline_ms = max(1, min(120000, int(float(timeout) * 1000)))
    return f"""
    (async () => {{
      const rawJsCode = {json.dumps(script)}.trim();
      const policy = {json.dumps(policy)};
      const token = {json.dumps(token)};
      const deadline = Date.now() + {deadline_ms};
      const scoped = policy === 'accept' || policy === 'dismiss';
      if (scoped) {{
        const scopes = Array.isArray(window.__btap_dialog_scopes)
          ? window.__btap_dialog_scopes : [];
        scopes.push({{token, policy, deadline}});
        window.__btap_dialog_scopes = scopes;
        window.__btap_suppress_until = Math.max(
          deadline, ...scopes.map(scope => Number(scope.deadline) || 0));
      }}
      try {{
        const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
        const lines = rawJsCode.split(/\r?\n/).filter(line => line.trim());
        const lastLine = lines.length ? lines[lines.length - 1].trim() : '';
        let value;
        if (lastLine.startsWith('return')) {{
          value = await (new AsyncFunction(rawJsCode))();
        }} else {{
          try {{
            value = eval(rawJsCode);
            if (value instanceof Promise) value = await value;
          }} catch (error) {{
            if (error instanceof SyntaxError && /return/i.test(error.message))
              value = await (new AsyncFunction(rawJsCode))();
            else throw error;
          }}
        }}
        let serializable = value;
        try {{ serializable = JSON.parse(JSON.stringify(value)); }}
        catch (_) {{ serializable = String(value); }}
        return {{ok: true, data: serializable}};
      }} catch (error) {{
        return {{ok: false, error: {{name: error.name || 'Error',
          message: error.message || String(error), stack: error.stack || ''}}}};
      }} finally {{
        if (scoped && Array.isArray(window.__btap_dialog_scopes)) {{
          window.__btap_dialog_scopes = window.__btap_dialog_scopes
            .filter(scope => scope.token !== token && Date.now() < scope.deadline);
          window.__btap_suppress_until = window.__btap_dialog_scopes.reduce(
            (latest, scope) => Math.max(latest, Number(scope.deadline) || 0), 0);
        }}
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
    remote = evaluation.get("result") if isinstance(evaluation.get("result"), dict) else {}
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
    return {
        "status": "success",
        "js_return": wrapped.get("data"),
        "tab_id": tab_id,
        "execution_mode": "cdp_fallback",
        "bridge_route_error": str(route_error),
    }


@mcp.tool(description="Execute arbitrary JS in the requested real-browser tab under one total deadline. BTAP pins every monitor/retry/result roundtrip to an explicit session, uses the service-worker/page route first, and falls back to directed Runtime.evaluate on SPA/CSP bridge failures without retargeting. Use wait_for/wait_for_url instead of setTimeout or sleep Promises; BTAP retries only proven-undelivered work, never an acknowledged script whose side effects may already have run.")
def execute_js(
    script: str,
    session_id: Optional[str] = None,
    no_monitor: bool = False,
    timeout: float = 15.0,
    dialog_policy: str = "dismiss",
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    deadline = time.monotonic() + float(timeout)

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    policy = _validate_dialog_policy(dialog_policy)
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
    if session_id is not None:
        requested_sid = str(session_id)
        if not any(str(session.get("id")) == requested_sid for session in sessions):
            raise RuntimeError(f"Session {requested_sid} not found")
        target_sid = requested_sid
    else:
        # Resolve once from the bounded snapshot. A stale implicit default may
        # be repicked; a caller-named dead session above is still refused.
        current = str(prev_default) if prev_default is not None else None
        if current and any(str(session.get("id")) == current for session in sessions):
            target_sid = current
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
            target_sid = str(candidates[0]["id"])
            driver.default_session_id = target_sid
    scope_token: Optional[str] = None
    client_id: Optional[str] = None
    tab_id: Optional[int] = None
    ext_cmd = getattr(driver, "ext_cmd", None)
    primary_error: Optional[BaseException] = None
    try:
        if target_sid is not None and callable(ext_cmd):
            client_id, tab_id = _split_session_target(target_sid)
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
                policy_request["source"] = script
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
                return _execute_js_cdp_fallback(
                    script,
                    policy=policy,
                    target_sid=target_sid,
                    client_id=client_id,
                    tab_id=tab_id,
                    deadline=deadline,
                    route_error=route_error,
                )
            raw_token = scope.get("token")
            if raw_token is None:
                route_error = RuntimeError(
                    "extension did not return a dialog scope token; command router may be stale"
                )
                if remaining() <= 0:
                    raise TimeoutError(
                        "execute_js total deadline exhausted before CDP fallback dispatch"
                    ) from route_error
                return _execute_js_cdp_fallback(
                    script,
                    policy=policy,
                    target_sid=target_sid,
                    client_id=client_id,
                    tab_id=tab_id,
                    deadline=deadline,
                    route_error=route_error,
                )
            scope_token = str(raw_token)
            if not re.fullmatch(r"[A-Za-z0-9._-]+", scope_token):
                raise RuntimeError("extension returned an invalid dialog scope token")
        scoped_script = (
            script
            if policy == "manual" else
            f"/*__btap_dialog_scope:{scope_token}*/\n{script}"
            if scope_token is not None else script
        )
        result = simphtml.execute_js_rich(
            scoped_script,
            driver,
            no_monitor=no_monitor,
            timeout=max(0.001, remaining()),
            before_sids=before_sids,
            session_id=target_sid,
            deadline=deadline,
        )
        wrapped = result.get("js_return")
        if isinstance(wrapped, dict) and wrapped.get("__btap_dialog_result") is True:
            result = dict(result)
            result["js_return"] = wrapped.get("value")
            wrapped_status = wrapped.get("status")
            result["manual_blocked"] = bool(
                wrapped.get("manual_blocked")
                or wrapped_status == "blocked_by_dialog"
            )
            if isinstance(wrapped_status, str):
                result["status"] = wrapped_status
            if "handled" in wrapped:
                result["handled"] = bool(wrapped.get("handled"))
            if "pending_execution" in wrapped:
                result["pending_execution"] = bool(
                    wrapped.get("pending_execution")
                )
            if isinstance(wrapped.get("error"), dict):
                result["error"] = dict(wrapped["error"])
            dialogs = wrapped.get("dialogs")
            if isinstance(dialogs, list) and dialogs:
                result["dialogs"] = dialogs
                result["dialog"] = dialogs[-1]
                if result["manual_blocked"]:
                    result["status"] = "blocked_by_dialog"
                elif policy != "manual":
                    result["status"] = "ok"
            elif isinstance(wrapped.get("dialog"), dict):
                result["dialog"] = dict(wrapped["dialog"])
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if (scope_token is not None and tab_id is not None
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
                    logger.warning("execute_js dialog policy cleanup failed: %s", cleanup_error)
        finally:
            if session_id is not None:
                driver.default_session_id = prev_default


@mcp.tool(description="Call one Chrome DevTools Protocol command. session_id accepts client:tabId; tab_id accepts either a native number or the same composite session string.")
def cdp_command(
    method: str,
    params_json: str = "{}",
    session_id: Optional[str] = None,
    tab_id: Optional[int | str] = None,
    extension_id: Optional[str] = None,
    target_id: Optional[str] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    params = json.loads(params_json or "{}")
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
        "Print a real-browser tab to a validated PDF file through bounded CDP. The file is "
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
    if not str(save_path).strip():
        raise ValueError("save_path must not be empty")
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
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError("save_pdf failed: Page.printToPDF returned invalid base64") from exc
    if len(raw) < 8 or not raw.startswith(b"%PDF-"):
        raise RuntimeError("save_pdf failed: decoded data is not a valid PDF document")

    path = Path(save_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
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
    )
)
def debugger_targets(session_id: Optional[str] = None) -> dict[str, Any]:
    driver = require_driver()
    client_id = (str(session_id).rsplit(":", 1)[0]
                 if session_id and ":" in str(session_id) else None)
    return driver.ext_cmd({"cmd": "debugger_targets"}, client_id=client_id, timeout=20.0)


@mcp.tool(description="Run a CDP bridge batch command; pass the full JSON command object as text.")
def cdp_batch(batch_json: str, session_id: Optional[str] = None) -> dict[str, Any]:
    payload = json.loads(batch_json)
    if payload.get("cmd") != "batch":
        raise RuntimeError("batch_json must be a JSON object with cmd='batch'")
    return exec_js(json.dumps(payload), session_id=session_id, timeout=30.0)


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
        previous_marker, started_at, previous_attempts = _PAGE_CHALLENGE_ATTEMPTS.get(
            session_id, (marker, 0.0, 0)
        )
        if (previous_marker != marker or previous_attempts == 0
                or now - started_at >= _PAGE_CHALLENGES.window_seconds):
            started_at = now
            previous_attempts = 0
        stalled = _PAGE_CHALLENGES.record(session_id, marker, now=now)
        attempts = previous_attempts + 1
        _PAGE_CHALLENGE_ATTEMPTS[session_id] = (marker, started_at, attempts)
        return stalled, attempts


def _blocked_page_challenge_attempts(session_id: str, marker: str) -> int | None:
    """Return the stalled attempt count while the identical marker window is live."""
    with _PAGE_CHALLENGE_LOCK:
        state = _PAGE_CHALLENGE_ATTEMPTS.get(session_id)
        if state is None:
            return None
        previous_marker, started_at, attempts = state
        now = time.monotonic()
        if now - started_at >= _PAGE_CHALLENGES.window_seconds:
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
            target_session = (
                switch_session(session_id=session_id) if directed else switch_session()
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
                raise RuntimeError(
                    "page input batch failed: "
                    f"{unwrapped.get('error') or 'unknown extension error'}"
                )
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
    element centre -- the same point the caller is about to compute from
    ``width``/``height`` below.
    """
    normalized = normalize_locator(selector)
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
            verify_hit=verify_hit,
            center_x=center_x,
            center_y=center_y,
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


def _page_type_target_info(
    selector: str | dict[str, Any],
    clear: bool,
    session_id: str,
    timeout: float,
) -> dict[str, Any]:
    # An empty selector is the legacy focused-element mode. Structured locator
    # validation only applies when the caller actually supplied a selector.
    normalized = selector if selector == "" else normalize_locator(selector)
    script = (
        type_target_script(normalized, select_all=clear)
        if isinstance(normalized, str)
        else structured_locator_script(
            normalized, purpose="type", select_all=clear
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
            raise RuntimeError(
                f"page_type target resolver returned invalid JSON: {raw!r}"
            ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"page_type target resolver returned an unexpected result: {raw!r}"
        )
    return raw


# --- Tools: page input (click, type, press, drag, upload) --------------------
@mcp.tool(
    description=(
        "Click a CSS/structured locator or viewport coordinates in a specific real browser tab "
        "using background CDP input. Ambiguous or unreachable targets dispatch nothing; the tab "
        "is not activated and the desktop cursor does not move. Selector offsets are measured "
        "from the element's top-left corner; an omitted axis uses the element centre. In selector "
        "mode the point is hit-tested before anything is dispatched: an element below the fold is "
        "scrolled into view, and a point owned by another element returns status 'obscured' (with "
        "occluded_by) or 'outside_viewport' having clicked nothing. Coordinate mode is not "
        "hit-tested -- coordinates name a pixel, not an element."
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
    if selector_mode == both_coordinates:
        raise InputValidationError(
            "page_click requires exactly one targeting mode: selector, or both x and y"
        )

    if not selector_mode:
        out = _run_page_input(
            click_commands(x, y, button=button, clicks=clicks),  # type: ignore[arg-type]
            session_id,
            timeout,
        )
        out["target"] = {"x": x, "y": y}
        return out

    driver = require_driver()
    prev_default = driver.default_session_id
    directed = session_id is not None
    try:
        target_session = switch_session(session_id=session_id) if directed else switch_session()
        resolver_x = 0 if offset_x is None else offset_x
        resolver_y = 0 if offset_y is None else offset_y
        before = _page_selector_info(
            selector,
            resolver_x,
            resolver_y,
            target_session,
            timeout,
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
                            "Dismiss the overlay, or scroll or resize the tab, then call "
                            "page_click again."
                        )
                    }
                    if before.get("status") in {"obscured", "outside_viewport"}
                    else {}
                ),
            }

        resolved_x = before.get("x")
        resolved_y = before.get("y")
        if offset_x is None:
            resolved_x += before.get("width", 0) / 2
        if offset_y is None:
            resolved_y += before.get("height", 0) / 2
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
        out = _run_page_input(
            click_commands(resolved_x, resolved_y, button=button, clicks=clicks),
            target_session,
            timeout,
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

        after = _page_selector_info(
            selector, resolver_x, resolver_y, target_session, timeout
        )
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
        "Optionally clear and submit a key. Missing, ambiguous, or unusable targets dispatch nothing."
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
    timeout = float(timeout)
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
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
                raise RuntimeError(f"Session {requested_sid} not found")
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
                candidates = sessions
                preferred_browser = os.environ.get(
                    "BROWSERTAP_PREFERRED_BROWSER", ""
                ).strip().lower()
                if preferred_browser:
                    preferred = [
                        session for session in sessions
                        if str(session.get("browser", "")).lower()
                        == preferred_browser
                    ]
                    if preferred:
                        candidates = preferred
                target_session = str(candidates[0]["id"])
                driver.default_session_id = target_session
        resolution_budget = max(0.0, deadline - time.monotonic())
        if resolution_budget <= 0:
            raise TimeoutError("page_type deadline exhausted before target resolution")
        target_info = _page_type_target_info(
            selector,
            clear,
            target_session,
            resolution_budget,
        )
        target = {"selector": selector} if selector else {"focused_element": True}
        if not target_info.get("found"):
            return {
                "status": target_info.get("status", "not_found"),
                "session_id": target_session,
                "input_mode": "cdp",
                "foreground_changed": False,
                "target": target,
                "target_kind": target_info.get("targetKind", "missing"),
                "typed_chars": 0,
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
        # The resolver above focused/selected the exact sink. Dispatch only the
        # trusted text/key portion after it positively identified a target.
        # Xterm forwards insertText to its backend asynchronously, so yield once
        # before Enter without breaking the single attached CDP batch.
        commands = type_commands(
            selector if isinstance(selector, str) else "",
            text,
            select_all=clear,
            submit_key=submit_key or None,
            submit_delay_ms=submit_delay_ms,
        )[1:]
        out = _run_page_input(
            commands,
            target_session,
            input_budget,
            session_validated=True,
            deadline=deadline,
        )
        out["target"] = target
        out["target_kind"] = target_kind
        out["typed_chars"] = len(text)
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
        "sequence, without activating the tab or moving the desktop cursor."
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
    files = [paths] if isinstance(paths, str) else list(paths)
    missing = [p for p in files if not Path(p).is_file()]
    if missing:
        raise RuntimeError(f"file(s) not found: {missing}")
    files = [str(Path(p).resolve()) for p in files]
    # One batch, one attach: DOM.getDocument's nodeId is only valid while the
    # debugger stays attached, so this cannot be split across cdp_command calls.
    batch = {
        "cmd": "batch",
        "commands": [
            {"cmd": "cdp", "method": "DOM.getDocument", "params": {"depth": -1}},
            {"cmd": "cdp", "method": "DOM.querySelector",
             "params": {"nodeId": "$0.root.nodeId", "selector": selector}},
            {"cmd": "cdp", "method": "DOM.setFileInputFiles",
             "params": {"nodeId": "$1.nodeId", "files": files}},
        ],
    }
    result = exec_js(json.dumps(batch), session_id=session_id, timeout=timeout)
    # The extension's batch reply arrives as a BARE ARRAY — ws.onmessage does
    # `res.data ?? res.results ?? res` and handleBatch returns {ok, results},
    # so `data` here is the results list, not a wrapper dict. Only the error
    # path (handleBatch catch) surfaces as {ok:false, error, results}.
    data = result.get("data")
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError(f"upload failed: {data.get('error')}")
    results = data if isinstance(data, list) else (
        data.get("results") if isinstance(data, dict) else None)
    node = None
    if isinstance(results, list) and len(results) > 1 and isinstance(results[1], dict):
        node = results[1].get("nodeId")
        if not node:
            raise RuntimeError(
                f"selector {selector!r} matched no element (DOM.querySelector returned "
                f"{results[1]}); check the selector and that the input is in the top frame")
    return {"status": "ok", "selector": selector, "files": files, "node_id": node}


# --- Cookies: read through the extension, write through CDP ------------------
@mcp.tool(description="Get cookies for the current page or specified tab via the Chrome extension bridge.")
def get_cookies(session_id: Optional[str] = None, tab_id: Optional[int] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"cmd": "cookies"}
    if tab_id is not None:
        payload["tabId"] = tab_id
    return exec_js(json.dumps(payload), session_id=session_id, timeout=15.0)


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
    if expires not in (None, ""):
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
        logger.warning("%s=%r is not a number; using %ss",
                       _APPROVAL_TIMEOUT_ENV, raw, _DEFAULT_APPROVAL_TIMEOUT)
        return _DEFAULT_APPROVAL_TIMEOUT
    return value if value > 0 else _DEFAULT_APPROVAL_TIMEOUT


async def _request_site_permission_approval(
    ctx: Context, permission: str, origin: str, duration_seconds: int
) -> bool:
    profile = _automation_profile()
    if profile["mode"] == "lab" and profile["no_elicit"]:
        return True
    approval_key = _approval_key(ctx)
    if profile["mode"] == "lab" and approval_key in _LAB_SITE_PERMISSION_APPROVALS:
        return True
    try:
        with anyio.fail_after(_approval_timeout()):
            result = await ctx.elicit(
                message=("BTAP requests temporary site permission: "
                         f"allow {permission} for {origin} for {duration_seconds} seconds"),
                schema=SitePermissionApproval,
            )
        approved = (
            result.action == "accept" and result.data is not None
            and result.data.approve is True
        )
        if approved and profile["mode"] == "lab":
            _LAB_SITE_PERMISSION_APPROVALS.add(approval_key)
        return approved
    except TimeoutError:
        logger.warning(
            "Site-permission approval for %s on %s went unanswered for %ss; treating it as "
            "declined so the tool lock is released.",
            permission, origin, _approval_timeout(),
        )
        return False
    except Exception:
        return False


def _site_permission_requires_user_action() -> dict[str, Any]:
    return {
        "status": "requires_user_action",
        "message": "Temporary site-permission approval was declined, cancelled, or unavailable.",
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
        "Only http/https origins and notifications, geolocation/location, camera, microphone, or clipboard are supported. "
        "safe asks on every allow; lab skips prompts by default and restores session approval only "
        "when BROWSERTAP_LAB_NO_ELICIT is explicitly disabled. All leases restore their prior setting."
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
        if normalized_setting == "allow" and not await _request_site_permission_approval(
            ctx, spec["setting"], normalized_origin, duration
        ):
            return _site_permission_requires_user_action()
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
        "Restore matching temporary site-permission leases now. Omit origin and permission to reset every "
        "lease for the selected browser; origin accepts only http/https."
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
def _cdp(method: str, params: dict[str, Any], session_id: Optional[str],
         tab_id: Optional[int], timeout: float) -> Any:
    payload: dict[str, Any] = {"cmd": "cdp", "method": method, "params": params}
    if tab_id is not None:
        payload["tabId"] = tab_id
    return exec_js(json.dumps(payload), session_id=session_id,
                   timeout=timeout).get("data")


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
@mcp.tool(
    description=(
        "Capture a viewport, full-page, or clipped screenshot of a page/tab via CDP with optional "
        "JPEG/WebP quality. Returns text metadata plus an "
        "attached MCP image even when save_path is set; save_path only controls disk output. "
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
    except (ValueError, base64.binascii.Error) as exc:
        raise RuntimeError("Screenshot failed because the bridge returned invalid base64 image data.") from exc

    out: dict[str, Any] = {
        "status": "success",
        "format": normalized_format,
        "full_page": bool(full_page),
        "size": len(raw),
        "image_attached": True,
        "model_note": (
            "Screenshot pixels are attached as MCP image content. If the current model does not "
            "support images, it has not seen those pixels and must not infer page state from this "
            "result; use scan_page, execute_js, a page-specific API, or OCR."
        ),
    }
    if save_path:
        path = Path(save_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
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


@mcp.tool(description="Capture the complete visible virtual desktop across all displays and return text metadata plus MCP image content; this is not a background-tab screenshot, save_path only adds a disk copy, and return_base64 is opt-in.")
def capture_desktop_screenshot(save_path: str = "", return_base64: bool = False) -> CallToolResult:
    import io
    try:
        import mss
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Desktop capture requires the optional desktop dependencies. "
            "Install `browsertap-mcp[desktop]`."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            "Desktop capture is unavailable because its imaging backend failed to load "
            f"({type(exc).__name__}: {exc})."
        ) from exc

    try:
        with mss.mss() as sct:
            if not sct.monitors:
                raise RuntimeError("Desktop capture failed because no display was detected.")
            # MSS index 0 is the virtual bounding rectangle containing every display;
            # indexes 1..N are individual monitors.
            monitor = sct.monitors[0]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            output = io.BytesIO()
            img.save(output, format="PNG")
            raw = output.getvalue()
    except RuntimeError:
        # Already an actionable message -- either the no-display check above or a
        # backend that speaks RuntimeError. Re-wrapping would bury it.
        raise
    except Exception as exc:
        # mss binds to the display in `mss.mss()` and raises ScreenShotError (not
        # RuntimeError) on a headless or locked session; Pillow can fail on the
        # encode. Name the cause instead of surfacing a bare backend traceback.
        raise RuntimeError(
            f"Desktop capture failed because the display could not be read "
            f"({type(exc).__name__}: {exc})."
        ) from exc
    out: dict[str, Any] = {
        "status": "success",
        "format": "png",
        "width": shot.width,
        "height": shot.height,
        "left": int(monitor.get("left", 0)),
        "top": int(monitor.get("top", 0)),
        "monitor_count": max(0, len(sct.monitors) - 1),
        "virtual_desktop": True,
        "size": len(raw),
        "image_attached": True,
        "model_note": (
            "Pixels are attached from the currently visible OS virtual desktop across all displays, "
            "not from a selected or background browser tab. If the current model does not support "
            "images, it has not seen those pixels; use capture_page_screenshot for one browser tab "
            "or structured page tools when possible."
        ),
    }
    if save_path:
        path = Path(save_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        out["saved_to"] = str(path)
    if return_base64:
        out["base64"] = base64.b64encode(raw).decode("ascii")
    text_metadata = {key: value for key, value in out.items() if key != "base64"}
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(text_metadata, ensure_ascii=False)),
            MCPImage(data=raw, format="png").to_image_content(),
        ],
        structuredContent=out,
    )


# --- Physical input: operator approval gate ----------------------------------
class PhysicalInputApproval(BaseModel):
    approve: StrictBool = Field(description="Approve this one physical input action")


async def _request_physical_approval(ctx: Context, summary: str) -> bool:
    """Ask the client for approval without touching any physical-input APIs."""
    profile = _automation_profile()
    if profile["mode"] == "lab" and profile["no_elicit"]:
        return True
    approval_key = _approval_key(ctx)
    if profile["mode"] == "lab" and approval_key in _LAB_PHYSICAL_APPROVALS:
        return True
    try:
        with anyio.fail_after(_approval_timeout()):
            result = await ctx.elicit(
                message=f"BTAP requests one physical input action: {summary}",
                schema=PhysicalInputApproval,
            )
        approved = (
            result.action == "accept" and result.data is not None
            and result.data.approve is True
        )
        if approved and profile["mode"] == "lab":
            _LAB_PHYSICAL_APPROVALS.add(approval_key)
        return approved
    except TimeoutError:
        # Never fall through to "approved" on a timeout, and never keep holding
        # the tool lock waiting for a human who has walked away.
        logger.warning(
            "Physical-input approval (%s) went unanswered for %ss; treating it as declined.",
            summary, _approval_timeout(),
        )
        return False
    except Exception:
        # Older MCP clients may not implement elicitation. Physical input is
        # deliberately unavailable in that case instead of silently proceeding.
        return False


def _requires_user_action() -> dict[str, Any]:
    return {
        "status": "requires_user_action",
        "message": "One-action physical-input approval was declined, cancelled, or unavailable.",
    }


def _physical_error_result(status: str, message: str) -> dict[str, Any]:
    return {"status": status, "message": message}


async def _run_approved_physical_action(
    ctx: Context,
    summary: str,
    action: Callable[[], dict[str, Any]],
    *,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = None,
) -> dict[str, Any]:
    if not await _request_physical_approval(ctx, summary):
        return _requires_user_action()

    should_activate = session_id is not None or activate_session not in (None, "none")
    action_started = False

    def worker() -> dict[str, Any]:
        nonlocal action_started
        def gated_action() -> dict[str, Any]:
            nonlocal action_started
            # Activation is itself foreground work and must wait until the
            # lease and quiet-input check have passed.
            action_started = False
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
                    }
            action_started = True
            result = action()
            if activated:
                result["activated"] = activated
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


_PHYSICAL_INPUT_NOTICE = (
    " Safe mode requires one-action approval; lab skips prompting by default and uses session "
    "approval only when BROWSERTAP_LAB_NO_ELICIT is explicitly disabled. By default BTAP "
    "foregrounds and verifies the selected browser tab after the quiet-input check; prefer an "
    "explicit session_id for browser input. activate_session='none' is only for intentional "
    "input to the already-visible desktop or native UI."
)


# --- Tools: physical mouse and keyboard --------------------------------------
@mcp.tool(description="Move the real mouse cursor to screen coordinates." + _PHYSICAL_INPUT_NOTICE)
async def mouse_move(
    ctx: Context,
    x: int,
    y: int,
    duration: float = 0.0,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = "current",
) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        pyautogui.moveTo(x, y, duration=duration)
        return {"status": "ok", "x": x, "y": y}

    return await _run_approved_physical_action(
        ctx,
        f"move cursor to ({x}, {y})",
        action,
        session_id=session_id,
        activate_session=activate_session,
    )


def _maybe_activate(activate_session: Optional[str],
                    session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Raise the target tab before a screen-coordinate action.

    Physical input lands on whatever is actually on screen, so skipping this
    makes a switch_tab + mouse_click pair click the previously visible tab —
    silently, since the coordinates are valid and pyautogui reports success.
    That is why raising the tab is the default and opting out is explicit.

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


@mcp.tool(
    description=(
        "Click on the real desktop at screen coordinates. Pass session_id — the same one you "
        "pass every other tool (preferred) — and that tab is raised after the quiet check so "
        "the click lands on it. "
        "Without one the current global target is raised, which another task may have "
        "changed. Approval may foreground the selected browser tab. "
        "activate_session='none' clicks the desktop as-is."
    )
)
async def mouse_click(
    ctx: Context,
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = "current",
) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        if x is not None and y is not None:
            pyautogui.click(x=x, y=y, clicks=clicks, interval=interval, button=button)
        else:
            pyautogui.click(clicks=clicks, interval=interval, button=button)
        return {"status": "ok", "x": x, "y": y, "button": button, "clicks": clicks}

    target = f" at ({x}, {y})" if x is not None and y is not None else " at the current pointer"
    return await _run_approved_physical_action(
        ctx,
        f"click{target} with {button} button",
        action,
        session_id=session_id,
        activate_session=activate_session,
    )


@mcp.tool(description="Drag the real mouse from one point to another." + _PHYSICAL_INPUT_NOTICE)
async def mouse_drag(
    ctx: Context,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration: float = 0.3,
    button: str = "left",
    session_id: Optional[str] = None,
    activate_session: Optional[str] = "current",
) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        pyautogui.moveTo(x1, y1)
        pyautogui.dragTo(x2, y2, duration=duration, button=button)
        return {"status": "ok", "from": [x1, y1], "to": [x2, y2], "button": button}

    return await _run_approved_physical_action(
        ctx,
        f"drag from ({x1}, {y1}) to ({x2}, {y2})",
        action,
        session_id=session_id,
        activate_session=activate_session,
    )


@mcp.tool(
    description=(
        "Type text via the real keyboard, optionally after clicking a field. Pass session_id — "
        "the same one you pass every other tool (preferred) — and that tab is raised after the "
        "quiet check so the keystrokes go to it. Without one the current global target is raised, "
        "which another task may have changed. Approval may foreground the selected browser tab. "
        "activate_session='none' types into whatever already has focus."
    )
)
async def type_text(
    ctx: Context,
    text: str,
    interval: float = 0.01,
    click_x: Optional[int] = None,
    click_y: Optional[int] = None,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = "current",
) -> dict[str, Any]:
    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        if click_x is not None and click_y is not None:
            pyautogui.click(click_x, click_y)
            time.sleep(0.1)
        pyautogui.write(text, interval=interval)
        return {"status": "ok", "typed_chars": len(text)}

    return await _run_approved_physical_action(
        ctx,
        f"type {len(text)} characters",
        action,
        session_id=session_id,
        activate_session=activate_session,
    )


@mcp.tool(description="Send a hotkey chord like 'command,l' or 'ctrl,shift,p' via the real keyboard." + _PHYSICAL_INPUT_NOTICE)
async def hotkey(
    ctx: Context,
    keys_csv: str,
    session_id: Optional[str] = None,
    activate_session: Optional[str] = "current",
) -> dict[str, Any]:
    keys = [k.strip() for k in keys_csv.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("keys_csv must contain at least one key")

    def action() -> dict[str, Any]:
        pyautogui = _pyautogui()
        pyautogui.hotkey(*keys)
        return {"status": "ok", "keys": keys}

    return await _run_approved_physical_action(
        ctx,
        f"send hotkey {keys_csv}",
        action,
        session_id=session_id,
        activate_session=activate_session,
    )


@mcp.tool(description="Report the current desktop mouse position and primary screen size.")
def pointer_info() -> dict[str, Any]:
    pyautogui = _pyautogui()

    x, y = pyautogui.position()
    w, h = pyautogui.size()
    return {"x": x, "y": y, "screen_width": w, "screen_height": h}


if __name__ == "__main__":
    configure_stdio_logging()
    get_driver()
    mcp.run(transport="stdio")
