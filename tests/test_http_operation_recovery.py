"""Lost HTTP replies must not require replaying a browser side effect."""

import json
from types import SimpleNamespace

import pytest
import requests

from browsertap_mcp import browser_bridge as T
from tests.test_browser_bridge_coverage import DormantThread, FakeResponse, driver_stub, wsgi_post
from tests.test_pending_bridge_operations import make_bridge


@pytest.fixture
def http_pair(monkeypatch):
    daemon = make_bridge()
    monkeypatch.setattr(T, "bridge_token", lambda: "")
    monkeypatch.setattr(T.threading, "Thread", DormantThread)
    daemon.start_http_server()
    client = driver_stub(remote=True)
    state = SimpleNamespace(drop_command=None, fault="connection", success=True, requests=[])
    socket = daemon.ext_clients["browser"]["ws"]

    def send(message):
        payload = json.loads(message)
        daemon.sent.append(payload)
        reply = {"id": payload["id"], "tabId": 1}
        if state.success:
            reply.update({"type": "result", "result": {"saved": True}})
        else:
            reply.update({"type": "error", "error": "page evaluation failed"})
        assert daemon._record_operation_reply(reply, transport="ws", owner=socket)

    def post(_url, *, headers, json, timeout):
        state.requests.append(dict(json))
        response = wsgi_post(daemon.app, "/link", json, headers=headers)
        assert response["status"] == 200
        if state.drop_command == json["cmd"]:
            if state.fault == "timeout":
                raise requests.exceptions.Timeout("response timed out after dispatch")
            if state.fault == "http_error":
                return FakeResponse(status_code=500, text="response encoding failed")
            if state.fault == "invalid_json":
                return FakeResponse(payload=ValueError("truncated JSON response"))
            if state.fault == "invalid_result":
                return FakeResponse(payload={"r": None})
            raise requests.exceptions.ConnectionError("response lost after dispatch")
        return FakeResponse(payload=T.json.loads(response["body"]))

    socket.send_message = send
    client._http = SimpleNamespace(post=post)
    return daemon, client, state


def dispatch(client, command):
    if command == "execute_js":
        return client.execute_js("save()", session_id="browser:1", requester_id="owner")
    return client.ext_cmd(
        {"cmd": "cdp", "method": "Runtime.evaluate", "tabId": 1},
        client_id="browser", requester_id="owner",
    )


@pytest.mark.parametrize("command", ["execute_js", "ext_cmd"])
@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("fault", ["connection", "timeout", "http_error", "invalid_json", "invalid_result"])
def test_completed_operation_survives_lost_http_reply(http_pair, command, success, fault):
    daemon, client, state = http_pair
    state.success = success
    state.fault = fault
    state.drop_command = command

    with pytest.raises((T.BridgeNoResponseError, TimeoutError)) as caught:
        dispatch(client, command)

    operation_id = caught.value.operation_id
    assert caught.value.retry_safe is False
    assert getattr(caught.value, "reservation_held", None) is None
    assert caught.value.diagnostics.get("reservation_held") is None
    assert operation_id == daemon.sent[0]["id"]
    result = client.get_execute_js_result(operation_id, requester_id="owner")
    assert result["status"] == ("success" if success else "failed")
    assert result["reservation_held"] is False
    if success:
        assert result["data"] == {"saved": True}
    else:
        assert "page evaluation failed" in result["error"]
    assert len(daemon.sent) == 1


@pytest.mark.parametrize("command", ["execute_js", "ext_cmd"])
def test_losing_result_query_reply_does_not_consume_result(http_pair, command):
    daemon, client, state = http_pair
    dispatch(client, command)
    operation_id = daemon.sent[0]["id"]
    state.drop_command = "get_execute_js_result"
    with pytest.raises(T.BridgeNoResponseError):
        client.get_execute_js_result(operation_id, requester_id="owner")

    state.drop_command = None
    first = client.get_execute_js_result(operation_id, requester_id="owner")
    second = client.get_execute_js_result(operation_id, requester_id="owner")
    assert first == second
    assert first["status"] == "success"
    assert first["data"] == {"saved": True}
    assert len(daemon.sent) == 1


@pytest.mark.parametrize("command", ["execute_js", "ext_cmd"])
def test_retained_http_results_keep_owner_and_duplicate_dispatch_guards(http_pair, command):
    daemon, client, state = http_pair
    dispatch(client, command)
    original_request = state.requests[-1]
    operation_id = daemon.sent[0]["id"]

    with pytest.raises(T.BridgeNoResponseError) as caught:
        client.get_execute_js_result(operation_id, requester_id="other")
    assert caught.value.error_code == "operation_owner_mismatch"
    replay = wsgi_post(daemon.app, "/link", original_request)
    assert json.loads(replay["body"])["r"]["error_code"] == "operation_already_exists"
    assert len(daemon.sent) == 1
    assert client.get_execute_js_result(operation_id, requester_id="owner")["data"] == {"saved": True}

    # Completion frees the tab without discarding the result receipt.
    client.execute_js("next()", session_id="browser:1", requester_id="other")
    assert len(daemon.sent) == 2


@pytest.mark.parametrize("command", ["execute_js", "ext_cmd"])
def test_result_query_cannot_steal_reply_from_synchronous_waiter(monkeypatch, command):
    daemon = make_bridge()
    socket = daemon.ext_clients["browser"]["ws"]
    original_send = socket.send_message
    observations = []

    def send(message):
        original_send(message)
        operation_id = json.loads(message)["id"]
        observations.append(daemon.get_execute_js_result(operation_id, requester_id="owner"))

    monkeypatch.setattr(socket, "send_message", send)
    if command == "execute_js":
        result = daemon.execute_js("save()", timeout=0.01, requester_id="owner")
    else:
        result = daemon.ext_cmd({"cmd": "cdp", "tabId": 1}, timeout=0.01, requester_id="owner")
    assert result["data"] == observations[0]["data"] == {"ok": True}
    assert len(daemon.sent) == 1
