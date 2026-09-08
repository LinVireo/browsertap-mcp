from __future__ import annotations

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from browsertap_mcp.browser_bridge import BrowserBridge, PageExecutionError, Session
from browsertap_mcp.capture_ownership import CaptureBusyError
from browsertap_mcp.command_scope import TargetBusyError
from browsertap_mcp.pending_operations import OperationAccessError, PendingOperations


def make_bridge():
    bridge = BrowserBridge.__new__(BrowserBridge)
    bridge.host, bridge.port = "127.0.0.1", 18765
    bridge.is_remote = False
    bridge.sessions, bridge.results, bridge.acks = {}, {}, {}
    bridge.ext_clients, bridge.client_last_seen = {}, {}
    bridge.default_session_id = "browser:1"
    bridge.latest_session_id = "browser:1"
    bridge._activity_condition = threading.Condition()
    bridge._activity_serial = 0
    bridge.sent = []

    def send(message):
        payload = json.loads(message)
        bridge.sent.append(payload)
        bridge.acks[payload["id"]] = time.time()
        if payload.get("code") != "pending" and payload.get("cmd", {}).get("method") != "pending":
            bridge.results[payload["id"]] = {
                "success": True, "data": {"ok": True},
                "tabId": payload.get("tabId"), "ts": time.time(),
            }
        bridge._notify_activity()

    socket = SimpleNamespace(send_message=send)
    bridge.ext_clients["browser"] = {"ws": socket}
    for tab_id in (1, 2):
        bridge.sessions[f"browser:{tab_id}"] = Session(
            f"browser:{tab_id}", {
                "type": "ext_ws", "client_id": "browser", "tab_id": tab_id,
                "url": "https://example.test", "generation": "original",
            }, socket,
        )
    return bridge


def finish(bridge, operation_id, value, *, success=True):
    bridge.results[operation_id] = {"success": success, "data": value, "ts": time.time()}
    bridge._notify_activity()


def reply_on_send(bridge, *, success=True, data=None):
    def send(message):
        payload = json.loads(message)
        bridge.sent.append(payload)
        bridge.acks[payload['id']] = time.time()
        bridge.results[payload['id']] = {'success': success, 'data': data, 'ts': time.time()}
        bridge._notify_activity()

    bridge.ext_clients['browser']['ws'].send_message = send


def test_async_ack_keeps_same_tab_busy_and_other_tab_available_until_result():
    bridge = make_bridge()
    pending = bridge.execute_js("pending", wait=False, timeout=0.2, requester_id="a")
    assert pending["status"] == "in_progress"
    assert len(bridge.sent) == 1
    for requester in ("a", "b"):
        with pytest.raises(TargetBusyError):
            bridge.execute_js("second", session_id="browser:1", requester_id=requester)
        with pytest.raises(TargetBusyError):
            bridge.ext_cmd({"cmd": "cdp", "tabId": 1}, client_id="browser", requester_id=requester)
    bridge.execute_js("other", session_id="browser:2", requester_id="b")
    assert len(bridge.sent) == 2

    finish(bridge, pending["operation_id"], 42)
    bridge.execute_js("after", session_id="browser:1", requester_id="b")
    result = bridge.get_execute_js_result(pending["operation_id"], requester_id="a")
    assert result["status"] == "success" and result["data"] == 42
    assert bridge.get_execute_js_result(pending["operation_id"], requester_id="a") == result


def test_sync_timeout_retains_result_and_ownership_after_call_returns():
    bridge = make_bridge()
    timed_out = bridge.execute_js("pending", timeout=0.01, requester_id="a")
    assert timed_out["delivery_state"] == "delivered_no_result"
    operation_id = timed_out["operation_id"]
    with pytest.raises(TargetBusyError):
        bridge.execute_js("second", requester_id="b")
    with pytest.raises(OperationAccessError, match="operation_owner_mismatch"):
        bridge.get_execute_js_result(operation_id, requester_id="b")
    finish(bridge, operation_id, {"saved": True})
    assert bridge.get_execute_js_result(operation_id, requester_id="a")["data"] == {"saved": True}


