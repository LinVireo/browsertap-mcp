"""Bound daemon state without discarding pending side effects or late replies."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from browsertap_mcp import browser_bridge as bridge_module
from browsertap_mcp import pending_operations as pending_module
from browsertap_mcp.pending_operations import OperationAccessError, PendingOperations
from tests.test_pending_bridge_operations import make_bridge


@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS", 2, raising=False)
    monkeypatch.setattr(pending_module, "MAX_RECOVERY_OPERATIONS", 1, raising=False)
    monkeypatch.setattr(pending_module, "MAX_COMPLETED_OPERATIONS", 2)


def test_full_registry_refuses_new_work_without_releasing_unknown_operations(small_limits):
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner")
    operations.complete("a", {"success": False}, outcome_unknown=True)
    operations.reserve("b", ["browser:2"], "owner")

    with pytest.raises(RuntimeError, match="capacity") as caught:
        operations.reserve("c", ["browser:3"], "owner")

    assert caught.value.error_code == "operation_capacity_exceeded"
    assert caught.value.delivery_state == "undelivered"
    assert caught.value.retry_safe is True
    assert set(operations.pending_ids()) == {"a", "b"}
    assert operations.read("a", "owner")["reservation_held"] is True
    assert "browser:3" not in operations._targets


def test_recovery_headroom_is_available_but_bounded(small_limits):
    operations = PendingOperations()
    for key in ("a", "b"):
        operations.reserve(key, [key], "owner")
    operations.reserve("recover-a", ["a"], "owner", cleanup=True)
    with pytest.raises(RuntimeError, match="capacity"):
        operations.reserve("recover-b", ["b"], "owner", cleanup=True)
    with pytest.raises(RuntimeError, match="capacity"):
        operations.reserve("ordinary", ["c"], "owner")
    assert operations._recoveries == {"a": "recover-a"}
    operations.complete("recover-a", {"success": True})
    operations.reserve("recover-b", ["b"], "owner", cleanup=True)
    assert set(operations.pending_ids()) == {"a", "b", "recover-b"}


@pytest.mark.parametrize("settlement", ["complete", "finish_target", "forget"])
def test_settlement_reopens_capacity(small_limits, settlement):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    operations.reserve("b", ["b"], "owner")
    if settlement == "complete":
        operations.complete("a", {"success": True})
    elif settlement == "finish_target":
        operations.finish_target("a", "tab_removed")
    else:
        operations.forget("a")
    operations.reserve("c", ["c"], "owner")
    with pytest.raises(RuntimeError, match="capacity"):
        operations.reserve("d", ["d"], "owner")


def test_concurrent_admission_cannot_overbook_capacity(small_limits):
    operations = PendingOperations()
    barrier = threading.Barrier(8)

    def reserve(index):
        barrier.wait(timeout=5)
        try:
            operations.reserve(str(index), [str(index)], "owner")
        except RuntimeError as error:
            assert error.error_code == "operation_capacity_exceeded"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(reserve, range(8)))
    assert sum(admitted) == 2
    assert len(operations.pending_ids()) == 2


@pytest.mark.parametrize("extension_command", [False, True])
def test_capacity_refusal_does_not_dispatch_and_survives_remote_envelope(small_limits, extension_command):
    bridge = make_bridge()
    for key in ("a", "b"):
        bridge._operation_state().reserve(key, [key], "owner")

    with pytest.raises(RuntimeError, match="capacity") as caught:
        if extension_command:
            bridge.ext_cmd({"cmd": "tabs", "method": "list"}, requester_id="owner")
        else:
            bridge.execute_js("side effect", requester_id="owner")

    assert bridge.sent == []
    payload = bridge_module._error_payload(caught.value)
    with pytest.raises(bridge_module.BridgeNoResponseError) as remote:
        bridge_module._raise_remote_error(payload)
    assert remote.value.error_code == "operation_capacity_exceeded"
    assert remote.value.delivery_state == "undelivered"
    assert remote.value.retry_safe is True


def test_wire_caches_follow_retained_operations_not_traffic_volume(small_limits):
    bridge = make_bridge()
    operations = bridge._operation_state()
    socket = bridge.ext_clients["browser"]["ws"]
    operations.reserve("pending", ["pending"], "owner", reply_transport="ws", reply_owner=socket)
    assert bridge._record_operation_reply({"type": "ack", "id": "pending"}, transport="ws", owner=socket)
    for index in range(10):
        key = str(index)
        operations.reserve(key, [key], "owner", reply_transport="ws", reply_owner=socket)
        assert bridge._record_operation_reply({"type": "ack", "id": key}, transport="ws", owner=socket)
        assert bridge._record_operation_reply(
            {"type": "result", "id": key, "result": index}, transport="ws", owner=socket,
        )

    assert set(bridge.results) == {"8", "9"}
    assert set(bridge.acks) <= {"pending", "8", "9"}
    assert operations.read("pending", "owner")["acknowledged"] is True
    with pytest.raises(OperationAccessError, match="operation_not_found"):
        operations.read("0", "owner")
    assert bridge.get_execute_js_result("9", requester_id="owner")["data"] == 9
    assert bridge.get_execute_js_result("8", requester_id="owner")["data"] == 8


@pytest.mark.parametrize("extension_command", [False, True])
def test_reply_published_at_timeout_remains_claimable(monkeypatch, extension_command):
    bridge = make_bridge()
    now = [100.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: now[0])

    def wait(serial, timeout):
        now[0] += 2.0
        return serial

    monkeypatch.setattr(bridge, "_wait_for_activity", wait)

    class DeadlineReplies(dict):
        misses = 0

        def pop(self, key, default=None):
            result = super().pop(key, default)
            if result is default:
                self.misses += 1
                if self.misses == 3:
                    # The ingress thread has published but has not notified yet.
                    self[key] = {"success": True, "data": "late", "ts": time.time()}
            return result

    bridge.results = DeadlineReplies()

    def send(message):
        bridge.sent.append(json.loads(message))

    bridge.ext_clients["browser"]["ws"].send_message = send
    if extension_command:
        with pytest.raises(TimeoutError):
            bridge.ext_cmd({"cmd": "tabs", "method": "pending", "tabId": 1}, timeout=1, requester_id="owner")
        operation_id = bridge.sent[-1]["id"]
    else:
        result = bridge.execute_js("pending", timeout=1, requester_id="owner")
        operation_id = result["operation_id"]
        assert result["delivery_state"] == "sent_unconfirmed"
        assert result["retry_safe"] is False
    assert bridge.get_execute_js_result(operation_id, requester_id="owner")["data"] == "late"
    assert len(bridge.sent) == 1


def complete_other_operations(bridge, count):
    operations = bridge._operation_state()
    socket = bridge.ext_clients["browser"]["ws"]
    for index in range(count):
        key = f"other-{index}"
        operations.reserve(key, [key], "other", reply_transport="ws", reply_owner=socket)
        assert bridge._record_operation_reply(
            {"type": "result", "id": key, "result": index}, transport="ws", owner=socket,
        )


@pytest.mark.parametrize("extension_command", [False, True])
def test_completed_result_survives_cache_pressure_until_waiting_caller_returns(
    monkeypatch, extension_command,
):
    monkeypatch.setattr(pending_module, "MAX_COMPLETED_OPERATIONS", 2)
    bridge = make_bridge()
    socket = bridge.ext_clients["browser"]["ws"]

    def send(message):
        payload = json.loads(message)
        bridge.sent.append(payload)
        assert bridge._record_operation_reply(
            {"type": "result", "id": payload["id"], "result": "own result"},
            transport="ws", owner=socket,
        )
        # Complete a burst before the original caller can resume its wait loop.
        complete_other_operations(bridge, 4)

    socket.send_message = send
    if extension_command:
        reply = bridge.ext_cmd({"cmd": "tabs", "method": "list"}, timeout=0.05, requester_id="owner")
    else:
        reply = bridge.execute_js("once", timeout=0.05, requester_id="owner")
    assert reply.get("data") == "own result"
    assert len(bridge.sent) == 1
    bridge._sync_pending_operations()
    assert len(bridge._operation_state().retained_ids()) <= 2
    assert len(bridge.results) <= 2


def test_result_poll_survives_cache_pressure_while_waiting(monkeypatch):
    monkeypatch.setattr(pending_module, "MAX_COMPLETED_OPERATIONS", 2)
    bridge = make_bridge()
    pending = bridge.execute_js("pending", wait=False, timeout=0.1, requester_id="owner")
    socket = bridge.ext_clients["browser"]["ws"]

    def wait(serial, timeout):
        assert bridge._record_operation_reply(
            {"type": "result", "id": pending["operation_id"], "result": "late result"},
            transport="ws", owner=socket,
        )
        complete_other_operations(bridge, 4)
        return serial

    monkeypatch.setattr(bridge, "_wait_for_activity", wait)
    assert bridge.get_execute_js_result(
        pending["operation_id"], timeout=1, requester_id="owner",
    )["data"] == "late result"
    assert len(bridge.sent) == 1


def test_waiting_completed_results_still_count_toward_admission_limit(small_limits):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    operations.reserve("b", ["b"], "owner")
    with operations.retain_for_waiter("a", "owner"):
        operations.complete("a", {"success": True})
        with pytest.raises(RuntimeError, match="capacity"):
            operations.reserve("c", ["c"], "owner")
    operations.reserve("c", ["c"], "owner")


def test_nested_waiters_protect_expired_result_until_last_waiter_exits(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(pending_module.time, "monotonic", lambda: now[0])
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    with operations.retain_for_waiter("a", "owner"):
        with operations.retain_for_waiter("a", "owner"):
            operations.complete("a", {"success": True, "data": 42})
            now[0] += pending_module.RESULT_TTL_SECONDS + 1
            assert operations.read("a", "owner")["wire_result"]["data"] == 42
        assert operations.read("a", "owner")["wire_result"]["data"] == 42
    with pytest.raises(OperationAccessError, match="operation_not_found"):
        operations.read("a", "owner")


def test_foreign_waiter_cannot_retain_result():
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    with pytest.raises(OperationAccessError, match="operation_owner_mismatch"):
        with operations.retain_for_waiter("a", "other"):
            pytest.fail("foreign waiter entered")
    assert operations._operations["a"].waiters == 0


@pytest.mark.parametrize("extension_command", [False, True])
@pytest.mark.parametrize("send_error", [False, True])
def test_waiter_releases_retention_on_timeout_or_dispatch_error(extension_command, send_error):
    bridge = make_bridge()

    def send(message):
        bridge.sent.append(json.loads(message))
        if send_error:
            raise OSError("connection lost")

    bridge.ext_clients["browser"]["ws"].send_message = send
    if extension_command or send_error:
        expected_error = (
            bridge_module.SessionDisconnectedError if not extension_command
            else bridge_module.BridgeNoResponseError if send_error else TimeoutError
        )
        with pytest.raises(expected_error):
            if extension_command:
                bridge.ext_cmd({"cmd": "tabs", "method": "list"}, timeout=0.01, requester_id="owner")
            else:
                bridge.execute_js("once", timeout=0.01, requester_id="owner")
    else:
        bridge.execute_js("once", timeout=0.01, requester_id="owner")
    operation = bridge._operation_state()._operations[bridge.sent[0]["id"]]
    assert operation.waiters == 0
    assert operation.status == "in_progress"
