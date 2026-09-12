"""Explicit, bounded Windows file-dialog inspection and cancellation.

The public surface accepts neither arbitrary handles nor global input. Native
APIs are loaded lazily after opt-in and desktop-extra checks. The OS adapter is
injectable so offline tests never inspect or operate the real desktop.
"""

from __future__ import annotations

import atexit
import ctypes
import importlib.metadata
import ntpath
import os
import secrets
import sys
import threading
import time
from collections import OrderedDict
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from . import physical_input
from .paths import state_dir

_TICKET_SECONDS = 15.0
_MAX_TICKETS = 8
_QUIET_SECONDS = 0.75
_MESSAGE_TIMEOUT_MS = 750
_BROWSER_EXECUTABLES = {"chrome": "chrome.exe", "edge": "msedge.exe"}


@dataclass(frozen=True)
class _Process:
    pid: int
    created: int
    image: str


@dataclass(frozen=True)
class _Window:
    hwnd: int
    pid: int
    tid: int
    class_name: str
    parent: int = 0
    owner: int = 0
    control_id: int = 0
    style: int = 0
    visible: bool = True
    enabled: bool = True
    minimized: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    def identity(self) -> tuple[Any, ...]:
        # Geometry and visibility are rechecked, rather than used as identity:
        # moving the same window does not give a ticket a different target.
        return (
            self.hwnd, self.pid, self.tid, self.class_name, self.parent,
            self.owner, self.control_id, self.style & 0xF,
        )


@dataclass(frozen=True)
class _Snapshot:
    dialog: _Window
    owners: tuple[_Window, ...]
    cancel: _Window
    shell_controls: tuple[_Window, ...]
    process: _Process
    browser: str

    def identity(self) -> tuple[Any, ...]:
        return (
            self.dialog.identity(), tuple(owner.identity() for owner in self.owners),
            self.cancel.identity(), tuple(window.identity() for window in self.shell_controls),
            self.process, self.browser,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "browser": self.browser,
            "dialog_hwnd": hex(self.dialog.hwnd),
            "cancel_hwnd": hex(self.cancel.hwnd),
            "owner_chain": [hex(owner.hwnd) for owner in self.owners],
            "process_id": self.process.pid,
            "process_created": self.process.created,
            "recognition": "windows_shell_file_dialog",
            "identity_source": "win32_owner_process_app_paths_and_window_property",
        }


@dataclass
class _Ticket:
    snapshot: _Snapshot
    adapter: Any
    marker_name: str
    marker_value: int
    expires: float
    timer: Any = None
    claimed: bool = False


@dataclass(frozen=True)
class _Cancellation:
    manager: NativeDialogManager
    ticket: str
    record: _Ticket | None
    desktop_opt_in: bool

    def needs_approval(self) -> bool:
        with self.manager._lock:
            return (
                self.record is not None
                and self.manager._tickets.get(self.ticket) is self.record
                and self.record.expires > self.manager._clock()
            )

    def cancel(self, *, approved: bool = True) -> dict[str, Any]:
        return self.manager._cancel_claimed(self, approved=approved)


class _Refused(Exception):
    def __init__(self, code: str, message: str, *, on_screen: bool | None = None):
        super().__init__(message)
        self.code = code
        self.on_screen = on_screen


