"""Focused offline branch coverage for server orchestration helpers."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from agent_browser_mcp import server as S


def test_switch_session_rejects_ambiguous_url_matches(monkeypatch):
    driver = SimpleNamespace(default_session_id="client:old")
    sessions = [
        {"id": "chrome:a:1", "browser": "chrome", "url": "https://example.test/one"},
        {"id": "chrome:a:2", "browser": "chrome", "url": "https://example.test/two"},
    ]
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda *args, **kwargs: sessions)
    driver.set_session = lambda pattern: (_ for _ in ()).throw(
        ValueError(
            f"URL pattern {pattern!r} matched 2 sessions: chrome:a:1, chrome:a:2. "
            "Pass the full session_id to select one."
        )
    )

    with pytest.raises(ValueError, match="matched 2 sessions"):
        S.switch_session(url_pattern="example.test")
    with pytest.raises(RuntimeError, match="matched 2 tabs") as exc:
        S.switch_session(browser="chrome", url_pattern="example.test")
    assert "chrome:a:1" in str(exc.value)
    assert "full session_id" in str(exc.value)
    assert driver.default_session_id == "client:old"


def test_switch_session_browser_url_pattern_requires_a_match(monkeypatch):
    driver = SimpleNamespace(default_session_id="client:old")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda *args, **kwargs: [
            {"id": "edge:a:1", "browser": "edge", "url": "https://other.test/"}
        ],
    )

    with pytest.raises(RuntimeError, match="No connected tab.*matches URL pattern"):
        S.switch_session(browser="edge", url_pattern="wanted.test")
    assert driver.default_session_id == "client:old"


def _install_page_driver(monkeypatch, *, default_session_id="client:old"):
    driver = SimpleNamespace(default_session_id=default_session_id)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "ensure_sessions",
        lambda *args, **kwargs: [{"id": "client:1", "url": "https://example.test/"}],
    )

    def switch_session(session_id=None, **_kwargs):
        selected = str(session_id) if session_id is not None else "client:1"
        driver.default_session_id = selected
        return selected

    monkeypatch.setattr(S, "switch_session", switch_session)
    monkeypatch.setattr(S, "compact_tabs", lambda *args, **kwargs: [{"id": "client:1"}])
    return driver


def _monotonic(monkeypatch, values):
    timeline = iter(values)
    monkeypatch.setattr(S.time, "monotonic", lambda: next(timeline))


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("read-only")])
def test_spawn_lock_permission_and_os_errors_stand_down(monkeypatch, tmp_path, error):
    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        S.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    assert S._acquire_spawn_lock() is None


def test_spawn_bridge_waiter_times_out_without_starting_another_daemon(monkeypatch):
    sleeps = []
    monkeypatch.setattr(S, "_acquire_spawn_lock", lambda: None)
    monkeypatch.setattr(S, "_port_open", lambda *_args: False)
    monkeypatch.setattr(S.time, "sleep", sleeps.append)
    _monotonic(monkeypatch, [0.0, 0.0, 11.0])

    assert S.spawn_bridge_daemon() is False
    assert sleeps == [0.25]


def test_spawn_bridge_failed_daemon_start_releases_lock(monkeypatch, tmp_path):
    lock = tmp_path / "spawn.lock"
    lock.write_text("owner", encoding="utf-8")
    monkeypatch.setattr(S, "_acquire_spawn_lock", lambda: lock)
    monkeypatch.setattr(S, "_spawn_bridge_daemon_locked", lambda: False)

    assert S.spawn_bridge_daemon() is False
    assert not lock.exists()


def test_spawn_bridge_locked_maps_process_start_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_port_open", lambda *_args: False)
    monkeypatch.setattr(S, "_bridge_log_path", lambda: tmp_path / "bridge.log")
    monkeypatch.setattr(
        S.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("blocked")),
    )

    assert S._spawn_bridge_daemon_locked() is False


def test_spawn_bridge_quotes_dash_prefixed_instance_id(monkeypatch, tmp_path):
    commands = []
    port_checks = iter([False, True])
    monkeypatch.setattr(S, "_port_open", lambda *_args: next(port_checks))
    monkeypatch.setattr(S, "_bridge_log_path", lambda: tmp_path / "bridge.log")
    monkeypatch.setattr(S.secrets, "token_urlsafe", lambda _size: "-leading-token")
    monkeypatch.setattr(
        S.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(),
    )

    assert S._spawn_bridge_daemon_locked() is True
    assert "--instance-id=-leading-token" in commands[0]
    assert "--instance-id" not in commands[0]


@pytest.mark.parametrize(
    ("no_spawn", "port_open", "expected_spawns"),
    [(None, False, 1), ("1", False, 0), (None, True, 0)],
)
def test_get_driver_bootstrap_and_cache_branches(
    monkeypatch, no_spawn, port_open, expected_spawns
):
    calls = []

    class FakeDriver:
        def __init__(self, *, host, port):
            calls.append(("driver", host, port))

    monkeypatch.setattr(S, "_driver", None)
    monkeypatch.setattr(S, "_port_open", lambda *_args: port_open)
    monkeypatch.setattr(
        S,
        "spawn_bridge_daemon",
        lambda: calls.append(("spawn",)) or False,
    )
    monkeypatch.setattr(S, "BrowserBridge", FakeDriver)
    if no_spawn is None:
        monkeypatch.delenv("AGENT_BROWSER_NO_SPAWN", raising=False)
    else:
        monkeypatch.setenv("AGENT_BROWSER_NO_SPAWN", no_spawn)

    first = S.get_driver()
    second = S.get_driver()

    assert first is second
    assert len([call for call in calls if call[0] == "driver"]) == 1
    assert calls.count(("spawn",)) == expected_spawns


@pytest.mark.parametrize(
    ("is_remote", "no_spawn", "port_open", "expected_spawns"),
    [
        (True, None, False, 1),
        (False, None, False, 0),
        (True, "1", False, 0),
        (True, None, True, 0),
    ],
)
def test_require_driver_recovery_branches(
    monkeypatch, is_remote, no_spawn, port_open, expected_spawns
):
    driver = SimpleNamespace(is_remote=is_remote)
    calls = []
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "_port_open", lambda *_args: port_open)
    monkeypatch.setattr(
        S,
        "spawn_bridge_daemon",
        lambda: calls.append("spawn") or False,
    )
    if no_spawn is None:
        monkeypatch.delenv("AGENT_BROWSER_NO_SPAWN", raising=False)
    else:
        monkeypatch.setenv("AGENT_BROWSER_NO_SPAWN", no_spawn)

    assert S.require_driver() is driver
    assert calls == ["spawn"] * expected_spawns


def test_scan_page_returns_links_and_background_visibility_hint(monkeypatch):
    driver = _install_page_driver(monkeypatch)

    def get_html(_driver, **kwargs):
        kwargs["link_refs"]["https://example.test/long/path"] = "r1"
        return "<main>ok</main><!--abm-offscreen:4 scrollY:0 viewH:0 docH:9000-->"

    monkeypatch.setattr(S.simphtml, "get_html", get_html)

    result = S.scan_page(session_id="client:1")

    assert result["status"] == "success"
    assert result["links"] == {"r1": "https://example.test/long/path"}
    assert result["offscreen"]["viewport_height"] == 0
    assert "activate_tab" in result["hint"]
    assert driver.default_session_id == "client:old"


def test_scan_page_text_only_pins_implicit_session_and_reports_scrolling_hint(monkeypatch):
    driver = _install_page_driver(monkeypatch, default_session_id=None)
    seen = {}

    def get_html(_driver, **kwargs):
        seen.update(kwargs)
        return "text<!--abm-offscreen:3 scrollY:500 viewH:700 docH:9000-->"

    monkeypatch.setattr(S.simphtml, "get_html", get_html)

    result = S.scan_page(text_only=True)

    assert result["active_session_id"] == "client:1"
    assert "scroll_page" in result["hint"]
    assert "links" not in result
    assert seen["link_refs"] is None
    assert driver.default_session_id == "client:1"


def test_scan_page_classifies_page_unavailable_and_restores_target(monkeypatch):
    driver = _install_page_driver(monkeypatch)
    monkeypatch.setattr(
        S.simphtml,
        "get_html",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(S.simphtml.PageUnavailable("page slept")),
    )

    result = S.scan_page(session_id="client:1")

    assert result["status"] == "no_response"
    assert result["error"] == "page slept"
    assert result["active_session_id"] == "client:1"
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize(
    ("condition", "gone", "needle"),
    [
        ({"text": "ready"}, False, "innerText"),
        ({"selector": "#gone"}, True, "!("),
        ({"url_pattern": "example"}, False, "RegExp"),
        ({"js": "window.ready"}, False, "window.ready"),
    ],
)
def test_wait_for_condition_success_paths(monkeypatch, condition, gone, needle):
    driver = _install_page_driver(monkeypatch)
    scripts = []
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.1])

    def exec_js(script, **kwargs):
        scripts.append((script, kwargs))
        return {"data": json.dumps({"met": True, "url": "https://example.test/", "title": "Done"})}

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for(**condition, gone=gone, timeout=1, session_id="client:1")

    assert result["status"] == "success"
    assert result["condition"].endswith(" gone") is gone
    assert needle in scripts[0][0]
    assert scripts[0][1]["session_id"] is None
    assert driver.default_session_id == "client:old"


def test_wait_for_structured_locator_preserves_timeout_details(monkeypatch):
    _install_page_driver(monkeypatch)
    monkeypatch.setattr(S, "normalize_locator", lambda _value: {"role": "button"})
    monkeypatch.setattr(S, "locator_query_script", lambda _value: "LOCATE_BUTTON()")
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0])
    scripts = []

    def exec_js(script, **_kwargs):
        scripts.append(script)
        return {
            "data": json.dumps(
                {
                    "met": False,
                    "url": "https://example.test/",
                    "title": "Waiting",
                    "locator_status": "ambiguous",
                    "matches": 2,
                    "stage": "role",
                    "error": "two matches",
                }
            )
        }

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for(selector={"role": "button"}, gone=True, timeout=1)

    assert result["status"] == "timeout"
    assert result["locator_status"] == "ambiguous"
    assert result["matches"] == 2
    assert result["stage"] == "role"
    assert result["error"] == "two matches"
    assert "located.status === 'not_found'" in scripts[0]


def test_wait_for_retries_page_unload_then_succeeds(monkeypatch):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.2, 0.3])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    responses = iter([RuntimeError("page unloaded"), {"data": {"met": True, "url": "u"}}])

    def exec_js(*_args, **_kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(S, "exec_js", exec_js)

    assert S.wait_for(text="ready", timeout=1)["status"] == "success"


def test_wait_for_timeout_reports_repeated_page_failure(monkeypatch):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("page unavailable")),
    )

    result = S.wait_for(text="ready", timeout=1)

    assert result["status"] == "timeout"
    assert "page unavailable" in result["error"]
    assert "hint" in result


def test_wait_for_url_rejects_empty_pattern():
    with pytest.raises(ValueError, match="url_pattern"):
        S.wait_for_url("")


def test_wait_for_url_keeps_javascript_regex_syntax(monkeypatch):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.1])
    scripts = []

    def exec_js(script, **_kwargs):
        scripts.append(script)
        return {
            "data": json.dumps(
                {"met": True, "url": "https://example.test/done", "title": "Done", "ready": "complete"}
            )
        }

    monkeypatch.setattr(S, "exec_js", exec_js)
    result = S.wait_for_url(r"(?<segment>done)", timeout=1)

    assert result["status"] == "success"
    assert "(?<segment>done)" in scripts[0]
    assert "catch (_) { return location.href.includes(pattern); }" in scripts[0]


@pytest.mark.parametrize("wait_ready", [True, False])
def test_wait_for_url_success_and_ready_policy(monkeypatch, wait_ready):
    driver = _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.1])
    scripts = []

    def exec_js(script, **_kwargs):
        scripts.append(script)
        return {
            "data": json.dumps(
                {"met": True, "url": "https://example.test/done", "title": "Done", "ready": "complete"}
            )
        }

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for_url("example.test/done", timeout=1, session_id="client:1", wait_ready=wait_ready)

    assert result["status"] == "success"
    assert result["waited_for_ready"] is wait_ready
    assert ("document.readyState === 'complete'" in scripts[0]) is wait_ready
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize(
    ("info", "expected_hint"),
    [
        ({"met": False, "url": "https://elsewhere.test/", "ready": "interactive", "error": "bad regex"}, "current URL"),
        ({"met": False}, "could not read the current URL"),
    ],
)
def test_wait_for_url_timeout_hints(monkeypatch, info, expected_hint):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": info})

    result = S.wait_for_url("target", timeout=1)

    assert result["status"] == "timeout"
    assert expected_hint in result["hint"]
    if info.get("error"):
        assert result["error"] == info["error"]


def test_wait_for_url_retries_unload_and_reports_last_error(monkeypatch):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("navigation blink")),
    )

    result = S.wait_for_url("target", timeout=1)

    assert result["status"] == "timeout"
    assert "navigation blink" in result["error"]


@pytest.mark.parametrize(
    ("target", "script_marker"),
    [
        ("bottom", "scrollHeight"),
        ("top", "scrollTo(0, 0)"),
        ("125.5", "scrollTo(0, 125.5)"),
        ("#submit", "scrollIntoView"),
    ],
)
def test_scroll_page_target_modes(monkeypatch, target, script_marker):
    driver = _install_page_driver(monkeypatch)
    scripts = []

    def exec_js(script, **kwargs):
        scripts.append((script, kwargs))
        return {"data": json.dumps({"before": 1, "after": 2, "viewH": 600, "docH": 900, "atBottom": False})}

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.scroll_page(target, session_id="client:1")

    assert result == {
        "status": "success",
        "scrolled_from": 1,
        "scroll_y": 2,
        "viewport_height": 600,
        "doc_height": 900,
        "at_bottom": False,
        "moved": True,
    }
    assert script_marker in scripts[0][0]
    assert scripts[0][1]["session_id"] is None
    assert driver.default_session_id == "client:old"


def test_scroll_page_selector_not_found_and_exception_restore(monkeypatch):
    driver = _install_page_driver(monkeypatch)
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": {"__not_found": True}})

    result = S.scroll_page("#missing", session_id="client:1")
    assert result["status"] == "not_found"
    assert result["selector"] == "#missing"
    assert driver.default_session_id == "client:old"

    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bridge failed")),
    )
    with pytest.raises(RuntimeError, match="bridge failed"):
        S.scroll_page("top", session_id="client:1")
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize("value", ["", "{broken", [], 1])
def test_parse_cookies_rejects_empty_or_invalid_inputs(value):
    with pytest.raises(ValueError, match="cookies"):
        S._parse_cookies_arg(value)


def test_parse_and_normalize_cookie_accepts_aliases_and_scopes():
    assert S._parse_cookies_arg('{"name":"sid"}') == [{"name": "sid"}]
    assert S._parse_cookies_arg({"name": "sid"}) == [{"name": "sid"}]

    normalized = S._normalize_cookie(
        {
            "name": "sid",
            "value": "value",
            "domain": ".example.test",
            "path": "/app",
            "http_only": 1,
            "secure": True,
            "expirationDate": "123.5",
            "same_site": "no_restriction",
        },
        0,
    )

    assert normalized == {
        "name": "sid",
        "value": "value",
        "domain": ".example.test",
        "path": "/app",
        "httpOnly": True,
        "secure": True,
        "expires": 123.5,
        "sameSite": "None",
    }


@pytest.mark.parametrize(
    ("cookie", "message"),
    [
        ("not-an-object", "must be an object"),
        ({}, "missing name"),
        ({"name": "bad name"}, "invalid separator"),
        ({"name": "sid", "value": "bad;value"}, "value"),
        ({"name": "sid", "expires": "later"}, "expires"),
        ({"name": "sid", "sameSite": "sometimes"}, "sameSite"),
        ({"name": "sid", "sameSite": "None"}, "secure=true"),
    ],
)
def test_normalize_cookie_rejects_unsafe_values(cookie, message):
    with pytest.raises(ValueError, match=message):
        S._normalize_cookie(cookie, 3)


def test_page_location_and_document_cookie_parse_string_or_object(monkeypatch):
    responses = iter(
        [
            {"data": json.dumps({"url": "https://example.test/", "host": "example.test"})},
            {"data": {"ok": True}},
            {"data": "[]"},
        ]
    )
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: next(responses))

    assert S._page_location()["host"] == "example.test"
    assert S._cookie_via_document({"name": "sid", "value": "1"}, None, 2) == {"ok": True}
    assert S._cookie_via_document({"name": "sid", "value": "1"}, None, 2) == {}


def test_set_cookies_success_partial_and_page_scope(monkeypatch):
    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {"url": "https://example.test/app"})
    replies = iter([{}, {"success": False}])
    calls = []

    def cdp(method, params, session_id, tab_id, timeout):
        calls.append((method, params, session_id, tab_id, timeout))
        return next(replies)

    monkeypatch.setattr(S, "_cdp", cdp)

    result = S.set_cookies(
        [
            {"name": "page", "value": "1"},
            {"name": "domain", "value": "2", "domain": ".example.test"},
        ],
        session_id="client:1",
    )

    assert result["status"] == "partial"
    assert result["set"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["scoped_to"] == "https://example.test/app"
    assert "hint" in result
    assert calls[0][1]["url"] == "https://example.test/app"


def test_set_cookies_requires_page_url_for_implicit_scope(monkeypatch):
    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {})

    with pytest.raises(RuntimeError, match="URL"):
        S.set_cookies({"name": "sid"})


@pytest.mark.parametrize(
    ("fallback", "expected_status", "expect_note"),
    [
        ({"ok": True}, "ok", True),
        ({"ok": False, "error": "blocked"}, "failed", False),
        (RuntimeError("fallback failed"), "failed", False),
    ],
)
def test_set_cookies_cdp_fallbacks_are_explicit(monkeypatch, fallback, expected_status, expect_note):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))

    def document_cookie(*_args, **_kwargs):
        if isinstance(fallback, Exception):
            raise fallback
        return fallback

    monkeypatch.setattr(S, "_cookie_via_document", document_cookie)

    result = S.set_cookies(
        {"name": "sid", "httpOnly": True, "url": "https://example.test/"}
    )

    entry = result["results"][0]
    assert entry["status"] == expected_status
    assert entry["method"] == "document.cookie"
    assert entry["httpOnly_dropped"] is True
    assert ("note" in entry) is expect_note


def test_set_cookies_does_not_fallback_into_wrong_named_tab(monkeypatch):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))
    monkeypatch.setattr(
        S,
        "_cookie_via_document",
        lambda *_args, **_kwargs: pytest.fail("must not write in the default tab"),
    )

    result = S.set_cookies({"name": "sid", "url": "https://example.test/"}, tab_id=7)

    assert result["status"] == "failed"
    assert "tab_id=7" in result["results"][0]["error"]


def test_delete_cookies_success_and_page_scope(monkeypatch):
    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {"url": "https://example.test/app"})
    calls = []
    monkeypatch.setattr(S, "_cdp", lambda *args: calls.append(args) or {})

    result = S.delete_cookies(" sid ", path="/app")

    assert result["status"] == "ok"
    assert result["scope"] == {"path": "/app", "url": "https://example.test/app"}
    assert calls[0][1]["name"] == "sid"


def test_delete_cookies_validation_and_missing_page_url(monkeypatch):
    with pytest.raises(ValueError, match="name"):
        S.delete_cookies("")

    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {})
    with pytest.raises(RuntimeError, match="URL"):
        S.delete_cookies("sid")


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [({"gone": True}, "ok"), ({"gone": False}, "failed"), (RuntimeError("js failed"), "failed")],
)
def test_delete_cookies_document_fallback(monkeypatch, response, expected_status):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))

    def exec_js(*_args, **_kwargs):
        if isinstance(response, Exception):
            raise response
        return {"data": json.dumps(response)}

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.delete_cookies("sid", url="https://example.test/")

    assert result["status"] == expected_status
    assert result["method"] == "document.cookie"


def test_delete_cookies_named_tab_refuses_document_fallback(monkeypatch):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: pytest.fail("must not run in default tab"))

    result = S.delete_cookies("sid", url="https://example.test/", tab_id=9)

    assert result["status"] == "failed"
    assert "tab_id=9" in result["error"]


@pytest.mark.parametrize("area", ["", "indexeddb"])
def test_storage_area_rejects_unknown_values(area):
    with pytest.raises(ValueError, match="area"):
        S._storage_area(area)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"offset": True},
        {"offset": -1},
        {"max_items": 0},
        {"max_items": True},
        {"max_bytes": 0},
        {"max_bytes": True},
    ],
)
def test_storage_get_validates_bounds(kwargs):
    with pytest.raises(ValueError):
        S.storage_get(**kwargs)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": True, "found": True, "value": "v"}, {"status": "success", "found": True, "value": "v"}),
        ({"ok": True, "found": False, "value": None}, {"status": "success", "found": False, "note": True}),
        ({"ok": False, "error": "blocked"}, {"status": "error", "error_code": "storage_unavailable"}),
        ([], {"status": "error", "error_code": "invalid_response"}),
    ],
)
def test_storage_get_key_results(monkeypatch, payload, expected):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": json.dumps(payload)})

    result = S.storage_get("key", area="session")

    for key, value in expected.items():
        if key == "note":
            assert "note" in result
        else:
            assert result[key] == value


@pytest.mark.parametrize("truncated", [True, False])
def test_storage_get_dump_pagination(monkeypatch, truncated):
    payload = {
        "ok": True,
        "items": {"a": "1"},
        "total_keys": 3,
        "bytes": 2,
        "truncated": truncated,
        "next_offset": 1,
    }
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": payload})

    result = S.storage_get(offset=0, max_items=1, max_bytes=10)

    assert result["status"] == "success"
    assert result["truncated"] is truncated
    assert ("next_offset" in result) is truncated


@pytest.mark.parametrize(
    ("error", "code", "delivery_state", "retry_safe"),
    [
        (
            S.BridgeNoResponseError(
                "bridge did not answer",
                delivery_state="delivered_no_result",
                retry_safe=False,
            ),
            "no_response",
            "delivered_no_result",
            True,
        ),
        (TimeoutError("late"), "timeout", "unknown", True),
        (RuntimeError("socket closed"), "bridge_error", "unknown", True),
    ],
)
def test_storage_get_classifies_transport_errors(
    monkeypatch, error, code, delivery_state, retry_safe
):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = S.storage_get("key")

    assert result["status"] == "error"
    assert result["error_code"] == code
    assert result["delivery_state"] == delivery_state
    assert result["retry_safe"] is retry_safe
    assert result["retryable"] is retry_safe


def test_storage_set_success_serializes_non_string(monkeypatch):
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: {"data": {"ok": True, "existed": True, "keys": 4}},
    )

    result = S.storage_set("settings", {"dark": True}, area="localstorage")

    assert result["status"] == "success"
    assert result["replaced"] is True
    assert result["total_keys"] == 4
    assert "JSON" in result["note"]


@pytest.mark.parametrize("key", ["", 1])
def test_storage_set_requires_nonempty_string_key(key):
    with pytest.raises(ValueError, match="key"):
        S.storage_set(key, "value")


@pytest.mark.parametrize("payload", [{"ok": False, "error": "quota"}, [], None])
def test_storage_set_reports_unconfirmed_write(monkeypatch, payload):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": payload})

    result = S.storage_set("key", "value")

    assert result["status"] == "failed"
    assert "hint" in result


@pytest.mark.parametrize(
    ("error", "code", "delivery_state", "retry_safe"),
    [
        (
            S.BridgeNoResponseError(
                "bridge did not answer",
                delivery_state="undelivered",
                retry_safe=True,
            ),
            "no_response",
            "undelivered",
            True,
        ),
        (
            S.BridgeNoResponseError(
                "bridge did not answer",
                delivery_state="delivered_no_result",
                retry_safe=False,
            ),
            "no_response",
            "delivered_no_result",
            False,
        ),
        (TimeoutError("late"), "timeout", "unknown", False),
        (RuntimeError("bridge failed"), "bridge_error", "unknown", False),
    ],
)
def test_storage_set_classifies_transport_errors(
    monkeypatch, error, code, delivery_state, retry_safe
):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = S.storage_set("key", "value")

    assert result["status"] == "error"
    assert result["error_code"] == code
    assert result["delivery_state"] == delivery_state
    assert result["retry_safe"] is retry_safe
    assert result["retryable"] is retry_safe


@pytest.mark.parametrize(
    "kwargs",
    [
        {"format": "gif"},
        {"format": "png", "quality": 80},
        {"format": "jpeg", "quality": True},
        {"format": "jpeg", "quality": 101},
        {"full_page": True, "clip": {"x": 0, "y": 0, "width": 1, "height": 1}},
        {"clip": "bad"},
        {"clip": {"x": 0, "y": 0, "width": 1, "height": 1, "extra": 1}},
        {"clip": {"x": 0, "y": 0, "width": 1}},
        {"clip": {"x": 0, "y": 0, "width": 0, "height": 1}},
        {"clip": {"x": 0, "y": 0, "width": 1, "height": 1, "scale": 0}},
        {"clip": {"x": float("nan"), "y": 0, "width": 1, "height": 1}},
        {"clip": {"x": 0, "y": float("inf"), "width": 1, "height": 1}},
        {"clip": {"x": 0, "y": 0, "width": float("-inf"), "height": 1}},
    ],
)
def test_capture_page_screenshot_validates_options(kwargs):
    with pytest.raises(ValueError):
        S.capture_page_screenshot(**kwargs)


class _ScreenshotDriver:
    def __init__(self, response, default_session_id="client:old"):
        self.response = response
        self.default_session_id = default_session_id
        self.calls = []

    def ext_cmd(self, payload, client_id=None, timeout=20.0):
        self.calls.append((payload, client_id, timeout))
        return self.response


def _install_screenshot(monkeypatch, response):
    driver = _ScreenshotDriver(response)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": "client:7"}])

    def switch_session(session_id=None, **_kwargs):
        sid = str(session_id) if session_id is not None else "client:7"
        driver.default_session_id = sid
        return sid

    monkeypatch.setattr(S, "switch_session", switch_session)
    return driver


def test_capture_page_screenshot_jpeg_clip_and_nested_payload(monkeypatch):
    raw = b"small image"
    b64 = base64.b64encode(raw).decode("ascii")
    driver = _install_screenshot(monkeypatch, {"data": {"data": b64}})

    result = S.capture_page_screenshot(
        session_id="client:7",
        format="jpg",
        clip={"x": 1, "y": 2, "width": 3, "height": 4},
        quality=80,
        return_base64=True,
    )

    assert result.structuredContent["format"] == "jpeg"
    assert result.structuredContent["base64"] == b64
    params = driver.calls[0][0]["params"]
    assert params["quality"] == 80
    assert params["clip"]["scale"] == 1.0
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize("payload", [None, "", {"data": ""}, "not-base64!"])
def test_capture_page_screenshot_rejects_missing_or_invalid_image(monkeypatch, payload):
    _install_screenshot(monkeypatch, {"data": payload})

    with pytest.raises(RuntimeError, match="Screenshot failed"):
        S.capture_page_screenshot()


def test_capture_page_screenshot_full_page_and_session_mismatch(monkeypatch):
    b64 = base64.b64encode(b"image").decode("ascii")
    driver = _install_screenshot(monkeypatch, {"data": b64})

    result = S.capture_page_screenshot(full_page=True, format="webp")
    assert result.structuredContent["full_page"] is True
    assert driver.calls[0][0]["fullPage"] is True

    with pytest.raises(ValueError, match="does not match"):
        S.capture_page_screenshot(session_id="client:7", tab_id=8)
    assert driver.default_session_id == "client:7"