def test_result_wait_wakes_without_replaying_script():
    bridge = make_bridge()
    pending = bridge.execute_js("pending", wait=False, timeout=0.2, requester_id="a")
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(bridge.get_execute_js_result, pending["operation_id"], 1, requester_id="a")
        finish(bridge, pending["operation_id"], "done")
        assert result.result(timeout=2)["data"] == "done"
    assert len(bridge.sent) == 1


def test_finish_tab_operations_ignores_malformed_native_tab_id():
    bridge = make_bridge()
    bridge._operation_state().reserve("op", ["browser:not-number"], "a")

    bridge._finish_tab_operations("browser:not-number", "tab_removed")

    result = bridge._operation_state().read("op", "a")
    assert result["status"] == "lifecycle_ended"
    assert result["lifecycle_reason"] == "tab_removed"


def test_finish_tab_operations_still_releases_capture_for_numeric_tab_id():
    bridge = make_bridge()
    capture = bridge._capture_state().prepare(
        "browser", {"cmd": "console", "method": "start", "tabId": 1}, "a",
    )
    bridge._capture_commands["capture"] = capture

    bridge._finish_tab_operations("browser:1", "tab_removed")

    assert bridge._capture_state()._owners == {}
    assert "capture" not in bridge._capture_commands


def test_extension_socket_loss_does_not_release_a_running_page_script():
    bridge = make_bridge()
    bridge.execute_js("pending", wait=False, timeout=0.2, requester_id="a")
    socket = bridge.ext_clients["browser"]["ws"]
    bridge._unregister_client(socket)
    bridge._claim_ext_client("browser", "chrome", socket)
    bridge._apply_extension_tabs("browser", "chrome", [
        {"id": 1, "url": "https://example.test", "generation": "original"},
        {"id": 2, "url": "https://example.test", "generation": "original"},
    ], socket)
    with pytest.raises(TargetBusyError):
        bridge.execute_js("second", requester_id="b")


def test_explicit_tab_removal_terminates_operation_without_accepting_late_result():
    bridge = make_bridge()
    pending = bridge.execute_js("pending", wait=False, timeout=0.2, requester_id="a")
    bridge._apply_extension_tabs("browser", "chrome", [], bridge.ext_clients["browser"]["ws"])
    finish(bridge, pending["operation_id"], "old page")
    result = bridge.get_execute_js_result(pending["operation_id"], requester_id="a")
    assert result["status"] == "navigated"
    assert result["lifecycle_reason"] == "tab_removed"
    assert "data" not in result


def test_ext_command_transport_timeout_keeps_tab_busy_until_reply():
    bridge = make_bridge()
    with pytest.raises(TimeoutError):
        bridge.ext_cmd({"cmd": "cdp", "method": "pending", "tabId": 1}, timeout=0.01, requester_id="a")
    operation_id = bridge.sent[-1]["id"]
    with pytest.raises(TargetBusyError):
        bridge.execute_js("second", requester_id="b")
    finish(bridge, operation_id, {"ok": True})
    bridge.execute_js("after", requester_id="b")


def test_capture_owner_can_use_tab_but_other_agent_cannot_stop_or_clear():
    bridge = make_bridge()
    bridge.ext_cmd({"cmd": "console", "method": "start", "tabId": 1}, requester_id="a")
    bridge.execute_js("input", requester_id="a")
    bridge.ext_cmd({"cmd": "console", "method": "get", "tabId": 1, "clear": False}, requester_id="b")
    for command in (
        {"cmd": "console", "method": "stop", "tabId": 1},
        {"cmd": "console", "method": "get", "tabId": 1, "clear": True},
        {"cmd": "console", "method": "start", "tabId": 1},
    ):
        with pytest.raises(CaptureBusyError):
            bridge.ext_cmd(command, requester_id="b")
    bridge.ext_cmd({"cmd": "console", "method": "stop", "tabId": 1}, requester_id="a")
    bridge.ext_cmd({"cmd": "console", "method": "start", "tabId": 1}, requester_id="b")


