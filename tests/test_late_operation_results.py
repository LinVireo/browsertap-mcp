"""Late terminal replies remain observable without reopening an expired operation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolRequest, CallToolResult

from browsertap_mcp import pending_operations as P
from browsertap_mcp import server as S
from browsertap_mcp.browser_bridge import BridgeNoResponseError
from browsertap_mcp.capture_ownership import CaptureBusyError
from browsertap_mcp.pending_operations import OperationAccessError
from tests.test_browser_bridge_coverage import (
    driver_stub,
    ext_ready,
    ws_handler_for,
    ws_peer,
    wsgi_post,
)
from tests.test_browser_bridge_coverage import http_app as http_app

TIMEOUT = {"code": "cdp_timeout", "message": "execution timed out", "dispatched": True}


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def channel(http_app, monkeypatch):
    handler = ws_handler_for(http_app, monkeypatch)
    peer = ws_peer(handler, ext_ready())
    peer.handle()

    def deliver(payload, source=peer):
        source.data = json.dumps(payload)
        source.handle()

    def send(message):
        payload = json.loads(message)
        peer.sent.append(payload)
        deliver({"type": "ack", "id": payload["id"]})

    peer.send_message = send
    return SimpleNamespace(driver=http_app, peer=peer, handler=handler, deliver=deliver)


def abandon(channel, clock, *, initial="timeout"):
    driver = channel.driver
    pending = driver.execute_js(
        "pending", wait=False, timeout=0.1, session_id="chrome:7", requester_id="owner",
    )
    operation_id = pending["operation_id"]
    if initial == "timeout":
        channel.deliver({"type": "error", "id": operation_id, "error": TIMEOUT})
    clock[0] += P.SILENT_GRACE_SECONDS + 1
    receipt = driver.get_execute_js_result(operation_id, requester_id="owner")
    assert receipt["status"] == "unknown"
    assert receipt["reservation_held"] is False
    assert receipt["retry_safe"] is False
    return operation_id, receipt


@pytest.mark.parametrize("initial", ["silent", "timeout"])
@pytest.mark.parametrize("success", [True, False])
def test_ws_late_result_is_readable_without_changing_receipt_or_successor(channel, clock, initial, success):
    driver = channel.driver
    operation_id, receipt = abandon(channel, clock, initial=initial)
    successor = driver.execute_js(
        "pending", wait=False, timeout=0.1, session_id="chrome:7", requester_id="other",
    )
    clock[0] += 5
    value = {"saved": 42} if success else {"code": "page_error", "message": "late page failure"}
    channel.deliver({
        "type": "result" if success else "error", "id": operation_id,
        "result" if success else "error": value, "tabId": 7,
    })

    result = driver.get_execute_js_result(operation_id, requester_id="owner")
    assert {key: result[key] for key in receipt} == receipt
    assert result["late_result"]["success"] is success
    assert result["late_result"]["data"] == value
    assert result["late_result"]["tabId"] == 7
    assert result["late_reply_age"] == 0
    assert driver.get_execute_js_result(operation_id, requester_id="owner") == result
    assert driver._operation_state().read(operation_id, "owner")["status"] == "abandoned"
    assert driver._operation_state()._targets == {"chrome:7": successor["operation_id"]}
    assert driver.get_execute_js_result(successor["operation_id"], requester_id="other")["status"] == "in_progress"
    assert len(channel.peer.sent) == 2


def test_late_reply_crosses_http_query_and_mcp_envelope_without_replay(channel, clock, monkeypatch):
    operation_id, receipt = abandon(channel, clock)
    channel.deliver({"type": "result", "id": operation_id, "result": {"saved": 42}})
    expected = channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    remote = driver_stub(remote=True)
    monkeypatch.setattr(remote, "_requester", lambda requester_id: requester_id or "owner")

    def remote_cmd(payload, timeout):
        response = wsgi_post(channel.driver.app, "/link", payload)
        assert response["status"] == 200
        return json.loads(response["body"])

    monkeypatch.setattr(remote, "_remote_cmd", remote_cmd)
    assert remote.get_execute_js_result(operation_id) == expected
    with pytest.raises(BridgeNoResponseError) as denied:
        remote.get_execute_js_result(operation_id, requester_id="other")
    assert denied.value.error_code == "operation_owner_mismatch"
    monkeypatch.setattr(S, "require_driver", lambda: remote)
    request = CallToolRequest(params={
        "name": "get_execute_js_result", "arguments": {"operation_id": operation_id},
    })
    result = asyncio.run(S.mcp._mcp_server.request_handlers[CallToolRequest](request)).root
    assert isinstance(result, CallToolResult)
    assert result.isError is True  # The original timeout receipt remains an error.
    envelope = result.structuredContent
    assert envelope["legacy"]["late_result"]["data"] == {"saved": 42}
    assert envelope["legacy"]["error"] == receipt["error"]
    assert envelope["error"]["retryable"] is False
    assert json.loads(result.content[0].text) == envelope
    assert len(channel.peer.sent) == 1


def test_late_ack_foreign_socket_and_wrong_transport_cannot_claim_the_result(channel, clock):
    operation_id, receipt = abandon(channel, clock)
    forged = {"type": "result", "id": operation_id, "result": "forged"}
    channel.deliver(forged, ws_peer(channel.handler, forged))
    response = wsgi_post(channel.driver.app, "/api/result", forged)
    assert response["status"] == 200
    channel.deliver({"type": "ack", "id": operation_id})
    assert channel.driver.rejected_operation_replies == 3
    assert channel.driver.get_execute_js_result(operation_id, requester_id="owner") == receipt

    channel.deliver({"type": "result", "id": operation_id, "result": "first"})
    first = channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    channel.deliver({"type": "result", "id": operation_id, "result": "duplicate"})
    assert channel.driver.get_execute_js_result(operation_id, requester_id="owner") == first
    assert first["late_result"]["data"] == "first"
    assert channel.driver.rejected_operation_replies == 4


@pytest.mark.parametrize("frame", [
    {"type": "error", "error": TIMEOUT},
    {"type": "result", "result": {
        "__btap_dialog_result": True, "pending_execution": True, "value": None,
    }},
])
def test_late_nonterminal_reply_leaves_room_for_a_real_result(channel, clock, frame):
    operation_id, receipt = abandon(channel, clock)
    channel.deliver({**frame, "id": operation_id})
    assert channel.driver.get_execute_js_result(operation_id, requester_id="owner") == receipt
    channel.deliver({"type": "result", "id": operation_id, "result": "done"})
    result = channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    assert result["late_result"]["data"] == "done"
    assert result["reservation_held"] is False


@pytest.mark.parametrize("legacy", [False, True])
def test_http_late_reply_keeps_session_ownership_and_legacy_compatibility(http_app, clock, legacy):
    operations = http_app._operation_state()
    operations.reserve(
        "http-late", ["http:7"], "owner", caller_budget=0.1,
        reply_transport="http", reply_owner="http:7",
    )
    clock[0] += P.SILENT_GRACE_SECONDS + 1
    receipt = http_app.get_execute_js_result("http-late", requester_id="owner")
    payload = {"type": "result", "id": "http-late", "result": "done"}
    wsgi_post(http_app.app, "/api/result", {**payload, "sessionId": "wrong:7"})
    assert http_app.get_execute_js_result("http-late", requester_id="owner") == receipt
    if not legacy:
        payload["sessionId"] = "http:7"
    assert wsgi_post(http_app.app, "/api/result", payload)["body"] == "ok"
    result = http_app.get_execute_js_result("http-late", requester_id="owner")
    assert result["late_result"]["data"] == "done"
    assert result["status"] == "unknown" and result["reservation_held"] is False
    assert http_app.rejected_operation_replies == 1


@pytest.mark.parametrize("expired_first", [False, True])
def test_tab_lifecycle_end_closes_late_reply_admission(channel, clock, expired_first):
    if expired_first:
        operation_id, _ = abandon(channel, clock)
    else:
        operation_id = channel.driver.execute_js(
            "pending", wait=False, timeout=0.1, session_id="chrome:7", requester_id="owner",
        )["operation_id"]
    channel.deliver({**ext_ready(), "type": "tabs_update", "tabs": []})
    channel.deliver({"type": "result", "id": operation_id, "result": "old page"})
    result = channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    assert "late_result" not in result and "data" not in result
    assert result["reservation_held"] is False
    assert channel.driver.rejected_operation_replies == 1


@pytest.mark.parametrize("received_first", [False, True])
def test_consumption_clears_late_data_and_prevents_revival(channel, clock, received_first):
    operation_id, _ = abandon(channel, clock)
    payload = {"type": "result", "id": operation_id, "result": "done"}
    if received_first:
        channel.deliver(payload)
    operations = channel.driver._operation_state()
    consumed = operations.read(operation_id, "owner", consume=True)
    assert ("late_result" in consumed) is received_first
    channel.deliver(payload)
    with pytest.raises(OperationAccessError, match="operation_consumed"):
        channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    assert getattr(operations._operations[operation_id], "late_result", None) is None


def test_late_result_keeps_the_original_retention_deadline(channel, clock):
    operation_id, _ = abandon(channel, clock)
    operations = channel.driver._operation_state()
    completed_at = operations._operations[operation_id].completed_at
    clock[0] += P.RESULT_TTL_SECONDS - 1
    channel.deliver({"type": "result", "id": operation_id, "result": "done"})
    assert channel.driver.get_execute_js_result(operation_id, requester_id="owner")["late_result"]["data"] == "done"
    assert operations._operations[operation_id].completed_at == completed_at
    clock[0] += 2
    channel.deliver({"type": "result", "id": operation_id, "result": "too late"})
    assert operation_id not in operations._operations
    channel.driver._sync_pending_operations()
    assert operation_id not in channel.driver.results
    assert operation_id not in channel.driver.acks
    with pytest.raises(OperationAccessError, match="operation_not_found"):
        channel.driver.get_execute_js_result(operation_id, requester_id="owner")


def test_capacity_eviction_removes_late_data_and_rejects_later_replies(channel, clock, monkeypatch):
    monkeypatch.setattr(P, "MAX_COMPLETED_OPERATIONS", 1)
    operation_id, _ = abandon(channel, clock)
    channel.deliver({"type": "result", "id": operation_id, "result": "done"})
    assert channel.driver.get_execute_js_result(operation_id, requester_id="owner")["late_result"]
    clock[0] += 1
    operations = channel.driver._operation_state()
    operations.reserve("new-result", ["chrome:8"], "other")
    operations.complete("new-result", {"success": True, "data": 1})
    channel.deliver({"type": "result", "id": operation_id, "result": "evicted"})
    channel.driver._sync_pending_operations()
    assert operation_id not in operations.retained_ids()
    assert operation_id not in channel.driver.results
    assert channel.driver.rejected_operation_replies == 1


def test_late_manual_recovery_cannot_change_its_successor(channel, clock):
    operations = channel.driver._operation_state()
    operations.reserve(
        "recovery", ["chrome:7"], "owner", kind="handle_dialog", caller_budget=0.1,
        reply_transport="ws", reply_owner=channel.peer,
    )
    clock[0] += P.SILENT_GRACE_SECONDS + 1
    assert operations.read("recovery", "owner")["status"] == "abandoned"
    operations.reserve("successor", ["chrome:7"], "owner")
    operations.complete("successor", {"success": True, "data": {
        "__btap_dialog_result": True, "pending_execution": True,
    }})
    before = operations.read("successor", "owner")
    channel.deliver({"type": "result", "id": "recovery", "result": {"pending_execution": False}})
    assert operations.read("successor", "owner") == before
    assert operations._targets == {"chrome:7": "successor"}
    assert operations.read("recovery", "owner")["late_result"]["data"] == {"pending_execution": False}


def test_first_reply_after_silence_deadline_only_adds_evidence(channel, clock):
    operation_id = channel.driver.execute_js(
        "pending", wait=False, timeout=0.1, session_id="chrome:7", requester_id="owner",
    )["operation_id"]
    # No intervening query/sweep: ingress itself must apply the expiry boundary.
    clock[0] += P.SILENT_GRACE_SECONDS + 1
    channel.deliver({"type": "result", "id": operation_id, "result": "done"})
    clock[0] += 2.5
    result = channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    assert result["status"] == "unknown" and result["retry_safe"] is False
    assert result["late_result"]["data"] == "done"
    assert result["late_reply_age"] == 2.5
    assert channel.driver._operation_state()._targets == {}


def test_late_capture_stop_does_not_settle_new_work(channel, clock):
    captures = channel.driver._capture_state()
    command = {"cmd": "console", "tabId": 7, "method": "start"}
    captures.prepare("chrome", command, "owner")
    stop = captures.prepare("chrome", {**command, "method": "stop"}, "owner")
    operations = channel.driver._operation_state()
    operations.reserve(
        "old-stop", ["chrome:7"], "owner", caller_budget=0.1,
        reply_transport="ws", reply_owner=channel.peer,
    )
    channel.driver._capture_commands["old-stop"] = stop
    clock[0] += P.SILENT_GRACE_SECONDS + 1
    assert operations.read("old-stop", "owner")["status"] == "abandoned"
    captures.prepare("chrome", command, "owner")
    channel.deliver({"type": "result", "id": "old-stop", "result": {"ok": True}})
    with pytest.raises(CaptureBusyError):
        captures.prepare("chrome", command, "other")
    assert operations.read("old-stop", "owner")["late_result"]["data"] == {"ok": True}


def test_collected_late_reply_survives_a_subsequent_lifecycle_end(channel, clock):
    operation_id, _ = abandon(channel, clock)
    channel.deliver({"type": "result", "id": operation_id, "result": "done"})
    before = channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    channel.deliver({**ext_ready(), "type": "tabs_update", "tabs": []})
    assert channel.driver.get_execute_js_result(operation_id, requester_id="owner") == before
    assert before["late_result"]["data"] == "done"


def test_large_late_value_remains_lossless_through_the_mcp_result(channel, clock, monkeypatch, tmp_path):
    operation_id, _ = abandon(channel, clock)
    value = {"text": "late content" * 5000}
    channel.deliver({"type": "result", "id": operation_id, "result": value})
    monkeypatch.setattr(S.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(S, "require_driver", lambda: channel.driver)
    monkeypatch.setattr(channel.driver, "_requester", lambda requester_id: requester_id or "owner")
    request = CallToolRequest(params={
        "name": "get_execute_js_result", "arguments": {"operation_id": operation_id},
    })
    result = asyncio.run(S.mcp._mcp_server.request_handlers[CallToolRequest](request)).root
    assert isinstance(result, CallToolResult) and result.isError is True
    late = result.structuredContent["legacy"]["late_result"]
    assert late["success"] is True and late["data"] is None
    payload = Path(late["result_file"]).read_bytes()
    assert json.loads(payload) == value
    assert late["result_bytes"] == len(payload)
    assert late["result_sha256"] == hashlib.sha256(payload).hexdigest()
    assert late["result_externalized"] is True
    assert value["text"] not in result.content[0].text
    # Exporting a query must not overwrite the retained raw browser result.
    retained = channel.driver.get_execute_js_result(operation_id, requester_id="owner")
    assert retained["late_result"]["data"] == value
