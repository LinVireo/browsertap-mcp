"""Offline contract tests for structured page locators."""

from __future__ import annotations

import json

import pytest

from browsertap_mcp import server as S
from browsertap_mcp.page_input import (
    InputValidationError,
    locator_query_script,
    normalize_locator,
    resolve_selector_script,
    structured_locator_script,
)


class _Driver:
    default_session_id = "chrome:test:7"


def test_css_locator_keeps_legacy_value_and_structured_fields_normalize():
    assert normalize_locator("button.submit") == "button.submit"
    assert normalize_locator(
        {
            "role": "button",
            "name": "Pay now",
            "exact": True,
            "frame": [{"css": "iframe.checkout"}],
            "shadow": ["checkout-shell"],
        }
    ) == {
        "role": "button",
        "name": "Pay now",
        "exact": True,
        "frame": [{"css": "iframe.checkout"}],
        "shadow": ["checkout-shell"],
    }


@pytest.mark.parametrize(
    "locator, message",
    [
        ({"css": "button", "text": "Pay"}, "exactly one"),
        ({"role": "button", "exact": "yes"}, "boolean"),
        ({"text": "Pay", "name": "wrong"}, "only valid with role"),
        ({"label": "Email", "frame": []}, "frame"),
        ({"css": "button", "shadow": [""]}, "shadow"),
    ],
)
def test_structured_locator_rejects_unsafe_or_ambiguous_definitions(locator, message):
    with pytest.raises(InputValidationError, match=message):
        normalize_locator(locator)


def test_locator_script_contains_escaped_structured_query():
    locator = {"role": "button", "name": 'Pay "now"', "exact": True}
    script = structured_locator_script(locator, purpose="click")
    assert json.dumps(locator, ensure_ascii=False) in script
    assert "status:'ambiguous'" in script
    assert "status:'cross_origin_frame'" in script
    assert locator_query_script(locator).startswith("(() =>")


def test_click_resolvers_reject_disabled_targets_without_changing_query_semantics():
    click_script = resolve_selector_script("button", require_interactable=True)
    query_script = locator_query_script("button")

    assert "const requireInteractable = true" in click_script
    assert "const requireInteractable = false" in query_script
    assert "aria-disabled" in click_script
    assert "frameOffsetX + rect.left" in structured_locator_script(
        {"css": "button", "frame": [{"css": "iframe"}]}, purpose="click"
    )


def test_page_click_ambiguous_locator_dispatches_nothing(monkeypatch):
    driver = _Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": "chrome:test:7"}])
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: session_id or driver.default_session_id)
    monkeypatch.setattr(
        S,
        "_page_selector_info",
        lambda *args, **kwargs: {"found": False, "status": "ambiguous", "matches": 2},
    )
    monkeypatch.setattr(S, "_clear_page_challenge", lambda *_args: None)
    monkeypatch.setattr(S, "_run_page_input", lambda *_args, **_kwargs: pytest.fail("input dispatched"))

    result = S.page_click(
        selector={"role": "button", "name": "Pay", "exact": True},
        session_id="chrome:test:7",
    )

    assert result["status"] == "ambiguous"
    assert result["matches"] == 2


def test_page_type_unusable_locator_dispatches_nothing(monkeypatch):
    driver = _Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": "chrome:test:7"}])
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: session_id or driver.default_session_id)
    monkeypatch.setattr(
        S,
        "_page_type_target_info",
        lambda *args, **kwargs: {"found": False, "status": "not_interactable", "targetKind": "unusable"},
    )
    monkeypatch.setattr(S, "_run_page_input", lambda *_args, **_kwargs: pytest.fail("input dispatched"))

    result = S.page_type(
        "secret",
        selector={"label": "Email"},
        session_id="chrome:test:7",
    )

    assert result["status"] == "not_interactable"
    assert result["typed_chars"] == 0


def test_wait_for_structured_locator_returns_success_without_polling(monkeypatch):
    driver = _Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda: None)
    seen = []

    def fake_exec_js(script, **kwargs):
        seen.append(script)
        return {"data": json.dumps({"met": True, "url": "https://example.test/", "title": "Ready"})}

    monkeypatch.setattr(S, "exec_js", fake_exec_js)
    result = S.wait_for(selector={"text": "Ready", "exact": True}, timeout=1)

    assert result["status"] == "success"
    assert seen and '"text": "Ready"' in seen[0]
