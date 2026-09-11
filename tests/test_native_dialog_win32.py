"""Synthetic Win32 ABI tests. Every DLL and registry read is replaced."""

import ctypes
import importlib.metadata
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from browsertap_mcp import native_dialog as N


class Function:
    def __init__(self, value=0):
        self.value = value
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.value(*args) if callable(self.value) else self.value


class Library:
    def __init__(self):
        self.functions = {}

    def __getattr__(self, name):
        return self.functions.setdefault(name, Function())


@pytest.fixture
def win32():
    user, kernel, dwm = Library(), Library(), Library()
    adapter = N._WindowsAdapter(user32=user, kernel32=kernel, dwmapi=dwm)
    return adapter, user, kernel, dwm


def test_win32_loader_is_lazy_and_sets_last_error_on_each_dll(monkeypatch):
    libraries = {name: Library() for name in ("user32", "kernel32", "dwmapi")}
    calls = []

    def load(name, **kwargs):
        calls.append((name, kwargs))
        return libraries[name]

    monkeypatch.setattr(N.ctypes, "WinDLL", load, raising=False)
    N._WindowsAdapter()

    assert calls == [(name, {"use_last_error": True}) for name in libraries]
    assert all(not function.calls for library in libraries.values() for function in library.functions.values())


def test_win32_binding_preserves_pointer_widths_and_fixed_windows_integer_sizes(win32):
    _, user, kernel, dwm = win32
    assert user.GetForegroundWindow.restype is ctypes.c_void_p
    assert user.GetPropW.restype is ctypes.c_void_p
    assert user.WindowFromPoint.restype is ctypes.c_void_p
    assert kernel.OpenProcess.restype is ctypes.c_void_p
    assert user.SendMessageTimeoutW.argtypes[2] is ctypes.c_size_t
    assert user.SendMessageTimeoutW.argtypes[3] is ctypes.c_ssize_t
    assert user.SendMessageTimeoutW.restype is ctypes.c_ssize_t
    assert ctypes.sizeof(user.GetWindowThreadProcessId.restype) == 4
    assert ctypes.sizeof(N._Point) == 8
    assert ctypes.sizeof(N._Rect) == 16
    assert ctypes.sizeof(N._FileTime) == 8
    assert dwm.DwmGetWindowAttribute.restype is ctypes.c_int32


def test_win32_dpi_context_restores_thread_state_even_when_probe_raises(win32):
    adapter, user, _, _ = win32
    user.SetThreadDpiAwarenessContext.value = 1234
    with pytest.raises(ValueError, match="synthetic failure"):
        with adapter.dpi_context():
            raise ValueError("synthetic failure")
    requested, restored = user.SetThreadDpiAwarenessContext.calls
    assert requested[0].value == ctypes.c_void_p(-4).value
    assert restored == (1234,)


def test_win32_missing_dpi_support_refuses_before_probing(win32):
    adapter, user, _, _ = win32
    with pytest.raises(OSError):
        with adapter.dpi_context():
            pytest.fail("unknown coordinate model must be refused")
    assert len(user.SetThreadDpiAwarenessContext.calls) == 1


def test_win32_dpi_restore_failure_is_not_hidden(win32):
    adapter, user, _, _ = win32
    values = iter([1234, 0])
    user.SetThreadDpiAwarenessContext.value = lambda context: next(values)
    with pytest.raises(OSError, match="restored"):
        with adapter.dpi_context():
            pass


def _window_fixture(user):
    user.IsWindow.value = 1

    def identity(hwnd, pid):
        ctypes.cast(pid, ctypes.POINTER(ctypes.c_uint32)).contents.value = 10
        return 11

    def class_name(hwnd, buffer, capacity):
        buffer.value = "#32770"
        return len(buffer.value)

    def geometry(hwnd, target):
        rect = ctypes.cast(target, ctypes.POINTER(N._Rect)).contents
        rect.left, rect.top, rect.right, rect.bottom = 100, 200, 900, 800
        return 1

    user.GetWindowThreadProcessId.value = identity
    user.GetClassNameW.value = class_name
    user.GetWindowRect.value = geometry
    user.GetParent.value = 0x10000000002
    user.GetWindow.value = 0x10000000003
    user.GetDlgCtrlID.value = 2
    style_name = "GetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "GetWindowLongW"
    getattr(user, style_name).value = 0x96000000
    user.IsWindowVisible.value = 1
    user.IsWindowEnabled.value = 1


