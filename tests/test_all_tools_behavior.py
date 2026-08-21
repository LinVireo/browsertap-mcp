"""Controlled behavior harness for thin tools without focused test evidence."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S

HARNESS_TOOLS = [
    "capture_desktop_screenshot",
    "cdp_batch",
    "console_capture_start",
    "console_capture_stop",
    "create_bookmark",
    "debugger_targets",
    "extension_path",
    "get_automation_profile",
    "get_bookmarks",
    "get_console_messages",
    "get_setup_status",
    "list_all_tabs",
    "list_extensions",
    "list_tabs",
    "network_capture_start",
    "network_capture_stop",
    "pointer_info",
    "remove_bookmark",
    "reset_site_permissions",
    "set_automation_profile",
    "set_extension_enabled",
    "uninstall_extension",
]

MUTATING_HARNESS_TOOLS = [
    "console_capture_start",
    "console_capture_stop",
    "create_bookmark",
    "network_capture_start",
    "network_capture_stop",
    "remove_bookmark",
    "set_automation_profile",
    "set_extension_enabled",
    "uninstall_extension",
]


class FakeDriver:
    def __init__(self, response=None):
        self.response = response or {"data": {"ok": True, "data": []}}
        self.calls = []
        self.default_session_id = "chrome:7"
        self.is_remote = False

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload, client_id, timeout))
        return self.response


def _install_driver(monkeypatch, response=None):
    driver = FakeDriver(response)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    return driver


def _install_desktop_capture(monkeypatch):
    class Shot:
        width = 2
        height = 1
        size = (2, 1)
        rgb = b"\x00\x00\x00\xff\xff\xff"

    class Capture:
        monitors = [
            {"left": -2, "top": 0, "width": 4, "height": 1},
            {"left": -2, "top": 0, "width": 2, "height": 1},
            {"left": 0, "top": 0, "width": 2, "height": 1},
        ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def grab(self, _monitor):
            return Shot()

    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Capture))


def _success(tool, monkeypatch, tmp_path):
    driver = _install_driver(monkeypatch)
    if tool == "capture_desktop_screenshot":
        _install_desktop_capture(monkeypatch)
        out = S.capture_desktop_screenshot(str(tmp_path / "desktop.png"))
        assert out.structuredContent["width"] == 2
        assert out.structuredContent["monitor_count"] == 2
        assert out.structuredContent["virtual_desktop"] is True
        assert "not from a selected or background browser tab" in out.structuredContent["model_note"]
        assert (tmp_path / "desktop.png").exists()
    elif tool == "cdp_batch":
        monkeypatch.setattr(S, "exec_js", lambda script, **kwargs: {"data": script})
        assert '"cmd": "batch"' in S.cdp_batch('{"cmd":"batch","commands":[]}')["data"]
    elif tool in {"network_capture_start", "network_capture_stop", "console_capture_start",
                  "get_console_messages", "console_capture_stop"}:
        monkeypatch.setattr(
            S, "_tab_extension_operation",
            lambda payload, **kwargs: {"status": "ok", "method": payload["method"]},
        )
        calls = {
            "network_capture_start": lambda: S.network_capture_start(max_entries=10),
            "network_capture_stop": lambda: S.network_capture_stop(),
            "console_capture_start": lambda: S.console_capture_start(max_entries=10),
            "get_console_messages": lambda: S.get_console_messages(max_items=1),
            "console_capture_stop": lambda: S.console_capture_stop(),
        }
        assert calls[tool]()["status"] == "ok"
    elif tool == "create_bookmark":
        assert S.create_bookmark("coverage", "https://example.test/")["status"] == "ok"
    elif tool == "remove_bookmark":
        assert S.remove_bookmark("coverage-id")["status"] == "ok"
    elif tool == "debugger_targets":
        S.debugger_targets("chrome:7")
        assert driver.calls[0][0] == {"cmd": "debugger_targets"}
    elif tool == "extension_path":
        monkeypatch.setattr(S, "chrome_extension_dir", lambda: tmp_path / "extension")
        assert S.extension_path() == {"extension_path": str(tmp_path / "extension")}
    elif tool == "get_automation_profile":
        monkeypatch.setattr(S, "_automation_profile", lambda: {"mode": "lab", "no_elicit": True})
        assert S.get_automation_profile()["mode"] == "lab"
    elif tool == "get_bookmarks":
        assert S.get_bookmarks()["status"] == "ok"
    elif tool == "get_setup_status":
        monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: [{"id": "chrome:7"}])
        monkeypatch.setattr(S, "chrome_extension_dir", lambda: tmp_path / "extension")
        assert S.get_setup_status()["connected_tabs"] == 1
    elif tool == "list_all_tabs":
        S.list_all_tabs("chrome:7")
        assert driver.calls[0][0] == {"cmd": "tabs", "all": True}
    elif tool == "list_extensions":
        S.list_extensions("chrome:7")
        assert driver.calls[0][0]["method"] == "list"
    elif tool == "list_tabs":
        monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: [{"id": "chrome:7"}])
        assert S.list_tabs()["tabs"] == [{"id": "chrome:7"}]
    elif tool == "pointer_info":
        monkeypatch.setattr(
            S, "_pyautogui",
            lambda: SimpleNamespace(position=lambda: (11, 12), size=lambda: (800, 600)),
        )
        assert S.pointer_info() == {"x": 11, "y": 12, "screen_width": 800, "screen_height": 600}
    elif tool == "reset_site_permissions":
        monkeypatch.setattr(S, "switch_session", lambda session_id=None: "chrome:7")
        assert S.reset_site_permissions(session_id="chrome:7")["status"] == "ok"
    elif tool == "set_automation_profile":
        assert S.set_automation_profile("lab")["mode"] == "lab"
    elif tool == "set_extension_enabled":
        assert S.set_extension_enabled("coverage-ext", False)["enabled"] is False
    elif tool == "uninstall_extension":
        assert S.uninstall_extension("coverage-ext", False)["operation"] == "uninstall_extension"
    else:  # pragma: no cover - the parametrized set is the exhaustive contract
        raise AssertionError(tool)


def _boundary(tool, monkeypatch, tmp_path):
    driver = _install_driver(monkeypatch)
    if tool == "capture_desktop_screenshot":
        class BrokenCapture:
            monitors = [{"left": 0, "top": 0, "width": 1, "height": 1}]
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def grab(self, _monitor): raise RuntimeError("desktop unavailable")
        monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=BrokenCapture))
        with pytest.raises(RuntimeError, match="desktop unavailable"):
            S.capture_desktop_screenshot()
    elif tool == "cdp_batch":
        with pytest.raises(RuntimeError, match="cmd='batch'"):
            S.cdp_batch('{"cmd":"not-batch"}')
    elif tool == "network_capture_start":
        with pytest.raises(ValueError, match="max_entries"):
            S.network_capture_start(max_entries=9)
    elif tool == "network_capture_stop":
        with pytest.raises(ValueError, match="status_min"):
            S.network_capture_stop(status_min=99)
    elif tool == "console_capture_start":
        with pytest.raises(ValueError, match="max_entries"):
            S.console_capture_start(max_entries=9)
    elif tool == "get_console_messages":
        with pytest.raises(ValueError, match="offset"):
            S.get_console_messages(offset=-1)
    elif tool == "console_capture_stop":
        monkeypatch.setattr(S, "_tab_extension_operation", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        with pytest.raises(RuntimeError, match="bridge down"):
            S.console_capture_stop()
    elif tool == "create_bookmark":
        with pytest.raises(ValueError, match="title"):
            S.create_bookmark(" ")
    elif tool == "remove_bookmark":
        with pytest.raises(ValueError, match="bookmark_id"):
            S.remove_bookmark(" ")
    elif tool == "debugger_targets":
        monkeypatch.setattr(driver, "ext_cmd", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        with pytest.raises(RuntimeError, match="bridge down"):
            S.debugger_targets()
    elif tool == "extension_path":
        monkeypatch.setattr(S, "chrome_extension_dir", lambda: (_ for _ in ()).throw(RuntimeError("path failed")))
        with pytest.raises(RuntimeError, match="path failed"):
            S.extension_path()
    elif tool == "get_automation_profile":
        monkeypatch.setattr(S, "_automation_profile", lambda: (_ for _ in ()).throw(RuntimeError("profile failed")))
        with pytest.raises(RuntimeError, match="profile failed"):
            S.get_automation_profile()
    elif tool == "get_bookmarks":
        monkeypatch.setattr(driver, "ext_cmd", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        with pytest.raises(RuntimeError, match="bridge down"):
            S.get_bookmarks()
    elif tool == "get_setup_status":
        monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        assert S.get_setup_status()["bridge_error"] == "bridge down"
    elif tool == "list_all_tabs":
        monkeypatch.setattr(driver, "ext_cmd", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        with pytest.raises(RuntimeError, match="bridge down"):
            S.list_all_tabs()
    elif tool == "list_extensions":
        monkeypatch.setattr(driver, "ext_cmd", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        with pytest.raises(RuntimeError, match="bridge down"):
            S.list_extensions()
    elif tool == "list_tabs":
        monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        assert S.list_tabs()["bridge_error"] == "bridge down"
    elif tool == "pointer_info":
        monkeypatch.setattr(S, "_pyautogui", lambda: SimpleNamespace(position=lambda: (_ for _ in ()).throw(RuntimeError("no desktop"))))
        with pytest.raises(RuntimeError, match="no desktop"):
            S.pointer_info()
    elif tool == "reset_site_permissions":
        with pytest.raises(ValueError, match="http or https"):
            S.reset_site_permissions(origin="file:///tmp/nope")
    elif tool == "set_automation_profile":
        with pytest.raises(ValueError, match="safe"):
            S.set_automation_profile("unsafe")
    elif tool == "set_extension_enabled":
        monkeypatch.setattr(driver, "ext_cmd", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bridge down")))
        with pytest.raises(RuntimeError, match="bridge down"):
            S.set_extension_enabled("coverage-ext", False)
    elif tool == "uninstall_extension":
        with pytest.raises(ValueError, match="extension_id"):
            S.uninstall_extension(" ")
    else:  # pragma: no cover
        raise AssertionError(tool)


@pytest.mark.parametrize("tool", HARNESS_TOOLS, ids=HARNESS_TOOLS)
def test_harness_success(tool, monkeypatch, tmp_path):
    _success(tool, monkeypatch, tmp_path)


@pytest.mark.parametrize("tool", HARNESS_TOOLS, ids=HARNESS_TOOLS)
def test_harness_boundary(tool, monkeypatch, tmp_path):
    _boundary(tool, monkeypatch, tmp_path)


@pytest.mark.parametrize("tool", MUTATING_HARNESS_TOOLS, ids=MUTATING_HARNESS_TOOLS)
def test_harness_cleanup(tool, monkeypatch):
    driver = _install_driver(monkeypatch)
    if tool in {
        "network_capture_start",
        "network_capture_stop",
        "console_capture_start",
        "console_capture_stop",
    }:
        calls = []
        monkeypatch.setattr(
            S,
            "_tab_extension_operation",
            lambda payload, **kwargs: calls.append(payload["method"])
            or {"status": "ok"},
        )
        if tool == "network_capture_start":
            try:
                S.network_capture_start(max_entries=10)
            finally:
                S.network_capture_stop()
            assert calls == ["start", "stop"]
        elif tool == "console_capture_start":
            try:
                S.console_capture_start(max_entries=10)
            finally:
                S.console_capture_stop()
            assert calls == ["start", "stop"]
        elif tool == "network_capture_stop":
            S.network_capture_stop()
            assert calls == ["stop"]
        else:
            S.console_capture_stop()
            assert calls == ["stop"]
    elif tool == "set_automation_profile":
        # Assert what the call does, not what the restore line just wrote:
        # comparing the override against the value the `finally` block assigned
        # to it passes even if `set_automation_profile` is a no-op.
        previous_mode = S._AUTOMATION_MODE_OVERRIDE
        previous_physical = set(S._LAB_PHYSICAL_APPROVALS)
        previous_sites = set(S._LAB_SITE_PERMISSION_APPROVALS)
        S._LAB_PHYSICAL_APPROVALS.add("chrome:7|mouse_click")
        S._LAB_SITE_PERMISSION_APPROVALS.add("chrome:7|https://example.test|camera")
        try:
            profile = S.set_automation_profile("safe")
            assert S._AUTOMATION_MODE_OVERRIDE == "safe"
            # Tightening the mode must invalidate approvals granted under the
            # looser one; leaving them cached would let a lab-era grant keep
            # skipping the prompts safe mode exists to force.
            assert S._LAB_PHYSICAL_APPROVALS == set()
            assert S._LAB_SITE_PERMISSION_APPROVALS == set()
            assert profile["mode"] == "safe"
            assert profile["physical_approval"] == "every_action"
            assert profile["site_permission_approval"] == "every_allow"
            assert profile["no_elicit"] is False
        finally:
            S._AUTOMATION_MODE_OVERRIDE = previous_mode
            S._LAB_PHYSICAL_APPROVALS.clear()
            S._LAB_PHYSICAL_APPROVALS.update(previous_physical)
            S._LAB_SITE_PERMISSION_APPROVALS.clear()
            S._LAB_SITE_PERMISSION_APPROVALS.update(previous_sites)
    elif tool == "set_extension_enabled":
        try:
            S.set_extension_enabled("coverage-ext", False)
        finally:
            S.set_extension_enabled("coverage-ext", True)
        assert [call[0]["method"] for call in driver.calls] == ["disable", "enable"]
    elif tool == "create_bookmark":
        created = S.create_bookmark("coverage", "https://example.test/")
        try:
            assert created["status"] == "ok"
        finally:
            S.remove_bookmark("coverage-id")
        assert driver.calls[-1][0]["method"] == "remove"
    elif tool == "remove_bookmark":
        S.remove_bookmark("coverage-id")
        assert driver.calls[-1][0]["method"] == "remove"
    elif tool == "uninstall_extension":
        # Uninstalling cannot be undone, so there is nothing to restore: the
        # cleanup contract is that the call stays confined to the disposable id.
        # Clearing `driver.calls` and then asserting it is empty proved nothing
        # about that — it passed no matter what the tool sent.
        result = S.uninstall_extension("disposable-coverage-ext", False)
        assert result["status"] == "ok"
        assert result["extension_id"] == "disposable-coverage-ext"
        assert result["confirmation_requested"] is False
        assert len(driver.calls) == 1
        payload, _client_id, timeout = driver.calls[0]
        assert payload == {
            "cmd": "management",
            "method": "uninstall",
            "extId": "disposable-coverage-ext",
            "showConfirmDialog": False,
        }
        assert timeout == 20.0
