"""Raw CDP must not silently bypass the browser-state tools' safety checks."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolRequest, CallToolResult

from browsertap_mcp import server as S


@pytest.fixture(autouse=True)
def default_policy(monkeypatch):
    monkeypatch.delenv("BROWSERTAP_ALLOW_UNSAFE_CDP", raising=False)
    monkeypatch.delenv("BROWSERTAP_MODE", raising=False)
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", None)


@pytest.fixture
def dispatch(monkeypatch):
    calls = []
    driver = SimpleNamespace(default_session_id="browser:7")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "switch_session", lambda **kw: "browser:7")
    monkeypatch.setattr(
        S, "_direct_cdp",
        lambda method, params, **kw: calls.append((method, params, kw)) or {"value": 1},
    )
    monkeypatch.setattr(
        S, "_extension_batch",
        lambda payload, **kw: calls.append((payload, kw)) or {"status": "ok"},
    )
    driver.ext_cmd = lambda payload, **kw: calls.append((payload, kw)) or {"status": "ok"}
    return calls


@pytest.mark.parametrize("method", [
    "Browser.close", "Browser.grantPermissions", "Browser.resetPermissions",
    "Browser.setPermission", "Browser.setDownloadBehavior", "Page.setDownloadBehavior",
    "Network.clearBrowserCookies", "Network.clearBrowserCache",
    "Storage.clearDataForOrigin", "Storage.clearDataForStorageKey", "Storage.clearCookies",
    "Emulation.setUserAgentOverride", "Network.setUserAgentOverride",
    "Page.close", "Target.closeTarget", "Target.disposeBrowserContext",
    "Target.sendMessageToTarget",
])
def test_default_install_refuses_state_bypass_before_resolving_a_browser(monkeypatch, method):
    calls = []
    monkeypatch.setattr(S, "require_driver", lambda: calls.append("driver"))
    with pytest.raises(PermissionError, match="raw_cdp_blocked"):
        S.cdp_command(method, session_id="browser:7")
    assert calls == []


@pytest.mark.parametrize("target", [
    {"extension_id": "synthetic-extension"}, {"target_id": "synthetic-worker"},
])
def test_non_tab_addressing_obeys_the_same_policy(dispatch, target):
    with pytest.raises(PermissionError, match="raw_cdp_blocked"):
        S.cdp_command("Network.clearBrowserCookies", **target)
    assert dispatch == []


@pytest.mark.parametrize("blocked_method", [
    "Network.clearBrowserCookies", "Page.setDownloadBehavior",
])
def test_entire_batch_is_checked_before_any_command_can_run(dispatch, blocked_method):
    payload = {"cmd": "batch", "commands": [
        {"cmd": "cdp", "method": "Runtime.evaluate", "params": {"expression": "1"}},
        {"cmd": "cdp", "method": blocked_method},
    ]}
    with pytest.raises(PermissionError, match="raw_cdp_blocked"):
        S.cdp_batch(json.dumps(payload), session_id="browser:7")
    assert dispatch == []


@pytest.mark.parametrize("method", ["DOM.getDocument", "Runtime.evaluate", "Browser.getVersion"])
def test_ordinary_diagnostics_still_dispatch(dispatch, method):
    assert S.cdp_command(method, session_id="browser:7")["data"] == {"value": 1}
    assert dispatch[0][0] == method


def test_lab_alone_is_not_permission_for_destructive_cdp(dispatch, monkeypatch):
    monkeypatch.setenv("BROWSERTAP_MODE", "lab")
    with pytest.raises(PermissionError, match="raw_cdp_blocked"):
        S.cdp_command("Network.clearBrowserCookies", session_id="browser:7")
    assert dispatch == []


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
def test_explicit_lab_opt_in_allows_single_and_batch(dispatch, monkeypatch, flag):
    monkeypatch.setenv("BROWSERTAP_ALLOW_UNSAFE_CDP", flag)
    monkeypatch.setenv("BROWSERTAP_MODE", "lab")
    S.cdp_command("Network.clearBrowserCookies", session_id="browser:7")
    S.cdp_batch(json.dumps({"cmd": "batch", "commands": [
        {"cmd": "cdp", "method": "Storage.clearDataForOrigin", "params": {}},
    ]}), session_id="browser:7")
    assert len(dispatch) == 2


@pytest.mark.parametrize("mode_source", ["environment", "session"])
def test_safe_mode_keeps_refusal_even_with_the_opt_in(dispatch, monkeypatch, mode_source):
    monkeypatch.setenv("BROWSERTAP_ALLOW_UNSAFE_CDP", "1")
    if mode_source == "environment":
        monkeypatch.setenv("BROWSERTAP_MODE", "safe")
    else:
        monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", "safe")
    with pytest.raises(PermissionError, match="raw_cdp_blocked"):
        S.cdp_command("Network.clearBrowserCookies", session_id="browser:7")
    assert dispatch == []


@pytest.mark.parametrize("method", ["", "Browser.close\n", " Browser.close", {}, None])
def test_malformed_methods_are_rejected_before_dispatch(dispatch, method):
    with pytest.raises(ValueError, match="method"):
        S.cdp_command(method, session_id="browser:7")
    assert dispatch == []


@pytest.mark.parametrize("params", ["[]", "null", '"text"'])
def test_params_must_be_an_object(dispatch, params):
    with pytest.raises(ValueError, match="params_json"):
        S.cdp_command("DOM.getDocument", params, session_id="browser:7")
    assert dispatch == []


@pytest.mark.parametrize("commands", [None, {}, [None], [{"cmd": "batch", "commands": []}],
                                         [{"cmd": "cdp", "method": "DOM.getDocument", "params": []}]])
def test_batch_shape_is_checked_before_dispatch(dispatch, commands):
    with pytest.raises(ValueError):
        S.cdp_batch(json.dumps({"cmd": "batch", "commands": commands}), session_id="browser:7")
    assert dispatch == []


def test_safe_batch_keeps_cookie_reads_tab_queries_and_parameter_references(dispatch):
    payload = {"cmd": "batch", "commands": [
        {"cmd": "tabs"}, {"cmd": "cookies"},
        {"cmd": "cdp", "method": "DOM.getDocument", "params": {"depth": 1}},
        {"cmd": "cdp", "method": "DOM.describeNode", "params": {"nodeId": "$2.root.nodeId"}},
    ]}
    assert S.cdp_batch(json.dumps(payload), session_id="browser:7")["status"] == "ok"
    assert dispatch[0][0] == payload


@pytest.mark.parametrize("tool,arguments", [
    ("cdp_command", {"method": "Browser.close"}),
    ("cdp_batch", {"batch_json": json.dumps({"cmd": "batch", "commands": [
        {"cmd": "cdp", "method": "DOM.getDocument"},
        {"cmd": "cdp", "method": "Storage.clearDataForOrigin"},
    ]})}),
])
def test_mcp_reports_policy_refusal_without_dispatch_or_retry(dispatch, tool, arguments):
    request = CallToolRequest(params={"name": tool, "arguments": arguments})
    handler = S.mcp._mcp_server.request_handlers[CallToolRequest]
    result = asyncio.run(handler(request)).root

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    envelope = result.structuredContent
    assert envelope["ok"] is False
    assert envelope["error_code"] == "raw_cdp_blocked"
    assert envelope["delivery_state"] == "undelivered"
    assert envelope["diagnostics"]["delivery_state"] == "undelivered"
    assert envelope["retryable"] is False
    assert json.loads(result.content[0].text) == envelope
    assert dispatch == []


@pytest.mark.parametrize("mode,flag,expected", [
    ("lab", None, "guarded"),
    ("lab", "false", "guarded"),
    ("lab", "1", "allow_unsafe"),
    ("safe", "1", "guarded"),
])
def test_advertised_profile_exposes_the_effective_policy(monkeypatch, mode, flag, expected):
    monkeypatch.setenv("BROWSERTAP_MODE", mode)
    if flag is not None:
        monkeypatch.setenv("BROWSERTAP_ALLOW_UNSAFE_CDP", flag)
    assert S.get_automation_profile()["raw_cdp_policy"] == expected