def test_finishing_one_target_does_not_release_other_members_of_a_batch():
    operations = PendingOperations()
    operations.reserve("batch", ["browser:1", "browser:2"], "a")
    operations.finish_target("browser:1", "tab_removed")
    operations.reserve("new-one", ["browser:1"], "b")
    with pytest.raises(TargetBusyError):
        operations.reserve("new-two", ["browser:2"], "b")


def test_remote_async_and_result_lookup_keep_the_same_requester_without_tab_lock(monkeypatch):
    bridge = make_bridge()
    bridge.is_remote = True
    sent = []

    def remote(payload, timeout):
        sent.append(payload)
        if payload["cmd"] == "execute_js":
            return {"r": {"status": "in_progress", "operation_id": payload["operationId"]}}
        return {"r": {"status": "success", "data": 4}}

    bridge._remote_cmd = remote
    pending = bridge.execute_js("pending", wait=False, session_id="browser:1")
    monkeypatch.setattr("browsertap_mcp.browser_bridge.guard_targets", lambda *args: pytest.fail("polling took a tab lock"))
    assert bridge.get_execute_js_result(pending["operation_id"])["data"] == 4
    assert sent[0]["wait"] == "0"
    assert sent[0]["requesterId"] == sent[1]["requesterId"]


@pytest.mark.parametrize('detail', [
    {'code': 'cdp_timeout', 'message': 'evaluation timed out', 'dispatched': True},
    'cdp_timeout: Runtime.evaluate exceeded 20000ms',
    'CSP-relaxed injection timed out',
])
def test_extension_watchdog_keeps_reservation_and_repeatedly_readable_result(detail):
    bridge = make_bridge()
    reply_on_send(bridge, success=False, data=detail)
    with pytest.raises(PageExecutionError) as caught:
        bridge.execute_js('pending', requester_id='a')
    operation_id = caught.value.operation_id
    assert caught.value.diagnostics['reservation_held'] is True
    for _ in range(2):
        reply = bridge.get_execute_js_result(operation_id, requester_id='a')
        assert reply['status'] == 'in_progress'
        assert reply['operation_status'] == 'outcome_unknown'
        assert reply['retry_safe'] is False
        with pytest.raises(TargetBusyError):
            bridge.execute_js('second', requester_id='a')
    finish(bridge, operation_id, 9)
    assert bridge.get_execute_js_result(operation_id, requester_id='a')['data'] == 9


@pytest.mark.parametrize('detail', [
    {'code': 'cdp_timeout', 'message': 'not dispatched', 'dispatched': False},
    'cdp_timeout: debugger attach exceeded 5000ms',
    {'message': 'ordinary JavaScript exception'},
])
def test_terminal_extension_errors_release_tab(detail):
    bridge = make_bridge()
    reply_on_send(bridge, success=False, data=detail)
    with pytest.raises(PageExecutionError):
        bridge.execute_js('fails', requester_id='a')
    reply_on_send(bridge, data='after')
    assert bridge.execute_js('after', requester_id='b')['data'] == 'after'


def test_ext_command_watchdog_does_not_forget_its_live_reservation():
    bridge = make_bridge()
    reply_on_send(bridge, success=False, data='cdp_timeout: Runtime.evaluate exceeded 100ms')
    with pytest.raises(PageExecutionError) as caught:
        bridge.ext_cmd({'cmd': 'cdp', 'tabId': 1, 'method': 'Runtime.evaluate'}, requester_id='a')
    assert caught.value.operation_id
    with pytest.raises(TargetBusyError):
        bridge.execute_js('second', requester_id='b')


