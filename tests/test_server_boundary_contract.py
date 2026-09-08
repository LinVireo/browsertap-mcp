"""Observable boundary behavior for routing, transport, and saved results."""

from __future__ import annotations

import ctypes
import errno
import json
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


@pytest.fixture
def routing(monkeypatch):
    tabs = [
        {"id": "chrome:7", "browser": "chrome", "url": "https://one.test/"},
        {"id": "edge:8", "browser": "edge", "url": "https://two.test/"},
    ]
    driver = SimpleNamespace(default_session_id=None, set_session=lambda pattern: None)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda **kwargs: list(tabs))
    monkeypatch.setattr(S, "ensure_sessions", lambda **kwargs: list(tabs))
    monkeypatch.setattr(S, "_resolve_session_target", lambda *args: None)
    monkeypatch.delenv("BROWSERTAP_PREFERRED_BROWSER", raising=False)
    return driver, tabs


def test_default_selection_requires_an_unambiguous_browser(routing, monkeypatch):
    driver, tabs = routing
    with pytest.raises(S.AmbiguousBrowserError):
        S.switch_session()
    assert driver.default_session_id is None
    monkeypatch.setenv("BROWSERTAP_PREFERRED_BROWSER", " EDGE ")
    assert S.switch_session() == "edge:8"
    assert S.switch_session() == "edge:8"
    assert S._browser_candidates(tabs, current="chrome:99") == [tabs[0]]
    with pytest.raises(S.ExtensionNotConnectedError):
        S._browser_candidates(tabs, current="absent:99")


def test_browser_and_url_selection_report_absence_or_ambiguity(routing):
    driver, tabs = routing
    with pytest.raises(RuntimeError, match="No connected tab"):
        S.switch_session(browser="opera")
    with pytest.raises(RuntimeError, match="No session matching"):
        S.switch_session(url_pattern="absent")
    driver.set_session = lambda pattern: "chrome:7"
    assert S.switch_session(url_pattern="one.test") == "chrome:7"
    assert S.switch_session(browser="chrome") == "chrome:7"
    tabs.append({"id": "other-chrome:9", "browser": "chrome"})
    with pytest.raises(S.AmbiguousBrowserError):
        S.switch_session(browser="chrome")
    assert driver.default_session_id == "chrome:7"


def test_stale_default_refreshes_without_crossing_browser(routing, monkeypatch):
    driver, tabs = routing
    assert S.prune_stale_default() is None
    driver.default_session_id = "chrome:7"
    assert S.prune_stale_default() == "chrome:7"
    monkeypatch.setattr(S, "active_sessions", lambda **kw: tabs if kw.get("fresh") else [])
    assert S.prune_stale_default() == "chrome:7"
    driver.default_session_id = "legacy"
    assert S.prune_stale_default() is None
    assert driver.default_session_id is None
    driver.default_session_id = "opera:1"
    assert S.prune_stale_default() is None
    assert driver.default_session_id == "opera:1"


@pytest.mark.parametrize("replacement", ["chrome:7", "chrome:9"])
def test_default_replacement_updates_ownership_only_when_changed(routing, monkeypatch, replacement):
    driver, _ = routing
    driver.default_session_id = "chrome:7"
    changed = []
    monkeypatch.setattr(S, "_resolve_session_target", lambda *a: {"session_id": replacement})
    monkeypatch.setattr(S._TAB_OWNERSHIP, "rebind", lambda *a: changed.append(a))
    assert S.prune_stale_default() == replacement
    assert changed == ([] if replacement == "chrome:7" else [("chrome:7", "chrome:9")])


@pytest.mark.parametrize("value", [[], True, -1, "invalid", None, ["chrome:7", "edge:8"]])
def test_invalid_close_targets_do_not_resolve_to_other_tabs(routing, value):
    with pytest.raises(ValueError):
        S._normalize_tab_targets(value)


def test_replacement_only_rewrites_its_own_native_target(routing, monkeypatch):
    changed = []
    monkeypatch.setattr(S, "_resolve_session_target", lambda *a: {"session_id": "chrome:9"})
    monkeypatch.setattr(S._TAB_OWNERSHIP, "rebind", lambda *a: changed.append(a))
    assert S._normalize_tab_targets("chrome:7") == ([9], "chrome")
    assert changed == [("chrome:7", "chrome:9")]
    values = ["chrome:7", 7, "7", True, 8, "edge:7", "invalid"]
    assert S._replace_rebound_tab_target(
        values, old_session_id="chrome:7", new_session_id="chrome:9",
    ) == ["chrome:9", 9, 9, True, 8, "edge:7", "invalid"]
    assert S._replace_rebound_tab_target(
        7, old_session_id="chrome:7", new_session_id="chrome:9",
    ) == 9


def test_implicit_browser_handles_no_sessions_and_legacy_ids(routing, monkeypatch):
    _, tabs = routing
    tabs[:] = [{"id": "legacy", "browser": "chrome"}]
    assert S._implicit_client_id() is None
    tabs[:] = [{"id": "chrome:7"}]
    assert S._implicit_client_id() == "chrome"
    def unavailable(**kwargs):
        raise RuntimeError("bridge disconnected")
    monkeypatch.setattr(S, "active_sessions", unavailable)
    assert S._implicit_client_id() is None
    assert S.normalize_session_id(None) is None
    assert S.normalize_session_id(7) == "7"


