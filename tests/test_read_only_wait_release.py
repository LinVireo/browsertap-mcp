"""Timed-out server probes release targets without discarding their receipts."""

from __future__ import annotations

import json
import time

import pytest
import requests

from browsertap_mcp import browser_bridge as T
from browsertap_mcp import server as S
from browsertap_mcp.command_scope import TargetBusyError
from browsertap_mcp.pending_operations import OperationAccessError, PendingOperations
from tests.test_pending_bridge_operations import finish, make_bridge
from tests.test_wait_reservation_contract import pending_wait_bridge


def read_only_wait(kind, **kwargs):
    if kind == "ready_url":
        return S.wait_for_url("missing", **kwargs)
    if kind == "locator":
        return S.wait_for(selector={"role": "button", "name": "Missing"}, **kwargs)
    if kind == "gone_selector":
        return S.wait_for(selector=".missing", gone=True, **kwargs)
    return S.wait_for(**{kind: "missing"}, **kwargs)


@pytest.mark.parametrize("kind", ["selector", "text", "url_pattern", "ready_url", "locator", "gone_selector"])
@pytest.mark.parametrize("acknowledged", [True, False])
def test_read_only_timeout_releases_target_but_keeps_original_reply(monkeypatch, kind, acknowledged):
    bridge, now = pending_wait_bridge(monkeypatch, acknowledged=acknowledged)
    outcome = read_only_wait(kind, timeout=1, session_id="browser:1")
    operation_id = outcome["operation_id"]
    receipt = S.get_execute_js_result(operation_id)

    assert outcome["status"] == "timeout"
    assert receipt["status"] == "in_progress"
    assert receipt["reservation_held"] is False
    assert outcome["reservation_held"] is False
    assert outcome["retry_safe"] is False
    assert outcome["poll_with"] == "get_execute_js_result"
    assert "no longer holds the tab" in outcome["hint"]
    assert "before another command" not in outcome.get("error", "")
    assert len(bridge.sent) == 1
    assert now[0] == pytest.approx(1001)

    bridge.ext_cmd({"cmd": "navigate", "tabId": 1}, requester_id="next")
    finish(bridge, operation_id, json.dumps({"met": False}))
    settled = S.get_execute_js_result(operation_id)
    assert settled["status"] == "success"
    assert settled["js_return"] == '{"met": false}'
    assert settled["reservation_held"] is False
    assert len(bridge.sent) == 2


def test_late_probe_reply_keeps_successor_busy_and_checks_original_socket(monkeypatch):
    bridge, _ = pending_wait_bridge(monkeypatch)
    outcome = read_only_wait("text", timeout=1, session_id="browser:1")
    operation_id = outcome["operation_id"]
    successor = bridge.execute_js("pending", wait=False, timeout=0.1, requester_id="next")
    bridge.rejected_operation_replies = 0
    reply = {"type": "result", "id": operation_id, "result": '{"met": false}'}
    assert bridge._record_operation_reply(reply, transport="ws", owner=object()) is False
    assert bridge._record_operation_reply(
        reply, transport="ws", owner=bridge.ext_clients["browser"]["ws"],
    ) is True
    assert S.get_execute_js_result(operation_id)["status"] == "success"
    assert bridge.get_execute_js_result(successor["operation_id"], requester_id="next")["reservation_held"] is True
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd({"cmd": "navigate", "tabId": 1}, requester_id="third")
    assert len(bridge.sent) == 2


@pytest.mark.parametrize("kind", ["execute_js", "navigate", "handle_dialog", "console", "network"])
def test_release_refuses_every_non_probe_kind(kind):
    operations = PendingOperations()
    operations.reserve("unsafe", ["browser:1"], "owner", kind=kind)
    assert operations.release_wait_probe("unsafe", "owner") is False
    assert operations.read("unsafe", "owner")["reservation_held"] is True
    with pytest.raises(TargetBusyError):
        operations.reserve("next", ["browser:1"], "next")


@pytest.mark.parametrize("status", ["outcome_unknown", "blocked_by_dialog", "completed", "lifecycle_ended"])
def test_release_refuses_non_pending_states_without_changing_receipt(status):
    operations = PendingOperations()
    operations.reserve("probe", ["browser:1"], "owner", kind="wait_probe")
    if status == "lifecycle_ended":
        operations.finish_target("browser:1", "tab_removed")
    else:
        data = {"__btap_dialog_result": True, "pending_execution": True} if status == "blocked_by_dialog" else 1
        operations.complete("probe", {"success": True, "data": data}, outcome_unknown=status == "outcome_unknown")
    before = operations.read("probe", "owner")
    assert before["status"] == status
    assert operations.release_wait_probe("probe", "owner") is False
    assert operations.read("probe", "owner") == before


def test_release_checks_owner_and_missing_operation():
    operations = PendingOperations()
    operations.reserve("probe", ["browser:1"], "owner", kind="wait_probe")
    with pytest.raises(OperationAccessError, match="operation_owner_mismatch"):
        operations.release_wait_probe("probe", "stranger")
    with pytest.raises(OperationAccessError, match="operation_not_found"):
        operations.release_wait_probe("absent", "owner")
    assert operations.read("probe", "owner")["reservation_held"] is True


