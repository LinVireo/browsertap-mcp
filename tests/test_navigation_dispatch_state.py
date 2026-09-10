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

from browsertap_mcp.browser_bridge import _execution_may_continue
from tests.test_dialog_policy import BACKGROUND, _run_node_harness


def _navigate_harness(*, page_enable: str, page_navigate: str, timeout_ms: int) -> dict:
    """page_enable / page_navigate: 'ok' | 'hang' | 'reject'."""
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
  if (mode === 'hang') return new Promise(() => {{}});
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
(async () => {{
  const result = await navigateWithDialogPolicy({{
    tabId: 42, url: 'https://new.example/', beforeunload: 'dismiss', timeoutMs: {timeout_ms},
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


def test_failure_after_navigate_was_sent_is_still_held():
    # Page.navigate is dispatched and hangs; the deadline then expires inside
    # the wait. The handler must not claim the navigation never happened.
    outcome = _navigate_harness(page_enable="ok", page_navigate="hang", timeout_ms=1200)
    result = outcome["result"]
    assert outcome["calls"]["Page.navigate"] == 1
    if result["ok"] is False:
        # Reached the outer catch after the send: dispatched must be true.
        assert result["error"]["dispatched"] is True
        assert _bridge_verdict(result) is True
    else:
        # Or the handler settled it inside as a timed-out navigation; either
        # way nothing says "not dispatched".
        assert result["data"]["status"] != "not_dispatched"


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
