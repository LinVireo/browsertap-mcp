"""Keep pending execution handles and dialog policy intact across the MCP layer."""

from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S
from browsertap_mcp import simphtml

SID = "chrome_pending:42"


@pytest.mark.parametrize("wait", [False, True])
def test_pending_execution_retains_its_handle_and_does_not_clear_dialog_policy(monkeypatch, wait):
    commands = []
    dispatched = []
    reply = {
        "status": "no_response" if wait else "in_progress",
        "operation_id": "pending-operation",
        "delivery_state": "delivered_no_result",
        "executed_tab_id": 42,
    }

    def execute(script, **kwargs):
        dispatched.append((script, kwargs))
        return dict(reply)

    def extension(command, **_kwargs):
        commands.append(command)
        return {"data": {"token": "pending-scope"}}

    driver = SimpleNamespace(default_session_id=SID, execute_js=execute, ext_cmd=extension)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **_kwargs: [{"id": SID}])

    result = S.execute_js("return pendingWork()", session_id=SID, no_monitor=True, wait=wait)

    assert result["operation_id"] == "pending-operation"
    assert result["poll_with"] == "get_execute_js_result"
    assert result["status"] == reply["status"]
    assert len(dispatched) == 1
    assert [command["cmd"] for command in commands] == ["set_dialog_policy"]


def test_manual_dialog_keeps_operation_metadata_and_does_not_probe_pending_tab(monkeypatch):
    commands = []
    calls = []

    def execute(*_args, **_kwargs):
        calls.append(True)
        return {
            "operation_id": "dialog-operation", "reservation_held": True,
            "operation_status": "blocked_by_dialog", "executed_tab_id": 42,
            "data": {"__btap_dialog_result": True, "status": "blocked_by_dialog",
                     "pending_execution": True, "value": None},
        }

    def extension(command, **_kwargs):
        commands.append(command)
        return {"data": {"token": "manual-scope"}}

    driver = SimpleNamespace(default_session_id=SID, execute_js=execute, ext_cmd=extension)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **_kwargs: [{"id": SID}])
    result = S.execute_js(
        "confirm('continue')", session_id=SID, no_monitor=True,
        dialog_policy="manual",
    )

    assert result["status"] == "blocked_by_dialog"
    assert result["operation_id"] == "dialog-operation"
    assert result["operation_status"] == "blocked_by_dialog"
    assert result["pending_execution"] is True
    assert result["reservation_held"] is True
    assert result["poll_with"] == "get_execute_js_result"
    assert calls == [True]
    assert [command["cmd"] for command in commands] == ["set_dialog_policy"]


@pytest.mark.parametrize("fallback", [False, True])
def test_direct_cdp_preserves_timeout_handle_without_replaying(monkeypatch, fallback):
    calls = []
    error = TimeoutError("extension did not return a result")
    error.operation_id = "pending-cdp"
    error.delivery_state = "delivered_no_result"
    error.retry_safe = False
    error.diagnostics = {"operation_id": "pending-cdp", "reservation_held": True}

    def extension(*_args, **_kwargs):
        calls.append("extension")
        if fallback:
            raise RuntimeError("Unknown cmd: cdp")
        raise error

    def execute(*_args, **_kwargs):
        calls.append("fallback")
        raise error

    monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace(
        ext_cmd=extension, execute_js=execute,
    ))
    with pytest.raises(TimeoutError) as caught:
        S._direct_cdp(
            "Runtime.evaluate", {"expression": "pendingWork()"},
            session_id=SID, client_id="chrome_pending", tab_id=42, timeout=15,
        )

    assert caught.value is error
    assert caught.value.operation_id == "pending-cdp"
    assert caught.value.diagnostics["reservation_held"] is True
    assert calls == (["extension", "fallback"] if fallback else ["extension"])