def test_owner_close_is_a_recovery_command_and_waits_for_real_tab_removal():
    bridge = make_bridge()
    pending = bridge.execute_js('pending', wait=False, requester_id='a')
    close = {'cmd': 'tabs', 'method': 'close', 'tabId': 1}
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd(close, requester_id='b')
    bridge.ext_cmd(close, requester_id='a')
    with pytest.raises(TargetBusyError):
        bridge.execute_js('still running', requester_id='a')
    bridge._apply_extension_tabs('browser', 'chrome', [
        {'id': 2, 'url': 'https://example.test', 'generation': 'original'},
    ], bridge.ext_clients['browser']['ws'])
    assert bridge.get_execute_js_result(pending['operation_id'], requester_id='a')['status'] == 'navigated'


def test_close_ack_settles_uncertain_operation_before_tabs_snapshot_arrives():
    """A successful tabs.remove reply must close the lifecycle race window."""
    bridge = make_bridge()
    pending = bridge.execute_js('pending', wait=False, requester_id='a')
    finish(bridge, pending['operation_id'], {
        'code': 'cdp_timeout', 'message': 'detached', 'dispatched': True,
    }, success=False)
    assert bridge.get_execute_js_result(
        pending['operation_id'], requester_id='a',
    )['operation_status'] == 'outcome_unknown'
    reply_on_send(bridge, data={'closed': [1], 'alreadyGone': []})
    bridge.ext_cmd(
        {'cmd': 'tabs', 'method': 'close', 'tabId': 1}, requester_id='a',
    )
    result = bridge.get_execute_js_result(pending['operation_id'], requester_id='a')
    assert result['status'] == 'navigated'
    assert result['lifecycle_reason'] == 'tab_removed'
    assert result['reservation_held'] is False


def test_manual_reply_and_dialog_handling_do_not_claim_the_script_ended():
    bridge = make_bridge()
    pending = bridge.execute_js('pending', wait=False, requester_id='a')
    finish(bridge, pending['operation_id'], {
        '__btap_dialog_result': True, 'pending_execution': True, 'status': 'blocked_by_dialog',
        'dialog': {'type': 'confirm', 'message': 'Continue?'},
    })
    snapshot = bridge.get_execute_js_result(pending['operation_id'], requester_id='a')
    assert snapshot['operation_status'] == 'blocked_by_dialog'
    dialog = {'cmd': 'handle_dialog', 'tabId': 1, 'action': 'accept'}
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd(dialog, requester_id='b')
    reply_on_send(bridge, data={'pending_execution': False, 'status': 'ok'})
    bridge.ext_cmd(dialog, requester_id='a')
    for _ in range(2):
        result = bridge.get_execute_js_result(pending['operation_id'], requester_id='a')
        assert result['status'] == 'in_progress'
        assert result['operation_status'] == 'outcome_unknown'
        assert result['reservation_held'] is True
        with pytest.raises(TargetBusyError):
            bridge.execute_js('second', requester_id='a')


def test_recovery_commands_do_not_overlap_even_for_the_same_owner():
    operations = PendingOperations()
    operations.reserve('js', ['browser:1'], 'a')
    operations.reserve('dialog', ['browser:1'], 'a', cleanup=True)
    with pytest.raises(TargetBusyError):
        operations.reserve('close', ['browser:1'], 'a', cleanup=True)
    operations.complete('js', {'success': True, 'data': 1})
    with pytest.raises(TargetBusyError):
        operations.reserve('new-js', ['browser:1'], 'b')
    operations.complete('dialog', {'success': True, 'data': None})
    operations.reserve('new-js', ['browser:1'], 'b')


