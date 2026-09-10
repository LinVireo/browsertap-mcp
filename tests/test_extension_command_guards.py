from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

BACKGROUND = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "browsertap_mcp"
    / "chrome_extension"
    / "background.js"
)


def _section(start: str, end: str) -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _run_capture(scenario: str) -> dict:
    harness = """
const networkCaptures = new Map();
const consoleCaptures = new Map();
const runtimeExecutionContexts = new Map();
const debuggerAttachments = new Map();
const protocolDialogStates = new Map();
const dialogAttachedTabs = new Set();
const attachGates = new Map();
const detachGates = new Map();
const attaches = [];
const detaches = [];
let failEnable = false;
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function boundedCdpTimeout(value, fallback = 20000) { return Number(value) || fallback; }
function debuggerFailureCode() { return 'cdp_error'; }
async function attachBtapDebugger(target) {
  attaches.push(target.tabId);
  const key = `tab:${target.tabId}`;
  let attachment = debuggerAttachments.get(key);
  if (!attachment) {
    attachment = {
      key, target, refs: 0, generation: key, attached: true,
      invalidated: false, pendingCommands: new Set(),
    };
    debuggerAttachments.set(key, attachment);
  }
  attachment.refs++;
  if (attachGates.has(target.tabId)) await attachGates.get(target.tabId);
  return { attachment, generation: attachment.generation, released: false };
}
async function sendDebuggerCommandWithTimeout() {
  if (failEnable) { failEnable = false; throw new Error('enable failed'); }
  return {};
}
function rejectPendingDebuggerCommandsForLease() {}
function rejectPendingDebuggerCommands() {}
async function detachDebuggerFromChrome(attachment) {
  detaches.push(attachment.target.tabId);
  if (detachGates.has(attachment.target.tabId)) await detachGates.get(attachment.target.tabId);
}
"""
    source = (
        _section("function boundedCaptureInteger", "\nfunction handleDebuggerEvent")
        + "\n"
        + _section(
            "async function detachBtapDebugger(",
            "\nasync function forceInvalidateDebuggerAttachment(",
        )
    )
    return _run_node(
        harness
        + source
        + "\n(async () => {\n"
        + scenario
        + "\n})().catch(error => { console.error(error); process.exitCode = 1; });\n"
    )


@pytest.mark.parametrize("kind", ["Network", "Console"])
def test_capture_start_refuses_overlaps_without_leaking_a_debugger_lease(kind):
    outcome = _run_capture(
        f"const handle = handle{kind}CaptureCommand;\n"
        + """
const gate = deferred();
attachGates.set(42, gate.promise);
const pending = handle({ method: 'start', tabId: 42 }, {});
const duplicate = await handle({ method: 'start', tabId: 42 }, {});
const earlyStop = await handle({ method: 'stop', tabId: 42 }, {});
const otherKind = handle === handleNetworkCaptureCommand
  ? handleConsoleCaptureCommand : handleNetworkCaptureCommand;
const overlappingKind = await otherKind({ method: 'start', tabId: 42 }, {});
const independent = await otherKind({ method: 'start', tabId: 43 }, {});
await otherKind({ method: 'stop', tabId: 43 }, {});
gate.resolve();
const started = await pending;
const alreadyRunning = await handle({ method: 'start', tabId: 42 }, {});
const stopped = await handle({ method: 'stop', tabId: 42 }, {});
process.stdout.write(JSON.stringify({
  duplicate, earlyStop, overlappingKind, independent, started, alreadyRunning, stopped,
  attaches, detaches, remainingAttachments: debuggerAttachments.size,
  remainingCaptures: networkCaptures.size + consoleCaptures.size,
}));
"""
    )
    for name in ("duplicate", "earlyStop", "overlappingKind"):
        assert outcome[name]["ok"] is False
        assert outcome[name]["code"] == "capture_busy"
        assert outcome[name]["retryable"] is True
        assert outcome[name]["dispatched"] is False
    assert outcome["independent"]["data"]["status"] == "capturing"
    assert outcome["started"]["data"]["status"] == "capturing"
    assert outcome["alreadyRunning"]["data"]["already_running"] is True
    assert outcome["stopped"]["data"]["status"] == "stopped"
    assert outcome["attaches"] == [42, 43]
    assert outcome["detaches"] == [43, 42]
    assert outcome["remainingAttachments"] == outcome["remainingCaptures"] == 0