def test_release_does_not_end_or_restore_a_recovery_lease():
    operations = PendingOperations()
    operations.reserve("original", ["browser:1"], "owner")
    operations.reserve("recovery", ["browser:1"], "owner", kind="wait_probe", cleanup=True)
    assert operations.release_wait_probe("recovery", "owner") is False
    assert operations.read("recovery", "owner")["reservation_held"] is True
    operations.complete("recovery", {"success": False}, outcome_unknown=True)
    operations.reserve("replacement", ["browser:1"], "owner", kind="wait_probe", cleanup=True, close_targets=True)
    assert operations.release_wait_probe("replacement", "owner") is False
    assert operations.read("recovery", "owner")["reservation_held"] is False
    assert operations.read("replacement", "owner")["reservation_held"] is True


def test_releasing_twice_preserves_receipt_deadlines_and_successor(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    operations = PendingOperations()
    socket = object()
    operations.reserve("probe", ["browser:1"], "owner", kind="wait_probe", reply_transport="ws", reply_owner=socket)
    original = operations._operations["probe"]
    timestamps = original.started_at, original.silent_after, original.completed_at
    assert operations.release_wait_probe("probe", "owner") is True
    receipt = operations.read("probe", "owner")
    assert receipt["status"] == "in_progress"
    assert receipt["released_targets"] == ["browser:1"]
    assert receipt["released_reason"] == "read_only_wait_probe_timeout"
    assert operations.accepts_reply("probe", "ws", socket) is True
    operations.reserve("next", ["browser:1"], "next")
    now[0] += 5
    assert operations.release_wait_probe("probe", "owner") is False
    assert operations.read("probe", "owner") == receipt
    assert (original.started_at, original.silent_after, original.completed_at) == timestamps
    assert operations.read("next", "next")["reservation_held"] is True


@pytest.mark.parametrize("mode", ["current", "old_bridge", "lost_reply"])
def test_remote_wait_uses_existing_requests_and_reports_only_confirmed_release(monkeypatch, mode):
    local, now = pending_wait_bridge(monkeypatch)
    remote = make_bridge()
    remote.is_remote = True
    calls = []

    def route(payload, timeout):
        calls.append(payload["cmd"])
        if payload["cmd"] == "execute_js":
            result = local.execute_js(
                payload["code"], timeout=float(payload["timeout"]), session_id=payload["sessionId"],
                requester_id=payload["requesterId"], operation_id=payload["operationId"],
                read_only_probe=payload.get("readOnlyProbe") is True and mode != "old_bridge",
            )
            if mode == "lost_reply":
                raise TimeoutError("simulated lost HTTP reply")
        else:
            assert payload["cmd"] == "get_execute_js_result"
            result = local.get_execute_js_result(
                payload["operationId"], timeout=payload["timeout"], requester_id=payload["requesterId"],
            )
        return {"r": result}

    monkeypatch.setattr(remote, "_remote_cmd", route)
    monkeypatch.setattr(S, "require_driver", lambda: remote)
    outcome = read_only_wait("selector", timeout=1, session_id="browser:1")
    assert now[0] == pytest.approx(1001)
    assert len(local.sent) == 1
    assert calls == ["execute_js"]
    assert outcome["status"] == "timeout"
    assert outcome["reservation_held"] is {"current": False, "old_bridge": True, "lost_reply": None}[mode]
    receipt = S.get_execute_js_result(outcome["operation_id"])
    assert receipt["reservation_held"] is (mode == "old_bridge")
    assert calls == ["execute_js", "get_execute_js_result"]
    if mode == "old_bridge":
        assert "no longer holds" not in outcome["hint"]
        with pytest.raises(TargetBusyError):
            local.ext_cmd({"cmd": "navigate", "tabId": 1}, requester_id="next")
    else:
        local.ext_cmd({"cmd": "navigate", "tabId": 1}, requester_id="next")


@pytest.fixture
def authenticated_probe_bridge(tmp_path, monkeypatch, request):
    monkeypatch.setenv(T.TOKEN_FILE_ENV, str(tmp_path / "bridge-token"))
    return request.getfixturevalue("link_bridge_auth")


@pytest.mark.parametrize("probe_flag", [True, False, "1", 1, None])
def test_authenticated_http_dispatch_marks_only_explicit_probe_boolean(authenticated_probe_bridge, probe_flag):
    link_bridge_auth = authenticated_probe_bridge
    local = link_bridge_auth.driver
    fake = make_bridge()
    for attr in ("sessions", "results", "acks", "ext_clients", "default_session_id", "latest_session_id",
                 "_activity_condition", "_activity_serial"):
        setattr(local, attr, getattr(fake, attr))
    payload = {
        "cmd": "execute_js", "code": "pending", "sessionId": "browser:1", "timeout": 0.02,
        "operationId": "http-probe", "requesterId": "owner", "readOnlyProbe": probe_flag,
    }
    with requests.Session() as client:
        client.trust_env = False
        response = client.post(
            f"http://127.0.0.1:{link_bridge_auth.port}/link", json=payload,
            headers={"Authorization": "Bearer s3cret-token"}, timeout=3,
        )
    assert response.status_code == 200
    pending = response.json()["r"]
    assert pending["reservation_held"] is (probe_flag is not True)
    assert pending["operation_id"] == "http-probe"
    assert len(fake.sent) == 1
    expected_kind = "wait_probe" if probe_flag is True else "execute_js"
    assert local._operation_state()._operations["http-probe"].kind == expected_kind
    if probe_flag is not True:
        assert local._operation_state().release_wait_probe("http-probe", "owner") is False
