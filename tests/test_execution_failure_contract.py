"""Execution must preserve its target, deadline, and original failure."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


@pytest.fixture
def execution(monkeypatch):
    now = [0.0]
    calls = []
    driver = SimpleNamespace(default_session_id="chrome:1")
    def extension(command, **kwargs):
        calls.append((command, kwargs))
        return {"data": {"token": "scope"}}
    driver.ext_cmd = extension
    driver.execute_js = lambda *a, **kw: {"data": 7, "executed_tab_id": 7}
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **kw: [{"id": "chrome:7", "browser": "chrome"}])
    monkeypatch.setattr(S, "_resolve_session_target", lambda *a: None)
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(S.simphtml, "execute_js_rich", lambda *a, **kw: {
        "status": "success", "js_return": 7, "tab_id": 7,
    })
    monkeypatch.delenv("BROWSERTAP_PREFERRED_BROWSER", raising=False)
    return driver, calls, now


def test_async_manual_execution_is_rejected_before_dispatch(execution):
    _, calls, _ = execution
    with pytest.raises(ValueError, match="manual"):
        S.execute_js("return 7", wait=False, dialog_policy="manual")
    assert calls == []


@pytest.mark.parametrize("stage", ["before", "during"])
def test_session_discovery_cannot_renew_an_expired_execution_deadline(execution, monkeypatch, stage):
    _, calls, now = execution
    if stage == "before":
        ticks = iter([0, 2])
        monkeypatch.setattr(S.time, "monotonic", lambda: next(ticks))
    else:
        def slow(**kw):
            now[0] = 2
            return [{"id": "chrome:7"}]
        monkeypatch.setattr(S, "ensure_sessions", slow)
    with pytest.raises(TimeoutError, match="session resolution"):
        S.execute_js("return 7", timeout=1, session_id="chrome:7")
    assert calls == []


def test_implicit_execution_honors_the_preferred_browser(execution, monkeypatch):
    driver, calls, _ = execution
    driver.default_session_id = None
    monkeypatch.setenv("BROWSERTAP_PREFERRED_BROWSER", "edge")
    monkeypatch.setattr(S, "ensure_sessions", lambda **kw: [
        {"id": "chrome:7", "browser": "chrome"}, {"id": "edge:9", "browser": "edge"},
    ])
    assert S.execute_js("return 7")["status"] == "success"
    assert driver.default_session_id == "edge:9"
    assert calls[0][0]["tabId"] == 9
    assert calls[0][1]["client_id"] == "edge"


def test_implicit_execution_keeps_a_live_default(execution):
    driver, calls, _ = execution
    driver.default_session_id = "chrome:7"

    assert S.execute_js("return 7")["js_return"] == 7
    assert calls[0][0]["tabId"] == 7
    assert driver.default_session_id == "chrome:7"


def test_execution_can_keep_a_bridge_confirmed_target_without_rebinding(execution, monkeypatch):
    driver, calls, _ = execution
    monkeypatch.setattr(S, "_resolve_session_target", lambda driver, session_id: {"session_id": session_id})
    monkeypatch.setattr(S._TAB_OWNERSHIP, "rebind", lambda *args: pytest.fail("the identity did not change"))

    assert S.execute_js("return 7", session_id="chrome:7")["js_return"] == 7
    assert calls[0][0]["tabId"] == 7
    assert driver.default_session_id == "chrome:1"


def test_execution_uses_the_available_browser_when_the_preference_is_not_connected(execution, monkeypatch):
    driver, calls, _ = execution
    driver.default_session_id = None
    monkeypatch.setenv("BROWSERTAP_PREFERRED_BROWSER", "edge")

    assert S.execute_js("return 7")["js_return"] == 7
    assert calls[0][1]["client_id"] == "chrome"
    assert driver.default_session_id == "chrome:7"


def test_undelivered_script_cannot_be_retried_after_its_budget_is_spent(execution):
    driver, _, now = execution
    calls = []

    def undelivered(*args, **kwargs):
        calls.append((args, kwargs))
        now[0] = 2
        return {
            "status": "no_response", "result": "no response", "delivery_state": "undelivered",
            "retry_safe": True,
        }

    driver.execute_js = undelivered

    with pytest.raises(S.BridgeNoResponseError) as raised:
        S.exec_js("return 7", session_id="chrome:7", timeout=1)

    assert raised.value.delivery_state == "undelivered"
    assert len(calls) == 1


def test_policy_setup_cannot_dispatch_after_target_resolution_consumes_the_deadline(execution, monkeypatch):
    driver, calls, _ = execution
    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr(S.time, "monotonic", lambda: next(ticks, 2))

    with pytest.raises(TimeoutError, match="policy setup"):
        S.execute_js("return 7", session_id="chrome:7", timeout=1)

    assert calls == []
    assert driver.default_session_id == "chrome:1"


def test_a_missing_scope_token_cannot_start_a_fallback_after_timeout(execution, monkeypatch):
    driver, _, now = execution

    def slow_policy(*args, **kwargs):
        now[0] = 2
        return {"data": {}}

    driver.ext_cmd = slow_policy
    monkeypatch.setattr(S, "_execute_js_cdp_fallback", lambda *args, **kwargs: pytest.fail("deadline expired"))

    with pytest.raises(TimeoutError, match="before CDP fallback dispatch"):
        S.execute_js("return 7", session_id="chrome:7", timeout=1)

    assert driver.default_session_id == "chrome:1"


def test_a_missing_scope_token_uses_one_bounded_cdp_fallback(execution, monkeypatch):
    driver, _, now = execution
    fallback_calls = []

    def old_router(*args, **kwargs):
        now[0] = 0.5
        return {"data": {}}

    def evaluate(*args, **kwargs):
        fallback_calls.append((args, kwargs))
        return {"result": {"value": {"ok": True, "data": 7}}}

    driver.ext_cmd = old_router
    monkeypatch.setattr(S, "_direct_cdp", evaluate)
    monkeypatch.setattr(S.simphtml, "execute_js_rich", lambda *args, **kwargs: pytest.fail("fallback already ran"))

    result = S.execute_js("return 7", session_id="chrome:7", timeout=1)

    assert result["status"] == "success"
    assert result["js_return"] == 7
    assert len(fallback_calls) == 1
    assert fallback_calls[0][1]["timeout"] == 0.5
    assert driver.default_session_id == "chrome:1"


def test_completed_execution_does_not_extend_the_deadline_for_policy_cleanup(execution, monkeypatch, caplog):
    driver, calls, now = execution

    def completed(*args, **kwargs):
        now[0] = 1
        return {"status": "success", "js_return": 7, "tab_id": 7}

    monkeypatch.setattr(S.simphtml, "execute_js_rich", completed)

    assert S.execute_js("return 7", session_id="chrome:7", timeout=1)["js_return"] == 7
    assert [payload["cmd"] for payload, _ in calls] == ["set_dialog_policy"]
    assert "dialog scope will expire naturally" in caplog.text
    assert driver.default_session_id == "chrome:1"


@pytest.mark.parametrize("failure", [TimeoutError("policy timeout"), RuntimeError("permission denied")])
def test_policy_failure_never_dispatches_user_code(execution, failure):
    driver, _, _ = execution
    def failed(*a, **kw):
        raise failure
    driver.ext_cmd = failed
    driver.execute_js = lambda *a, **kw: pytest.fail("user script must not run")
    with pytest.raises(type(failure)):
        S.execute_js("return 7", session_id="chrome:7")
    assert driver.default_session_id == "chrome:1"


@pytest.mark.parametrize("failure", ["missing", "unknown"])
def test_async_execution_without_durable_router_is_refused(execution, failure):
    driver, _, _ = execution
    def unsupported(*a, **kw):
        if failure == "unknown":
            raise RuntimeError("unknown command")
        return {"data": {}}
    driver.ext_cmd = unsupported
    with pytest.raises(RuntimeError, match="durable operation handle"):
        S.execute_js("return 7", session_id="chrome:7", wait=False)
    assert driver.default_session_id == "chrome:1"


def test_invalid_scope_token_is_not_interpolated_into_script(execution):
    driver, _, _ = execution
    driver.ext_cmd = lambda *a, **kw: {"data": {"token": "*/malicious/*"}}
    with pytest.raises(RuntimeError, match="invalid dialog scope"):
        S.execute_js("return 7", session_id="chrome:7")
    assert driver.default_session_id == "chrome:1"


@pytest.mark.parametrize("raw,status,cleared", [
    ({"data": 7, "executed_tab_id": 7}, "success", True),
    ({"status": "in_progress", "operation_id": "op", "executed_tab_id": 7}, "in_progress", False),
    ({"status": "no_response", "delivery_state": "sent_unconfirmed", "operation_id": "op"}, "no_response", False),
])
def test_async_return_cleans_only_completed_execution_scopes(execution, raw, status, cleared):
    driver, calls, _ = execution
    driver.execute_js = lambda *a, **kw: raw
    result = S.execute_js("return 7", session_id="chrome:7", wait=False)
    assert result["status"] == status
    assert [call[0]["cmd"] for call in calls] == (
        ["set_dialog_policy", "clear_dialog_policy"] if cleared else ["set_dialog_policy"]
    )
    if cleared:
        assert result["js_return"] == 7
    else:
        assert result["poll_with"] == "get_execute_js_result"


def test_execution_can_use_an_embedded_driver_without_extension_router(execution):
    driver, _, _ = execution
    del driver.ext_cmd
    assert S.execute_js("return 7", session_id="chrome:7")["js_return"] == 7


@pytest.mark.parametrize("evaluation,error", [
    (None, "unexpected result"),
    ({"exceptionDetails": {"text": "exception"}}, "exception"),
    ({"exceptionDetails": {"exception": {"description": "boom"}}}, "boom"),
    ({"result": None}, "no serializable value"),
    ({"result": {"value": 7}}, "no serializable value"),
])
def test_cdp_fallback_does_not_treat_malformed_or_exception_replies_as_success(execution, monkeypatch, evaluation, error):
    monkeypatch.setattr(S, "_direct_cdp", lambda *a, **kw: evaluation)
    with pytest.raises(RuntimeError, match=error):
        S._execute_js_cdp_fallback("return 7", policy="dismiss", target_sid="chrome:7",
                                   client_id="chrome", tab_id=7, deadline=1,
                                   route_error=RuntimeError("unknown command"))


@pytest.mark.parametrize("error,message", [("failed", "failed"), ({"message": "bad"}, "bad"), (None, "CDP evaluation failed")])
def test_cdp_fallback_preserves_a_failed_page_value(execution, monkeypatch, error, message):
    monkeypatch.setattr(S, "_direct_cdp", lambda *a, **kw: {
        "result": {"value": {"ok": False, "error": error}},
    })
    result = S._execute_js_cdp_fallback("return 7", policy="dismiss", target_sid="chrome:7",
                                       client_id="chrome", tab_id=7, deadline=1,
                                       route_error=RuntimeError("unknown command"))
    assert result["status"] == "failed" and result["error"] == message
    assert result["tab_id"] == 7 and result["js_return"] is None


def test_cdp_fallback_cannot_dispatch_after_its_deadline(execution):
    with pytest.raises(TimeoutError):
        S._execute_js_cdp_fallback("return 7", policy="dismiss", target_sid="chrome:7",
                                   client_id="chrome", tab_id=7, deadline=-1,
                                   route_error=RuntimeError("unknown command"))


@pytest.mark.parametrize("timeout", [None, "bad", -1, 121, float("nan")])
def test_result_poll_rejects_invalid_timeouts_before_contacting_bridge(execution, timeout):
    with pytest.raises(ValueError, match="timeout"):
        S.get_execute_js_result("op", timeout=timeout)


@pytest.mark.parametrize("raw", [
    {"status": "in_progress", "operation_id": "op", "executed_tab_id": 7},
    {"status": "failed", "operation_id": "op", "error": "page error"},
])
def test_result_poll_preserves_nonterminal_and_failure_states(execution, raw):
    driver, _, _ = execution
    driver.get_execute_js_result = lambda *a, **kw: raw
    result = S.get_execute_js_result("op")
    assert result["status"] == raw["status"]
    if raw["status"] == "in_progress":
        assert result["tab_id"] == 7 and result["poll_with"] == "get_execute_js_result"
    else:
        assert result["error"] == "page error"


@pytest.mark.parametrize("default,fresh", [(None, []), ("chrome:7", [{"id": "chrome:7"}])])
def test_input_resolution_refreshes_a_stale_snapshot(execution, monkeypatch, default, fresh):
    driver, _, _ = execution
    driver.default_session_id = default
    calls = []
    def sessions(**kw):
        calls.append(kw["fresh"])
        return fresh if kw["fresh"] else []
    monkeypatch.setattr(S, "active_sessions", sessions)
    if default is None:
        with pytest.raises(RuntimeError, match="No connected"):
            S._resolve_page_input_session(driver, None, deadline=1, operation="test")
    else:
        assert S._resolve_page_input_session(driver, None, deadline=1, operation="test") == default
    assert calls == [False, True]


def test_input_resolution_rejects_an_explicit_missing_tab(execution, monkeypatch):
    driver, _, _ = execution
    monkeypatch.setattr(S, "active_sessions", lambda **kw: [{"id": "chrome:8"}])
    with pytest.raises(Exception, match="chrome:7"):
        S._resolve_page_input_session(driver, "chrome:7", deadline=1, operation="test")


@pytest.mark.parametrize("explicit", [False, True])
def test_input_resolution_preserves_a_cached_or_just_opened_target(execution, monkeypatch, explicit):
    driver, _, _ = execution
    driver.default_session_id = "chrome:7"
    requests = []

    def sessions(**kwargs):
        requests.append(kwargs)
        return [{"id": "chrome:7"}] if not explicit or kwargs["fresh"] else []

    monkeypatch.setattr(S, "active_sessions", sessions)
    result = S._resolve_page_input_session(
        driver, "chrome:7" if explicit else None, deadline=1, operation="test",
    )

    assert result == "chrome:7"
    assert [request["fresh"] for request in requests] == ([False, True] if explicit else [False])
    assert all(request["timeout"] == 1 for request in requests)
    assert driver.default_session_id == "chrome:7"


def test_input_resolution_avoids_unscriptable_implicit_targets(execution, monkeypatch):
    driver, _, _ = execution
    driver.default_session_id = None
    monkeypatch.setattr(S, "active_sessions", lambda **kw: [
        {"id": "chrome:1", "url": "chrome://extensions"},
        {"id": "chrome:7", "url": "https://example.test/"},
    ])
    assert S._resolve_page_input_session(driver, None, deadline=1, operation="test") == "chrome:7"


@pytest.mark.parametrize("during", [False, True])
def test_input_resolution_is_bounded_by_the_original_deadline(execution, monkeypatch, during):
    driver, _, now = execution
    def slow(**kw):
        now[0] = 2
        return [{"id": "chrome:7"}]
    monkeypatch.setattr(S, "active_sessions", slow)
    with pytest.raises(TimeoutError, match="session resolution"):
        S._resolve_page_input_session(driver, "chrome:7", deadline=1 if during else -1, operation="test")


@pytest.mark.parametrize("commands,validated", [([], False), ([{"method": "Input.insertText"}], True)])
def test_input_batch_requires_commands_and_an_explicit_validated_target(execution, commands, validated):
    with pytest.raises(S.InputValidationError):
        S._run_page_input(commands, None, 1, session_validated=validated)


def test_input_batch_cannot_dispatch_after_target_validation_uses_its_budget(execution):
    driver, calls, _ = execution
    with pytest.raises(TimeoutError, match="before batch dispatch"):
        S._run_page_input(
            [{"method": "Input.insertText", "params": {"text": "x"}}],
            "chrome:7", 1, session_validated=True, deadline=0,
        )
    assert calls == []
    assert driver.default_session_id == "chrome:1"
