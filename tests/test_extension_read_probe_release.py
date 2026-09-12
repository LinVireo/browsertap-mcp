"""A silent browser inventory must not reserve every browser-wide tabs command."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from browsertap_mcp import browser_bridge as T
from browsertap_mcp import pending_operations as P
from browsertap_mcp import server as S
from browsertap_mcp.command_scope import TargetBusyError, command_scope
from browsertap_mcp.paths import STATE_DIR_ENV
from tests.test_command_scope import _probe, lock_environment  # noqa: F401
from tests.test_pending_bridge_operations import make_bridge


@pytest.fixture
def silent_tabs(monkeypatch, tmp_path):
    """Use the real bridge/registry with a synthetic peer and deterministic time."""
    bridge = make_bridge()
    now = [1000.0]
    peer = SimpleNamespace(respond=False)
    socket = bridge.ext_clients["browser"]["ws"]
    bridge.rejected_operation_replies = 0
    bridge._pending_operations = P.PendingOperations(requester_liveness=lambda _owner: "dead")
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(T, "process_requester_id", lambda: "probe-owner")
    monkeypatch.setattr(S, "require_driver", lambda: bridge)
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", S._TabOwnershipRegistry())
    monkeypatch.setattr(T, "bridge_token", lambda: pytest.fail("offline probe read a real token"))
    monkeypatch.setattr(T.BrowserBridge, "_remote_cmd", lambda *_args, **_kwargs: pytest.fail("live traffic"))
    monkeypatch.setattr(T.time, "monotonic", lambda: now[0])

    def wait_activity(serial, timeout):
        now[0] += timeout
        return serial

    def send(message):
        payload = json.loads(message)
        bridge.sent.append(payload)
        if peer.respond:
            data = []
            if payload["cmd"].get("method") == "create_status":
                data = {"operation_id": payload["cmd"]["operation_id"], "operation_status": "not_found"}
            assert bridge._record_operation_reply(
                {"type": "result", "id": payload["id"], "result": data}, transport="ws", owner=socket,
            )

    monkeypatch.setattr(bridge, "_wait_for_activity", wait_activity)
    monkeypatch.setattr(socket, "send_message", send)
    return bridge, peer, now


READS = [
    {"cmd": "tabs"},
    {"cmd": "tabs", "all": True},
    {"cmd": "tabs", "method": "create_status", "operation_id": "open-tab-fixture"},
]


@pytest.mark.parametrize("command", READS)
def test_read_timeout_releases_target_and_keeps_collectible_receipt(silent_tabs, command):
    bridge, peer, now = silent_tabs
    with pytest.raises(TimeoutError) as caught:
        bridge.ext_cmd(command, timeout=3, requester_id="probe-owner", operation_id="wire-probe")
    assert now[0] == pytest.approx(1003)
    receipt = bridge.get_execute_js_result("wire-probe", requester_id="probe-owner")
    assert receipt["status"] == "in_progress"
    assert receipt["reservation_held"] is False
    assert caught.value.diagnostics["reservation_held"] is False
    assert caught.value.retry_safe is False
    assert caught.value.poll_with == "get_execute_js_result"
    peer.respond = True
    assert bridge.ext_cmd({"cmd": "tabs", "all": True}, requester_id="next")["data"] == []
    assert len(bridge.sent) == 2


def test_late_reply_cannot_release_the_successor_or_change_socket_ownership(silent_tabs):
    bridge, _, _ = silent_tabs
    with pytest.raises(TimeoutError):
        bridge.ext_cmd(READS[2], timeout=0.2, requester_id="probe-owner", operation_id="wire-probe")
    with pytest.raises(TimeoutError):
        bridge.ext_cmd(
            {"cmd": "tabs", "method": "create", "operation_id": "new-create"},
            timeout=0.2, requester_id="next", operation_id="wire-create",
        )
    reply = {"type": "result", "id": "wire-probe", "result": {"operation_status": "not_found"}}
    assert bridge._record_operation_reply(reply, transport="ws", owner=object()) is False
    assert bridge._record_operation_reply(
        reply, transport="ws", owner=bridge.ext_clients["browser"]["ws"],
    ) is True
    receipt = bridge.get_execute_js_result("wire-probe", requester_id="probe-owner")
    assert receipt["status"] == "success" and receipt["reservation_held"] is False
    assert bridge.get_execute_js_result("wire-probe", requester_id="probe-owner") == receipt
    assert bridge.get_execute_js_result("wire-create", requester_id="next")["reservation_held"] is True
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd({"cmd": "tabs", "all": True}, requester_id="third")
    assert len(bridge.sent) == 2


@pytest.mark.parametrize("command", [
    {"cmd": "tabs", "method": "create", "operation_id": "new-create"},
    {"cmd": "tabs", "method": "close", "tabId": 1},
    {"cmd": "tabs", "method": "switch", "tabId": 1},
    {"cmd": "tabs", "method": "remove", "tabId": 1},
    {"cmd": "tabs", "method": "future-action"},
    {"cmd": "tabs", "method": None},
    {"cmd": "tabs", "method": ""},
    {"cmd": "batch", "commands": [{"cmd": "tabs", "all": True}]},
    {"cmd": "cdp", "tabId": 1},
])
def test_writes_and_non_whitelisted_commands_keep_their_reservation(silent_tabs, command):
    bridge, _, _ = silent_tabs
    with pytest.raises(TimeoutError):
        bridge.ext_cmd(command, timeout=0.2, requester_id="original", operation_id="wire-write")
    assert bridge.get_execute_js_result("wire-write", requester_id="original")["reservation_held"] is True
    # The injected owner liveness says dead; a read cannot use that to steal it.
    successor = {"cmd": "tabs", "all": True}
    if "tabId" in command:
        successor["tabId"] = command["tabId"]
    if command["cmd"] == "batch":
        successor = command
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd(successor, timeout=0.2, requester_id="other")
    assert len(bridge.sent) == 1


def test_pending_read_cannot_be_preempted_before_its_own_timeout(silent_tabs, monkeypatch):
    bridge, peer, _ = silent_tabs
    entered, release = threading.Event(), threading.Event()
    socket = bridge.ext_clients["browser"]["ws"]
    send = socket.send_message

    def blocked_send(message):
        entered.set()
        assert release.wait(timeout=5)
        send(message)

    monkeypatch.setattr(socket, "send_message", blocked_send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            bridge.ext_cmd, READS[2], timeout=0.2,
            requester_id="original", operation_id="wire-probe",
        )
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(TargetBusyError):
                bridge.ext_cmd(READS[0], requester_id="other")
        finally:
            release.set()
        with pytest.raises(TimeoutError):
            pending.result(timeout=5)
    peer.respond = True
    assert bridge.ext_cmd(READS[0], requester_id="other")["data"] == []


def test_timeout_does_not_release_a_live_mcp_scope_os_lock(silent_tabs, request):
    bridge, _, _ = silent_tabs
    environment = request.getfixturevalue("lock_environment")
    target = "browser/extension/tabs"
    with command_scope():
        with pytest.raises(TimeoutError):
            bridge.ext_cmd(READS[2], timeout=0.2, requester_id="original", operation_id="wire-probe")
        assert bridge.get_execute_js_result("wire-probe", requester_id="original")["reservation_held"] is False
        assert _probe(environment, [target])["status"] == "busy"
    assert _probe(environment, [target])["status"] == "acquired"


@pytest.mark.parametrize("resuming", [False, True])
def test_open_tab_keeps_wire_probe_separate_from_the_create_handle(silent_tabs, resuming):
    bridge, peer, _ = silent_tabs
    options = {"operation_id": "open-tab-original", "owner_id": "owned-capability"} if resuming else {}
    outcome = S.open_new_tab("https://synthetic.invalid/", client_id="browser", **options)
    assert outcome["status"] == "unknown"
    assert outcome["may_have_created"] is resuming
    assert outcome["retry_safe"] is not resuming
    assert outcome["owned"] is False
    assert outcome["reconciliation"]["phase"] == "client_discovery"
    wire = outcome["reconciliation"]["bridge_operation"]
    assert wire["operation_id"] == bridge.sent[0]["id"]
    assert wire["operation_id"] != outcome["operation_id"]
    assert wire["reservation_held"] is False
    assert wire["poll_with"] == "get_execute_js_result"
    assert wire["retry_safe"] is False
    assert len(bridge.sent) == 1 and bridge.sent[0]["cmd"]["method"] == "create_status"
    envelope = S._result_envelope("open_new_tab", outcome)
    assert envelope["diagnostics"]["operation_id"] == outcome["operation_id"]
    assert envelope["legacy"]["reconciliation"]["bridge_operation"] == wire
    if resuming:
        assert outcome["owner_id"] == "owned-capability"
        assert outcome["recovery"]["operation_id"] == "open-tab-original"
        assert "open_new_tab again" in outcome["recovery"]["instruction"]
    else:
        assert outcome["owner_id"] is None and "recovery" not in outcome
    peer.respond = True
    assert bridge.ext_cmd(READS[0], requester_id="next")["data"] == []


@pytest.mark.parametrize("lost_http_reply", [False, True])
def test_remote_probe_reports_only_confirmed_release_and_preserves_wire_id(silent_tabs, monkeypatch, lost_http_reply):
    local, _, _ = silent_tabs
    remote = make_bridge()
    remote.is_remote = True
    sent = []

    def route(payload, timeout):
        sent.append(payload)
        try:
            result = local.ext_cmd(
                payload["payload"], client_id=payload["clientId"], timeout=float(payload["timeout"]),
                requester_id=payload["requesterId"], operation_id=payload["operationId"],
            )
        except TimeoutError as exc:
            if lost_http_reply:
                raise TimeoutError("synthetic lost HTTP response") from None
            return {"r": T._error_payload(exc)}
        return {"r": result}

    monkeypatch.setattr(remote, "_remote_cmd", route)
    monkeypatch.setattr(S, "require_driver", lambda: remote)
    outcome = S.open_new_tab("https://synthetic.invalid/", client_id="browser")
    wire = outcome["reconciliation"]["bridge_operation"]
    assert wire["operation_id"] == sent[0]["operationId"] == local.sent[0]["id"]
    assert wire["operation_id"] != outcome["operation_id"]
    assert wire.get("reservation_held") is (None if lost_http_reply else False)
    assert local.get_execute_js_result(wire["operation_id"], requester_id="probe-owner")["reservation_held"] is False
    assert outcome["retry_safe"] is True and outcome["may_have_created"] is False
    assert len(sent) == 1


def test_expired_read_keeps_late_evidence_without_releasing_a_successor(silent_tabs):
    bridge, _, now = silent_tabs
    with pytest.raises(TimeoutError):
        bridge.ext_cmd(READS[2], timeout=0.2, requester_id="original", operation_id="wire-probe")
    operation = bridge._operation_state()._operations["wire-probe"]
    now[0] += P.SILENT_GRACE_SECONDS + 0.1
    abandoned = bridge.get_execute_js_result("wire-probe", requester_id="original")
    assert abandoned["status"] == "unknown" and abandoned["retry_safe"] is False
    assert abandoned["reservation_held"] is False
    timestamps = operation.started_at, operation.silent_after, operation.completed_at
    with pytest.raises(TimeoutError):
        bridge.ext_cmd(
            {"cmd": "tabs", "method": "create", "operation_id": "next-create"},
            timeout=0.2, requester_id="next", operation_id="wire-create",
        )
    assert bridge._record_operation_reply(
        {"type": "result", "id": "wire-probe", "result": {"operation_status": "not_found"}},
        transport="ws", owner=bridge.ext_clients["browser"]["ws"],
    )
    late = bridge.get_execute_js_result("wire-probe", requester_id="original")
    assert late["status"] == "unknown" and late["retry_safe"] is False
    assert late["late_result"]["data"] == {"operation_status": "not_found"}
    assert (operation.started_at, operation.silent_after, operation.completed_at) == timestamps
    assert bridge.get_execute_js_result("wire-create", requester_id="next")["reservation_held"] is True
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd(READS[0], requester_id="third")
    assert len(bridge.sent) == 2


def test_uncertain_reply_on_a_probe_keeps_its_reservation(silent_tabs, monkeypatch):
    bridge, _, _ = silent_tabs
    socket = bridge.ext_clients["browser"]["ws"]

    def uncertain_reply(message):
        payload = json.loads(message)
        bridge.sent.append(payload)
        assert bridge._record_operation_reply({
            "type": "error", "id": payload["id"],
            "error": {"code": "cdp_timeout", "message": "synthetic uncertain result", "dispatched": True},
        }, transport="ws", owner=socket)

    monkeypatch.setattr(socket, "send_message", uncertain_reply)
    with pytest.raises(T.PageExecutionError):
        bridge.ext_cmd(READS[2], requester_id="original", operation_id="wire-probe")
    assert bridge._operation_state().release_tabs_probe("wire-probe", "original") is False
    assert bridge.get_execute_js_result("wire-probe", requester_id="original")["reservation_held"] is True
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd(READS[0], requester_id="next")
    assert len(bridge.sent) == 1


def test_reconciliation_timeout_preserves_probe_receipt_without_replaying_create(silent_tabs, monkeypatch):
    bridge, _, _ = silent_tabs
    socket = bridge.ext_clients["browser"]["ws"]

    def pending_create(message):
        payload = json.loads(message)
        bridge.sent.append(payload)
        if len(bridge.sent) <= 2:
            assert bridge._record_operation_reply({
                "type": "result", "id": payload["id"], "result": {
                    "operation_id": payload["cmd"]["operation_id"],
                    "operation_status": "not_found" if len(bridge.sent) == 1 else "pending",
                },
            }, transport="ws", owner=socket)

    monkeypatch.setattr(socket, "send_message", pending_create)
    outcome = S.open_new_tab("https://synthetic.invalid/", timeout=1, client_id="browser", owner_id="owner")
    assert outcome["status"] == "unknown" and outcome["may_have_created"] is True
    assert outcome["retry_safe"] is False and outcome["owner_id"] == "owner"
    assert outcome["recovery"]["operation_id"] == outcome["operation_id"]
    assert outcome["reconciliation"]["phase"] == "reconciliation"
    wire = outcome["reconciliation"]["bridge_operation"]
    assert wire["operation_id"] == bridge.sent[-1]["id"]
    assert wire["operation_id"] != outcome["operation_id"]
    assert wire["reservation_held"] is False
    assert [message["cmd"]["method"] for message in bridge.sent].count("create") == 1
