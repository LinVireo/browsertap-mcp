"""Navigation must preserve intent and only fall back after a definite refusal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


@pytest.fixture
def navigation(monkeypatch):
    now = [0.0]
    calls = []
    tabs = [{"id": "chrome:7", "url": "https://before.test/", "browser": "chrome"}]
    driver = SimpleNamespace(default_session_id=None)
    def navigate(command, **kwargs):
        calls.append((command, kwargs))
        return {"data": {"status": "ok", "url": command["url"]}}
    driver.ext_cmd = navigate
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **kw: tabs)
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)
    monkeypatch.setattr(S, "_lab_auto_accepts_beforeunload", lambda *a: False)
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(S.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.delenv("BROWSERTAP_PREFERRED_BROWSER", raising=False)
    return driver, tabs, calls, now


@pytest.mark.parametrize("default,preferred", [(None, ""), (None, "edge"), ("chrome:7", "edge"), ("chrome:99", "")])
def test_implicit_navigation_keeps_the_task_browser(navigation, monkeypatch, default, preferred):
    driver, tabs, calls, _ = navigation
    driver.default_session_id = default
    if preferred:
        monkeypatch.setenv("BROWSERTAP_PREFERRED_BROWSER", preferred)
        tabs.append({"id": "edge:9", "url": "https://other.test/", "browser": "edge"})
    result = S.open_url("https://after.test/")
    expected = "edge:9" if preferred and default is None else "chrome:7"
    assert result["active_session_id"] == driver.default_session_id == expected
    assert calls[0][0]["tabId"] == int(expected.split(":")[-1])


def test_navigation_refuses_an_explicit_dead_target(navigation):
    _, _, calls, _ = navigation
    with pytest.raises(Exception, match="chrome:99"):
        S.open_url("https://after.test/", session_id="chrome:99")
    assert calls == []


@pytest.mark.parametrize("stage", ["before", "sessions", "navigation", "fallback"])
def test_navigation_never_renews_its_deadline(navigation, monkeypatch, stage):
    driver, tabs, calls, now = navigation
    if stage == "before":
        ticks = iter([0, 2])
        monkeypatch.setattr(S.time, "monotonic", lambda: next(ticks))
    elif stage == "sessions":
        def slow(**kw):
            now[0] = 2
            return tabs
        monkeypatch.setattr(S, "ensure_sessions", slow)
    elif stage == "navigation":
        def slow(*a):
            now[0] = 2
            return False
        monkeypatch.setattr(S, "_lab_auto_accepts_beforeunload", slow)
    else:
        def slow(*a, **kw):
            now[0] = 2
            raise RuntimeError("unknown cmd navigate")
        driver.ext_cmd = slow
    with pytest.raises(TimeoutError, match="deadline"):
        S.open_url("https://after.test/", timeout=1, session_id="chrome:7")
    assert calls == []


@pytest.mark.parametrize("landing", [None, {}, {"result": None}, {"result": {"value": {"url": "https://before.test/"}}}, RuntimeError("renderer gone")])
def test_fallback_requires_observed_landing_and_does_not_echo_requested_url(navigation, monkeypatch, landing):
    driver, _, _, now = navigation
    def unsupported(*a, **kw):
        raise RuntimeError("unknown command navigate")
    driver.ext_cmd = unsupported
    def cdp(method, *a, **kw):
        if method == "Page.navigate":
            return {"frameId": "frame"}
        now[0] += 0.3
        if isinstance(landing, Exception):
            raise landing
        return landing
    monkeypatch.setattr(S, "_direct_cdp", cdp)
    result = S.open_url("https://after.test/", timeout=1, session_id="chrome:7")
    assert result["status"] == "navigation_timeout"
    assert result["url"] == ""
    assert result["navigation_mode"] == "cdp_fallback"
    assert driver.default_session_id is None


@pytest.mark.parametrize("before", ["", "https://after.test/"])
def test_fallback_accepts_a_read_back_matching_destination(navigation, monkeypatch, before):
    driver, tabs, _, _ = navigation
    tabs[0]["url"] = before
    def unsupported(*a, **kw):
        raise RuntimeError("unknown command navigate")
    driver.ext_cmd = unsupported
    monkeypatch.setattr(S, "_direct_cdp", lambda method, *a, **kw: {} if method == "Page.navigate" else {
        "result": {"value": {"url": "https://after.test/", "title": "Destination"}},
    })
    result = S.open_url("https://after.test/", timeout=1)
    assert result["url"] == "https://after.test/"
    assert result["title"] == "Destination"
    assert result["navigation_mode"] == "cdp_fallback"


@pytest.mark.parametrize("response", [{"data": "buffer"}, {"data": {"ok": True, "data": [1, 2]}}, {"data": {"ok": False, "error": "denied"}}])
def test_numeric_extension_operation_preserves_payload_and_default(navigation, monkeypatch, response):
    driver, _, _, _ = navigation
    driver.default_session_id = "chrome:1"
    monkeypatch.setattr(S, "_normalize_tab_targets", lambda *a, **kw: ([7], "chrome"))
    sent = []
    driver.ext_cmd = lambda command, **kw: sent.append((command, kw)) or response
    result = S._tab_extension_operation({"cmd": "test"}, operation="test", session_id="7", timeout=1)
    assert result["session_id"] == "chrome:7"
    assert result["tab_id"] == 7
    assert driver.default_session_id == "chrome:1"
    assert sent[0][0]["tabId"] == 7
    if isinstance(response["data"], dict) and response["data"].get("ok") is False:
        assert result["status"] == "error"


def test_numeric_extension_operation_requires_a_browser(navigation, monkeypatch):
    monkeypatch.setattr(S, "_normalize_tab_targets", lambda *a, **kw: ([7], None))
    with pytest.raises(ValueError, match="infer browser"):
        S._tab_extension_operation({"cmd": "test"}, operation="test", session_id="7", timeout=1)
