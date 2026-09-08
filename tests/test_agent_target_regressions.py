from __future__ import annotations

import pytest

from browsertap_mcp import server as S
from browsertap_mcp.browser_bridge import ExtensionNotConnectedError


class _CreateDriver:
    def __init__(self):
        self.default_session_id = None
        self.calls = []
        self.sessions = [
            {"id": "chrome:alpha:1", "browser": "chrome", "url": "https://chrome.test/"},
            {"id": "edge:beta:2", "browser": "edge", "url": "https://edge.test/"},
        ]

    def resolve_session_target(self, session_id):
        if any(item["id"] == session_id for item in self.sessions):
            return {"session_id": session_id}
        return None

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload["method"], client_id))
        return {
            "client_id": client_id or "chrome:alpha",
            "data": {
                "operation_id": payload["operation_id"],
                "operation_status": "not_found",
            },
        }

    def newtab(self, *, url, client_id, timeout, active, operation_id):
        self.calls.append(("create", client_id))
        self.sessions.append({
            "id": f"{client_id}:99",
            "generation": "created-generation",
            "url": url,
        })
        return {
            "client_id": client_id,
            "data": {
                "id": 99,
                "client_id": client_id,
                "generation": "created-generation",
                "url": url,
                "operation_id": operation_id,
                "operation_status": "completed",
            },
        }


@pytest.fixture
def create_driver(monkeypatch):
    driver = _CreateDriver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda **kwargs: list(driver.sessions))
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", S._TabOwnershipRegistry())
    return driver


@pytest.mark.parametrize(
    ("target", "expected_client"),
    [({}, "edge:beta"), ({"session_id": "chrome:alpha:1"}, "chrome:alpha"),
     ({"client_id": "chrome:alpha"}, "chrome:alpha")],
)
def test_create_uses_this_tasks_selected_browser(create_driver, target, expected_client):
    S.switch_tab(session_id="edge:beta:2")

    result = S.open_new_tab("https://new.test/", timeout=0.5, **target)

    assert result["session_id"] == f"{expected_client}:99"
    assert create_driver.calls == [
        ("create_status", expected_client), ("create", expected_client),
    ]
    assert create_driver.default_session_id == "edge:beta:2"


def test_create_keeps_selected_browser_when_it_disconnects(create_driver, monkeypatch):
    S.switch_tab(session_id="edge:beta:2")

    def disconnected(payload, client_id=None, timeout=15.0):
        create_driver.calls.append((payload["method"], client_id))
        raise ExtensionNotConnectedError("selected Edge client disconnected")

    monkeypatch.setattr(create_driver, "ext_cmd", disconnected)

    result = S.open_new_tab("https://new.test/", timeout=0.5)

    assert result["status"] == "unknown"
    assert result["client_id"] == "edge:beta"
    assert result["may_have_created"] is False
    assert create_driver.calls == [("create_status", "edge:beta")]


@pytest.mark.parametrize("failure", ["timeout", "disconnected", "wrong_client", "malformed"])
def test_recovery_probe_failure_preserves_create_uncertainty(
    create_driver, monkeypatch, failure,
):
    def failed_probe(payload, client_id=None, timeout=15.0):
        create_driver.calls.append((payload["method"], client_id))
        if failure == "timeout":
            raise TimeoutError("recovery status ACK lost")
        if failure == "disconnected":
            raise ExtensionNotConnectedError("browser disconnected during recovery")
        return {
            "client_id": "edge:beta" if failure == "wrong_client" else client_id,
            "data": {},
        }

    monkeypatch.setattr(create_driver, "ext_cmd", failed_probe)

    result = S.open_new_tab(
        "https://existing.test/", timeout=0.5, operation_id="previously-dispatched",
        client_id="chrome:alpha", owner_id="existing-owner",
    )

    assert result["status"] == "unknown"
    assert result["may_have_created"] is True
    assert result["retry_safe"] is False
    assert result["owner_id"] == "existing-owner"
    assert result["recovery"]["operation_id"] == "previously-dispatched"
    assert result["recovery"]["client_id"] == "chrome:alpha"
    assert result["recovery"]["owner_id"] == "existing-owner"
    assert create_driver.calls == [("create_status", "chrome:alpha")]


def test_new_create_probe_timeout_still_reports_no_mutation(create_driver, monkeypatch):
    def timed_out(payload, client_id=None, timeout=15.0):
        create_driver.calls.append((payload["method"], client_id))
        raise TimeoutError("initial status ACK lost")

    monkeypatch.setattr(create_driver, "ext_cmd", timed_out)

    result = S.open_new_tab("https://new.test/", timeout=0.5, client_id="chrome:alpha")

    assert result["status"] == "unknown"
    assert result["may_have_created"] is False
    assert result["retry_safe"] is True
    assert result["owner_id"] is None
    assert "recovery" not in result
    assert create_driver.calls == [("create_status", "chrome:alpha")]
