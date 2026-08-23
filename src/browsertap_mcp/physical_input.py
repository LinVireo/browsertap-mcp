"""Cross-process lease and user-activity gate for physical input actions."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from .paths import state_dir


class PhysicalInputBusy(RuntimeError):
    """Raised when another process owns the physical-input lease."""


class InputActivityDetected(RuntimeError):
    """Raised when user input changes during the quiet window."""


class CoordinatesOffScreen(RuntimeError):
    """Raised when a requested point lies on no display at all."""


_T = TypeVar("_T")
_Marker = tuple[int | None, int | None, int | None]


def _pid_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        still_active = 259
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER is how OpenProcess reports a PID
                # that does not exist. Access-denied and unknown failures do
                # not prove death, so they must not authorize lock stealing.
                return kernel32.GetLastError() != 87
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _record_token(record: dict[str, Any] | None) -> str | None:
    if record is None:
        return None
    token = record.get("owner_token")
    return token if isinstance(token, str) and token else None


def _lock_guard_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise PhysicalInputBusy("physical-input arbitration is busy") from None


def _unlock_guard_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # Closing the fd below also releases an advisory lock owned by it.
        pass


class _ArbitrationGuard:
    """Non-blocking OS lock used for metadata changes and live ownership."""

    def __init__(self, lease_path: Path) -> None:
        self.path = Path(f"{lease_path}.guard")
        self._fd: int | None = None

    def __enter__(self) -> _ArbitrationGuard:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # Windows byte-range locking needs a persistent byte to lock.
            if os.fstat(fd).st_size < 1:
                os.ftruncate(fd, 1)
            _lock_guard_fd(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                _unlock_guard_fd(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _unlink_if_token(path: Path, owner_token: str) -> bool:
    """Unlink a matching record; caller must hold the sibling guard."""
    record = _read_record(path)
    if _record_token(record) != owner_token:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> None:
    """Unlink the created inode; caller must hold the sibling guard."""
    try:
        current = path.stat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except OSError:
        pass


def _record_is_stale(record: dict[str, Any], now: float) -> bool:
    expires_at = record.get("expires_at")
    expired = (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and math.isfinite(expires_at)
        and expires_at <= now
    )
    pid = record.get("pid")
    owner_dead = (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and not _pid_alive(pid)
    )
    return expired or owner_dead


class PhysicalInputLease:
    """A non-queued, process-safe lease represented by one JSON lock file."""

    def __init__(
        self,
        *,
        path: str | os.PathLike[str],
        ttl_seconds: float = 30.0,
        action_summary: str = "physical input",
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive finite number")
        if not isinstance(action_summary, str) or not action_summary:
            raise ValueError("action_summary must be a non-empty string")
        self.path = Path(path)
        self.ttl_seconds = float(ttl_seconds)
        self.action_summary = action_summary
        self.owner_token = secrets.token_hex(16)
        self._acquired = False
        self._guard: _ArbitrationGuard | None = None

    def _create(self) -> None:
        created_at = time.time()
        record = {
            "owner_token": self.owner_token,
            "pid": os.getpid(),
            "action_summary": self.action_summary,
            "created_at": created_at,
            "expires_at": created_at + self.ttl_seconds,
        }
        payload = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            opened = os.fstat(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            # _create always runs under the arbitration guard, so no compliant
            # contender can replace the empty pathname before this cleanup.
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        identity = (opened.st_dev, opened.st_ino)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("could not write physical-input lease")
                offset += written
            os.fsync(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            _unlink_if_identity(self.path, identity)
            raise
        else:
            try:
                os.close(fd)
            except BaseException:
                _unlink_if_identity(self.path, identity)
                raise

    def _recover_stale(self) -> bool:
        record = _read_record(self.path)
        token = _record_token(record)
        if record is None or token is None or not _record_is_stale(record, time.time()):
            return False
        return _unlink_if_token(self.path, token)

    def __enter__(self) -> PhysicalInputLease:
        if self._acquired:
            raise RuntimeError("physical-input lease is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        guard = _ArbitrationGuard(self.path)
        guard.__enter__()
        try:
            try:
                self._create()
            except FileExistsError:
                if not self._recover_stale():
                    raise PhysicalInputBusy(f"physical input is busy: {self.path}") from None
                try:
                    self._create()
                except FileExistsError:
                    raise PhysicalInputBusy(f"physical input is busy: {self.path}") from None
        except BaseException:
            guard.__exit__(None, None, None)
            raise
        self._guard = guard
        self._acquired = True
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._acquired:
            guard = self._guard
            try:
                _unlink_if_token(self.path, self.owner_token)
            except OSError:
                # Leaving the tokenized record is safe. Once this guard is
                # released, expiry/dead-PID recovery can reclaim it.
                pass
            finally:
                self._guard = None
                self._acquired = False
                if guard is not None:
                    guard.__exit__(exc_type, exc_value, traceback)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]


def _windows_pointer_position() -> tuple[int | None, int | None]:
    point = _Point()
    try:
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return int(point.x), int(point.y)
    except (AttributeError, OSError):
        pass
    return None, None


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _macos_pointer_position() -> tuple[int | None, int | None]:
    library_name = ctypes.util.find_library("ApplicationServices")
    if not library_name:
        return None, None
    event = None
    try:
        application_services = ctypes.CDLL(library_name)
        application_services.CGEventCreate.argtypes = [ctypes.c_void_p]
        application_services.CGEventCreate.restype = ctypes.c_void_p
        application_services.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        application_services.CGEventGetLocation.restype = _CGPoint
        application_services.CFRelease.argtypes = [ctypes.c_void_p]
        event = application_services.CGEventCreate(None)
        if not event:
            return None, None
        point = application_services.CGEventGetLocation(event)
        return int(point.x), int(point.y)
    except (AttributeError, OSError):
        return None, None
    finally:
        if event:
            application_services.CFRelease(event)


def _x11_pointer_position() -> tuple[int | None, int | None]:
    library_name = ctypes.util.find_library("X11")
    if not library_name:
        return None, None
    display = None
    try:
        x11 = ctypes.CDLL(library_name)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(None)
        if not display:
            return None, None
        root = x11.XDefaultRootWindow(display)
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        window_x = ctypes.c_int()
        window_y = ctypes.c_int()
        mask = ctypes.c_uint()
        available = x11.XQueryPointer(
            display,
            root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(window_x),
            ctypes.byref(window_y),
            ctypes.byref(mask),
        )
        if available:
            return int(root_x.value), int(root_y.value)
        return None, None
    except (AttributeError, OSError):
        return None, None
    finally:
        if display:
            x11.XCloseDisplay(display)


def _pointer_position() -> tuple[int | None, int | None]:
    if sys.platform == "win32":
        return _windows_pointer_position()
    if sys.platform == "darwin":
        return _macos_pointer_position()
    return _x11_pointer_position()


def last_input_marker() -> _Marker:
    """Return the available OS input timestamp and current pointer position."""
    last_input_time: int | None = None
    if sys.platform == "win32":
        info = _LastInputInfo(cbSize=ctypes.sizeof(_LastInputInfo))
        try:
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                last_input_time = int(info.dwTime)
        except (AttributeError, OSError):
            pass
    pointer_x, pointer_y = _pointer_position()
    return last_input_time, pointer_x, pointer_y


# The names are part of a tool result, so they are the caller's vocabulary for
# what this machine could actually be watched for. Order matches _Marker.
QUIET_MARKER_NAMES = ("os_last_input_time", "pointer_x", "pointer_y")


def wait_for_quiet(quiet_seconds: float = 0.75) -> dict[str, Any]:
    """Raise if any marker available in both samples changes in the window.

    Returns what the gate was able to observe, which is not a detail: only
    Windows exposes `os_last_input_time`, and the pointer probe answers
    `(None, None)` on Wayland, in a headless container, and on macOS without the
    accessibility permission. With no marker available in both samples there is
    nothing to compare, so the window elapses and the check passes no matter what
    the human at the keyboard is doing. Refusing instead would take physical
    input away from every such machine, where it otherwise works; the honest
    move is the one `_activate()` already makes for `on_screen` -- do the thing
    and report what is actually known. `enforced` is False for exactly that case
    and rides along in the tool result, because a caller cannot tell the
    difference from the outside.
    """
    if (
        isinstance(quiet_seconds, bool)
        or not isinstance(quiet_seconds, (int, float))
        or not math.isfinite(quiet_seconds)
        or quiet_seconds < 0
    ):
        raise ValueError("quiet_seconds must be a non-negative finite number")
    before = last_input_marker()
    time.sleep(quiet_seconds)
    after = last_input_marker()
    comparable = [
        (name, old, new)
        for name, old, new in zip(QUIET_MARKER_NAMES, before, after)
        if old is not None and new is not None
    ]
    if any(old != new for _name, old, new in comparable):
        raise InputActivityDetected("physical input changed during the quiet window")
    observed = [name for name, _old, _new in comparable]
    report: dict[str, Any] = {
        "quiet_seconds": float(quiet_seconds),
        "observed": observed,
        "enforced": bool(observed),
    }
    if not observed:
        # Said in full rather than as a flag: this is the one line that stops a
        # pass from reading as "the human was idle".
        report["note"] = (
            "this machine exposes no OS input signal that BTAP can sample, so the "
            "quiet window elapsed without being able to detect concurrent human "
            "input; treat a pass as unverified rather than as an idle machine"
        )
    return report


def _process_is_dpi_aware() -> bool | None:
    """Whether Windows is still virtualizing this process's screen metrics.

    None means the question could not be answered, which is not the same as
    False: `GetProcessDpiAwareness` is Windows 8.1+, and refusing to guess is
    what keeps `_win32_virtual_screen` from reporting a scaled rectangle as if
    it were the real one.
    """
    try:
        level = ctypes.c_int(-1)
        if ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(level)) == 0:
            return level.value != 0
    except (AttributeError, OSError):
        pass
    try:
        return bool(ctypes.windll.user32.IsProcessDPIAware())
    except (AttributeError, OSError):
        return None


def _win32_virtual_screen() -> dict[str, int] | None:
    """Virtual-desktop rectangle from the Win32 metrics mss itself reads.

    Only correct once the process is DPI-aware, and silently wrong before that:
    measured on a 1920x1080 panel at 125% scaling, a DPI-unaware process reads
    1536x864 here, so every x from 1537 to 1919 would look off-screen and a
    legitimate click would be refused. So this declines instead of answering --
    an unreadable bound is a reported gap, while a wrong bound is a refusal the
    caller cannot argue with.

    Nothing here sets the awareness level. pyautogui sets it to SYSTEM_AWARE
    while importing and mss sets it to PER_MONITOR while constructing, whichever
    runs first; a third setter in the validation path would decide that for a
    process whose input half has not chosen yet.
    """
    if _process_is_dpi_aware() is not True:
        return None
    sm_x_virtualscreen, sm_y_virtualscreen = 76, 77
    sm_cx_virtualscreen, sm_cy_virtualscreen = 78, 79
    try:
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(sm_cx_virtualscreen))
        height = int(user32.GetSystemMetrics(sm_cy_virtualscreen))
        if width <= 0 or height <= 0:
            return None
        return {
            "left": int(user32.GetSystemMetrics(sm_x_virtualscreen)),
            "top": int(user32.GetSystemMetrics(sm_y_virtualscreen)),
            "width": width,
            "height": height,
        }
    except (AttributeError, OSError):
        return None


def _mss_virtual_screen() -> dict[str, int] | None:
    """Virtual-desktop rectangle from mss, which answers on all three platforms.

    Index 0 is the bounding rectangle over every display; 1..N are individual
    monitors. This is the same read `capture_desktop_screenshot` reports as
    `width`/`height`/`left`/`top`, deliberately: a refusal computed from one
    rectangle and a screenshot framed by another would tell a caller to click a
    point its own picture shows.

    Unlike the Win32 fallback this needs no cooperation from the caller: mss
    makes itself DPI-aware inside `mss.mss()` before reading, so it answers
    1920x1080 on a 125% display even when the calling process is still
    unaware -- measured. That is why it is tried first.
    """
    try:
        import mss
    except Exception:
        # mss is part of the optional desktop extra, and binds to the display
        # while importing on some platforms. Either way it is a missing
        # capability, not an error to report.
        return None
    try:
        with mss.mss() as sct:
            if not sct.monitors:
                return None
            monitor = sct.monitors[0]
            width = int(monitor.get("width", 0))
            height = int(monitor.get("height", 0))
            if width <= 0 or height <= 0:
                return None
            return {
                "left": int(monitor.get("left", 0)),
                "top": int(monitor.get("top", 0)),
                "width": width,
                "height": height,
            }
    except Exception:
        return None


def screen_bounds() -> dict[str, Any] | None:
    """Return the virtual-desktop rectangle, or None when it cannot be read.

    mss first because it covers every display on every platform and needs no
    particular load order; the Win32 metrics are the fallback so a Windows
    install carrying pyautogui without mss still gets a real bound instead of an
    unchecked pass. The two agree where both answer (measured: 1920x1080 from
    each, same origin).
    """
    for source, probe in (
        ("mss_virtual_desktop", _mss_virtual_screen),
        ("win32_virtual_screen", _win32_virtual_screen),
    ):
        rect = probe()
        if rect is not None:
            return {**rect, "source": source}
    return None


def check_screen_bounds(
    points: Sequence[tuple[Any, Any]],
    *,
    bounds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refuse points that are on no display, and report what could be checked.

    This is the one gate here that refuses rather than reports, and the
    difference is that the failure is provable from the request alone: a point
    outside every display cannot be the point the caller meant. `SetCursorPos`
    does not fail on one -- measured, it **clamps**: (2400, 1300) on a
    1920x1080 desktop moved the cursor to (1919, 1079) and returned success,
    which is the Windows bottom-right hot corner, so the observable result of a
    typo is not a missed click but every window minimised. Substituting the
    nearest edge for a named target is the substitution AGENTS.md section 3
    forbids, and pyautogui reports nothing about it.

    Unknown bounds are a different fact from an out-of-range point, and follow
    `wait_for_quiet` instead: the window elapses, `enforced` is False, and the
    action proceeds. Refusing there would take physical input away from every
    machine whose display geometry cannot be read but whose input works.

    Nothing here loads or configures the input backend, so this can run before
    activation without reordering any desktop side effect: the mss probe makes
    itself DPI-aware internally and the Win32 fallback declines rather than
    answer from a virtualized metric.

    `pyautogui.onScreen()` exists and is deliberately not used: its own
    docstring says it does not work for secondary screens, so it would refuse
    legitimate coordinates on the multi-monitor setups this check most needs to
    be right about.
    """
    rect = screen_bounds() if bounds is None else bounds
    if rect is not None:
        # A rectangle without a positive extent describes no display, so it is
        # the same fact as an unreadable probe -- not a rectangle that every
        # point is outside of. Getting this backwards would refuse everything on
        # a machine whose geometry merely came back partial.
        try:
            usable = int(rect.get("width", 0)) > 0 and int(rect.get("height", 0)) > 0
        except (TypeError, ValueError):
            usable = False
        if not usable:
            rect = None
    checked: list[list[int]] = []
    offenders: list[list[int]] = []
    for point in points:
        try:
            x, y = point
        except (TypeError, ValueError):
            continue
        if isinstance(x, bool) or isinstance(y, bool):
            continue
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        pair = [int(x), int(y)]
        checked.append(pair)
        if rect is None:
            continue
        left = int(rect.get("left", 0))
        top = int(rect.get("top", 0))
        # Exclusive edges: a 1920-wide desktop at x=0 ends at 1919. Comparing
        # against the width itself is off by one in the direction that lets the
        # clamp through, which is the whole point of the check.
        if not (left <= pair[0] < left + int(rect.get("width", 0))):
            offenders.append(pair)
        elif not (top <= pair[1] < top + int(rect.get("height", 0))):
            offenders.append(pair)
    report: dict[str, Any] = {
        "bounds": rect,
        "checked": checked,
        "enforced": bool(rect is not None and checked),
    }
    if offenders:
        listed = ", ".join(f"({x}, {y})" for x, y in offenders)
        raise CoordinatesOffScreen(
            f"{listed} is on no display; nothing was dispatched. The virtual desktop is "
            f"{int(rect.get('width', 0))}x{int(rect.get('height', 0))} at "
            f"({int(rect.get('left', 0))}, {int(rect.get('top', 0))}). The OS clamps an "
            "out-of-range pointer move to the nearest edge and reports success, so this "
            "would have acted on a screen corner instead of the requested point. Read the "
            "real geometry from pointer_info or capture_desktop_screenshot."
        )
    if rect is None and checked:
        # Said in full, like the quiet gate's: this is the line that stops a
        # pass from reading as "the coordinates were checked and are on screen".
        report["note"] = (
            "this machine's display geometry could not be read, so the coordinates were "
            "accepted without being compared against any screen; an out-of-range point "
            "will be clamped to a screen edge by the OS without reporting an error"
        )
    return report


def _default_lock_path() -> Path:
    return state_dir() / "physical-input.lock"


def run_physical_action(
    action_summary: str,
    action: Callable[[], _T],
    *,
    lock_path: str | os.PathLike[str] | None = None,
    quiet_seconds: float = 0.75,
    ttl_seconds: float = 30.0,
) -> _T:
    """Run one action closure after acquiring the lease and quiet gate."""
    path = _default_lock_path() if lock_path is None else Path(lock_path)
    with PhysicalInputLease(path=path, ttl_seconds=ttl_seconds, action_summary=action_summary):
        quiet = wait_for_quiet(quiet_seconds)
        result = action()
        # Attached the way server.py already attaches `activated`, and with
        # setdefault so an action that reported its own gate keeps it. Non-dict
        # results are left alone: there is nowhere to put it and no caller of
        # this module returns one.
        if isinstance(result, dict):
            result.setdefault("input_quiet", quiet)
        return result
