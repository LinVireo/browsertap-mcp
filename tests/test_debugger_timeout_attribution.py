"""The command deadline must survive Chrome's reply during debugger cleanup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

BACKGROUND = Path(__file__).resolve().parents[1] / "src/browsertap_mcp/chrome_extension/background.js"


def _run_debugger(scenario: str) -> dict:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function debuggerTargetKey")
    end = source.index("\nasync function handleProtocolDialog", start)
    harness = """
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
const manualExecutionGenerations = new Map();
const timers = [];
const nativeCommands = new Map();
const detachGate = deferred();
const detachedTabs = [];
let duringDetach = 'reject';
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setTimeout(callback, delay) {
  const timer = { callback, delay, cleared: false };
  timers.push(timer);
  return timer;
}
function clearTimeout(timer) { if (timer) timer.cleared = true; }
const flush = () => new Promise(setImmediate);
function observed(promise) {
  const record = { settled: false, result: null };
  record.promise = promise.then(
    value => { record.settled = true; record.result = { value }; return record.result; },
    error => {
      record.settled = true;
      record.result = {
        error: error.message, code: debuggerFailureCode(error),
        method: error.method || null, timeoutMs: error.timeoutMs || null,
        dispatched: error.dispatched,
        ...(error.zombie ? { zombie: error.zombie } : {}),
      };
      return record.result;
    },
  );
  return record;
}
const chrome = { debugger: {
  attach() { return Promise.resolve(); },
  sendCommand(target) {
    const command = deferred();
    nativeCommands.set(target.tabId, command);
    return command.promise;
  },
  detach(target) {
    detachedTabs.push(target.tabId);
    const command = nativeCommands.get(target.tabId);
    if (duringDetach === 'resolve') command.resolve({ late: true });
    if (duringDetach === 'reject') command.reject(new Error('Detached while handling command.'));
    if (duringDetach === 'event') handleDebuggerDetach(target);
    return detachGate.promise;
  },
} };
"""
    completed = subprocess.run(
        ["node", "-"],
        input=harness + source[start:end] + "\n(async () => {\n" + scenario
        + "\n})().catch(error => { console.error(error); process.exitCode = 1; });\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("native_reply", ["reject", "resolve", "event"])
def test_deadline_wins_over_replies_during_its_cleanup_and_keeps_other_tabs_running(native_reply):
    result = _run_debugger("duringDetach = " + json.dumps(native_reply) + ";\n" + """
const first = await attachBtapDebugger({ tabId: 7 });
const second = await attachBtapDebugger({ tabId: 8 });
const other = observed(sendDebuggerCommandWithTimeout(first, 'Runtime.evaluate', {}, 2000));
const dispatch = {};
const expired = observed(sendDebuggerCommandWithTimeout(
  second, 'Emulation.setFocusEmulationEnabled', { enabled: true }, 100, 100, dispatch,
));
await flush();
const watchdog = timers.find(timer => !timer.cleared && timer.delay === 100);
if (!watchdog) throw new Error('missing command watchdog');
const cleanup = watchdog.callback();
await flush();
const settledBeforeCleanup = expired.settled;
const otherBeforeCleanup = {
  settled: other.settled,
  invalidated: first.attachment.invalidated,
  tracked: debuggerAttachments.get('tab:7') === first.attachment,
  pending: first.attachment.pendingCommands.size,
};
detachGate.resolve();
await cleanup;
const failure = await expired.promise;
const afterCleanup = {
  attached: second.attachment.attached,
  tracked: debuggerAttachments.has('tab:8'),
  pending: second.attachment.pendingCommands.size,
  detachedTabs: [...detachedTabs],
};
nativeCommands.get(7).resolve({ completed: 7 });
const otherResult = await other.promise;
await detachBtapDebugger(first);
const replacement = await attachBtapDebugger({ tabId: 8 });
const next = observed(sendDebuggerCommandWithTimeout(replacement, 'Runtime.evaluate', {}, 100));
await flush();
nativeCommands.get(8).resolve({ completed: 8 });
const nextResult = await next.promise;
await detachBtapDebugger(replacement);
process.stdout.write(JSON.stringify({
  settledBeforeCleanup, otherBeforeCleanup, failure, afterCleanup, otherResult, nextResult,
  dispatched: dispatch.dispatched,
  activeTimers: timers.filter(timer => !timer.cleared).length,
}));
""")
    assert result["failure"] == {
        "error": "cdp_timeout: Emulation.setFocusEmulationEnabled exceeded 100ms",
        "code": "cdp_timeout", "method": "Emulation.setFocusEmulationEnabled",
        "timeoutMs": 100, "dispatched": True,
    }
    assert result["settledBeforeCleanup"] is False
    assert result["otherBeforeCleanup"] == {
        "settled": False, "invalidated": False, "tracked": True, "pending": 1,
    }
    assert result["afterCleanup"] == {
        "attached": False, "tracked": False, "pending": 0, "detachedTabs": [8],
    }
    assert result["otherResult"] == {"value": {"completed": 7}}
    assert result["nextResult"] == {"value": {"completed": 8}}
    assert result["dispatched"] is True
    assert result["activeTimers"] == 0


@pytest.mark.parametrize("reply", ["success", "error", "external_detach"])
def test_reply_before_deadline_preserves_its_original_outcome(reply):
    result = _run_debugger("const reply = " + json.dumps(reply) + ";\n" + """