@pytest.mark.parametrize('liveness', ['alive', 'unknown', 'dead'])
def test_only_proven_dead_owner_allows_a_different_requester_to_close(liveness):
    operations = PendingOperations(requester_liveness=lambda _: liveness)
    operations.reserve('js', ['browser:1'], 'a')
    with pytest.raises(TargetBusyError):
        operations.reserve('new-js', ['browser:1'], 'b')
    if liveness != 'dead':
        with pytest.raises(TargetBusyError):
            operations.reserve('close', ['browser:1'], 'b', cleanup=True, recover_dead_owner=True)
    else:
        operations.reserve('close', ['browser:1'], 'b', cleanup=True, recover_dead_owner=True)
        operations.complete('close', {'success': True, 'data': None})
        with pytest.raises(TargetBusyError):
            operations.reserve('new-js', ['browser:1'], 'b')


def test_changed_generation_after_socket_loss_ends_old_execution_and_capture():
    bridge = make_bridge()
    bridge.ext_cmd({'cmd': 'console', 'method': 'start', 'tabId': 1}, requester_id='a')
    pending = bridge.execute_js('pending', wait=False, requester_id='a')
    socket = bridge.ext_clients['browser']['ws']
    bridge._unregister_client(socket)
    bridge._claim_ext_client('browser', 'chrome', socket)
    bridge._apply_extension_tabs('browser', 'chrome', [
        {'id': 1, 'url': 'https://example.test', 'generation': 'replacement'},
    ], socket)
    bridge.execute_js('new page', requester_id='b')
    bridge.ext_cmd({'cmd': 'console', 'method': 'start', 'tabId': 1}, requester_id='b')
    result = bridge.get_execute_js_result(pending['operation_id'], requester_id='a')
    assert result['status'] == 'navigated'
    assert result['lifecycle_reason'] == 'generation_changed'


def test_http_queued_script_after_timeout_is_not_reported_safe_to_replay():
    bridge = make_bridge()
    commands = queue.Queue()
    bridge.sessions['http:1'] = Session('http:1', {
        'type': 'http', 'url': 'https://example.test',
    }, commands)
    result = bridge.execute_js('queued side effect', session_id='http:1', timeout=0.01, requester_id='a')
    assert result['delivery_state'] == 'sent_unconfirmed'
    assert result['retry_safe'] is False
    late_delivery = json.loads(commands.get_nowait())
    assert late_delivery['id'] == result['operation_id']
    assert late_delivery['code'] == 'queued side effect'
    with pytest.raises(TargetBusyError):
        bridge.execute_js('replayed side effect', session_id='http:1', requester_id='a')


def test_capture_stop_finishes_ownership_before_its_tab_reservation_releases(monkeypatch):
    bridge = make_bridge()
    bridge.ext_cmd({'cmd': 'console', 'method': 'start', 'tabId': 1}, requester_id='a')
    captures = bridge._capture_state()
    original = captures.finish
    entered = False

    def finish_capture(operation, *, success):
        nonlocal entered
        if operation.method == 'stop':
            entered = True
            with pytest.raises(TargetBusyError):
                bridge._operation_state().reserve('concurrent-input', ['browser:1'], 'a')
        return original(operation, success=success)

    monkeypatch.setattr(captures, 'finish', finish_capture)
    bridge.ext_cmd({'cmd': 'console', 'method': 'stop', 'tabId': 1}, requester_id='a')
    assert entered
    bridge.ext_cmd({'cmd': 'console', 'method': 'start', 'tabId': 1}, requester_id='b')