def test_win32_window_snapshot_uses_os_class_owner_pid_thread_and_rect(win32):
    adapter, user, _, _ = win32
    _window_fixture(user)
    hwnd = 0x10000000001
    result = adapter.read_window(hwnd)
    assert result.hwnd == hwnd
    assert result.pid == 10 and result.tid == 11
    assert result.parent == 0x10000000002
    assert result.owner == 0x10000000003
    assert result.class_name == "#32770"
    assert result.rect == (100, 200, 900, 800)
    assert result.visible and result.enabled and not result.minimized
    assert user.GetWindow.calls == [(hwnd, 4)]
    assert len(user.GetWindowThreadProcessId.calls) == 2


@pytest.mark.parametrize("failed", ["IsWindow", "GetClassNameW", "GetWindowRect", "GetWindowThreadProcessId"])
def test_win32_window_snapshot_refuses_unreadable_identity(win32, failed):
    adapter, user, _, _ = win32
    _window_fixture(user)
    getattr(user, failed).value = 0
    with pytest.raises(OSError):
        adapter.read_window(100)


def test_win32_window_snapshot_refuses_pid_change_mid_read(win32):
    adapter, user, _, _ = win32
    _window_fixture(user)
    identities = iter([10, 99])

    def identity(hwnd, pid):
        ctypes.cast(pid, ctypes.POINTER(ctypes.c_uint32)).contents.value = next(identities)
        return 11

    user.GetWindowThreadProcessId.value = identity
    with pytest.raises(OSError, match="changed"):
        adapter.read_window(100)


def _process_fixture(kernel):
    kernel.OpenProcess.value = 0x10000000004

    def image(handle, flags, buffer, capacity):
        assert ctypes.cast(capacity, ctypes.POINTER(ctypes.c_uint32)).contents.value == 32768
        buffer.value = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        return 1

    def times(handle, created, exited, kernel_time, user_time):
        value = ctypes.cast(created, ctypes.POINTER(N._FileTime)).contents
        value.low, value.high = 0xABCDEF12, 0x00112233
        return 1

    def live(handle, exit_code):
        ctypes.cast(exit_code, ctypes.POINTER(ctypes.c_uint32)).contents.value = 259
        return 1

    kernel.QueryFullProcessImageNameW.value = image
    kernel.GetProcessTimes.value = times
    kernel.GetExitCodeProcess.value = live


def test_win32_process_identity_uses_one_handle_and_closes_it(win32):
    adapter, _, kernel, _ = win32
    _process_fixture(kernel)
    process = adapter.read_process(10)
    assert process.pid == 10
    assert process.created == 0x00112233ABCDEF12
    assert process.image == r"c:\program files\google\chrome\application\chrome.exe"
    assert kernel.OpenProcess.calls == [(0x1000, False, 10)]
    assert kernel.CloseHandle.calls == [(0x10000000004,)]
    assert all(
        call[0] == 0x10000000004
        for function in (kernel.QueryFullProcessImageNameW, kernel.GetProcessTimes, kernel.GetExitCodeProcess)
        for call in function.calls
    )


@pytest.mark.parametrize("failed", ["QueryFullProcessImageNameW", "GetProcessTimes", "GetExitCodeProcess"])
def test_win32_failed_process_reads_still_close_handle(win32, failed):
    adapter, _, kernel, _ = win32
    _process_fixture(kernel)
    getattr(kernel, failed).value = 0
    with pytest.raises(OSError):
        adapter.read_process(10)
    assert kernel.CloseHandle.calls == [(0x10000000004,)]


def test_win32_open_process_failure_does_not_close_an_invalid_handle(win32):
    adapter, _, kernel, _ = win32
    with pytest.raises(OSError):
        adapter.read_process(10)
    assert not kernel.CloseHandle.calls


def test_win32_exited_process_is_not_identity_evidence(win32):
    adapter, _, kernel, _ = win32
    _process_fixture(kernel)
    kernel.GetExitCodeProcess.value = 1  # Output remains zero: process exited.
    with pytest.raises(OSError, match="not live"):
        adapter.read_process(10)
    assert kernel.CloseHandle.calls == [(0x10000000004,)]


def test_win32_cloaking_preserves_unknown_instead_of_assuming_visible(win32):
    adapter, _, _, dwm = win32
    assert adapter.is_cloaked(100) is False
    dwm.DwmGetWindowAttribute.value = -1
    assert adapter.is_cloaked(100) is None

    def cloaked(hwnd, attribute, value, size):
        assert attribute == 14 and size == 4
        ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1
        return 0

    dwm.DwmGetWindowAttribute.value = cloaked
    assert adapter.is_cloaked(100) is True


def test_win32_gui_focus_preserves_full_handles_and_checks_structure_size(win32):
    adapter, user, _, _ = win32

    def read(tid, pointer):
        state = ctypes.cast(pointer, ctypes.POINTER(N._GuiThreadInfo)).contents
        assert state.cbSize == ctypes.sizeof(N._GuiThreadInfo)
        state.hwndActive, state.hwndFocus = 0x10000000001, 0x10000000002
        return 1

    user.GetGUIThreadInfo.value = read
    assert adapter.gui_thread_state(11) == (0x10000000001, 0x10000000002)
    user.GetGUIThreadInfo.value = 0
    with pytest.raises(OSError):
        adapter.gui_thread_state(11)


