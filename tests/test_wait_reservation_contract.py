"""Wait timeouts must leave no page timer and must retain pending operation IDs."""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S
from browsertap_mcp.command_scope import TargetBusyError
from tests.test_pending_bridge_operations import finish, make_bridge


def pending_wait_bridge(monkeypatch, *, acknowledged=True):
    """Real wait/bridge/registry chain with only transport and time replaced."""
    bridge = make_bridge()
    now = [1000.0]

    def advance(delay):
        now[0] += delay

    def wait_activity(seen, timeout):
        advance(timeout)
        return bridge._activity_serial

    def send(message):
        payload = json.loads(message)
        bridge.sent.append(payload)
        if acknowledged:
            bridge.acks[payload["id"]] = time.time()
        if "code" not in payload:
            finish(bridge, payload["id"], {"ok": True})
        bridge._notify_activity()

    bridge.ext_clients["browser"]["ws"].send_message = send
    monkeypatch.setattr(bridge, "_wait_for_activity", wait_activity)
    monkeypatch.setattr(S, "require_driver", lambda: bridge)
    monkeypatch.setattr(S, "ensure_sessions", lambda: [])
    monkeypatch.setattr(S, "switch_session", lambda **kw: "browser:1")
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(S.time, "sleep", advance)
    return bridge, now