@pytest.mark.parametrize('method', ['close', 'remove'])
def test_owner_can_close_after_a_manual_recovery_loses_its_result(method):
    bridge = make_bridge()
    pending = bridge.execute_js('pending', wait=False, requester_id='a')
    finish(bridge, pending['operation_id'], {
        '__btap_dialog_result': True, 'pending_execution': True, 'status': 'blocked_by_dialog',
    })
    bridge.get_execute_js_result(pending['operation_id'], requester_id='a')
    reply_on_send(bridge, success=False, data='Detached while handling command')
    with pytest.raises(PageExecutionError) as caught:
        bridge.ext_cmd({'cmd': 'handle_dialog', 'tabId': 1, 'action': 'dismiss'}, requester_id='a')
    recovery_id = caught.value.operation_id
    assert bridge.get_execute_js_result(recovery_id, requester_id='a')['operation_status'] == 'outcome_unknown'

    reply_on_send(bridge, data={'ok': True})
    bridge.ext_cmd({'cmd': 'tabs', 'method': method, 'tabId': 1}, requester_id='a')
    with pytest.raises(TargetBusyError):
        bridge.execute_js('new input', requester_id='a')
    with pytest.raises(TargetBusyError):
        bridge.ext_cmd({'cmd': 'handle_dialog', 'tabId': 1, 'action': 'dismiss'}, requester_id='a')

    bridge._apply_extension_tabs('browser', 'chrome', [
        {'id': 2, 'url': 'https://example.test', 'generation': 'original'},
    ], bridge.ext_clients['browser']['ws'])
    finish(bridge, recovery_id, {'pending_execution': False})
    for operation_id in (pending['operation_id'], recovery_id):
        result = bridge.get_execute_js_result(operation_id, requester_id='a')
        assert result['status'] == 'navigated'
        assert result['lifecycle_reason'] == 'tab_removed'
        assert result['reservation_held'] is False


@pytest.mark.parametrize('status', ['in_progress', 'blocked_by_dialog', 'outcome_unknown'])
def test_close_can_replace_only_an_unknown_recovery(status):
    operations = PendingOperations()
    operations.reserve('js', ['browser:1'], 'a')
    operations.reserve('dialog', ['browser:1'], 'a', kind='handle_dialog', cleanup=True)
    if status == 'outcome_unknown':
        operations.complete('dialog', {'success': False, 'data': 'detached'}, outcome_unknown=True)
    elif status == 'blocked_by_dialog':
        operations.complete('dialog', {
            'success': True, 'data': {'__btap_dialog_result': True, 'pending_execution': True},
        })
    if status == 'outcome_unknown':
        operations.reserve('close', ['browser:1'], 'a', cleanup=True, close_targets=True)
    else:
        with pytest.raises(TargetBusyError):
            operations.reserve('close', ['browser:1'], 'a', cleanup=True, close_targets=True)


@pytest.mark.parametrize('liveness', ['alive', 'unknown', 'dead'])
def test_replacing_another_owners_unknown_recovery_requires_proven_death(liveness):
    operations = PendingOperations(requester_liveness=lambda _: liveness)
    operations.reserve('js', ['browser:1'], 'a')
    operations.reserve('dialog', ['browser:1'], 'a', cleanup=True)
    operations.complete('dialog', {'success': False, 'data': 'detached'}, outcome_unknown=True)
    if liveness == 'dead':
        operations.reserve('close', ['browser:1'], 'b', cleanup=True, close_targets=True, recover_dead_owner=True)
    else:
        with pytest.raises(TargetBusyError):
            operations.reserve('close', ['browser:1'], 'b', cleanup=True, close_targets=True, recover_dead_owner=True)


def test_a_dead_execution_owner_does_not_allow_replacing_a_live_recovery_owner():
    liveness = {'a': 'dead', 'b': 'alive'}
    operations = PendingOperations(requester_liveness=lambda owner: liveness.get(owner, 'unknown'))
    operations.reserve('js', ['browser:1'], 'a')
    operations.reserve('old-close', ['browser:1'], 'b', cleanup=True, recover_dead_owner=True)
    operations.complete('old-close', {'success': False, 'data': 'detached'}, outcome_unknown=True)
    with pytest.raises(TargetBusyError):
        operations.reserve('close', ['browser:1'], 'c', cleanup=True, close_targets=True, recover_dead_owner=True)
    liveness['b'] = 'dead'
    operations.reserve('close', ['browser:1'], 'c', cleanup=True, close_targets=True, recover_dead_owner=True)


