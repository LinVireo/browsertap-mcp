"""A receipt must prove completion or retry safety before changing either state."""

from __future__ import annotations

import json
import subprocess

import pytest

from browsertap_mcp import server as S
from browsertap_mcp import simphtml as H
from tests.test_browser_bridge_coverage import FakeSocket, driver_stub
from tests.test_phase0_recovery import _websocket_connect_source, _ws_exec_source


class ReplyDriver:
    default_session_id = "synthetic-browser:7"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def execute_js(self, script, **kwargs):
        self.calls.append((script, kwargs))
        return dict(self.replies[min(len(self.calls) - 1, len(self.replies) - 1)])


def run_helper(route, driver, monkeypatch):
    if route == "server":
        monkeypatch.setattr(S, "require_driver", lambda: driver)
        return S.exec_js("submit_once()", session_id=driver.default_session_id, timeout=3)
    return H.execute_js_rich(
        "submit_once()", driver, session_id=driver.default_session_id,
        no_monitor=True, before_sids=set(), timeout=3,
    )


@pytest.mark.parametrize("route", ["server", "rich"])
@pytest.mark.parametrize("delivery", [
    {"delivery_state": "undelivered"},
    {"result": "No response (script not polled)"},
])
def test_explicit_retry_refusal_prevents_a_second_dispatch(monkeypatch, route, delivery):
    receipt = {
        **delivery, "retry_safe": False, "operation_id": "original-operation",
        "reservation_held": True,
    }
    driver = ReplyDriver([receipt, {"data": "would replay"}])
    if route == "server":
        with pytest.raises(S.BridgeNoResponseError) as raised:
            run_helper(route, driver, monkeypatch)
        assert raised.value.retry_safe is False
        assert raised.value.operation_id == "original-operation"
        assert raised.value.reservation_held is True
    else:
        result = run_helper(route, driver, monkeypatch)
        assert result["status"] == "no_response"
        assert result["retry_safe"] is False
        assert result["btap_retried"] is False
        assert result["operation_id"] == "original-operation"
        assert result["reservation_held"] is True
    assert len(driver.calls) == 1


@pytest.mark.parametrize("route", ["server", "rich"])
def test_refusal_on_the_bounded_retry_stays_non_retryable(monkeypatch, route):
    driver = ReplyDriver([
        {"delivery_state": "undelivered", "retry_safe": True},
        {"delivery_state": "undelivered", "retry_safe": False, "operation_id": "retry-operation"},
    ])
    if route == "server":
        with pytest.raises(S.BridgeNoResponseError) as raised:
            run_helper(route, driver, monkeypatch)
        assert raised.value.retry_safe is False
        assert raised.value.operation_id == "retry-operation"
    else:
        result = run_helper(route, driver, monkeypatch)
        assert result["retry_safe"] is False
        assert result["btap_retried"] is True
        assert result["operation_id"] == "retry-operation"
    assert len(driver.calls) == 2


@pytest.mark.parametrize("route", ["server", "rich"])
@pytest.mark.parametrize("receipt", [
    {"delivery_state": "undelivered", "retry_safe": True},
    {"delivery_state": "undelivered"},
    {"result": "No response (script not polled)"},
])
def test_proven_undelivered_without_a_refusal_keeps_its_one_retry(monkeypatch, route, receipt):
    driver = ReplyDriver([receipt, {"data": False}])
    result = run_helper(route, driver, monkeypatch)
    assert result["data" if route == "server" else "js_return"] is False
    assert len(driver.calls) == 2
    assert all(call[1]["session_id"] == driver.default_session_id for call in driver.calls)