const lease = await attachBtapDebugger({ tabId: 7 });
const call = observed(sendDebuggerCommandWithTimeout(lease, 'Runtime.evaluate', {}, 100));
await flush();
if (reply === 'success') nativeCommands.get(7).resolve({ completed: 7 });
if (reply === 'error') nativeCommands.get(7).reject(new Error('invalid parameter'));
if (reply === 'external_detach') handleDebuggerDetach({ tabId: 7 });
const outcome = await call.promise;
const beforeCleanup = {
  detachedTabs: [...detachedTabs],
  activeTimers: timers.filter(timer => !timer.cleared).length,
  pending: lease.attachment.pendingCommands.size,
};
detachGate.resolve();
await detachBtapDebugger(lease);
process.stdout.write(JSON.stringify({ outcome, beforeCleanup }));
""")
    assert result["beforeCleanup"] == {"detachedTabs": [], "activeTimers": 0, "pending": 0}
    if reply == "success":
        assert result["outcome"] == {"value": {"completed": 7}}
    else:
        assert result["outcome"]["code"] == ("cdp_error" if reply == "error" else "debugger_detached")
        assert result["outcome"]["method"] is None
        assert result["outcome"]["timeoutMs"] is None
        assert result["outcome"]["dispatched"] is True


@pytest.mark.parametrize("detach_outcome", ["hang", "reject"])
def test_failed_detach_is_bounded_and_preserves_the_command_timeout(detach_outcome):
    result = _run_debugger("const detachOutcome = " + json.dumps(detach_outcome) + ";\n" + """
chrome.debugger.detach = target => {
  detachedTabs.push(target.tabId);
  nativeCommands.get(target.tabId).reject(new Error('Detached while handling command.'));
  return detachOutcome === 'reject'
    ? Promise.reject(new Error('detach cleanup failed'))
    : new Promise(() => {});
};
const lease = await attachBtapDebugger({ tabId: 7 });
const call = observed(sendDebuggerCommandWithTimeout(lease, 'Runtime.evaluate', {}, 100));
await flush();
const watchdog = timers.find(timer => !timer.cleared && timer.delay === 100);
const cleanup = watchdog.callback();
await flush();
const settledBeforeDetachDeadline = call.settled;
if (detachOutcome === 'hang') {
  const detachTimer = timers.find(timer => !timer.cleared && timer.delay === 1000);
  if (!detachTimer) throw new Error('detach must have its own bounded watchdog');
  detachTimer.callback();
}
await cleanup;
const outcome = await call.promise;
process.stdout.write(JSON.stringify({
  outcome, settledBeforeDetachDeadline, detachedTabs,
  tracked: debuggerAttachments.size,
  pending: lease.attachment.pendingCommands.size,
  activeTimers: timers.filter(timer => !timer.cleared).length,
}));
""")
    assert result["outcome"] == {
        "error": "cdp_timeout: Runtime.evaluate exceeded 100ms",
        "code": "cdp_timeout", "method": "Runtime.evaluate", "timeoutMs": 100,
        "dispatched": True,
    }
    if detach_outcome == "hang":
        assert result["settledBeforeDetachDeadline"] is False
    assert result["detachedTabs"] == [7]
    assert result["tracked"] == result["pending"] == result["activeTimers"] == 0


@pytest.mark.parametrize("native_reply", ["reject", "resolve"])
def test_a_reply_during_the_probe_does_not_strip_the_probe_verdict(native_reply):
    """Measured 2026-09-10 in a real Chrome: the command settled while the
    beforeInvalidate hook was still running, and the caller got the timeout
    error before the probe had written its verdict onto it -- reported as
    'the timeout path did not run the zombie probe' while the probe was in
    fact mid-flight. The deadline path has to be awaited to its end."""
    result = _run_debugger("""
const lease = await attachBtapDebugger({ tabId: 9 });
const probeGate = deferred();
let probeSaw = null;
const hook = async (attachment) => {
  probeSaw = attachment === lease.attachment;
  await probeGate.promise;
  return { zombie: 'killed', zombie_detail: 'probe finished' };
};
const expired = observed(sendDebuggerCommandWithTimeout(
  lease, 'Runtime.evaluate', {}, 100, 100, null, hook,
));
await flush();
const watchdog = timers.find(timer => !timer.cleared && timer.delay === 100);
const cleanup = watchdog.callback();
await flush();
// The native command settles while the probe is still blocked.
const native = nativeCommands.get(9);
""" + ("native.reject(new Error('Detached while handling command.'));" if native_reply == "reject"
       else "native.resolve({ late: true });") + """
await flush();
const settledDuringProbe = expired.settled;
probeGate.resolve();
detachGate.resolve();
await cleanup;
const failure = await expired.promise;
process.stdout.write(JSON.stringify({ settledDuringProbe, probeSaw, failure }));
""")
    assert result["probeSaw"] is True
    assert result["settledDuringProbe"] is False
    assert result["failure"]["code"] == "cdp_timeout"
    assert result["failure"]["zombie"] == "killed"