def _normal_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _desktop_extra() -> dict[str, Any]:
    versions: dict[str, str] = {}
    missing: list[str] = []
    for package in ("PyAutoGUI", "mss", "Pillow"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            missing.append(package)
    return {
        "available": not missing, "missing": missing, "versions": versions,
        "check": "distribution_metadata",
    }


def _unchecked_quiet() -> dict[str, Any]:
    return {"checked": False, "enforced": False, "observed": []}


def _visible(adapter: Any, window: _Window) -> None:
    if not window.visible or window.minimized:
        raise _Refused(
            "native_dialog_unverifiable", "The dialog or its owner is not visible.",
            on_screen=False,
        )
    cloaked = adapter.is_cloaked(window.hwnd)
    if cloaked is not False:
        raise _Refused(
            "native_dialog_unverifiable", "The window's uncloaked state could not be verified.",
            on_screen=False if cloaked is True else None,
        )


def _snapshot(adapter: Any, expected: _Snapshot | None = None) -> _Snapshot:
    foreground = adapter.foreground_window()
    if expected is not None and foreground != expected.dialog.hwnd:
        raise _Refused(
            "native_dialog_not_foreground", "The inspected dialog is no longer foreground.",
        )
    if not foreground:
        raise _Refused("native_dialog_unverifiable", "No foreground native window is available.")
    dialog = adapter.read_window(foreground)
    if dialog.class_name != "#32770" or dialog.style & 0x40000000:
        raise _Refused("native_dialog_unsupported", "The foreground window is not a standard dialog.")
    _visible(adapter, dialog)
    if not dialog.enabled:
        raise _Refused("native_dialog_unverifiable", "The foreground dialog is disabled.")

    owners: list[_Window] = []
    owner_hwnd = dialog.owner
    seen = {dialog.hwnd}
    while owner_hwnd:
        if owner_hwnd in seen or len(owners) >= 8:
            raise _Refused("native_dialog_unverifiable", "The native owner chain is ambiguous.")
        seen.add(owner_hwnd)
        owner = adapter.read_window(owner_hwnd)
        if owner.pid != dialog.pid:
            raise _Refused("native_dialog_unverifiable", "The native owner belongs to another process.")
        _visible(adapter, owner)
        owners.append(owner)
        owner_hwnd = owner.owner
    if not owners or owners[-1].class_name != "Chrome_WidgetWin_1":
        raise _Refused("native_dialog_unverifiable", "No supported browser owner was verified.")

    process = adapter.read_process(dialog.pid)
    browser = None
    for name, paths in adapter.registered_browser_paths().items():
        executable = _BROWSER_EXECUTABLES.get(name)
        if (
            executable is not None and process.pid == dialog.pid and process.created > 0
            and ntpath.basename(process.image).casefold() == executable
            and _normal_path(process.image) in {_normal_path(path) for path in paths}
        ):
            browser = name
            break
    if browser is None:
        raise _Refused(
            "native_dialog_unverifiable", "The owner process image does not match a registered browser.",
        )

    handles = adapter.descendant_handles(dialog.hwnd)
    if len(handles) > 256 or len(set(handles)) != len(handles):
        raise _Refused("native_dialog_unsupported", "The native dialog has an unsupported control tree.")
    children = [adapter.read_window(hwnd) for hwnd in handles]
    cancel_candidates = [
        window for window in children
        if window.control_id == 2 and window.parent == dialog.hwnd
        and window.class_name.casefold() == "button" and window.style & 0xF in (0, 1)
    ]
    if len(cancel_candidates) != 1:
        raise _Refused("native_dialog_unsupported", "An unambiguous standard Cancel button is required.")
    cancel = cancel_candidates[0]
    if (
        cancel.pid != dialog.pid or cancel.tid != dialog.tid
        or not cancel.visible or not cancel.enabled
    ):
        raise _Refused("native_dialog_unverifiable", "The Cancel button cannot be verified as usable.")
    view = [window for window in children if window.class_name.casefold() == "shelldll_defview"]
    filename = [
        window for window in children
        if window.control_id == 0x47C and window.class_name.casefold() in ("combobox", "comboboxex32")
    ]
    filetype = [
        window for window in children
        if window.control_id == 0x470 and window.class_name.casefold() == "combobox"
    ]
    if any(len(group) != 1 for group in (view, filename, filetype)):
        raise _Refused(
            "native_dialog_unsupported", "The standard Shell file-list and filename/type controls are required.",
        )
    shell = (view[0], filename[0], filetype[0])
    if any(
        window.pid != dialog.pid or not window.visible
        or not adapter.is_child(dialog.hwnd, window.hwnd)
        for window in (*shell, cancel)
    ):
        raise _Refused("native_dialog_unverifiable", "The file-dialog controls have unverifiable ownership.")

    active, focus = adapter.gui_thread_state(dialog.tid)
    if active != dialog.hwnd or not (focus == dialog.hwnd or adapter.is_child(dialog.hwnd, focus)):
        raise _Refused("native_dialog_not_foreground", "The active window and focus must belong to the dialog.")
    left, top, right, bottom = cancel.rect
    dl, dt, dr, db = dialog.rect
    if right - left < 3 or bottom - top < 3 or not (dl <= left < right <= dr and dt <= top < bottom <= db):
        raise _Refused("native_dialog_obscured", "The Cancel button is outside the verified dialog.", on_screen=False)
    points = (
        ((left + right) // 2, (top + bottom) // 2),
        (left + 1, top + 1), (right - 2, top + 1),
        (left + 1, bottom - 2), (right - 2, bottom - 2),
    )
    if any(
        not adapter.point_on_monitor(point)
        or adapter.top_level_at_point(point) != dialog.hwnd
        or adapter.window_at_point(point) != cancel.hwnd
        for point in points
    ):
        raise _Refused("native_dialog_obscured", "The Cancel button is off screen or obscured.", on_screen=False)
    snapshot = _Snapshot(dialog, tuple(owners), cancel, shell, process, browser)
    if expected is not None and snapshot.identity() != expected.identity():
        raise _Refused("native_dialog_identity_changed", "The inspected native dialog identity changed.")
    if adapter.foreground_window() != dialog.hwnd:
        raise _Refused("native_dialog_not_foreground", "Foreground changed during native inspection.")
    return snapshot


class NativeDialogManager:
    """Keep short-lived, single-attempt tickets and enforce one native boundary."""

    def __init__(
        self, *, adapter_factory: Callable[[], Any] | None = None,
        platform: Callable[[], str] | None = None,
        extra_probe: Callable[[], dict[str, Any]] = _desktop_extra,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timer_factory: Callable[..., Any] = threading.Timer,
        lock_path: str | os.PathLike[str] | None = None,
    ):
        self._adapter_factory = adapter_factory or _WindowsAdapter
        self._platform = platform or (lambda: sys.platform)
        self._extra_probe = extra_probe
        self._clock = clock
        self._sleep = sleep
        self._timer_factory = timer_factory
        self._lock_path = lock_path
        self._lock = threading.Lock()
        self._tickets: OrderedDict[str, _Ticket] = OrderedDict()

    def _base(self, action: str, opt_in: bool) -> dict[str, Any]:
        return {
            "ok": False, "status": "refused", "on_screen": None,
            "input_quiet": _unchecked_quiet(), "dispatched": False,
            "delivery_state": "undelivered", "retry_safe": False,
            "desktop": {
                "capability": "native_file_dialog", "action": action,
                "explicit_opt_in": opt_in is True, "platform": self._platform(),
                "input_method": "targeted_window_message",
            },
        }

    def _preflight(self, result: dict[str, Any], opt_in: bool) -> None:
        if opt_in is not True:
            raise _Refused("desktop_opt_in_required", "Explicit desktop_opt_in=true is required.")
        if result["desktop"]["platform"] != "win32":
            raise _Refused("native_dialog_unsupported", "Native file-dialog cancellation is supported only on Windows.")
        extra = self._extra_probe()
        result["desktop"]["extra"] = extra
        if extra.get("available") is not True:
            raise _Refused("desktop_extra_unavailable", "Install browsertap-mcp[desktop] for this explicit capability.")

    @staticmethod
    def _failure(result: dict[str, Any], exc: _Refused) -> dict[str, Any]:
        result.update(ok=False, code=exc.code, message=str(exc))
        result["on_screen"] = exc.on_screen
        if exc.code in ("native_dialog_unsupported", "desktop_extra_unavailable"):
            result["status"] = "unsupported"
        return result

    @staticmethod
    def _clean(record: _Ticket) -> bool:
        if record.timer is not None:
            record.timer.cancel()
        try:
            record.adapter.remove_marker(
                record.snapshot.dialog.hwnd, record.marker_name, record.marker_value,
            )
            return True
        except Exception:
            # Cleanup never broadens to another property or a window message.
            # A destroyed/reused window cannot carry this ticket's live marker.
            return False

    def _expire(self, token: str) -> None:
        with self._lock:
            record = self._tickets.pop(token, None)
        if record is not None:
            self._clean(record)

    def _publish(self, token: str, record: _Ticket) -> None:
        record.expires = self._clock() + _TICKET_SECONDS
        record.timer = self._timer_factory(_TICKET_SECONDS, self._expire, args=(token,))
        record.timer.daemon = True
        evicted = None
        with self._lock:
            if len(self._tickets) >= _MAX_TICKETS:
                _, evicted = self._tickets.popitem(last=False)
            self._tickets[token] = record
            try:
                record.timer.start()
            except Exception:
                self._tickets.pop(token, None)
                raise
            finally:
                if evicted is not None:
                    self._clean(evicted)

    def close(self) -> None:
        with self._lock:
            records = list(self._tickets.values())
            self._tickets.clear()
        for record in records:
            self._clean(record)

    def ticket_needs_approval(self, ticket: str) -> bool:
        if not isinstance(ticket, str):
            return False
        with self._lock:
            record = self._tickets.get(ticket)
            return record is not None and not record.claimed and record.expires > self._clock()

    @contextmanager
    def claim(self, ticket: str, *, desktop_opt_in: bool = False) -> Iterator[_Cancellation]:
        record = None
        if desktop_opt_in is True:
            with self._lock:
                available = self._tickets.get(ticket) if isinstance(ticket, str) else None
                if available is not None and not available.claimed:
                    record = available
                    record.claimed = True
        try:
            # Keep claimed records in the bounded store with their timer active
            # while approval awaits. Only this attempt may consume the record.
            yield _Cancellation(self, ticket, record, desktop_opt_in)
        finally:
            if record is not None:
                with self._lock:
                    if self._tickets.get(ticket) is record:
                        self._tickets.pop(ticket)
                    else:
                        record = None  # A worker, expiry, or eviction owns cleanup.
                if record is not None:
                    self._clean(record)

    def inspect(self, *, desktop_opt_in: bool = False) -> dict[str, Any]:
        result = self._base("inspect", desktop_opt_in)
        record = None
        published = False
        try:
            self._preflight(result, desktop_opt_in)
            adapter = self._adapter_factory()
            with adapter.dpi_context():
                snapshot = _snapshot(adapter)
                record = _Ticket(
                    snapshot, adapter, "BTAP.NativeFileDialog." + secrets.token_hex(24),
                    secrets.randbelow(0x7FFFFFFE) + 1, self._clock() + _TICKET_SECONDS,
                )
                adapter.set_marker(snapshot.dialog.hwnd, record.marker_name, record.marker_value)
                if adapter.marker(snapshot.dialog.hwnd, record.marker_name) != record.marker_value:
                    raise _Refused("native_dialog_marker_unavailable", "The window lifetime marker could not be verified.")
                _snapshot(adapter, snapshot)
                if adapter.marker(snapshot.dialog.hwnd, record.marker_name) != record.marker_value:
                    raise _Refused("native_dialog_identity_changed", "The window lifetime changed during inspection.")
            token = secrets.token_urlsafe(32)
            self._publish(token, record)
            published = True
            result.update(
                ok=True, status="inspected", ticket=token,
                expires_in_seconds=_TICKET_SECONDS, on_screen=True,
            )
            result["desktop"].update(snapshot.diagnostics(), lifetime_marker="installed")
            return result
        except _Refused as exc:
            return self._failure(result, exc)
        except Exception:
            return self._failure(result, _Refused(
                "native_dialog_unavailable", "The native identity APIs could not verify this dialog.",
            ))
        finally:
            if record is not None and not published:
                result["desktop"]["marker_cleanup_verified"] = self._clean(record)

    def cancel(
        self, ticket: str, *, desktop_opt_in: bool = False, approved: bool = True,
    ) -> dict[str, Any]:
        with self.claim(ticket, desktop_opt_in=desktop_opt_in) as cancellation:
            return cancellation.cancel(approved=approved)

    def _cancel_claimed(self, cancellation: _Cancellation, *, approved: bool) -> dict[str, Any]:
        desktop_opt_in = cancellation.desktop_opt_in
        result = self._base("cancel", desktop_opt_in)
        record = None
        try:
            if cancellation.record is not None:
                with self._lock:
                    if self._tickets.get(cancellation.ticket) is cancellation.record:
                        record = self._tickets.pop(cancellation.ticket)
                if record is not None:
                    record.timer.cancel()
                    result["desktop"].update(record.snapshot.diagnostics(), ticket_consumed=True)
            self._preflight(result, desktop_opt_in)
            if record is None:
                raise _Refused("native_dialog_ticket_invalid", "Inspect the foreground file dialog to obtain a fresh ticket.")
            if self._clock() >= record.expires:
                raise _Refused("native_dialog_ticket_expired", "The ticket expired; inspect the dialog again.")
            if approved is not True:
                raise _Refused("requires_user_action", "Physical-input approval was declined, cancelled, or unavailable.")
            path = state_dir() / "physical-input.lock" if self._lock_path is None else Path(self._lock_path)
            with physical_input.PhysicalInputLease(path=path, action_summary="cancel inspected native file dialog"):
                adapter = record.adapter
                with adapter.dpi_context():
                    # GetCursorPos must use the same coordinate model for the
                    # quiet window and both sides of native revalidation.
                    before = physical_input.last_input_marker()
                    result["input_quiet"] = {
                        **physical_input.wait_for_quiet(_QUIET_SECONDS), "checked": True,
                    }
                    quiet = result["input_quiet"]
                    if quiet.get("enforced") is not True or "os_last_input_time" not in quiet.get("observed", []):
                        raise _Refused("input_quiet_unavailable", "Windows keyboard input must be observed before cancellation.")
                    snapshot = _snapshot(adapter, record.snapshot)
                    result["on_screen"] = True
                    if adapter.marker(snapshot.dialog.hwnd, record.marker_name) != record.marker_value:
                        raise _Refused("native_dialog_identity_changed", "The inspected window lifetime marker changed.")
                    if self._clock() >= record.expires:
                        raise _Refused("native_dialog_ticket_expired", "The ticket expired during validation; inspect again.")
                    after = physical_input.last_input_marker()
                    if before[0] is None or after[0] is None:
                        raise _Refused("input_quiet_unavailable", "Windows input observation was lost before dispatch.")
                    if any(old != new for old, new in zip(before, after, strict=True) if old is not None and new is not None):
                        raise physical_input.InputActivityDetected("Input changed during native revalidation.")
                    pressed = adapter.input_is_pressed()
                    quiet.update(held_input_checked=True, input_held=pressed)
                    if pressed:
                        raise physical_input.InputActivityDetected("A keyboard or mouse input is still held.")
                    if adapter.foreground_window() != snapshot.dialog.hwnd:
                        raise _Refused("native_dialog_not_foreground", "Foreground changed before cancellation.")
                    # The ticket is already consumed. No exception after this
                    # point may authorise replay, including a timeout or failed
                    # post-dispatch visibility/identity observation.
                    result.update(dispatched=True, may_have_executed=True, delivery_state="unknown")
                    result["desktop"]["closed_observed"] = False
                    adapter.send_cancel(snapshot.cancel.hwnd, _MESSAGE_TIMEOUT_MS)
                    result["delivery_state"] = "acknowledged"
                    for observation in range(11):
                        if adapter.window_exists(snapshot.dialog.hwnd) is False:
                            result.update(ok=True, status="success", cancelled=True)
                            result["desktop"]["closed_observed"] = True
                            return result
                        if adapter.marker(snapshot.dialog.hwnd, record.marker_name) != record.marker_value:
                            break
                        if observation < 10:
                            self._sleep(0.05)
                    result.update(
                        status="unknown", code="native_dialog_cancel_unconfirmed",
                        message="Cancel was dispatched but closure was not confirmed; inspect state before another action.",
                    )
                    return result
        except physical_input.PhysicalInputBusy:
            return self._failure(result, _Refused(
                "physical_input_busy", "Physical input is busy; after it finishes, inspect again for a fresh ticket.",
            ))
        except physical_input.InputActivityDetected:
            result["input_quiet"].update(checked=True, enforced=True, activity_detected=True)
            return self._failure(result, _Refused(
                "input_activity_detected", "User input is active or changed; inspect again after input is quiet.",
            ))
        except _Refused as exc:
            return self._failure(result, exc)
        except Exception:
            if result["dispatched"]:
                result.update(
                    ok=False, status="unknown", code="native_dialog_dispatch_unknown",
                    message="Native cancellation delivery is unknown; inspect state before another action.",
                )
                return result
            return self._failure(result, _Refused(
                "native_dialog_unavailable", "Native cancellation could not verify its target; no input was dispatched.",
            ))
        finally:
            if record is not None:
                result["desktop"]["marker_cleanup_verified"] = self._clean(record)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int32), ("top", ctypes.c_int32),
        ("right", ctypes.c_int32), ("bottom", ctypes.c_int32),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", _Rect),
    ]