@pytest.mark.parametrize("transport", ["ws", "http"])
@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_missing_wire_result_keeps_the_original_reservation_until_a_valid_receipt(transport, value):
    driver = driver_stub()
    owner = FakeSocket() if transport == "ws" else "synthetic-browser:7"
    operations = driver._operation_state()
    operations.reserve(
        "original-operation", ["synthetic-browser:7"], "requester",
        reply_transport=transport, reply_owner=owner,
    )
    assert driver._record_operation_reply(
        {"type": "ack", "id": "original-operation"}, transport=transport, owner=owner,
    )
    assert not driver._record_operation_reply(
        {"type": "result", "id": "original-operation"}, transport=transport, owner=owner,
    )
    pending = driver.get_execute_js_result("original-operation", requester_id="requester")
    assert pending["status"] == "in_progress"
    assert pending["reservation_held"] is True
    assert pending["acknowledged"] is True
    assert "original-operation" not in driver.results
    assert driver.rejected_operation_replies == 1

    assert driver._record_operation_reply(
        {"type": "result", "id": "original-operation", "result": value},
        transport=transport, owner=owner,
    )
    completed = driver.get_execute_js_result("original-operation", requester_id="requester")
    assert completed["status"] == "success"
    assert completed["reservation_held"] is False
    assert completed["data"] == value and type(completed["data"]) is type(value)


@pytest.mark.parametrize("sender", ["exec", "command", "batch"])
@pytest.mark.parametrize(("expression", "expected"), [
    ("undefined", None), ("null", None), ("false", False), ("0", 0),
    ("''", ""), ("[]", []), ("{}", {}),
])
def test_completed_sender_value_always_has_an_explicit_wire_result(sender, expression, expected):
    harness = r"""
const replies = [];
const listeners = new Set();
const console = { log() {}, error() {} };
class WebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  constructor() { this.readyState = WebSocket.OPEN; }
  send(value) { replies.push(JSON.parse(value)); }
}
let ws = null;
let connectInFlight = false;
let lastPongAt = 0;
const WS_URL = 'ws://127.0.0.1:18765';
function broadcastBridgeStatus() {}
function setTimeout(callback, delay) {
  if (delay === 200) queueMicrotask(callback);
  return {};
}
function clearTimeout() {}
function isWorkerGoneError() { return false; }
function currentExecDialogPolicy() { return {policy:'manual', token:'synthetic-scope'}; }
async function executeManualScript() { return {ok:true, data:__VALUE__}; }
async function handleExtMessage(message) {
  return message.cmd === 'batch' ? {ok:true, results:__VALUE__} : {ok:true, data:__VALUE__};
}
const chrome = {tabs:{onCreated:{
  addListener(listener) { listeners.add(listener); },
  removeListener(listener) { listeners.delete(listener); },
}}};
__EXEC_SOURCE__
__CONNECT_SOURCE__
(async () => {
  connectWS();
  const sender = __SENDER__;
  const message = sender === 'exec'
    ? {id:'synthetic-operation', tabId:7, code:'return undefined'}
    : {id:'synthetic-operation', cmd:{cmd:sender}};
  await ws.onmessage({data:JSON.stringify(message)});
  process.stdout.write(JSON.stringify({replies, listenerCount:listeners.size}));
})().catch(error => { process.stderr.write(error.stack); process.exit(1); });
"""
    harness = (
        harness.replace("__VALUE__", expression)
        .replace("__SENDER__", json.dumps(sender))
        .replace("__EXEC_SOURCE__", _ws_exec_source())
        .replace("__CONNECT_SOURCE__", _websocket_connect_source())
    )
    process = subprocess.run(
        ["node", "-"], input=harness, text=True, encoding="utf-8",
        capture_output=True, timeout=10, check=False,
    )
    assert process.returncode == 0, process.stderr
    outcome = json.loads(process.stdout)
    assert outcome["listenerCount"] == 0
    replies = outcome["replies"]
    assert [reply["type"] for reply in replies] == (["ack", "result"] if sender == "exec" else ["result"])
    result = replies[-1]
    assert "result" in result, result
    assert result["result"] == expected and type(result["result"]) is type(expected)