@pytest.fixture
def waiting(monkeypatch):
    now = [0.0]
    driver = SimpleNamespace(default_session_id="chrome:7")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda: [{"id": "chrome:7"}])
    monkeypatch.setattr(S, "switch_session", lambda **kw: "chrome:7")
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(S.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return driver, now


def _wait(kind, **kwargs):
    return S.wait_for(js="false", **kwargs) if kind == "condition" else S.wait_for_url("missing", **kwargs)


@pytest.mark.parametrize("kind", ["selector", "condition", "url"])
@pytest.mark.parametrize("acknowledged", [True, False])
def test_long_wait_keeps_expired_receipt_without_replaying(monkeypatch, kind, acknowledged):
    bridge, now = pending_wait_bridge(monkeypatch, acknowledged=acknowledged)
    kwargs = {"timeout": 140, "session_id": "browser:1"}
    outcome = S.wait_for(selector="missing", **kwargs) if kind == "selector" else _wait(kind, **kwargs)
    operation_id = bridge.sent[0]["id"]

    assert len(bridge.sent) == 1
    assert 1060 < now[0] <= 1140
    assert outcome["status"] == "timeout"
    assert outcome["operation_id"] == operation_id
    assert outcome["operation_status"] == "unknown"
    assert outcome["reservation_held"] is False
    assert outcome["retry_safe"] is False
    assert outcome["poll_with"] == "get_execute_js_result"
    if kind == "condition":
        assert "read-only" not in outcome["hint"]
    receipt = S.get_execute_js_result(operation_id)
    assert receipt["status"] == "unknown"
    assert receipt["reservation_held"] is False

    successor = bridge.execute_js("pending", wait=False, timeout=0.1, requester_id="next")
    assert bridge._record_operation_reply(
        {"type": "result", "id": operation_id, "result": json.dumps({"met": True})},
        transport="ws", owner=bridge.ext_clients["browser"]["ws"],
    ) is True
    late = S.get_execute_js_result(operation_id)
    assert late["status"] == "unknown"
    assert late["late_result"]["success"] is True
    assert json.loads(late["late_result"]["data"]) == {"met": True}
    assert late["retry_safe"] is False
    assert bridge.get_execute_js_result(successor["operation_id"], requester_id="next")["reservation_held"] is True
    assert len(bridge.sent) == 2


@pytest.mark.parametrize("kind", ["condition", "url"])
@pytest.mark.parametrize("status", ["navigated", "completed_without_result", "failed"])
def test_wait_returns_unusable_receipt_without_replaying(waiting, monkeypatch, kind, status):
    driver, now = waiting
    dispatched, queried = [], []

    def execute(script, **kw):
        dispatched.append(script)
        now[0] += 0.4
        raise S.BridgeNoResponseError(
            "response pending", delivery_state="delivered_no_result", retry_safe=False,
            operation_id="wait-operation", reservation_held=True,
        )

    def result(operation_id, timeout):
        queried.append(operation_id)
        return {"status": status, "operation_id": operation_id, "reservation_held": False}

    monkeypatch.setattr(S, "exec_js", execute)
    driver.get_execute_js_result = result
    outcome = _wait(kind, timeout=2)
    assert len(dispatched) == 1
    assert queried == ["wait-operation"]
    assert outcome["status"] == "timeout"
    assert outcome["operation_id"] == "wait-operation"
    assert outcome["operation_status"] == status
    assert outcome["reservation_held"] is False
    assert outcome["retry_safe"] is False


@pytest.mark.parametrize("acknowledged", [True, False])
def test_caller_js_wait_timeout_holds_target_until_late_reply(monkeypatch, acknowledged):
    bridge, now = pending_wait_bridge(monkeypatch, acknowledged=acknowledged)
    outcome = _wait("condition", timeout=1, session_id="browser:1")
    operation_id = outcome["operation_id"]
    pending = S.get_execute_js_result(operation_id)
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd({"cmd": "navigate", "tabId": 1}, requester_id="next")
    assert len(bridge.sent) == 1
    assert now[0] == pytest.approx(1001)
    finish(bridge, operation_id, json.dumps({"met": False}))
    settled = S.get_execute_js_result(operation_id)
    bridge.ext_cmd({"cmd": "navigate", "tabId": 1}, requester_id="next")
    assert len(bridge.sent) == 2
    assert settled["operation_id"] == operation_id
    assert settled["status"] == "success" and settled["reservation_held"] is False
    assert outcome["status"] == "timeout" and outcome["waited_ms"] == 1000
    assert pending["status"] == "in_progress" and pending["reservation_held"] is True
    assert outcome["reservation_held"] is True
    assert outcome["delivery_state"] == ("delivered_no_result" if acknowledged else "sent_unconfirmed")
    assert outcome["retry_safe"] is False
    assert outcome["poll_with"] == "get_execute_js_result"
    for guidance in (outcome["hint"], outcome["error"]):
        assert "get_execute_js_result" in guidance and "same MCP session" in guidance
        assert "scan_page" not in guidance and "then retry" not in guidance


@pytest.mark.parametrize("kind", ["condition", "url"])
def test_settled_probe_does_not_leave_pending_error_guidance(monkeypatch, kind):
    bridge, now = pending_wait_bridge(monkeypatch)
    wait_activity = bridge._wait_for_activity

    def late_reply(seen, timeout):
        serial = wait_activity(seen, timeout)
        if now[0] >= 1001.95:
            finish(bridge, bridge.sent[0]["id"], json.dumps({"met": False}))
        return serial

    monkeypatch.setattr(bridge, "_wait_for_activity", late_reply)
    outcome = _wait(kind, timeout=2, session_id="browser:1")
    assert len(bridge.sent) == 1
    assert outcome["status"] == "timeout" and "operation_id" not in outcome
    assert "error" not in outcome
    assert "get_execute_js_result" not in outcome["hint"]


@pytest.mark.parametrize("kind", ["condition", "url"])
def test_wait_probes_resolve_without_page_timer_dispatch(waiting, monkeypatch, kind):
    _, now = waiting
    scripts = []
    def execute(script, **kw):
        scripts.append(script)
        now[0] += 1
        return {"data": {"met": False}}
    monkeypatch.setattr(S, "exec_js", execute)
    assert _wait(kind, timeout=1)["status"] == "timeout"
    harness = """
const location = { href: 'https://example.test/' };
const document = { title: 'test', readyState: 'complete' };
function setTimeout() { throw new Error('page timers are throttled'); }
const result = new Function(SOURCE)();
if (result && typeof result.then === 'function') throw new Error('wait left a pending page promise');
process.stdout.write(JSON.stringify(JSON.parse(result)));
""".replace("SOURCE", json.dumps(scripts[0]))
    completed = subprocess.run(["node", "-"], input=harness, text=True, capture_output=True,
                               timeout=5, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["met"] is False


@pytest.mark.parametrize("kind", ["condition", "url"])
def test_tail_budget_is_not_spent_on_an_unreturnable_dispatch(waiting, monkeypatch, kind):
    _, now = waiting
    calls = []
    def execute(script, **kw):
        calls.append(kw["timeout"])
        now[0] += 0.8
        return {"data": {"met": False}}
    monkeypatch.setattr(S, "exec_js", execute)
    assert _wait(kind, timeout=1)["status"] == "timeout"
    assert len(calls) == 1
    assert now[0] == pytest.approx(1)


@pytest.mark.parametrize("kind", ["condition", "url"])
@pytest.mark.parametrize("completes", [False, True])
def test_wait_queries_the_same_pending_operation_without_replaying(waiting, monkeypatch, kind, completes):
    driver, now = waiting
    dispatched, queried = [], []
    def execute(script, **kw):
        dispatched.append(script)
        now[0] += 0.4
        raise S.BridgeNoResponseError(
            "response pending", delivery_state="delivered_no_result", retry_safe=False,
            operation_id="wait-operation", reservation_held=True,
        )
    def result(operation_id, timeout):
        queried.append(operation_id)
        now[0] += timeout if not completes else 0.1
        return ({"status": "success", "data": {"met": True}} if completes else
                {"status": "in_progress", "operation_id": operation_id, "reservation_held": True})
    monkeypatch.setattr(S, "exec_js", execute)
    driver.get_execute_js_result = result
    outcome = _wait(kind, timeout=2)
    assert len(dispatched) == 1
    assert queried == ["wait-operation"]
    assert outcome["status"] == ("success" if completes else "timeout")
    if not completes:
        assert outcome["operation_id"] == "wait-operation"
        assert outcome["reservation_held"] is True
        assert outcome["retry_safe"] is False
        assert outcome["poll_with"] == "get_execute_js_result"