def _last_error() -> int:
    return int(getattr(ctypes, "get_last_error", lambda: 0)())


def _read_app_paths() -> dict[str, tuple[str, ...]]:
    winreg = importlib.import_module("winreg")

    registered: dict[str, tuple[str, ...]] = {}
    for browser, executable in _BROWSER_EXECUTABLES.items():
        paths: set[str] = set()
        key_name = "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + executable
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                        value, kind = winreg.QueryValueEx(key, "")
                except OSError:
                    continue
                if not isinstance(value, str) or kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    continue
                if kind == winreg.REG_EXPAND_SZ:
                    value = os.path.expandvars(value)
                value = value.strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                if ntpath.isabs(value) and ntpath.basename(value).casefold() == executable:
                    paths.add(_normal_path(value))
        registered[browser] = tuple(sorted(paths))
    return registered


class _WindowsAdapter:
    """Small, synchronous Win32 adapter; construction itself sends no input."""

    def __init__(self, *, user32: Any = None, kernel32: Any = None, dwmapi: Any = None):
        if user32 is None or kernel32 is None or dwmapi is None:
            loader = importlib.import_module("ctypes").WinDLL
            user32 = loader("user32", use_last_error=True)
            kernel32 = loader("kernel32", use_last_error=True)
            dwmapi = loader("dwmapi", use_last_error=True)
        self._user32 = user32
        self._kernel32 = kernel32
        self._dwmapi = dwmapi
        handle, dword, integer = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        pointer = ctypes.POINTER
        callback_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        self._enum_callback = callback_factory(integer, handle, ctypes.c_ssize_t)

        def bind(library: Any, name: str, args: list[Any], returns: Any) -> Any:
            function = getattr(library, name)
            function.argtypes = args
            function.restype = returns
            return function

        window_bindings: list[tuple[str, list[Any], Any]] = [
            ("GetForegroundWindow", [], handle),
            ("GetTopWindow", [handle], handle),
            ("IsWindow", [handle], integer),
            ("GetWindowThreadProcessId", [handle, pointer(dword)], dword),
            ("GetClassNameW", [handle, ctypes.c_wchar_p, integer], integer),
            ("GetWindow", [handle, dword], handle),
            ("GetParent", [handle], handle),
            ("GetDlgCtrlID", [handle], integer),
            ("IsWindowVisible", [handle], integer),
            ("IsWindowEnabled", [handle], integer),
            ("IsIconic", [handle], integer),
            ("GetWindowRect", [handle, pointer(_Rect)], integer),
            ("GetGUIThreadInfo", [dword, pointer(_GuiThreadInfo)], integer),
            ("IsChild", [handle, handle], integer),
            ("EnumChildWindows", [handle, self._enum_callback, ctypes.c_ssize_t], integer),
            ("MonitorFromPoint", [_Point, dword], handle),
            ("WindowFromPoint", [_Point], handle),
            ("GetAsyncKeyState", [integer], ctypes.c_int16),
            ("SetPropW", [handle, ctypes.c_wchar_p, handle], integer),
            ("GetPropW", [handle, ctypes.c_wchar_p], handle),
            ("RemovePropW", [handle, ctypes.c_wchar_p], handle),
            ("SetThreadDpiAwarenessContext", [handle], handle),
            (
                "SendMessageTimeoutW",
                [handle, dword, ctypes.c_size_t, ctypes.c_ssize_t, dword, dword, pointer(ctypes.c_size_t)],
                ctypes.c_ssize_t,
            ),
        ]
        for name, args, returns in window_bindings:
            bind(user32, name, args, returns)
        style_function = "GetWindowLongPtrW" if ctypes.sizeof(handle) == 8 else "GetWindowLongW"
        self._window_style = bind(user32, style_function, [handle, integer], ctypes.c_ssize_t)
        process_bindings: list[tuple[str, list[Any], Any]] = [
            ("OpenProcess", [dword, integer, dword], handle),
            ("QueryFullProcessImageNameW", [handle, dword, ctypes.c_wchar_p, pointer(dword)], integer),
            ("GetProcessTimes", [handle, pointer(_FileTime), pointer(_FileTime), pointer(_FileTime), pointer(_FileTime)], integer),
            ("GetExitCodeProcess", [handle, pointer(dword)], integer),
            ("CloseHandle", [handle], integer),
        ]
        for name, args, returns in process_bindings:
            bind(kernel32, name, args, returns)
        bind(dwmapi, "DwmGetWindowAttribute", [handle, dword, handle, dword], ctypes.c_int32)

    @contextmanager
    def dpi_context(self) -> Iterator[None]:
        # Scope the coordinate model to this worker thread, and restore it.
        # A process-wide DPI setter would interfere with existing desktop tools.
        previous = self._user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise OSError(_last_error(), "Thread DPI awareness is unavailable.")
        try:
            yield
        finally:
            if not self._user32.SetThreadDpiAwarenessContext(previous):
                raise OSError(_last_error(), "Thread DPI awareness could not be restored.")

    def foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def read_window(self, hwnd: int) -> _Window:
        if not self._user32.IsWindow(hwnd):
            raise OSError("Native window no longer exists.")
        pid = ctypes.c_uint32()
        tid = int(self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)))
        name = ctypes.create_unicode_buffer(256)
        rect = _Rect()
        if not tid or not pid.value or not self._user32.GetClassNameW(hwnd, name, len(name)):
            raise OSError(_last_error(), "Native window identity is unavailable.")
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError(_last_error(), "Native window geometry is unavailable.")
        window = _Window(
            hwnd, int(pid.value), tid, name.value,
            parent=int(self._user32.GetParent(hwnd) or 0),
            owner=int(self._user32.GetWindow(hwnd, 4) or 0),  # GW_OWNER
            control_id=int(self._user32.GetDlgCtrlID(hwnd)),
            style=int(self._window_style(hwnd, -16)) & 0xFFFFFFFF,  # GWL_STYLE
            visible=bool(self._user32.IsWindowVisible(hwnd)),
            enabled=bool(self._user32.IsWindowEnabled(hwnd)),
            minimized=bool(self._user32.IsIconic(hwnd)),
            rect=(rect.left, rect.top, rect.right, rect.bottom),
        )
        again = ctypes.c_uint32()
        again_tid = int(self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(again)))
        if (again.value, again_tid) != (window.pid, window.tid):
            raise OSError("Native window process identity changed during inspection.")
        return window

    def read_process(self, pid: int) -> _Process:
        handle = self._kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            raise OSError(_last_error(), "Browser process identity is unavailable.")
        try:
            capacity = ctypes.c_uint32(32768)
            image = ctypes.create_unicode_buffer(capacity.value)
            created, exited, kernel, user = (_FileTime() for _ in range(4))
            exit_code = ctypes.c_uint32()
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(capacity)):
                raise OSError(_last_error(), "Browser process image is unavailable.")
            if not self._kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user),
            ):
                raise OSError(_last_error(), "Browser process creation identity is unavailable.")
            if not self._kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                raise OSError("Browser process is not live.")
            return _Process(pid, (int(created.high) << 32) | int(created.low), _normal_path(image.value))
        finally:
            self._kernel32.CloseHandle(handle)

    def registered_browser_paths(self) -> dict[str, tuple[str, ...]]:
        return _read_app_paths()

    def is_cloaked(self, hwnd: int) -> bool | None:
        cloaked = ctypes.c_uint32()
        result = self._dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return bool(cloaked.value) if result == 0 else None

    def gui_thread_state(self, tid: int) -> tuple[int, int]:
        state = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
        if not self._user32.GetGUIThreadInfo(tid, ctypes.byref(state)):
            raise OSError(_last_error(), "Native focus identity is unavailable.")
        return int(state.hwndActive or 0), int(state.hwndFocus or 0)

    def is_child(self, parent: int, child: int) -> bool:
        return bool(child and self._user32.IsChild(parent, child))

    def descendant_handles(self, hwnd: int) -> tuple[int, ...]:
        handles: list[int] = []

        def collect(child: int, _data: int) -> int:
            handles.append(int(child))
            return int(len(handles) <= 256)

        callback = self._enum_callback(collect)
        self._user32.EnumChildWindows(hwnd, callback, 0)
        if len(handles) > 256:
            raise _Refused("native_dialog_unsupported", "The native control tree exceeds the inspection limit.")
        return tuple(handles)

    def point_on_monitor(self, point: tuple[int, int]) -> bool:
        return bool(self._user32.MonitorFromPoint(_Point(*point), 0))  # DEFAULTTONULL

    def window_at_point(self, point: tuple[int, int]) -> int:
        return int(self._user32.WindowFromPoint(_Point(*point)) or 0)

    def top_level_at_point(self, point: tuple[int, int]) -> int:
        # WindowFromPoint skips disabled windows. A disabled overlay may still
        # obscure the Cancel button, so also inspect visible top-level z-order.
        # Unknown geometry or cloaking refuses instead of treating a gap as clear.
        hwnd = int(self._user32.GetTopWindow(0) or 0)
        seen: set[int] = set()
        while hwnd:
            if hwnd in seen or len(seen) >= 256:
                raise OSError("The top-level window order could not be verified.")
            seen.add(hwnd)
            if self._user32.IsWindowVisible(hwnd) and not self._user32.IsIconic(hwnd):
                rect = _Rect()
                if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    raise OSError("Potential overlay geometry is unavailable.")
                x, y = point
                if rect.left <= x < rect.right and rect.top <= y < rect.bottom:
                    cloaked = self.is_cloaked(hwnd)
                    if cloaked is None:
                        raise OSError("Potential overlay visibility is unavailable.")
                    if not cloaked:
                        return hwnd
            hwnd = int(self._user32.GetWindow(hwnd, 2) or 0)  # GW_HWNDNEXT
        return 0

    def input_is_pressed(self) -> bool:
        # The quiet timestamp does not detect a button held motionless before
        # its first sample. Read only the currently-down bit, never key text.
        return any(self._user32.GetAsyncKeyState(key) & 0x8000 for key in range(1, 256))

    def set_marker(self, hwnd: int, name: str, value: int) -> None:
        if not self._user32.SetPropW(hwnd, name, value):
            raise OSError(_last_error(), "Window lifetime marking is unavailable.")

    def marker(self, hwnd: int, name: str) -> int:
        return int(self._user32.GetPropW(hwnd, name) or 0)

    def remove_marker(self, hwnd: int, name: str, value: int) -> None:
        # Each ticket has a random property name as well as a random value.
        # An absent/mismatched value is never removed, including on HWND reuse.
        if self.marker(hwnd, name) == value:
            self._user32.RemovePropW(hwnd, name)
            if self.marker(hwnd, name) == value:
                raise OSError(_last_error(), "Window lifetime marker cleanup could not be verified.")

    def window_exists(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(hwnd))

    def send_cancel(self, hwnd: int, timeout_ms: int) -> None:
        result = ctypes.c_size_t()
        acknowledged = self._user32.SendMessageTimeoutW(
            hwnd, 0x00F5, 0, 0, 0x01 | 0x02, timeout_ms, ctypes.byref(result),
        )  # BM_CLICK; BLOCK | ABORTIFHUNG
        # ERRORONEXIT would also reject the expected case where handling
        # Cancel destroys its own Button. Closure is checked separately.
        if not acknowledged:
            error = _last_error()
            if error in (0, 1460):
                raise TimeoutError("Native Cancel message did not acknowledge within its bounded wait.")
            raise OSError(error, "Native Cancel delivery could not be confirmed.")


_MANAGER = NativeDialogManager()
atexit.register(_MANAGER.close)


def inspect_native_file_dialog(*, desktop_opt_in: bool = False) -> dict[str, Any]:
    return _MANAGER.inspect(desktop_opt_in=desktop_opt_in)


def ticket_needs_approval(ticket: str) -> bool:
    return _MANAGER.ticket_needs_approval(ticket)


def _claim_cancellation(
    ticket: str, *, desktop_opt_in: bool = False,
) -> AbstractContextManager[_Cancellation]:
    return _MANAGER.claim(ticket, desktop_opt_in=desktop_opt_in)


def cancel_native_file_dialog(
    ticket: str, *, desktop_opt_in: bool = False, approved: bool = True,
) -> dict[str, Any]:
    return _MANAGER.cancel(ticket, desktop_opt_in=desktop_opt_in, approved=approved)