def test_win32_child_enumeration_is_bounded(win32):
    adapter, user, _, _ = win32
    counts = []

    def enumerate_children(hwnd, callback, data):
        for index in range(500):
            counts.append(index)
            if not callback(1000 + index, data):
                break
        return 1

    user.EnumChildWindows.value = enumerate_children
    with pytest.raises(N._Refused, match="inspection limit"):
        adapter.descendant_handles(100)
    assert len(counts) == 257


def test_win32_child_enumeration_and_hit_tests_use_screen_points(win32):
    adapter, user, _, _ = win32

    def enumerate_children(hwnd, callback, data):
        callback(101, data)
        callback(102, data)
        return 1

    user.EnumChildWindows.value = enumerate_children
    user.MonitorFromPoint.value = 1
    user.WindowFromPoint.value = 102
    user.IsChild.value = 1
    user.GetForegroundWindow.value = 100
    user.IsWindow.value = 1
    assert adapter.descendant_handles(100) == (101, 102)
    assert adapter.point_on_monitor((-900, 300)) is True
    assert adapter.window_at_point((-900, 300)) == 102
    assert user.MonitorFromPoint.calls[0][0].x == -900
    assert user.MonitorFromPoint.calls[0][1] == 0
    assert adapter.is_child(100, 102) is True
    assert adapter.is_child(100, 0) is False
    assert user.IsChild.calls == [(100, 102)]
    assert adapter.foreground_window() == 100
    assert adapter.window_exists(100) is True


def test_win32_z_order_detects_disabled_visible_overlay(win32):
    adapter, user, _, _ = win32
    user.GetTopWindow.value = 999
    user.IsWindowVisible.value = 1
    user.IsWindowEnabled.value = 0

    def geometry(hwnd, target):
        rect = ctypes.cast(target, ctypes.POINTER(N._Rect)).contents
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 900, 900
        return 1

    user.GetWindowRect.value = geometry
    assert adapter.top_level_at_point((600, 550)) == 999
    assert not user.IsWindowEnabled.calls  # Disabled does not mean visually absent.


def test_win32_z_order_skips_only_proven_non_occluding_windows(win32):
    adapter, user, _, dwm = win32
    user.GetTopWindow.value = 999
    user.GetWindow.value = lambda hwnd, relation: 100 if hwnd == 999 else 0
    user.IsWindowVisible.value = 1

    def geometry(hwnd, target):
        rect = ctypes.cast(target, ctypes.POINTER(N._Rect)).contents
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 900, 900
        return 1

    def visibility(hwnd, attribute, target, size):
        ctypes.cast(target, ctypes.POINTER(ctypes.c_uint32)).contents.value = int(hwnd == 999)
        return 0

    user.GetWindowRect.value = geometry
    dwm.DwmGetWindowAttribute.value = visibility
    assert adapter.top_level_at_point((600, 550)) == 100
    assert user.GetWindow.calls == [(999, 2)]


@pytest.mark.parametrize("unknown", ["geometry", "cloaking", "cycle"])
def test_win32_z_order_refuses_unknown_overlay_state(win32, unknown):
    adapter, user, _, dwm = win32
    user.GetTopWindow.value = 999
    user.IsWindowVisible.value = 1

    def geometry(hwnd, target):
        rect = ctypes.cast(target, ctypes.POINTER(N._Rect)).contents
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 900, 900
        return 1

    user.GetWindowRect.value = geometry
    if unknown == "geometry":
        user.GetWindowRect.value = 0
    elif unknown == "cloaking":
        dwm.DwmGetWindowAttribute.value = -1
    else:
        user.IsWindowVisible.value = 0
        user.GetWindow.value = 999
    with pytest.raises(OSError):
        adapter.top_level_at_point((600, 550))


def test_win32_held_input_checks_only_current_down_bit_with_bounded_scan(win32):
    adapter, user, _, _ = win32
    user.GetAsyncKeyState.value = 1  # The historical "pressed since last call" bit.
    assert adapter.input_is_pressed() is False
    assert len(user.GetAsyncKeyState.calls) == 255
    user.GetAsyncKeyState.calls.clear()
    user.GetAsyncKeyState.value = lambda key: -32768 if key == 2 else 0
    assert adapter.input_is_pressed() is True
    assert user.GetAsyncKeyState.calls == [(1,), (2,)]


