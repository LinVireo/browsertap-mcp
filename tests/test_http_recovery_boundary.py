"""HTTP recovery and failure replies preserve typed protocol outcomes."""

from __future__ import annotations

import json
import threading

import pytest

from browsertap_mcp import browser_bridge as B
from tests.test_browser_bridge_coverage import driver_stub, wsgi_post
from tests.test_browser_bridge_coverage import http_app as http_app


@pytest.mark.parametrize("outcome", ["none", "replacement", "failure"])
def test_http_session_resolution_preserves_missing_replaced_and_failed_states(http_app, outcome):
    def resolve(sid):
        if outcome == "failure":
            raise RuntimeError("resolution unavailable")
        return None if outcome == "none" else {"session_id": "browser:8", "rebound_from": sid}
    http_app._resolve_local_session_target = resolve
    response = wsgi_post(http_app.app, "/link", {"cmd": "resolve_session", "sessionId": "browser:7"})
    result = json.loads(response["body"])["r"]
    assert response["status"] == 200
    if outcome == "failure":
        assert result["error_code"] == "internal_error"
        assert "resolution unavailable" in result["error"]
    elif outcome == "none":
        assert result is None
    else:
        assert result == {"session_id": "browser:8", "rebound_from": "browser:7"}


def test_http_resolution_without_a_target_does_not_invent_one(http_app):
    http_app._resolve_local_session_target = lambda sid: pytest.fail("no target was provided")
    response = wsgi_post(http_app.app, "/link", {"cmd": "resolve_session"})
    assert json.loads(response["body"]) == {"r": None}


@pytest.mark.parametrize("code,exception", [
    ("ambiguous_browser", B.AmbiguousBrowserError),
    ("session_disconnected", B.SessionDisconnectedError),
    ("extension_not_connected", B.ExtensionNotConnectedError),
])
def test_remote_errors_keep_their_specific_exception_and_diagnostics(code, exception):
    with pytest.raises(exception) as caught:
        B._raise_remote_error({
            "error_code": code, "error": "unavailable", "diagnostics": {"candidate_client_ids": ["a", "b"]},
        })
    assert caught.value.error_code == code
    assert caught.value.diagnostics["candidate_client_ids"] == ["a", "b"]


@pytest.mark.parametrize("response", [None, [], "invalid"])
def test_remote_session_resolution_rejects_malformed_success(response):
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *a, **kw: {"r": response}
    if response is None:
        assert driver.resolve_session_target("browser:7") is None
    else:
        with pytest.raises(RuntimeError, match="malformed"):
            driver.resolve_session_target("browser:7")


def test_closed_replacement_is_not_reused_for_session_resolution():
    driver = driver_stub()
    driver._rebindings = {"browser:7": {"replacement_session_id": "browser:8"}}
    assert driver.resolve_session_target("browser:7") is None
    assert driver._rebindings == {}


def test_result_publication_between_snapshot_and_wait_is_observed(monkeypatch):
    driver = driver_stub()
    driver._activity_condition = threading.Condition()
    driver._activity_serial = 2
    monkeypatch.setattr(driver._activity_condition, "wait", lambda *a: pytest.fail("already changed; must not sleep"))
    assert driver._wait_for_activity(1, 1) == 2
    assert driver._wait_for_activity(2, 0) == 2


def test_page_injection_refusal_is_retryable_only_with_explicit_non_dispatch_proof():
    code, retry, diagnostics = B._page_execution_metadata({
        "message": "injection refused", "dispatched": False, "may_have_executed": False,
    })
    assert code == "page_execution_failed" and retry is True
    assert diagnostics["retry_safe"] is True
    _, retry, diagnostics = B._page_execution_metadata({"message": "unknown failure"})
    assert retry is False and diagnostics["retry_safe"] is False