def test_delivered_timeout_is_not_replayed_and_points_to_result_collection():
    calls = []

    def execute(script, **kwargs):
        calls.append((script, kwargs))
        return {
            "operation_id": "late-result",
            "delivery_state": "delivered_no_result",
            "executed_tab_id": 42,
        }

    result = simphtml.execute_js_rich(
        "return pendingWork()", SimpleNamespace(execute_js=execute),
        no_monitor=True, before_sids=set(), session_id=SID,
    )

    assert len(calls) == 1
    assert result["status"] == "no_response"
    assert result["retry_safe"] is False
    assert result["operation_id"] == "late-result"
    assert result["poll_with"] == "get_execute_js_result"
    assert "get_execute_js_result" in result["suggestion"]


def test_collected_result_retains_target_identity_and_unwraps_dialog_result(monkeypatch):
    driver = SimpleNamespace(get_execute_js_result=lambda *_args, **_kwargs: {
        "status": "success",
        "operation_id": "completed-operation",
        "session_id": SID,
        "executed_tab_id": 42,
        "data": {"__btap_dialog_result": True, "value": 7, "dialogs": []},
    })
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    result = S.get_execute_js_result("completed-operation")

    assert result["js_return"] == 7
    assert result["session_id"] == SID
    assert result["tab_id"] == 42
    assert result["operation_id"] == "completed-operation"


@pytest.mark.parametrize("wait", [False, True])
def test_extension_watchdog_preserves_unknown_execution_and_dialog_scope(monkeypatch, wait):
    commands = []
    error = S.PageExecutionError("cdp_timeout: Runtime.evaluate timed out")
    error.diagnostics.update({
        "operation_id": "uncertain-operation",
        "operation_status": "outcome_unknown",
        "reservation_held": True,
    })
    error.delivery_state = "delivered_no_result"
    error.retry_safe = False

    def execute(*_args, **_kwargs):
        raise error

    def extension(command, **_kwargs):
        commands.append(command)
        return {"data": {"token": "pending-scope"}}

    driver = SimpleNamespace(default_session_id=SID, execute_js=execute, ext_cmd=extension)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **_kwargs: [{"id": SID}])

    if wait:
        result = S.execute_js("return pendingWork()", session_id=SID, no_monitor=True)
        assert result["status"] == "failed"
        assert result["operation_status"] == "outcome_unknown"
        assert result["operation_id"] == "uncertain-operation"
        assert result["reservation_held"] is True
        assert result["retry_safe"] is False
        assert result["poll_with"] == "get_execute_js_result"
    else:
        with pytest.raises(S.PageExecutionError):
            S.execute_js("return pendingWork()", session_id=SID, no_monitor=True, wait=False)
    assert [command["cmd"] for command in commands] == ["set_dialog_policy"]


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("delivery_state", ["sent_unconfirmed", "delivered_no_result"])
def test_lost_http_response_preserves_handle_and_dialog_scope(monkeypatch, wait, delivery_state):
    commands = []
    error = S.BridgeNoResponseError(
        "Bridge response lost", delivery_state=delivery_state, retry_safe=False,
        operation_id="uncertain-operation", poll_with="get_execute_js_result",
    )

    def execute(*_args, **_kwargs):
        raise error

    def extension(command, **_kwargs):
        commands.append(command)
        return {"data": {"token": "pending-scope"}}

    driver = SimpleNamespace(default_session_id=SID, execute_js=execute, ext_cmd=extension)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **_kwargs: [{"id": SID}])

    if wait:
        result = S.execute_js("return pendingWork()", session_id=SID, no_monitor=True)
        assert result["operation_id"] == "uncertain-operation"
        assert result["delivery_state"] == delivery_state
        assert result.get("reservation_held") is None
        assert result["retry_safe"] is False
        assert result["poll_with"] == "get_execute_js_result"
    else:
        with pytest.raises(S.BridgeNoResponseError) as caught:
            S.execute_js("return pendingWork()", session_id=SID, no_monitor=True, wait=False)
        assert caught.value is error
    assert [command["cmd"] for command in commands] == ["set_dialog_policy"]