@pytest.mark.parametrize("response,expected", [
    ({"client_id": "first", "clientId": "second"}, "first"),
    ({"clientId": "second"}, "second"),
    ({"data": {"client_id": "third"}}, "third"),
    ({"data": {"clientId": "fourth"}}, "fourth"),
    ({"data": {}}, "fallback"), (None, "fallback"),
])
def test_extension_result_retains_the_browser_namespace(response, expected):
    assert S._response_client_id(response, "fallback") == expected


@pytest.mark.parametrize("state,expected", [
    ({}, "no_body"),
    ({"has_body": True, "ready_state": "interactive"}, "loading"),
    ({"has_body": True, "ready_state": "complete", "loading": True}, "hydrating"),
    ({"has_body": True, "ready_state": "complete", "html_chars": 10000}, "shell_only"),
    ({"has_body": True, "ready_state": "complete", "text_chars": 100}, "content"),
])
def test_render_state_distinguishes_loading_from_content(state, expected):
    driver = SimpleNamespace(execute_js=lambda *a, **kw: {"data": json.dumps(state)})
    result = S._page_render_state(driver, "chrome:7", 1)
    assert result["state"] == expected
    assert result["content_ready"] is (expected == "content")


@pytest.mark.parametrize("response", [None, [], "invalid json", {"data": []}])
def test_malformed_optional_render_probe_does_not_break_scanning(response):
    driver = SimpleNamespace(execute_js=lambda *a, **kw: response)
    assert S._page_render_state(driver, "chrome:7", 1) is None


def test_missing_or_failed_optional_render_probe_is_unavailable():
    def failed(*a, **kw):
        raise TimeoutError("probe timed out")
    assert S._page_render_state(SimpleNamespace(), "chrome:7", 1) is None
    assert S._page_render_state(SimpleNamespace(execute_js=failed), "chrome:7", 1) is None


@pytest.mark.parametrize("timeout,deadline", [(0, None), (1, -1)])
def test_expired_direct_cdp_never_dispatches(routing, timeout, deadline):
    driver, _ = routing
    driver.ext_cmd = lambda *a, **kw: pytest.fail("expired command dispatched")
    with pytest.raises(TimeoutError, match="deadline"):
        S._direct_cdp("Page.navigate", {}, session_id="chrome:7", client_id="chrome",
                      tab_id=7, timeout=timeout, deadline=deadline)


@pytest.mark.parametrize("failure", [TimeoutError("unknown cmd"), RuntimeError("denied")])
def test_direct_cdp_ambiguous_or_denied_commands_are_never_replayed(routing, failure):
    driver, _ = routing
    def failed(*a, **kw):
        raise failure
    driver.ext_cmd = failed
    driver.execute_js = lambda *a, **kw: pytest.fail("mutation replayed")
    with pytest.raises(type(failure)) as caught:
        S._direct_cdp("Page.navigate", {}, session_id="chrome:7", client_id="chrome",
                      tab_id=7, timeout=2)
    assert caught.value is failure


def test_direct_cdp_fallback_requires_an_available_transport(routing):
    driver, _ = routing
    def unsupported(*a, **kw):
        raise RuntimeError("unknown cmd: cdp")
    driver.ext_cmd = unsupported
    with pytest.raises(RuntimeError, match="unknown cmd"):
        S._direct_cdp("Page.navigate", {}, session_id="chrome:7", client_id="chrome",
                      tab_id=7, timeout=2)
    calls = []
    driver.execute_js = lambda script, **kw: calls.append((json.loads(script), kw)) or {
        "data": {"ok": True, "data": {"frameId": "frame"}},
    }
    assert S._direct_cdp("Page.navigate", {}, session_id="chrome:7", client_id="chrome",
                         tab_id=7, timeout=2) == {"frameId": "frame"}
    assert calls[0][0]["tabId"] == 7
    assert calls[0][1]["session_id"] == "chrome:7"


def test_large_js_values_are_lossless_and_small_failures_are_not_exported(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "tempdir", str(tmp_path))
    original = {"status": "success", "js_return": {"text": "content" * 5000}}
    result = S._externalize_execute_js_result(original)
    assert result["result_externalized"] is True
    assert json.loads(S.Path(result["result_file"]).read_bytes()) == original["js_return"]
    assert result["result_bytes"] > S.EXECUTE_JS_INLINE_MAX_BYTES
    assert S._externalize_execute_js_result(None) is None
    failed = {"status": "failed", "js_return": original["js_return"]}
    assert S._externalize_execute_js_result(failed) is failed
    cycle = []
    cycle.append(cycle)
    assert json.loads(S._serialize_execute_js_value(cycle)) == "[[...]]"


@pytest.mark.parametrize("mode", ["zero", "error", "size"])
def test_atomic_save_rejects_incomplete_writes_without_replacing_data(monkeypatch, tmp_path, mode):
    target = tmp_path / "capture.png"
    target.write_bytes(b"original")
    def failed_write(*args):
        if mode == "zero":
            return 0
        raise OSError(errno.EIO, "device failure")
    monkeypatch.setattr(S.os, "write", failed_write)
    with pytest.raises(ValueError if mode == "size" else RuntimeError):
        S._atomic_write_bytes(target, b"new", max_size=1 if mode == "size" else None)
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("error,alive", [(5, True), (6, True), (87, False)])
def test_inaccessible_windows_spawn_owner_is_not_treated_as_dead(monkeypatch, error, alive):
    native = SimpleNamespace(
        OpenProcess=lambda *a: 0,
        GetExitCodeProcess=lambda *a: False,
        CloseHandle=lambda *a: None,
        GetLastError=lambda: error,
    )
    monkeypatch.setattr(S.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=native), raising=False)
    assert S._pid_alive(999999) is alive
