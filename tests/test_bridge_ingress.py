"""Exercise untrusted frames through the actual bridge handlers, without sockets."""

import copy
import json
import time
from unittest.mock import MagicMock

import pytest

from browsertap_mcp import browser_bridge as T
from tests.test_browser_bridge_coverage import (
    FakeSocket,
    driver_stub,
    ext_ready,
    ws_handler_for,
    ws_peer,
    wsgi_post,
)
from tests.test_browser_bridge_coverage import http_app as http_app


@pytest.mark.parametrize("patch", [
    {"clientId": []}, {"clientId": {}}, {"clientId": True},
    {"browser": []}, {"tabs": None}, {"tabs": {}},
    {"tabs": [{"id": 8}, None]},
    {"tabs": [{"id": 8}, {"id": "bad"}]},
    {"tabs": [{"id": 8}, {"id": True}]},
    {"tabs": [{"id": 8}, {"id": 1.5}]},
    {"tabs": [{"id": 8}, {"id": -1}]},
    {"tabs": [{"id": 8}, {"id": 8}]},
    {"tabs": [{"id": 8}, {"id": 9, "url": []}]},
    {"tabs": [{"id": 8}, {"id": 9, "generation": {}}]},
])
def test_bad_snapshot_cannot_publish_any_state(monkeypatch, caplog, patch):
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    peer = ws_peer(handler, ext_ready())
    peer.handle()
    driver._operation_state().reserve("pending", ["chrome:7"], "owner")
    before = (
        copy.deepcopy(driver.sessions["chrome:7"].info),
        dict(driver.ext_clients["chrome"]),
        copy.deepcopy(driver.client_last_seen),
        driver.last_ext_seen,
    )
    peer.data = json.dumps({**ext_ready(), **patch})

    peer.handle()

    assert set(driver.sessions) == {"chrome:7"}
    assert driver.sessions["chrome:7"].is_active()
    assert (
        driver.sessions["chrome:7"].info,
        driver.ext_clients["chrome"],
        driver.client_last_seen,
        driver.last_ext_seen,
    ) == before
    assert driver._operation_state().read("pending", "owner")["reservation_held"]
    assert "Error handling WebSocket message" not in caplog.text


@pytest.mark.parametrize("patch", [
    {"tabId": "bad"}, {"tabId": True}, {"tabId": 1.5},
    {"newTabs": {}}, {"newTabs": [None]},
    {"newTabs": [{"id": -1}]},
])
def test_bad_reply_cannot_complete_or_replace_a_valid_result(monkeypatch, patch):
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    peer = ws_peer(handler, ext_ready())
    peer.handle()
    driver._operation_state().reserve(
        "owned", ["chrome:7"], "owner", reply_transport="ws", reply_owner=peer,
    )
    peer.data = json.dumps({"type": "result", "id": "owned", "result": 42, **patch})

    peer.handle()

    assert driver.results == {}
    assert driver._operation_state().read("owned", "owner")["reservation_held"]
    peer.data = json.dumps({"type": "result", "id": "owned", "result": 42, "tabId": 7})
    peer.handle()
    assert driver.get_execute_js_result("owned", requester_id="owner")["data"] == 42


@pytest.mark.parametrize("session_id", [[], {}, True, 1, "", "   "])
def test_bad_legacy_registration_cannot_mutate_sessions(monkeypatch, caplog, session_id):
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    peer = ws_peer(handler, {"type": "ready", "sessionId": session_id})
    peer.handle()
    assert driver.sessions == {}
    assert "Error handling WebSocket message" not in caplog.text


@pytest.mark.parametrize("stamp", [None, "bad", float("nan"), float("inf"), [], {}])
def test_cleanup_discards_invalid_timestamps_without_losing_live_operations(stamp):
    driver = driver_stub()
    driver.sessions["chrome:7"] = T.Session("chrome:7", {"type": "ws"}, FakeSocket())
    driver._rebindings = {"old": {"replacement_session_id": "chrome:7", "ts": stamp}}
    driver.results["bad"] = {"ts": stamp}
    driver.acks["bad"] = stamp
    driver.client_last_seen["old"] = {"ts": stamp}
    driver._operation_state().reserve("pending", ["chrome:7"], "owner")
    driver.acks["pending"] = time.time()

    driver.clean_sessions()

    assert driver._rebindings == {}
    assert "bad" not in driver.results
    assert "bad" not in driver.acks
    assert "old" not in driver.client_last_seen
    assert driver._operation_state().read("pending", "owner")["reservation_held"]
    assert "pending" in driver.acks


@pytest.mark.parametrize("patch", [
    {"sessionId": []}, {"sessionId": {}}, {"sessionId": True},
    {"sessionId": 7}, {"sessionId": "   "}, {"url": []}, {"title": {}},
])
def test_bad_http_registration_is_rejected_before_state_change(http_app, patch):
    response = wsgi_post(http_app.app, "/api/longpoll", {"sessionId": "http:1", **patch})
    assert response["status"] == 200
    assert json.loads(response["body"])["ret"] == "invalid request"
    assert http_app.sessions == {}


def test_constructor_keeps_diagnostic_counters_per_instance_without_binding(monkeypatch):
    probe = MagicMock()
    probe.__enter__.return_value = probe
    probe.connect_ex.return_value = 0
    monkeypatch.setattr(T.socket, "socket", lambda *args, **kwargs: probe)
    first, second = T.BrowserBridge(), T.BrowserBridge()
    try:
        first._record_operation_reply({"type": "ack", "id": "unknown"}, transport="ws")
        assert first.rejected_operation_replies == 1
        assert second.rejected_operation_replies == 0
        assert second.last_rejected_operation_reply is None
        assert first.is_remote and second.is_remote
        probe.bind.assert_not_called()
    finally:
        first._http.close()
        second._http.close()


def test_terminal_reply_is_committed_before_notification(monkeypatch):
    driver = driver_stub()
    socket = FakeSocket()
    driver._operation_state().reserve(
        "owned", ["chrome:7"], "owner", reply_transport="ws", reply_owner=socket,
    )
    # Pause at the publication/notification boundary, where another HTTP
    # handler can submit a duplicate before the first one wakes the waiter.
    monkeypatch.setattr(driver, "_notify_activity", lambda: None)
    assert driver._record_operation_reply(
        {"type": "result", "id": "owned", "result": "original"}, transport="ws", owner=socket,
    )
    assert not driver._record_operation_reply(
        {"type": "result", "id": "owned", "result": "duplicate"}, transport="ws", owner=socket,
    )
    assert driver.get_execute_js_result("owned", requester_id="owner")["data"] == "original"
