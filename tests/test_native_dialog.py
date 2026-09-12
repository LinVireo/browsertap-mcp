"""Offline identity and dispatch tests; no native desktop API is called."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import anyio
import pytest

from browsertap_mcp import native_dialog as N

IMAGE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeTimer:
    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.cancelled = False
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function(*self.args)


class FakeWindows:
    def __init__(self):
        self.windows = {
            100: N._Window(100, 10, 11, "#32770", owner=200, rect=(100, 100, 700, 600)),
            200: N._Window(200, 10, 12, "Chrome_WidgetWin_1", rect=(0, 0, 900, 700)),
            102: N._Window(
                102, 10, 11, "Button", parent=100, control_id=2,
                style=0x50010000, rect=(590, 540, 680, 580),
            ),
            110: N._Window(110, 10, 11, "ComboBox", parent=100, control_id=0x47C),
            111: N._Window(111, 10, 11, "ComboBox", parent=100, control_id=0x470),
            112: N._Window(112, 10, 11, "SHELLDLL_DefView", parent=100),
        }
        self.processes = {10: N._Process(10, 123456789, IMAGE)}
        self.paths = {"chrome": (IMAGE,)}
        self.foreground = 100
        self.active = 100
        self.focus = 110
        self.cloaked = set()
        self.obscured = False
        self.overlay = False
        self.held_input = False
        self.on_monitor = True
        self.properties = {}
        self.calls = []
        self.send_behavior = "close"
        self.before_send = None
        self.after_mark = None

    @contextmanager
    def dpi_context(self):
        self.calls.append("dpi_enter")
        try:
            yield
        finally:
            self.calls.append("dpi_exit")

    def foreground_window(self):
        return self.foreground

    def read_window(self, hwnd):
        self.calls.append(("window", hwnd))
        if hwnd not in self.windows:
            raise OSError("window unavailable")
        return self.windows[hwnd]

    def read_process(self, pid):
        self.calls.append(("process", pid))
        return self.processes[pid]

    def registered_browser_paths(self):
        return self.paths

    def is_cloaked(self, hwnd):
        return hwnd in self.cloaked

    def gui_thread_state(self, tid):
        return self.active, self.focus

    def is_child(self, parent, child):
        seen = set()
        while child in self.windows and child not in seen:
            seen.add(child)
            child = self.windows[child].parent
            if child == parent:
                return True
        return False

    def descendant_handles(self, hwnd):
        return tuple(handle for handle in self.windows if self.is_child(hwnd, handle))

    def point_on_monitor(self, point):
        return self.on_monitor

    def window_at_point(self, point):
        return 999 if self.obscured else 102

    def top_level_at_point(self, point):
        return 999 if self.overlay else 100

    def input_is_pressed(self):
        return self.held_input

    def set_marker(self, hwnd, name, value):
        self.calls.append(("mark", hwnd, name))
        self.properties[hwnd, name] = value
        if self.after_mark:
            self.after_mark()

    def marker(self, hwnd, name):
        return self.properties.get((hwnd, name), 0)

    def remove_marker(self, hwnd, name, value):
        if self.marker(hwnd, name) == value:
            self.calls.append(("remove", hwnd, name))
            del self.properties[hwnd, name]

    def window_exists(self, hwnd):
        return hwnd in self.windows

    def send_cancel(self, hwnd, timeout_ms):
        self.calls.append(("send", hwnd, timeout_ms))
        if self.before_send:
            self.before_send()
        if self.send_behavior == "timeout":
            raise TimeoutError("bounded message timeout")
        if self.send_behavior == "error":
            raise OSError("bounded message failure")
        if self.send_behavior == "close":
            self.windows.pop(100)
            self.properties.clear()
        elif self.send_behavior == "marker_removed":
            self.properties.clear()
        elif self.send_behavior == "reuse":
            self.properties.clear()
            self.windows[100] = replace(self.windows[100], tid=51)


@pytest.fixture
def native(monkeypatch, tmp_path):
    os = FakeWindows()
    clock = FakeClock()
    monkeypatch.setattr(N.physical_input, "last_input_marker", lambda: (42, 10, 20))
    monkeypatch.setattr(
        N.physical_input, "wait_for_quiet",
        lambda seconds: {
            "quiet_seconds": seconds, "enforced": True,
            "observed": ["os_last_input_time", "pointer_x", "pointer_y"],
        },
    )
    manager = N.NativeDialogManager(
        adapter_factory=lambda: os, platform=lambda: "win32",
        extra_probe=lambda: {"available": True, "missing": []},
        clock=clock, sleep=clock.sleep, timer_factory=FakeTimer,
        lock_path=tmp_path / "physical-input.lock",
    )
    yield manager, os, clock
    manager.close()


def _inspect(manager):
    result = manager.inspect(desktop_opt_in=True)
    assert result["ok"], result
    return result["ticket"]


def _sends(os):
    return [call for call in os.calls if isinstance(call, tuple) and call[0] == "send"]


def _assert_refused(result, code):
    assert result["ok"] is False
    assert result["code"] == code
    assert result["dispatched"] is False
    assert result["delivery_state"] == "undelivered"
    assert "desktop" in result and "on_screen" in result and "input_quiet" in result


def _structured(result):
    return result if isinstance(result, dict) else result.structuredContent


def test_inspect_native_file_dialog_returns_lifetime_bound_ticket_without_input(native):
    manager, os, _ = native
    result = manager.inspect(desktop_opt_in=True)

    assert result["ok"] is True
    assert result["on_screen"] is True
    assert result["expires_in_seconds"] == 15.0
    assert result["input_quiet"]["checked"] is False
    assert result["input_quiet"]["enforced"] is False
    assert result["desktop"]["browser"] == "chrome"
    assert result["desktop"]["process_created"] == 123456789
    assert len(os.properties) == 1
    assert not _sends(os)
    assert IMAGE not in str(result)
    assert result["ticket"] not in str(result["desktop"])


def test_inspect_native_file_dialog_requires_explicit_opt_in_before_native_probe(native):
    manager, os, _ = native
    result = manager.inspect()
    _assert_refused(result, "desktop_opt_in_required")
    assert result["on_screen"] is None
    assert os.calls == []


def test_cancel_native_file_dialog_sends_one_cancel_and_observes_closed_window(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    result = manager.cancel(ticket, desktop_opt_in=True)

    assert result["ok"] is True
    assert result["status"] == "success" and result["cancelled"] is True
    assert result["desktop"]["closed_observed"] is True
    assert result["input_quiet"]["enforced"] is True
    assert result["dispatched"] is True
    assert result["retry_safe"] is False
    assert _sends(os) == [("send", 102, 750)]
    assert not os.properties
    assert not Path(manager._lock_path).exists()


def test_cancel_native_file_dialog_rejects_same_process_hwnd_reuse_without_marker(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    os.properties.clear()  # A new window may reuse every numeric HWND and PID.

    result = manager.cancel(ticket, desktop_opt_in=True)

    _assert_refused(result, "native_dialog_identity_changed")
    assert not _sends(os)
    _assert_refused(
        manager.cancel(ticket, desktop_opt_in=True), "native_dialog_ticket_invalid",
    )


def test_cancel_native_file_dialog_cleanup_releases_marker_and_lease_on_refusal(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    os.obscured = True
    result = manager.cancel(ticket, desktop_opt_in=True)

    _assert_refused(result, "native_dialog_obscured")
    assert result["on_screen"] is False
    assert not os.properties
    assert not Path(manager._lock_path).exists()
    assert not _sends(os)


def test_inspect_native_file_dialog_cleanup_removes_only_owned_marker_values(native):
    manager, os, _ = native
    _inspect(manager)
    key = next(iter(os.properties))
    os.properties[key] += 1
    replacement = os.properties[key]

    manager.close()

    assert os.properties[key] == replacement
    assert not [call for call in os.calls if isinstance(call, tuple) and call[0] == "remove"]


def test_inspect_native_file_dialog_cleanup_removes_its_marker_on_shutdown(native):
    manager, os, _ = native
    _inspect(manager)
    assert len(os.properties) == 1
    manager.close()
    assert not os.properties
    assert not manager._tickets
    assert not _sends(os)


@pytest.mark.parametrize("platform", ["linux", "darwin", "unknown"])
def test_unsupported_platform_never_opens_native_adapter(native, platform):
    manager, os, _ = native
    manager._platform = lambda: platform
    result = manager.inspect(desktop_opt_in=True)
    _assert_refused(result, "native_dialog_unsupported")
    assert result["desktop"]["platform"] == platform
    assert not os.calls


def test_missing_desktop_extra_never_opens_native_adapter(native):
    manager, os, _ = native
    manager._extra_probe = lambda: {"available": False, "missing": ["mss"]}
    result = manager.inspect(desktop_opt_in=True)
    _assert_refused(result, "desktop_extra_unavailable")
    assert result["desktop"]["extra"]["missing"] == ["mss"]
    assert not os.calls


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("not_dialog", "native_dialog_unsupported"),
        ("no_owner", "native_dialog_unverifiable"),
        ("owner_cycle", "native_dialog_unverifiable"),
        ("foreign_owner", "native_dialog_unverifiable"),
        ("unknown_browser_root", "native_dialog_unverifiable"),
        ("unregistered_image", "native_dialog_unverifiable"),
        ("wrong_original_image", "native_dialog_unverifiable"),
        ("no_shell_view", "native_dialog_unsupported"),
        ("no_filename", "native_dialog_unsupported"),
        ("no_filetype", "native_dialog_unsupported"),
        ("cancel_not_button", "native_dialog_unsupported"),
        ("cancel_not_pushbutton", "native_dialog_unsupported"),
        ("cancel_not_direct_child", "native_dialog_unsupported"),
        ("cancel_disabled", "native_dialog_unverifiable"),
        ("cancel_hidden", "native_dialog_unverifiable"),
        ("active_changed", "native_dialog_not_foreground"),
        ("focus_outside", "native_dialog_not_foreground"),
        ("dialog_hidden", "native_dialog_unverifiable"),
        ("dialog_minimized", "native_dialog_unverifiable"),
        ("dialog_cloaked", "native_dialog_unverifiable"),
        ("owner_minimized", "native_dialog_unverifiable"),
        ("obscured", "native_dialog_obscured"),
        ("off_monitor", "native_dialog_obscured"),
        ("no_foreground", "native_dialog_unverifiable"),
        ("dialog_disabled", "native_dialog_unverifiable"),
        ("foreign_shell_control", "native_dialog_unverifiable"),
        ("cancel_outside_dialog", "native_dialog_obscured"),
        ("duplicate_control_identity", "native_dialog_unsupported"),
    ],
)
def test_recognition_refuses_incomplete_os_evidence(native, change, code):
    manager, os, _ = native
    if change == "not_dialog":
        os.windows[100] = replace(os.windows[100], class_name="Chrome_WidgetWin_1")
    elif change == "no_owner":
        os.windows[100] = replace(os.windows[100], owner=0)
    elif change == "owner_cycle":
        os.windows[200] = replace(os.windows[200], owner=100)
    elif change == "foreign_owner":
        os.windows[200] = replace(os.windows[200], pid=99)
    elif change == "unknown_browser_root":
        os.windows[200] = replace(os.windows[200], class_name="Notepad")
    elif change == "unregistered_image":
        os.paths = {}
    elif change == "wrong_original_image":
        os.processes[10] = N._Process(10, 123456789, r"C:\Unrelated\chrome.exe")
    elif change == "no_shell_view":
        os.windows.pop(112)
    elif change == "no_filename":
        os.windows.pop(110)
        os.focus = 102
    elif change == "no_filetype":
        os.windows.pop(111)
    elif change == "cancel_not_button":
        os.windows[102] = replace(os.windows[102], class_name="Edit")
    elif change == "cancel_not_pushbutton":
        os.windows[102] = replace(os.windows[102], style=0x50010003)
    elif change == "cancel_not_direct_child":
        os.windows[102] = replace(os.windows[102], parent=112)
    elif change == "cancel_disabled":
        os.windows[102] = replace(os.windows[102], enabled=False)
    elif change == "cancel_hidden":
        os.windows[102] = replace(os.windows[102], visible=False)
    elif change == "active_changed":
        os.active = 200
    elif change == "focus_outside":
        os.focus = 200
    elif change == "dialog_hidden":
        os.windows[100] = replace(os.windows[100], visible=False)
    elif change == "dialog_minimized":
        os.windows[100] = replace(os.windows[100], minimized=True)
    elif change == "dialog_cloaked":
        os.cloaked.add(100)
    elif change == "owner_minimized":
        os.windows[200] = replace(os.windows[200], minimized=True)
    elif change == "obscured":
        os.obscured = True
    elif change == "off_monitor":
        os.on_monitor = False
    elif change == "no_foreground":
        os.foreground = 0
    elif change == "dialog_disabled":
        os.windows[100] = replace(os.windows[100], enabled=False)
    elif change == "foreign_shell_control":
        os.windows[112] = replace(os.windows[112], pid=99)
    elif change == "cancel_outside_dialog":
        os.windows[102] = replace(os.windows[102], rect=(10, 10, 80, 50))
    elif change == "duplicate_control_identity":
        os.descendant_handles = lambda hwnd: (102, 110, 111, 112, 112)

    _assert_refused(manager.inspect(desktop_opt_in=True), code)
    assert not os.properties
    assert not _sends(os)


@pytest.mark.parametrize("change", ["process", "dialog_thread", "owner", "cancel", "filename"])
def test_cancel_rechecks_frozen_identity_after_quiet_gate(native, change):
    manager, os, _ = native
    ticket = _inspect(manager)
    if change == "process":
        os.processes[10] = replace(os.processes[10], created=99999999)
    elif change == "dialog_thread":
        os.windows[100] = replace(os.windows[100], tid=99)
        os.windows[102] = replace(os.windows[102], tid=99)
    elif change == "owner":
        os.windows[201] = replace(os.windows.pop(200), hwnd=201)
        os.windows[100] = replace(os.windows[100], owner=201)
    elif change == "cancel":
        os.windows[103] = replace(os.windows.pop(102), hwnd=103)
        os.window_at_point = lambda point: 103
    elif change == "filename":
        os.windows[113] = replace(os.windows.pop(110), hwnd=113)
        os.focus = 113

    _assert_refused(
        manager.cancel(ticket, desktop_opt_in=True), "native_dialog_identity_changed",
    )
    assert not _sends(os)
    assert not os.properties


def test_inspection_rechecks_identity_after_installing_marker(native):
    manager, os, _ = native
    os.after_mark = lambda: os.processes.update({10: N._Process(10, 987654321, IMAGE)})
    _assert_refused(manager.inspect(desktop_opt_in=True), "native_dialog_identity_changed")
    assert not os.properties
    assert not _sends(os)


def test_inspection_refuses_when_marker_is_removed_during_either_validation(native):
    manager, os, _ = native
    os.after_mark = os.properties.clear
    _assert_refused(manager.inspect(desktop_opt_in=True), "native_dialog_marker_unavailable")
    assert not manager._tickets
    assert not os.properties

    os.after_mark = None
    original = os.marker
    reads = []

    def remove_after_snapshot(hwnd, name):
        reads.append(name)
        if len(reads) == 2:
            os.properties.clear()
        return original(hwnd, name)

    os.marker = remove_after_snapshot
    _assert_refused(manager.inspect(desktop_opt_in=True), "native_dialog_identity_changed")
    assert not manager._tickets
    assert not os.properties
    assert not _sends(os)


def test_inspection_timer_failure_cannot_publish_an_unbounded_ticket(native):
    manager, os, _ = native

    class BrokenTimer(FakeTimer):
        def start(self):
            raise RuntimeError("synthetic timer unavailable")

    manager._timer_factory = BrokenTimer
    result = manager.inspect(desktop_opt_in=True)
    _assert_refused(result, "native_dialog_unavailable")
    assert result["desktop"]["marker_cleanup_verified"] is True
    assert not manager._tickets
    assert not os.properties
    assert not _sends(os)


def test_inspection_foreground_change_during_snapshot_refuses_before_marking(native):
    manager, os, _ = native
    foregrounds = iter([100, 200])
    os.foreground_window = lambda: next(foregrounds)
    _assert_refused(manager.inspect(desktop_opt_in=True), "native_dialog_not_foreground")
    assert not os.properties
    assert not _sends(os)


def test_cancel_foreground_change_during_quiet_gate_does_not_activate(native, monkeypatch):
    manager, os, _ = native
    ticket = _inspect(manager)
    gate = N.physical_input.wait_for_quiet

    def change_foreground(seconds):
        os.foreground = 200
        return gate(seconds)

    monkeypatch.setattr(N.physical_input, "wait_for_quiet", change_foreground)
    _assert_refused(
        manager.cancel(ticket, desktop_opt_in=True), "native_dialog_not_foreground",
    )
    assert os.foreground == 200
    assert not _sends(os)


def test_cancel_foreground_change_after_final_input_sample_refuses(native):
    manager, os, _ = native
    ticket = _inspect(manager)

    def change_foreground():
        os.foreground = 200
        return False

    os.input_is_pressed = change_foreground
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_not_foreground")
    assert not _sends(os)


def test_cancel_os_identity_read_error_releases_lease_and_consumes_ticket(native):
    manager, os, _ = native
    ticket = _inspect(manager)

    def unavailable(pid):
        raise OSError("synthetic process identity unavailable")

    os.read_process = unavailable
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_unavailable")
    assert not manager._tickets
    assert not os.properties
    assert not Path(manager._lock_path).exists()
    assert not _sends(os)


def test_cancel_reports_unverified_marker_cleanup_without_dispatching_a_refused_action(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    os.obscured = True

    def cleanup_unavailable(hwnd, name, value):
        raise OSError("synthetic cleanup unavailable")

    os.remove_marker = cleanup_unavailable
    result = manager.cancel(ticket, desktop_opt_in=True)
    _assert_refused(result, "native_dialog_obscured")
    assert result["desktop"]["marker_cleanup_verified"] is False
    assert not manager._tickets
    assert not Path(manager._lock_path).exists()
    assert not _sends(os)


@pytest.mark.parametrize("observed", [[], ["pointer_x", "pointer_y"]])
def test_cancel_requires_keyboard_input_observation(native, monkeypatch, observed):
    manager, os, _ = native
    ticket = _inspect(manager)
    monkeypatch.setattr(
        N.physical_input, "wait_for_quiet",
        lambda seconds: {"enforced": bool(observed), "observed": observed},
    )

    result = manager.cancel(ticket, desktop_opt_in=True)

    _assert_refused(result, "input_quiet_unavailable")
    assert result["input_quiet"]["observed"] == observed
    assert not _sends(os)


def test_cancel_human_input_during_quiet_gate_refuses_and_releases(native, monkeypatch):
    manager, os, _ = native
    ticket = _inspect(manager)

    def busy(seconds):
        raise N.physical_input.InputActivityDetected("activity changed")

    monkeypatch.setattr(N.physical_input, "wait_for_quiet", busy)
    result = manager.cancel(ticket, desktop_opt_in=True)

    _assert_refused(result, "input_activity_detected")
    assert result["input_quiet"]["activity_detected"] is True
    assert not _sends(os)
    assert not os.properties
    assert not Path(manager._lock_path).exists()


@pytest.mark.parametrize("last", [(43, 10, 20), (42, 11, 20), (None, 10, 20)])
def test_cancel_input_change_after_quiet_before_dispatch_refuses(native, monkeypatch, last):
    manager, os, _ = native
    ticket = _inspect(manager)
    samples = iter([(42, 10, 20), last])
    monkeypatch.setattr(N.physical_input, "last_input_marker", lambda: next(samples))
    code = "input_quiet_unavailable" if last[0] is None else "input_activity_detected"
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), code)
    assert not _sends(os)


def test_cancel_busy_lease_consumes_ticket_without_dispatch(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    with N.physical_input.PhysicalInputLease(path=manager._lock_path):
        _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "physical_input_busy")
        assert Path(manager._lock_path).exists()
    assert not os.properties
    assert not _sends(os)
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_ticket_invalid")
    assert _inspect(manager)


def test_cancel_refuses_a_disabled_overlay_that_window_from_point_would_skip(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    os.overlay = True
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_obscured")
    assert not _sends(os)


def test_cancel_refuses_input_already_held_before_quiet_sampling(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    os.held_input = True
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "input_activity_detected")
    assert not _sends(os)


def test_opted_in_cancel_consumes_ticket_even_when_desktop_extra_becomes_unavailable(native):
    manager, os, _ = native
    ticket = _inspect(manager)
    manager._extra_probe = lambda: {"available": False, "missing": ["mss"]}
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "desktop_extra_unavailable")
    assert ticket not in manager._tickets
    assert not os.properties
    assert not _sends(os)


def test_cancel_approval_decline_consumes_ticket_without_lease_or_quiet(native, monkeypatch):
    manager, os, _ = native
    ticket = _inspect(manager)

    def forbidden(seconds):
        pytest.fail("declined cancellation reached quiet gate")

    monkeypatch.setattr(N.physical_input, "wait_for_quiet", forbidden)
    _assert_refused(
        manager.cancel(ticket, desktop_opt_in=True, approved=False), "requires_user_action",
    )
    assert not os.properties
    assert not _sends(os)
    assert not Path(manager._lock_path).exists()


@pytest.mark.parametrize("behavior", ["timeout", "error", "stay", "marker_removed", "reuse"])
def test_cancel_uncertain_delivery_never_replays_or_claims_closed(native, behavior):
    manager, os, _ = native
    ticket = _inspect(manager)
    os.send_behavior = behavior

    result = manager.cancel(ticket, desktop_opt_in=True)

    assert result["ok"] is False
    assert result["status"] == "unknown"
    assert result["retry_safe"] is False
    assert result["may_have_executed"] is True
    assert result["dispatched"] is True
    assert result["desktop"]["closed_observed"] is False
    assert len(_sends(os)) == 1
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_ticket_invalid")
    assert len(_sends(os)) == 1
    assert not Path(manager._lock_path).exists()


def test_cancel_holds_lease_until_post_dispatch_observation_finishes(native):
    manager, os, _ = native
    ticket = _inspect(manager)

    def observe_lease():
        with pytest.raises(N.physical_input.PhysicalInputBusy):
            with N.physical_input.PhysicalInputLease(path=manager._lock_path):
                pytest.fail("cancel released the physical-input lease before dispatch")

    os.before_send = observe_lease
    assert manager.cancel(ticket, desktop_opt_in=True)["ok"] is True


def test_ticket_expiry_consumes_and_cleans_marker(native):
    manager, os, clock = native
    ticket = _inspect(manager)
    clock.now += 16
    assert manager.ticket_needs_approval(ticket) is False
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_ticket_expired")
    assert not os.properties
    assert not _sends(os)


def test_ticket_expiry_during_quiet_gate_refuses(native, monkeypatch):
    manager, os, clock = native
    ticket = _inspect(manager)
    gate = N.physical_input.wait_for_quiet

    def expire(seconds):
        clock.now += 16
        return gate(seconds)

    monkeypatch.setattr(N.physical_input, "wait_for_quiet", expire)
    _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_ticket_expired")
    assert not os.properties
    assert not _sends(os)


def test_ticket_timer_removes_only_its_own_marker(native):
    manager, os, clock = native
    first = _inspect(manager)
    second = _inspect(manager)
    assert first != second
    records = list(manager._tickets.values())
    assert records[0].marker_name != records[1].marker_name
    assert len(os.properties) == 2
    clock.now += 16
    records[0].timer.fire()

    assert len(os.properties) == 1
    assert second in manager._tickets
    assert first not in manager._tickets
    assert not _sends(os)


def test_expired_timer_callback_cannot_remove_a_later_tickets_marker(native):
    manager, os, _ = native
    first = _inspect(manager)
    old_timer = manager._tickets[first].timer
    os.obscured = True
    _assert_refused(manager.cancel(first, desktop_opt_in=True), "native_dialog_obscured")
    os.obscured = False
    second = _inspect(manager)
    properties = dict(os.properties)

    old_timer.fire()

    assert os.properties == properties
    assert second in manager._tickets
    assert not _sends(os)


def test_ticket_store_is_bounded_and_eviction_cleans_marker(native):
    manager, os, _ = native
    first = _inspect(manager)
    for _ in range(8):
        _inspect(manager)
    assert len(manager._tickets) == len(os.properties) == 8
    assert first not in manager._tickets
    assert not _sends(os)


def test_two_cancel_calls_cannot_dispatch_the_same_ticket(native, monkeypatch):
    manager, os, _ = native
    ticket = _inspect(manager)
    entered = Event()
    release = Event()
    results = []
    gate = N.physical_input.wait_for_quiet

    def wait(seconds):
        entered.set()
        assert release.wait(3)
        return gate(seconds)

    monkeypatch.setattr(N.physical_input, "wait_for_quiet", wait)
    thread = Thread(target=lambda: results.append(manager.cancel(ticket, desktop_opt_in=True)))
    thread.start()
    try:
        assert entered.wait(3)
        _assert_refused(manager.cancel(ticket, desktop_opt_in=True), "native_dialog_ticket_invalid")
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert len(results) == 1 and results[0]["ok"] is True
    assert len(_sends(os)) == 1


def test_module_tools_delegate_to_injected_manager(native, monkeypatch):
    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = N.inspect_native_file_dialog(desktop_opt_in=True)["ticket"]
    assert N.ticket_needs_approval(ticket) is True
    assert N.cancel_native_file_dialog(ticket, desktop_opt_in=True)["ok"] is True
    assert len(_sends(os)) == 1


@pytest.mark.anyio
async def test_native_tool_schemas_expose_only_explicit_opt_in_and_ticket():
    from browsertap_mcp import server as S

    tools = {tool.name: tool for tool in await S.mcp.list_tools()}
    inspect_schema = tools["inspect_native_file_dialog"].inputSchema
    cancel_schema = tools["cancel_native_file_dialog"].inputSchema
    assert set(inspect_schema["properties"]) == {"desktop_opt_in"}
    assert set(cancel_schema["properties"]) == {"desktop_opt_in", "ticket"}
    assert inspect_schema["properties"]["desktop_opt_in"]["default"] is False
    assert cancel_schema["properties"]["desktop_opt_in"]["default"] is False
    assert cancel_schema["required"] == ["ticket"]


@pytest.mark.anyio
async def test_registered_native_tools_preserve_envelope_and_existing_approval_gate(native, monkeypatch):
    from browsertap_mcp import server as S

    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    approvals = []

    async def approve(ctx, summary):
        approvals.append(summary)
        return S._PhysicalApprovalDecision(True)

    monkeypatch.setattr(S, "_request_physical_approval", approve)
    inspect_fn = S.mcp._tool_manager.get_tool("inspect_native_file_dialog").fn
    cancel_fn = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    inspected = await inspect_fn(desktop_opt_in=True)
    assert inspected["ok"] is True
    assert inspected["result_contract"] == "btap.result.v1"
    assert inspected["diagnostics"]["desktop"]["action"] == "inspect"
    assert inspected["diagnostics"]["input_quiet"]["checked"] is False
    assert inspected["target"] is None
    cancelled = _structured(await cancel_fn(
        ticket=inspected["data"]["ticket"], desktop_opt_in=True, ctx=object(),
    ))
    assert cancelled["ok"] is True, cancelled.get("error")
    assert cancelled["diagnostics"]["desktop"]["closed_observed"] is True
    assert cancelled["diagnostics"]["input_quiet"]["enforced"] is True
    assert cancelled["diagnostics"]["on_screen"] is True
    assert approvals == ["cancel the inspected native file dialog"]
    assert len(_sends(os)) == 1


@pytest.mark.anyio
async def test_registered_cancel_opt_out_and_unknown_ticket_never_prompt_or_probe(native, monkeypatch):
    from browsertap_mcp import server as S

    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)

    async def forbidden(ctx, summary):
        pytest.fail("an invalid or opted-out native request must not prompt")

    monkeypatch.setattr(S, "_request_physical_approval", forbidden)
    function = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    opted_out = _structured(await function(ticket="invalid", ctx=object()))
    assert opted_out["ok"] is False
    assert opted_out["error"]["code"] == "desktop_opt_in_required"
    invalid = _structured(await function(ticket="invalid", desktop_opt_in=True, ctx=object()))
    assert invalid["error"]["code"] == "native_dialog_ticket_invalid"
    assert invalid["diagnostics"]["on_screen"] is None
    assert invalid["diagnostics"]["desktop"]["explicit_opt_in"] is True
    assert os.calls == []


@pytest.mark.anyio
async def test_registered_cancel_declined_approval_consumes_ticket_and_preserves_diagnostics(native, monkeypatch):
    from browsertap_mcp import server as S

    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)

    async def decline(ctx, summary):
        return S._PhysicalApprovalDecision(False, "declined")

    monkeypatch.setattr(S, "_request_physical_approval", decline)
    function = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    result = _structured(await function(ticket=ticket, desktop_opt_in=True, ctx=object()))
    assert result["ok"] is False
    assert result["error"]["code"] == "requires_user_action"
    assert result["error"]["retryable"] is False
    assert result["diagnostics"]["desktop"]["ticket_consumed"] is True
    assert result["diagnostics"]["desktop"]["marker_cleanup_verified"] is True
    assert not os.properties
    assert not _sends(os)


@pytest.mark.anyio
async def test_registered_cancel_unknown_dispatch_is_not_retryable(native, monkeypatch):
    from browsertap_mcp import server as S

    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    os.send_behavior = "marker_removed"

    async def approve(ctx, summary):
        return S._PhysicalApprovalDecision(True)

    monkeypatch.setattr(S, "_request_physical_approval", approve)
    function = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    result = _structured(await function(ticket=ticket, desktop_opt_in=True, ctx=object()))
    assert result["ok"] is False
    assert result["error"]["code"] == "native_dialog_cancel_unconfirmed"
    assert result["error"]["retryable"] is False
    assert result["diagnostics"]["retry_safe"] is False
    assert result["diagnostics"]["may_have_executed"] is True
    assert result["diagnostics"]["desktop"]["closed_observed"] is False
    assert len(_sends(os)) == 1


@pytest.mark.parametrize("activity", ["none", "pointer", "keyboard"])
def test_cancel_samples_scaled_pointer_in_one_restored_dpi_context(monkeypatch, tmp_path, activity):
    os = FakeWindows()
    clock = FakeClock()
    state = {"dpi": "default", "timestamp": 42, "x": 300, "y": 200}
    samples = []

    @contextmanager
    def dpi_context():
        previous = state["dpi"]
        state["dpi"] = "pmv2"
        try:
            yield
        finally:
            state["dpi"] = previous

    def sample():
        if len(samples) == 3:
            if activity == "pointer":
                state["x"] += 1
            elif activity == "keyboard":
                state["timestamp"] += 1
        # A stationary physical pointer at 150% scaling has two representations.
        point = (state["x"], state["y"]) if state["dpi"] == "pmv2" else (200, 133)
        samples.append(state["dpi"])
        return state["timestamp"], *point

    os.dpi_context = dpi_context
    monkeypatch.setattr(N.physical_input, "last_input_marker", sample)
    monkeypatch.setattr(N.physical_input.time, "sleep", clock.sleep)
    manager = N.NativeDialogManager(
        adapter_factory=lambda: os, platform=lambda: "win32",
        extra_probe=lambda: {"available": True, "missing": []},
        clock=clock, sleep=clock.sleep, timer_factory=FakeTimer,
        lock_path=tmp_path / "physical-input.lock",
    )
    try:
        result = manager.cancel(_inspect(manager), desktop_opt_in=True)
        if activity == "none":
            assert result["ok"] is True, result
            assert len(_sends(os)) == 1
        else:
            _assert_refused(result, "input_activity_detected")
            assert not _sends(os)
        assert samples == ["pmv2"] * 4
        assert state["dpi"] == "default"
        assert result["desktop"]["marker_cleanup_verified"] is True
        assert not os.properties
        assert not Path(manager._lock_path).exists()
    finally:
        manager.close()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["approval", "before_worker"])
async def test_registered_cancel_external_cancellation_consumes_ticket(native, monkeypatch, phase):
    from browsertap_mcp import server as S

    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    function = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    entered = anyio.Event()
    scopes = []
    approvals = []
    cancellations = []
    original_run_sync = S.anyio.to_thread.run_sync

    async def approve(ctx, summary):
        approvals.append(summary)
        if phase == "approval":
            entered.set()
            await anyio.sleep_forever()
        return S._PhysicalApprovalDecision(True)

    async def pending_worker(function, *args, **kwargs):
        entered.set()
        await anyio.sleep_forever()

    async def caller():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            try:
                await function(ticket=ticket, desktop_opt_in=True, ctx=object())
            except anyio.get_cancelled_exc_class():
                cancellations.append(True)
                raise

    monkeypatch.setattr(S, "_request_physical_approval", approve)
    if phase == "before_worker":
        monkeypatch.setattr(S.anyio.to_thread, "run_sync", pending_worker)
    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            group.start_soon(caller)
            await entered.wait()
            scopes[0].cancel()

    assert cancellations == [True]
    assert len(approvals) == 1
    assert manager.ticket_needs_approval(ticket) is False
    assert ticket not in manager._tickets
    assert not os.properties
    assert not _sends(os)
    assert not Path(manager._lock_path).exists()

    async def unexpected_approval(ctx, summary):
        pytest.fail("a cancelled attempt must never approve the same ticket again")

    monkeypatch.setattr(S, "_request_physical_approval", unexpected_approval)
    monkeypatch.setattr(S.anyio.to_thread, "run_sync", original_run_sync)
    retry = _structured(await function(ticket=ticket, desktop_opt_in=True, ctx=object()))
    assert retry["error"]["code"] == "native_dialog_ticket_invalid"
    assert not _sends(os)


@pytest.mark.anyio
@pytest.mark.parametrize("ordinary", [True, False])
async def test_registered_cancel_approval_exception_cleans_without_dispatch(native, monkeypatch, ordinary):
    from browsertap_mcp import server as S

    class ApprovalInterrupted(BaseException):
        pass

    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)

    async def interrupted(ctx, summary):
        if ordinary:
            raise RuntimeError("synthetic approval failure")
        raise ApprovalInterrupted("synthetic approval interruption")

    monkeypatch.setattr(S, "_request_physical_approval", interrupted)
    function = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    if ordinary:
        result = _structured(await function(ticket=ticket, desktop_opt_in=True, ctx=object()))
        assert result["ok"] is False
    else:
        with pytest.raises(ApprovalInterrupted):
            await function(ticket=ticket, desktop_opt_in=True, ctx=object())
    assert manager.ticket_needs_approval(ticket) is False
    assert not manager._tickets
    assert not os.properties
    assert not _sends(os)
    assert not Path(manager._lock_path).exists()


@pytest.mark.anyio
async def test_registered_cancel_claims_before_concurrent_approval(native, monkeypatch):
    from browsertap_mcp import server as S

    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    function = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    entered, release = anyio.Event(), anyio.Event()
    approvals, results = [], []

    async def approve(ctx, summary):
        approvals.append(summary)
        if len(approvals) == 1:
            entered.set()
            await release.wait()
        return S._PhysicalApprovalDecision(True)

    async def caller():
        results.append(_structured(await function(ticket=ticket, desktop_opt_in=True, ctx=object())))

    monkeypatch.setattr(S, "_request_physical_approval", approve)
    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            group.start_soon(caller)
            await entered.wait()
            try:
                duplicate = _structured(await function(ticket=ticket, desktop_opt_in=True, ctx=object()))
            finally:
                release.set()
    assert duplicate["ok"] is False
    assert duplicate["error"]["code"] == "native_dialog_ticket_invalid"
    assert len(approvals) == 1
    assert len(results) == 1 and results[0]["ok"] is True
    assert len(_sends(os)) == 1
    assert not os.properties


@pytest.mark.anyio
@pytest.mark.parametrize("cleanup", ["timer", "eviction", "shutdown", "clock"])
async def test_pending_native_approval_keeps_ticket_lifetime_bound(native, monkeypatch, cleanup):
    from browsertap_mcp import server as S

    manager, os, clock = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    record = manager._tickets[ticket]
    function = S.mcp._tool_manager.get_tool("cancel_native_file_dialog").fn
    entered, release = anyio.Event(), anyio.Event()
    results = []

    async def approve(ctx, summary):
        entered.set()
        await release.wait()
        return S._PhysicalApprovalDecision(True)

    async def caller():
        results.append(_structured(await function(ticket=ticket, desktop_opt_in=True, ctx=object())))

    monkeypatch.setattr(S, "_request_physical_approval", approve)
    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            group.start_soon(caller)
            await entered.wait()
            try:
                assert record.timer.cancelled is False
                if cleanup == "timer":
                    clock.now += 16
                    record.timer.fire()
                elif cleanup == "eviction":
                    for _ in range(8):
                        _inspect(manager)
                    assert len(manager._tickets) == len(os.properties) == 8
                elif cleanup == "shutdown":
                    manager.close()
                else:
                    clock.now += 16
                if cleanup != "clock":
                    assert ticket not in manager._tickets
                    assert os.marker(100, record.marker_name) == 0
            finally:
                release.set()
    assert len(results) == 1 and results[0]["ok"] is False
    code = "native_dialog_ticket_expired" if cleanup == "clock" else "native_dialog_ticket_invalid"
    assert results[0]["error"]["code"] == code
    assert not _sends(os)
    assert os.marker(100, record.marker_name) == 0