def test_win32_marker_removal_compares_value_and_checks_cleanup(win32):
    adapter, user, _, _ = win32
    user.SetPropW.value = 1
    value = {"current": 77}
    user.GetPropW.value = lambda hwnd, name: value["current"]

    def remove(hwnd, name):
        value["current"] = 0
        return 77

    user.RemovePropW.value = remove
    adapter.set_marker(100, "synthetic.marker", 77)
    adapter.remove_marker(100, "synthetic.marker", 99)
    assert not user.RemovePropW.calls
    adapter.remove_marker(100, "synthetic.marker", 77)
    assert user.RemovePropW.calls == [(100, "synthetic.marker")]
    assert adapter.marker(100, "synthetic.marker") == 0


def test_win32_marker_failures_are_not_reported_as_verified(win32):
    adapter, user, _, _ = win32
    with pytest.raises(OSError):
        adapter.set_marker(100, "synthetic.marker", 77)
    user.GetPropW.value = 77
    with pytest.raises(OSError, match="cleanup"):
        adapter.remove_marker(100, "synthetic.marker", 77)


def test_win32_cancel_is_one_bounded_button_message_without_global_input(win32):
    adapter, user, _, _ = win32
    user.SendMessageTimeoutW.value = 1
    adapter.send_cancel(0x10000000002, 750)
    calls = user.SendMessageTimeoutW.calls
    assert len(calls) == 1
    assert calls[0][:6] == (0x10000000002, 0xF5, 0, 0, 0x03, 750)
    assert not user.GetForegroundWindow.calls
    assert not user.SetThreadDpiAwarenessContext.calls
    assert all(
        forbidden not in user.functions
        for forbidden in ("SendInput", "SetForegroundWindow", "SetCursorPos", "keybd_event", "mouse_event")
    )


@pytest.mark.parametrize(("error", "exception"), [(0, TimeoutError), (1460, TimeoutError), (5, OSError)])
def test_win32_cancel_unknown_delivery_is_reported_once(win32, monkeypatch, error, exception):
    adapter, user, _, _ = win32
    monkeypatch.setattr(N, "_last_error", lambda: error)
    with pytest.raises(exception):
        adapter.send_cancel(102, 750)
    assert len(user.SendMessageTimeoutW.calls) == 1


def test_app_paths_parser_uses_only_registered_absolute_browser_executables(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
        KEY_WOW64_32KEY=0x200, KEY_WOW64_64KEY=0x100, KEY_READ=0x19,
        REG_SZ=1, REG_EXPAND_SZ=2,
    )

    @contextmanager
    def open_key(hive, name, reserved, access):
        calls.append((hive, name, access))
        yield hive, name, access

    def value(key, name):
        hive, path, access = key
        if path.endswith("msedge.exe"):
            return r"C:\Browser\chrome.exe", 1  # Wrong executable for this key.
        if hive == 1 and access & fake.KEY_WOW64_32KEY:
            return '"C:\\Browser\\chrome.exe"', 1
        if hive == 2 and access & fake.KEY_WOW64_64KEY:
            return r"relative\chrome.exe", 1
        return r"C:\Browser\chrome.exe --launch", 1

    fake.OpenKey = open_key
    fake.QueryValueEx = value
    monkeypatch.setitem(sys.modules, "winreg", fake)

    result = N._read_app_paths()

    assert result == {"chrome": (r"c:\browser\chrome.exe",), "edge": ()}
    assert len(calls) == 8


def test_app_paths_unreadable_registry_has_no_basename_fallback(monkeypatch):
    def denied(*args):
        raise PermissionError("synthetic registry denied")

    fake = SimpleNamespace(
        HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
        KEY_WOW64_32KEY=0x200, KEY_WOW64_64KEY=0x100, KEY_READ=0x19,
        REG_SZ=1, REG_EXPAND_SZ=2, OpenKey=denied,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert N._read_app_paths() == {"chrome": (), "edge": ()}


def test_adapter_app_paths_delegates_to_the_registry_parser(win32, monkeypatch):
    adapter, _, _, _ = win32
    expected = {"chrome": (r"c:\browser\chrome.exe",)}
    monkeypatch.setattr(N, "_read_app_paths", lambda: expected)
    assert adapter.registered_browser_paths() == expected


def test_desktop_extra_uses_distribution_metadata_without_importing_input_backends(monkeypatch):
    calls = []

    def version(name):
        calls.append(name)
        if name == "mss":
            raise importlib.metadata.PackageNotFoundError(name)
        return "1.2.3"

    monkeypatch.setattr(N.importlib.metadata, "version", version)
    result = N._desktop_extra()
    assert result == {
        "available": False, "missing": ["mss"],
        "versions": {"PyAutoGUI": "1.2.3", "Pillow": "1.2.3"},
        "check": "distribution_metadata",
    }
    assert calls == ["PyAutoGUI", "mss", "Pillow"]