@pytest.mark.parametrize('success', [True, False])
@pytest.mark.parametrize('original_finishes_first', [True, False])
def test_finishing_a_close_restores_an_unsettled_recovery_reservation(success, original_finishes_first):
    operations = PendingOperations()
    operations.reserve('js', ['browser:1'], 'a')
    operations.reserve('dialog', ['browser:1'], 'a', cleanup=True)
    operations.complete('dialog', {'success': False, 'data': 'detached'}, outcome_unknown=True)
    if original_finishes_first:
        operations.complete('js', {'success': True, 'data': 1})
    operations.reserve('close', ['browser:1'], 'a', cleanup=True, close_targets=True)
    if not original_finishes_first:
        operations.complete('js', {'success': True, 'data': 1})
    operations.complete('close', {'success': success, 'data': None})
    for cleanup in (False, True):
        with pytest.raises(TargetBusyError):
            operations.reserve('new-input', ['browser:1'], 'a', cleanup=cleanup)
    operations.reserve('close-again', ['browser:1'], 'a', cleanup=True, close_targets=True)
    operations.finish_target('browser:1', 'tab_removed')
    for operation_id in ('dialog', 'close-again'):
        result = operations.read(operation_id, 'a')
        assert result['status'] == 'lifecycle_ended'
        assert result['reservation_held'] is False
    operations.reserve('new-page', ['browser:1'], 'b')


def test_late_superseded_recovery_reply_cannot_release_the_current_close():
    operations = PendingOperations()
    operations.reserve('js', ['browser:1'], 'a')
    operations.reserve('dialog', ['browser:1'], 'a', cleanup=True)
    operations.complete('dialog', {'success': False, 'data': 'detached'}, outcome_unknown=True)
    operations.reserve('close', ['browser:1'], 'a', cleanup=True, close_targets=True)
    operations.complete('js', {'success': True, 'data': 1})
    operations.complete('dialog', {'success': True, 'data': None})
    with pytest.raises(TargetBusyError):
        operations.reserve('new-input', ['browser:1'], 'a')
    with pytest.raises(TargetBusyError):
        operations.reserve('second-close', ['browser:1'], 'a', cleanup=True, close_targets=True)
    operations.finish_target('browser:1', 'tab_removed')
    assert operations.read('close', 'a')['status'] == 'lifecycle_ended'


def test_multi_tab_close_replacement_checks_every_target_before_taking_any():
    operations = PendingOperations()
    operations.reserve('js', ['browser:1', 'browser:2'], 'a')
    operations.reserve('unknown', ['browser:1'], 'a', cleanup=True)
    operations.complete('unknown', {'success': False, 'data': 'detached'}, outcome_unknown=True)
    operations.reserve('live-recovery', ['browser:2'], 'a', cleanup=True)
    with pytest.raises(TargetBusyError):
        operations.reserve('close-both', ['browser:1', 'browser:2'], 'a', cleanup=True, close_targets=True)
    operations.complete('unknown', {'success': True, 'data': None})
    operations.reserve('next-dialog', ['browser:1'], 'a', cleanup=True)


def test_partial_lifecycle_cleanup_does_not_restore_old_recovery_on_removed_target():
    operations = PendingOperations()
    operations.reserve('js', ['browser:1', 'browser:2'], 'a')
    operations.reserve('dialog', ['browser:1', 'browser:2'], 'a', cleanup=True)
    operations.complete('dialog', {'success': False, 'data': 'detached'}, outcome_unknown=True)
    operations.reserve(
        'close', ['browser:1', 'browser:2'], 'a', cleanup=True, close_targets=True,
    )
    operations.finish_target('browser:1', 'tab_removed')
    operations.complete('close', {'success': True, 'data': None})
    operations.reserve('new-tab-one', ['browser:1'], 'b')
    with pytest.raises(TargetBusyError):
        operations.reserve('new-tab-two', ['browser:2'], 'b')
