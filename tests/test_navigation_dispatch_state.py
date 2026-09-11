"""A navigation that never reached Page.navigate must say so.

The bridge holds a tab for the caller budget plus a grace period whenever a
command may have changed the page. Before 2026-09-11 the navigate handler
returned a bare error string for a failure at any stage, so a `Page.enable`
timeout -- which happens before `Page.navigate` is ever sent -- was held like a
navigation of unknown outcome. Measured 2026-09-10 in a live run: one such
timeout cost every later call on that tab a `target_busy` for ~60s.

These harnesses drive the real `navigateWithDialogPolicy` in node with the
debugger stubbed, and read `error.dispatched` the same way the bridge does
(`_page_error_source` -> `_execution_may_continue`).
"""

from __future__ import annotations

import json

import pytest

from browsertap_mcp import pending_operations as pending_module
from browsertap_mcp.browser_bridge import PageExecutionError, _execution_may_continue
from browsertap_mcp.command_scope import TargetBusyError
from tests.test_dialog_policy import BACKGROUND, _run_node_harness
from tests.test_pending_bridge_operations import make_bridge, reply_on_send


def _navigate_harness(
    *, page_enable: str, page_navigate: str, timeout_ms: int,
    beforeunload: str = "dismiss",
) -> dict:
    """Drive real navigation, debugger cancellation, and deadline handling."""
    return _run_node_harness(
        f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const protocolDialogStates = new Map();
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const pendingNavigations = new Map();
const pendingManualExecutions = new Map();
const manualExecutionGenerations = new Map();
const execDialogPolicies = new Map();
const runtimeExecutionContexts = new Map();
const runtimeContextWaiters = new Map();
const dialogEventSequences = new Map();
const DIALOG_STATE_TTL_MS = 30000;
function validDialogPolicy(policy) {{
  return policy === 'dismiss' || policy === 'accept' || policy === 'manual';
}}
function currentProtocolDialog(tabId) {{ return protocolDialogStates.get(tabId) || null; }}
function rememberProtocolDialog(tabId, params) {{
  const dialog = {{ type: params.type, message: params.message || '', url: params.url || '',
    defaultPrompt: params.defaultPrompt || '', openedAt: Date.now() }};
  protocolDialogStates.set(tabId, dialog);
  return dialog;
}}
function debuggerTargetKey(target) {{ return `tab:${{target.tabId}}`; }}
const behaviour = {{ 'Page.enable': {json.dumps(page_enable)}, 'Page.navigate': {json.dumps(page_navigate)} }};
const calls = {{ 'Page.enable': 0, 'Page.navigate': 0 }};
function act(method) {{
  calls[method] += 1;
  const mode = behaviour[method];
  if (method === 'Page.navigate' && {json.dumps(beforeunload)} === 'accept') {{
    queueMicrotask(() => handleDebuggerEvent({{ tabId: 42 }},
      'Page.javascriptDialogOpening', {{
        type: 'beforeunload', message: 'Leave?', url: 'https://old.example/',
      }}));
  }}
  if (mode === 'detach') return new Promise(() => {{
    setTimeout(() => {{ void handleDebuggerDetach({{ tabId: 42 }}); }}, 25);
  }});
  if (mode === 'timeout_error') return Promise.reject(Object.assign(
    new Error('navigation watchdog expired'), {{ code: 'cdp_timeout' }},
  ));
  if (mode === 'detached_error') return Promise.reject(Object.assign(
    new Error('debugger detached during navigation'), {{ code: 'debugger_detached' }},
  ));
  if (mode === 'slow_success') return new Promise(resolve => {{
    setTimeout(() => resolve({{ frameId: 'f' }}), 3500);
  }});
  if (['hang', 'wait_timeout', 'watchdog_timeout'].includes(mode)) {{
    return new Promise(() => {{}});
  }}
  if (mode === 'reject') return Promise.reject(new Error(method + ' refused by stub'));
  return Promise.resolve(method === 'Page.navigate' ? {{ frameId: 'f' }} : {{}});
}}
const chrome = {{
  debugger: {{
    onEvent: {{ addListener() {{}} }},
    onDetach: {{ addListener() {{}} }},
    attach() {{ return Promise.resolve(); }},
    detach() {{ return Promise.resolve(); }},
    sendCommand(target, method) {{
      if (method in behaviour) return act(method);
      if (method === 'Page.handleJavaScriptDialog') return Promise.resolve({{}});
      throw new Error('unexpected command: ' + method);
    }},
  }},
  tabs: {{
    onRemoved: {{ addListener() {{}} }},
    async get() {{ return {{ url: 'https://old.example/', pendingUrl: '', title: 'Old' }}; }},
  }},
}};
eval(source.slice(
  source.indexOf('function handleDebuggerEvent'),
  source.indexOf('async function handleExtMessage'),
));
// The navigation wait and CDP watchdog have separate deadlines. Control their
// order while retaining the real dispatcher, cancellation, and cleanup paths.
const sendWithDeadline = sendDebuggerCommandWithTimeout;
sendDebuggerCommandWithTimeout = function(lease, method, params, timeout, ...rest) {{
  if (method === 'Page.navigate') {{
    if (behaviour[method] === 'wait_timeout') timeout += 2000;
    if (behaviour[method] === 'watchdog_timeout') timeout = 100;
  }}
  return sendWithDeadline(lease, method, params, timeout, ...rest);
}};
(async () => {{
  const result = await navigateWithDialogPolicy({{
    tabId: 42, url: 'https://new.example/',
    beforeunload: {json.dumps(beforeunload)}, timeoutMs: {timeout_ms},
  }});
  process.stdout.write(JSON.stringify({{ result, calls, pendingAtEnd: pendingNavigations.has(42) }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    )


def _bridge_verdict(result: dict) -> bool:
    """What the bridge decides from this reply: True = hold the tab."""
    return _execution_may_continue({"success": False, "data": result["error"]})


def test_page_enable_timeout_before_navigate_is_reported_as_not_dispatched():
    outcome = _navigate_harness(page_enable="hang", page_navigate="ok", timeout_ms=1200)
    result = outcome["result"]
    assert result["ok"] is False
    assert outcome["calls"]["Page.navigate"] == 0
    error = result["error"]
    assert isinstance(error, dict), error
    assert error["code"] == "cdp_timeout"
    assert error["message"].startswith("cdp_timeout: Page.enable exceeded")
    assert error["dispatched"] is False
    # The bridge releases the tab for this reply instead of holding it.
    assert _bridge_verdict(result) is False
    assert outcome["pendingAtEnd"] is False


@pytest.mark.parametrize(("mode", "policy", "code"), [
    ("hang", "dismiss", "cdp_timeout"),
    ("wait_timeout", "dismiss", "cdp_timeout"),
    ("watchdog_timeout", "dismiss", "cdp_timeout"),
    ("detach", "dismiss", "debugger_detached"),
    ("timeout_error", "dismiss", "cdp_timeout"),
    ("detached_error", "dismiss", "debugger_detached"),
    ("hang", "accept", "cdp_timeout"),
    ("wait_timeout", "accept", "cdp_timeout"),
    ("detach", "accept", "debugger_detached"),
    ("slow_success", "accept", "cdp_timeout"),
])
def test_failure_after_navigate_was_sent_is_still_held(monkeypatch, mode, policy, code):
    outcome = _navigate_harness(
        page_enable="ok", page_navigate=mode, timeout_ms=1200, beforeunload=policy,
    )
    result = outcome["result"]
    assert outcome["calls"]["Page.navigate"] == 1
    assert result["ok"] is False, result
    assert result["error"]["code"] == code
    assert result["error"]["dispatched"] is True
    assert _bridge_verdict(result) is True
    assert outcome["pendingAtEnd"] is False

    # Feed the real extension outcome through the bridge and registry. Merely
    # checking error.dispatched would miss a success-shaped reply that releases
    # the target, as the previous test's permissive else branch did.
    now = [1000.0]
    monkeypatch.setattr(pending_module.time, "monotonic", lambda: now[0])
    bridge = make_bridge()
    reply_on_send(bridge, success=False, data=result["error"])
    with pytest.raises(PageExecutionError) as caught:
        bridge.ext_cmd(
            {"cmd": "navigate", "tabId": 1, "url": "https://new.example/"},
            requester_id="navigator", operation_id="navigation-receipt", timeout=5,
        )
    operation_id = caught.value.operation_id
    assert operation_id == "navigation-receipt"
    assert caught.value.retry_safe is False
    assert caught.value.diagnostics["reservation_held"] is True
    for _ in range(2):
        receipt = bridge.get_execute_js_result(operation_id, requester_id="navigator")
        assert receipt["operation_id"] == operation_id
        assert receipt["operation_status"] == "outcome_unknown"
        assert receipt["reservation_held"] is True
        assert receipt["retry_safe"] is False
        for requester in ("navigator", "other"):
            with pytest.raises(TargetBusyError):
                bridge.execute_js("second", requester_id=requester)
    assert len(bridge.sent) == 1

    now[0] += pending_module.silent_deadline_seconds(5) + 1
    expired = bridge.get_execute_js_result(operation_id, requester_id="navigator")
    assert expired["status"] == "unknown"
    assert expired["reservation_held"] is False
    assert expired["retry_safe"] is False
    assert expired["abandoned_reason"] == "unknown_outcome_reservation_ttl"
    reply_on_send(bridge, data="after")
    assert bridge.execute_js("after", requester_id="other")["data"] == "after"
    assert sum(message.get("cmd", {}).get("cmd") == "navigate" for message in bridge.sent) == 1


def test_accepted_navigation_can_finish_after_three_seconds_within_caller_budget():
    """An accepted dialog must not replace the caller's deadline with a 3s cap."""
    outcome = _navigate_harness(
        page_enable="ok", page_navigate="slow_success", timeout_ms=5500,
        beforeunload="accept",
    )
    result = outcome["result"]
    assert result["ok"] is True, result
    assert result["data"]["status"] == "ok"
    assert result["data"]["navigation"] == {"frameId": "f"}
    assert result["data"]["handled"] is True
    assert result["data"]["pending_execution"] is False
    assert outcome["calls"]["Page.navigate"] == 1
    assert outcome["pendingAtEnd"] is False


@pytest.mark.parametrize(("mode", "status"), [
    ("ok", "ok"), ("reject", "navigation_failed"),
])
def test_known_navigation_outcomes_keep_their_terminal_shape(mode, status):
    outcome = _navigate_harness(page_enable="ok", page_navigate=mode, timeout_ms=1200)
    assert outcome["result"]["ok"] is True
    assert outcome["result"]["data"]["status"] == status
    assert outcome["result"]["data"]["pending_execution"] is False
    assert outcome["calls"]["Page.navigate"] == 1
    assert outcome["pendingAtEnd"] is False


@pytest.mark.parametrize("stage", ["tabs.get", "Page.enable"])
def test_pre_navigate_failures_share_the_not_dispatched_shape(stage):
    if stage == "Page.enable":
        outcome = _navigate_harness(page_enable="reject", page_navigate="ok", timeout_ms=3000)
    else:
        outcome = _run_node_harness(
            f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const protocolDialogStates = new Map();
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const pendingNavigations = new Map();
const pendingManualExecutions = new Map();
const manualExecutionGenerations = new Map();
const execDialogPolicies = new Map();
const runtimeExecutionContexts = new Map();
const runtimeContextWaiters = new Map();
const dialogEventSequences = new Map();
const DIALOG_STATE_TTL_MS = 30000;
function validDialogPolicy(policy) {{ return true; }}
function currentProtocolDialog() {{ return null; }}
function debuggerTargetKey(target) {{ return `tab:${{target.tabId}}`; }}
const chrome = {{
  debugger: {{ onEvent: {{ addListener() {{}} }}, onDetach: {{ addListener() {{}} }},
    attach() {{ return Promise.resolve(); }}, detach() {{ return Promise.resolve(); }},
    sendCommand() {{ throw new Error('must not be reached'); }} }},
  tabs: {{ onRemoved: {{ addListener() {{}} }},
    async get() {{ throw new Error('No tab with id: 42.'); }} }},
}};
eval(source.slice(
  source.indexOf('function handleDebuggerEvent'),
  source.indexOf('async function handleExtMessage'),
));
(async () => {{
  const result = await navigateWithDialogPolicy({{
    tabId: 42, url: 'https://new.example/', beforeunload: 'dismiss', timeoutMs: 3000,
  }});
  process.stdout.write(JSON.stringify({{ result, calls: {{ 'Page.navigate': 0 }}, pendingAtEnd: false }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        )
    result = outcome["result"]
    assert result["ok"] is False
    assert outcome["calls"]["Page.navigate"] == 0
    assert result["error"]["dispatched"] is False
    assert _bridge_verdict(result) is False