@pytest.mark.parametrize("kind", ["Network", "Console"])
def test_capture_stop_blocks_a_replacement_until_detach_finishes(kind):
    outcome = _run_capture(
        f"const handle = handle{kind}CaptureCommand;\n"
        + """
await handle({ method: 'start', tabId: 42 }, {});
const gate = deferred();
detachGates.set(42, gate.promise);
const pending = handle({ method: 'stop', tabId: 42 }, {});
const prematureStart = await handle({ method: 'start', tabId: 42 }, {});
gate.resolve();
const stopped = await pending;
const restarted = await handle({ method: 'start', tabId: 42 }, {});
await handle({ method: 'stop', tabId: 42 }, {});
process.stdout.write(JSON.stringify({
  prematureStart, stopped, restarted, attaches, detaches,
  remainingAttachments: debuggerAttachments.size,
}));
"""
    )
    assert outcome["prematureStart"]["code"] == "capture_busy"
    assert outcome["stopped"]["data"]["status"] == "stopped"
    assert outcome["restarted"]["data"]["status"] == "capturing"
    assert outcome["attaches"] == outcome["detaches"] == [42, 42]
    assert outcome["remainingAttachments"] == 0


@pytest.mark.parametrize("kind", ["Network", "Console"])
def test_failed_capture_start_releases_the_lock_and_lease(kind):
    outcome = _run_capture(
        f"const handle = handle{kind}CaptureCommand;\n"
        + """
failEnable = true;
const failed = await handle({ method: 'start', tabId: 42 }, {});
const restarted = await handle({ method: 'start', tabId: 42 }, {});
await handle({ method: 'stop', tabId: 42 }, {});
process.stdout.write(JSON.stringify({
  failed, restarted, attaches, detaches, remainingAttachments: debuggerAttachments.size,
}));
"""
    )
    assert outcome["failed"]["ok"] is False
    assert outcome["failed"]["error"] == "enable failed"
    assert outcome["restarted"]["data"]["status"] == "capturing"
    assert outcome["attaches"] == outcome["detaches"] == [42, 42]
    assert outcome["remainingAttachments"] == 0


