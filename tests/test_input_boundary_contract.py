"""Invalid locators and focus changes must stop before any input dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from browsertap_mcp import page_input as P
from browsertap_mcp import server as S


@pytest.mark.parametrize("locator", [
    "", None, {"unknown": "x"}, {"x": 1, "frame": "iframe"},
    {"x": 1, "y": 2, "frame": "iframe", "css": "button"},
    {"x": 1, "y": 2, "frame": "iframe", "shadow": "host"},
    {"x": 1, "y": 2}, {"selector": "button", "css": "a"},
    {"css": "button", "text": "x"}, {"css": "a", "name": "name"},
    {"text": "x", "exact": 1}, {"label": "x", "exact": True},
    {"css": "a", "frame": []}, {"css": "a", "frame": {"css": "iframe", "frame": "inner"}},
    {"css": "a", "frame": {"x": 1, "y": 2, "frame": "iframe"}},
    {"css": "a", "shadow": []},
])
def test_invalid_locators_are_rejected_before_script_construction(locator):
    with pytest.raises(P.InputValidationError):
        P.normalize_locator(locator)


def test_shadow_and_frame_aliases_keep_exact_target_structure():
    assert P.normalize_locator({"selector": "#submit", "shadow": "app-root", "frame": ["iframe"]}) == {
        "css": "#submit", "shadow": ["app-root"], "frame": [{"css": "iframe"}],
    }


@pytest.mark.parametrize("options", [
    {"purpose": "unknown"}, {"select_all": 1}, {"verify_hit": "true"},
    {"center_x": 1}, {"center_y": None},
])
def test_structured_resolver_requires_typed_modes_and_flags(options):
    with pytest.raises(P.InputValidationError):
        P.structured_locator_script({"css": "button"}, **options)


@pytest.mark.parametrize("options", [{"purpose": "query"}, {"purpose": "click", "verify_hit": True}])
def test_frame_points_do_not_silently_acquire_element_semantics(options):
    with pytest.raises(P.InputValidationError):
        P.structured_locator_script({"x": 1, "y": 2, "frame": "iframe"}, **options)


@pytest.mark.parametrize("selector,options", [("", {}), (None, {}), ("button", {"center_x": 1})])
def test_css_resolver_rejects_missing_selector_or_invalid_flags(selector, options):
    with pytest.raises(P.InputValidationError):
        P.resolve_selector_script(selector, **options)


@pytest.mark.parametrize("call,args,options", [
    (P._key_details, (None,), {}), (P._key_details, ("unsupported-key",), {}),
    (P.press_commands, (None,), {}), (P.press_commands, ("ctrl,control,a",), {}),
    (P.type_commands, ("input", None), {}),
    (P.type_commands, ("input", "x"), {"submit_delay_ms": True}),
    (P.type_target_script, (None,), {}), (P.type_target_script, ("input",), {"select_all": 1}),
    (P.ChallengeAttemptTracker, (), {"max_attempts": 0}),
    (P.ChallengeAttemptTracker, (), {"window_seconds": 0}),
])
def test_invalid_input_configuration_has_a_validation_error(call, args, options):
    with pytest.raises(P.InputValidationError):
        call(*args, **options)


def test_challenge_tracker_requires_an_attributable_session_and_marker():
    tracker = P.ChallengeAttemptTracker()
    for session, marker in [("", "marker"), ("chrome:7", "")]:
        with pytest.raises(P.InputValidationError):
            tracker.record(session, marker)
    with pytest.raises(P.InputValidationError):
        tracker.clear("")


@pytest.fixture
def typing(monkeypatch):
    now = [0.0]
    driver = SimpleNamespace(default_session_id=None)
    driver.ext_cmd = lambda *a, **kw: {"data": {"capabilities": {"batch_result_guard": True}}}
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **kw: [{"id": "chrome:7", "url": "https://example.test/"}])
    monkeypatch.setattr(S, "_page_type_target_info", lambda *a: {"found": True, "focusConfirmed": True})
    calls = []
    monkeypatch.setattr(S, "_run_page_input", lambda *a, **kw: calls.append((a, kw)) or {"status": "success"})
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    return driver, now, calls


@pytest.mark.parametrize("arguments", [
    {"text": None}, {"text": "x", "selector": None},
    {"text": "x", "clear": 1}, {"text": "x", "submit_key": None},
])
def test_typing_validates_before_target_resolution(typing, arguments):
    _, _, calls = typing
    with pytest.raises(P.InputValidationError):
        S.page_type(**arguments)
    assert calls == []


@pytest.mark.parametrize("default", [None, "chrome:1", "chrome:7"])
def test_implicit_typing_chooses_a_live_target_within_its_browser(typing, default):
    driver, _, calls = typing
    driver.default_session_id = default
    result = S.page_type("typed", selector={"css": "input"})
    assert result["status"] == "success"
    assert result["typed_chars"] == 5
    assert calls[0][0][1] == "chrome:7"


def test_typing_never_replaces_an_explicit_missing_target(typing):
    _, _, calls = typing
    with pytest.raises(Exception, match="chrome:8"):
        S.page_type("x", session_id="chrome:8")
    assert calls == []


@pytest.mark.parametrize("stage", [
    "before_sessions", "sessions", "before_resolver", "resolver", "capability", "xterm",
])
def test_typing_deadline_is_checked_before_every_dispatch_stage(typing, monkeypatch, stage):
    driver, now, calls = typing
    if stage in {"before_sessions", "before_resolver"}:
        ticks = iter([0, 2] if stage == "before_sessions" else [0, 0, 0, 2])
        monkeypatch.setattr(S.time, "monotonic", lambda: next(ticks, 2))
    elif stage == "sessions":
        def slow(**kw):
            now[0] = 2
            return [{"id": "chrome:7"}]
        monkeypatch.setattr(S, "ensure_sessions", slow)
    elif stage in {"resolver", "xterm"}:
        def slow(*a):
            now[0] = 2 if stage == "resolver" else 0.95
            return {"found": True, "targetKind": "xterm"}
        monkeypatch.setattr(S, "_page_type_target_info", slow)
    else:
        def slow(*a, **kw):
            now[0] = 2
            return {"data": {"capabilities": {"batch_result_guard": True}}}
        driver.ext_cmd = slow
    with pytest.raises(TimeoutError):
        S.page_type("x", submit_key="enter", timeout=1, session_id="chrome:7")
    assert calls == []


@pytest.mark.parametrize("stage", ["target_resolution", "input_dispatch"])
def test_clicking_stops_when_the_shared_deadline_expires(typing, monkeypatch, stage):
    driver, now, calls = typing
    driver.default_session_id = "chrome:1"
    resolutions = []

    def resolve_session(*args, **kwargs):
        if stage == "target_resolution":
            now[0] = 2
        return "chrome:7"

    def resolve_selector(*args, **kwargs):
        resolutions.append(args)
        now[0] = 2
        return {"found": True, "x": 1, "y": 2, "width": 10, "height": 10}

    monkeypatch.setattr(S, "_resolve_page_input_session", resolve_session)
    monkeypatch.setattr(S, "_page_selector_info", resolve_selector)
    monkeypatch.setattr(S, "_clear_page_challenge", lambda *args: None)

    with pytest.raises(TimeoutError, match=stage.replace("_", " ")):
        S.page_click(selector="button", session_id="chrome:7", timeout=1)

    assert calls == []
    assert len(resolutions) == (stage == "input_dispatch")
    assert driver.default_session_id == "chrome:1"


def test_non_guard_input_failure_is_preserved(typing, monkeypatch):
    def failed(*a, **kw):
        raise S.PageExecutionError({"code": "cdp_timeout", "message": "timed out"})
    monkeypatch.setattr(S, "_run_page_input", failed)
    with pytest.raises(S.PageExecutionError):
        S.page_type("x", session_id="chrome:7")


@pytest.mark.parametrize("raw", ["invalid-json", [], None])
@pytest.mark.parametrize("kind", ["click", "type"])
def test_invalid_resolver_response_is_never_treated_as_a_target(monkeypatch, raw, kind):
    monkeypatch.setattr(S, "exec_js", lambda *a, **kw: {"data": raw})
    with pytest.raises(RuntimeError, match="resolver returned"):
        if kind == "click":
            S._page_selector_info("button", 0, 0, "chrome:7", 1)
        else:
            S._page_type_target_info("input", False, "chrome:7", 1)