def _run_close(lookup: str) -> dict:
    source = _section(
        "async function validateTabCloseGenerations", "\n// --- Temporary, origin-scoped"
    )
    return _run_node(
        "const removed = []; const checked = [];\n"
        "const chrome = { tabs: { get: async tabId => {\n"
        + lookup
        + "\n}, remove: async ids => removed.push(...ids) } };\n"
        "async function tabGenerationFor(tabId) { checked.push(tabId); return 'generation'; }\n"
        + source
        + """
(async () => {
  const result = await closeTabsWithGenerations([8, 42], { 8: 'generation', 42: 'generation' })
    .then(value => ({ value }), error => ({ error: error.message }));
  process.stdout.write(JSON.stringify({ result, removed, checked }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )


@pytest.mark.parametrize(
    "message",
    ["No SW", "No RPH", "Extension context invalidated", "No tab", "No tab with id: 43."],
)
def test_close_refuses_unknown_tab_lookup_failures_before_removing_any_tab(message):
    outcome = _run_close(
        f"if (tabId === 8) return {{ id: tabId }};\nthrow new Error({json.dumps(message)});"
    )
    assert outcome["result"] == {"error": message}
    assert outcome["removed"] == outcome["checked"] == []


@pytest.mark.parametrize("suffix", ["", "."])
def test_close_only_reports_already_gone_for_the_explicit_missing_tab(suffix):
    outcome = _run_close(
        "if (tabId === 8) return { id: tabId };\n"
        f"throw new Error({json.dumps('No tab with id: 42' + suffix)});"
    )
    assert outcome["result"] == {"value": {"closed": [8], "alreadyGone": [42]}}
    assert outcome["removed"] == outcome["checked"] == [8]


def test_close_does_not_infer_a_missing_tab_from_an_empty_lookup_result():
    outcome = _run_close("if (tabId === 8) return { id: tabId }; return undefined;")
    assert "no matching tab" in outcome["result"]["error"]
    assert outcome["removed"] == outcome["checked"] == []


def _run_batch_guard(probe_result: dict, guard: bool = True) -> dict:
    source = _section("function batchDeadlineRemainingMs", "\nasync function handleCDP")
    return _run_node(
        f"const probeResult = {json.dumps(probe_result)};\n"
        f"const guard = {json.dumps(guard)};\n"
        + """
const sent = [];
let attaches = 0;
let detaches = 0;
function boundedCdpTimeout(value, fallback = 20000) { return Number(value) || fallback; }
function debuggerFailureCode() { return 'cdp_error'; }
async function attachBtapDebugger() { attaches++; return { attachment: {} }; }
async function detachBtapDebugger() { detaches++; }
async function sendDebuggerCommandWithTimeout(_lease, method) {
  sent.push(method);
  return method === 'Runtime.evaluate' ? probeResult : {};
}
"""
        + source
        + """
(async () => {
  const result = await handleBatch({ tabId: 42, commands: [
    { cmd: 'cdp', method: 'Emulation.setFocusEmulationEnabled', params: { enabled: true } },
    { cmd: 'cdp', method: 'Runtime.evaluate', params: { expression: 'document.hasFocus()' }, assertTruthy: guard },
    { cmd: 'cdp', method: 'Input.insertText', params: { text: 'example' } },
  ] }, {});
  process.stdout.write(JSON.stringify({ result, sent, attaches, detaches }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )


@pytest.mark.parametrize(
    "probe_result",
    [
        {"result": {"type": "boolean", "value": False}},
        {"result": {"type": "string", "value": "true"}},
        {"result": {"type": "undefined"}},
        {"result": {"value": True}, "exceptionDetails": {"text": "guard failed"}},
    ],
)
def test_batch_guard_failure_prevents_subsequent_input_and_preserves_diagnostics(probe_result):
    outcome = _run_batch_guard(probe_result)
    result = outcome["result"]
    assert result["ok"] is False
    assert result["code"] == "batch_guard_failed"
    assert result["failed_command_index"] == 1
    assert result["commands_completed"] == result["commands_dispatched"] == 2
    assert result["results"][-1] == probe_result
    assert result["error"]["code"] == "batch_guard_failed"
    assert result["error"]["failed_command_index"] == 1
    assert result["error"]["retryable"] is False
    assert outcome["sent"] == ["Emulation.setFocusEmulationEnabled", "Runtime.evaluate"]
    assert outcome["attaches"] == outcome["detaches"] == 1


def test_batch_guard_accepts_true_and_preserves_one_debugger_attachment():
    outcome = _run_batch_guard({"result": {"type": "boolean", "value": True}})
    assert outcome["result"]["ok"] is True
    assert outcome["sent"] == [
        "Emulation.setFocusEmulationEnabled",
        "Runtime.evaluate",
        "Input.insertText",
    ]
    assert outcome["attaches"] == outcome["detaches"] == 1


def test_unguarded_batch_keeps_existing_runtime_evaluation_semantics():
    outcome = _run_batch_guard({"result": {"type": "boolean", "value": False}}, guard=False)
    assert outcome["result"]["ok"] is True
    assert outcome["sent"][-1] == "Input.insertText"


def test_extension_advertises_the_batch_guard_capability():
    source = _section("async function handleExtMessage", "\nchrome.runtime.onMessage.addListener")
    outcome = _run_node(
        "const BTAP_BUILD = 'test-build'; const tabGenerationLoadFailures = 0;\n"
        "const chrome = { runtime: { getManifest: () => ({ version: 'test-version' }) } };\n"
        + source
        + "\nhandleExtMessage({ cmd: 'bridge_status' }, {}).then(value => {\n"
        "process.stdout.write(JSON.stringify(value));\n"
        "}).catch(error => { console.error(error); process.exitCode = 1; });\n"
    )
    assert outcome["data"]["capabilities"]["batch_result_guard"] is True
