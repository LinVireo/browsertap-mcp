"""Cancellation must own dispatch and late CDP cleanup, even with shared leases."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[1] / "src/browsertap_mcp/chrome_extension"

HARNESS = r"""
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
const manualExecutionGenerations = new Map();
const pendingManualExecutions = new Map();
let nextManualExecutionGeneration = 1;
const timers = [];
const events = [];
const evaluations = [];
const remoteGroups = new Map();
let chromeAttached = false;
let chromeEpoch = 0;
let attachGate = null;
let releaseGate = null;
let conversionGate = null;
let dispatchOwner = null;
let timerObserver = null;
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
function setTimeout(callback, delay) {
  const timer = {callback, delay, cleared:false};
  timers.push(timer);
  if (timerObserver) timerObserver(timer);
  return timer;
}
function clearTimeout(timer) { if (timer) timer.cleared = true; }
const flush = () => new Promise(setImmediate);
async function waitForDefaultRuntimeExecutionContext() { return 101; }
function observed(promise) {
  return promise.then(value => ({value}), error => ({
    error:error.message, code:error.code, dispatched:error.dispatched,
  }));
}
const chrome = {debugger:{
  attach(target) {
    events.push({method:'attach', target});
    return (attachGate ? attachGate.promise : Promise.resolve()).then(() => {
      chromeAttached = true;
      chromeEpoch += 1;
    });
  },
  detach(target) {
    events.push({method:'detach', target});
    chromeAttached = false;
    remoteGroups.clear();
    return Promise.resolve();
  },
  sendCommand(target, method, params = {}) {
    const epoch = chromeEpoch;
    events.push({method, params, target, epoch,
      ...(dispatchOwner ? {ownerReleased:dispatchOwner.released} : {})});
    if (!chromeAttached) return Promise.reject(new Error('not attached'));
    if (method === 'Page.enable' || method === 'Runtime.enable') return Promise.resolve({});
    if (method === 'Page.getFrameTree') return Promise.resolve({frameTree:{frame:{id:'main-frame'}}});
    if (method === 'Runtime.evaluate') {
      if (params.expression === 'void 0') return Promise.resolve({result:{type:'undefined'}});
      const evaluation = deferred();
      evaluations.push({...evaluation, params, epoch});
      return evaluation.promise.then(value => {
        // A queued old receipt can arrive after detach. Its objects belong to
        // the old connection, whose groups Chrome already destroyed.
        const objectId = value?.result?.objectId || value?.exceptionDetails?.exception?.objectId;
        if (chromeAttached && epoch === chromeEpoch && params.objectGroup && objectId) {
          remoteGroups.set(params.objectGroup, objectId);
        }
        return value;
      });
    }
    if (method === 'Runtime.releaseObjectGroup') {
      const gate = releaseGate;
      return (gate ? gate.promise : Promise.resolve()).then(() => {
        if (chromeAttached && epoch === chromeEpoch) remoteGroups.delete(params.objectGroup);
        return {};
      });
    }
    if (method === 'Runtime.callFunctionOn') {
      return conversionGate ? conversionGate.promise : Promise.resolve({result:{value:{synthetic:true}}});
    }
    throw new Error('unexpected command: ' + method);
  },
}};
"""


def _run(scenario: str, **options) -> dict:
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    serializer = (EXTENSION / "result_serialization.js").read_text(encoding="utf-8")

    def section(start, end):
        offset = source.index(start)
        return source[offset:source.index(end, offset)]

    program = (
        HARNESS
        + "\nglobalThis.smartProcessResult = eval(" + json.dumps(serializer) + ");\n"
        + section("function debuggerTargetKey", "\nasync function handleProtocolDialog")
        + "\n" + section("function manualExecutionResult", "\n// --- Scoped, temporary CSP removal")
        + "\nconst options = " + json.dumps(options) + ";\n(async () => {\n" + scenario
        + "\n})().catch(error => {console.error(error.stack);process.exitCode=1;});\n"
    )
    completed = subprocess.run(
        ["node", "-"], input=program, text=True, encoding="utf-8",
        capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout, completed.stderr or "Node exited before the scenario settled"
    return json.loads(completed.stdout)


@pytest.mark.parametrize("replacement", ["none", "capture", "manual"])
def test_cancel_during_attach_releases_only_the_late_old_lease(replacement):
    result = _run(r"""
attachGate = deferred();
const first = executeManualScript(42, 'first()', {token:'first',policy:'manual',timeoutMs:2000});
await flush();
const old = pendingManualExecutions.get(42);
await cancelManualExecution(42, 'synthetic cancel during attach', old);
let capturePromise = null;
let next = null;
if (options.replacement === 'capture') capturePromise = attachBtapDebugger({tabId:42});
if (options.replacement === 'manual') {
  next = executeManualScript(42, 'second()', {token:'second',policy:'manual',timeoutMs:2000});
}
await flush();
attachGate.resolve();
const firstResult = await first;
const capture = capturePromise ? await capturePromise : null;
await flush();
const attachment = debuggerAttachments.get('tab:42');
const afterLateAttach = {
  attached:chromeAttached, refs:attachment?.refs || 0,
  pending:pendingManualExecutions.size, oldReleased:old.released,
  oldLeaseRetained:Boolean(old.debuggerLease && !old.debuggerLease.released),
  enabled:events.filter(event => event.method === 'Page.enable').length,
  evaluations:evaluations.length,
  currentToken:pendingManualExecutions.get(42)?.token || null,
};
let nextResult = null;
if (next) {
  evaluations[0].resolve({result:{type:'number',value:22}});
  nextResult = await next;
}
if (capture) await detachBtapDebugger(capture);
process.stdout.write(JSON.stringify({firstResult, afterLateAttach, nextResult,
  afterCleanup:{attached:chromeAttached,pending:pendingManualExecutions.size,
    tracked:debuggerAttachments.has('tab:42')}}));
""", replacement=replacement)
    assert result["firstResult"]["ok"] is False
    after = result["afterLateAttach"]
    assert after["oldReleased"] is True
    assert after["oldLeaseRetained"] is False
    assert after["refs"] == (0 if replacement == "none" else 1)
    assert after["attached"] is (replacement != "none")
    assert after["enabled"] == after["evaluations"] == (1 if replacement == "manual" else 0)
    assert after["pending"] == (1 if replacement == "manual" else 0)
    assert after["currentToken"] == ("second" if replacement == "manual" else None)
    if replacement == "manual":
        assert result["nextResult"] == {"ok": True, "data": 22}
    assert result["afterCleanup"] == {"attached": False, "pending": 0, "tracked": False}


@pytest.mark.parametrize("cancellation", ["release", "invalidate", "detach_event"])
def test_command_cancelled_before_its_microtask_never_calls_chrome(cancellation):
    result = _run(r"""
const capture = await attachBtapDebugger({tabId:42});
const owner = await attachBtapDebugger({tabId:42});
dispatchOwner = owner;
const dispatch = {};
const command = observed(sendDebuggerCommandWithTimeout(owner,
  'Runtime.evaluate', {expression:'syntheticSideEffect()'}, 2000, 100, dispatch));
if (options.cancellation === 'release') await detachBtapDebugger(owner);
if (options.cancellation === 'invalidate') {
  await forceInvalidateDebuggerAttachment(owner.attachment, 'synthetic invalidation');
}
if (options.cancellation === 'detach_event') {
  chromeAttached = false;
  handleDebuggerDetach({tabId:42});
}
const outcome = await command;
await flush();
const after = {outcome, dispatch, evaluations:evaluations.length,
  sends:events.filter(event => event.method === 'Runtime.evaluate').length,
  pendingCommands:owner.attachment.pendingCommands.size};
for (const evaluation of evaluations) evaluation.resolve({result:{type:'number',value:1}});
await flush();
await detachBtapDebugger(capture);
process.stdout.write(JSON.stringify(after));
""", cancellation=cancellation)
    assert result["outcome"]["code"] == "debugger_detached"
    assert result["outcome"]["dispatched"] is False
    assert result["dispatch"]["dispatched"] is False
    assert result["evaluations"] == result["sends"] == result["pendingCommands"] == 0


def test_released_lease_refuses_before_call_with_explicit_dispatch_evidence():
    result = _run(r"""
const owner = await attachBtapDebugger({tabId:42});
await detachBtapDebugger(owner);
const dispatch = {};
const outcome = await observed(sendDebuggerCommandWithTimeout(owner,
  'Runtime.evaluate', {expression:'syntheticSideEffect()'}, 2000, 100, dispatch));
process.stdout.write(JSON.stringify({outcome,dispatch,evaluations:evaluations.length}));
""")
    assert result["outcome"]["code"] == "debugger_detached"
    assert result["outcome"]["dispatched"] is False
    assert result["dispatch"]["dispatched"] is False
    assert result["evaluations"] == 0


def test_cancel_after_actual_dispatch_preserves_the_uncertain_delivery_evidence():
    result = _run(r"""
const capture = await attachBtapDebugger({tabId:42});
const owner = await attachBtapDebugger({tabId:42});
const dispatch = {};
const command = observed(sendDebuggerCommandWithTimeout(owner,
  'Runtime.evaluate', {expression:'syntheticSideEffect()'}, 2000, 100, dispatch));
await flush();
await detachBtapDebugger(owner);
const outcome = await command;
const after = {outcome,dispatch,evaluations:evaluations.length,
  attached:chromeAttached,refs:capture.attachment.refs};
evaluations[0].resolve({result:{type:'number',value:1}});
await flush();
await detachBtapDebugger(capture);
process.stdout.write(JSON.stringify(after));
""")
    assert result["outcome"]["code"] == "debugger_detached"
    assert result["outcome"]["dispatched"] is True
    assert result["dispatch"]["dispatched"] is True
    assert result["evaluations"] == result["refs"] == 1
    assert result["attached"] is True


@pytest.mark.parametrize("shared_capture", [False, True])
def test_manual_cancel_before_evaluation_dispatch_stops_it_before_group_cleanup(shared_capture):
    result = _run(r"""
const capture = options.sharedCapture ? await attachBtapDebugger({tabId:42}) : null;
let cancelQueued = false;
timerObserver = () => {
  const pending = pendingManualExecutions.get(42);
  if (!cancelQueued && pending?.state === 'armed') {
    cancelQueued = true;
    queueMicrotask(() => { void cancelManualExecution(42, 'synthetic queued cancel', pending); });
  }
};
const outcome = await executeManualScript(42, 'mustNotRun()',
  {token:'queued-cancel',policy:'manual',timeoutMs:2000});
await flush();
const after = {outcome,evaluations:evaluations.length,pending:pendingManualExecutions.size,
  refs:debuggerAttachments.get('tab:42')?.refs || 0,attached:chromeAttached,
  userSends:events.filter(event => event.method === 'Runtime.evaluate' && event.params.expression === 'mustNotRun()').length};
for (const evaluation of evaluations) evaluation.resolve({result:{type:'number',value:1}});
await flush();
if (capture) await detachBtapDebugger(capture);
process.stdout.write(JSON.stringify(after));
""", sharedCapture=shared_capture)
    assert result["outcome"]["ok"] is False
    assert result["evaluations"] == result["userSends"] == result["pending"] == 0
    assert result["refs"] == (1 if shared_capture else 0)
    assert result["attached"] is shared_capture


@pytest.mark.parametrize("cancellation", ["cancel", "expiry"])
@pytest.mark.parametrize("reply", ["object", "exception"])
def test_late_raw_reply_releases_its_objects_without_detaching_capture(cancellation, reply):
    result = _run(r"""
const capture = await attachBtapDebugger({tabId:42});
const run = executeManualScript(42, 'first()', {token:'first',policy:'manual',timeoutMs:2000});
await flush();
const pending = pendingManualExecutions.get(42);
if (options.cancellation === 'cancel') await cancelManualExecution(42, 'synthetic cancel', pending);
else pending.expiryTimer.callback();
const outcome = await run;
const beforeLate = {groups:remoteGroups.size,refs:capture.attachment.refs,pending:pendingManualExecutions.size};
evaluations[0].resolve(options.reply === 'object'
  ? {result:{type:'object',objectId:'late-object'}}
  : {exceptionDetails:{text:'synthetic failure',exception:{objectId:'late-exception'}}});
await flush();
const afterLate = {groups:remoteGroups.size,refs:capture.attachment.refs,
  pending:pendingManualExecutions.size,attached:chromeAttached,
  releases:events.filter(event => event.method === 'Runtime.releaseObjectGroup').map(event => event.params.objectGroup),
  conversions:events.filter(event => event.method === 'Runtime.callFunctionOn').length,
  attaches:events.filter(event => event.method === 'attach').length};
await detachBtapDebugger(capture);
process.stdout.write(JSON.stringify({outcome,beforeLate,afterLate,group:pending.objectGroup}));
""", cancellation=cancellation, reply=reply)
    assert result["outcome"]["ok"] is False
    assert result["beforeLate"] == {"groups": 0, "refs": 1, "pending": 0}
    assert result["afterLate"] == {
        "groups": 0, "refs": 1, "pending": 0, "attached": True,
        "releases": [result["group"], result["group"]], "conversions": 0, "attaches": 1,
    }


def test_late_old_result_cleans_only_its_group_while_new_result_conversion_is_pending():
    result = _run(r"""
const capture = await attachBtapDebugger({tabId:42});
const first = executeManualScript(42, 'first()', {token:'first',policy:'manual',timeoutMs:2000});
await flush();
const old = pendingManualExecutions.get(42);
await cancelManualExecution(42, 'synthetic cancel', old);
await first;
const second = executeManualScript(42, 'second()', {token:'second',policy:'manual',timeoutMs:2000});
await flush();
const current = pendingManualExecutions.get(42);
conversionGate = deferred();
evaluations[1].resolve({result:{type:'object',objectId:'new-object'}});
await flush();
evaluations[0].resolve({result:{type:'object',objectId:'old-object'}});
await flush();
const afterOld = {oldPresent:remoteGroups.has(old.objectGroup),
  newPresent:remoteGroups.has(current.objectGroup),newStillOwns:pendingManualExecutions.get(42) === current,
  newReleased:current.released,refs:capture.attachment.refs,
  oldGroup:old.objectGroup,newGroup:current.objectGroup};
conversionGate.resolve({result:{value:{current:true}}});
const secondResult = await second;
const afterSecond = {groups:remoteGroups.size,pending:pendingManualExecutions.size,refs:capture.attachment.refs};
await detachBtapDebugger(capture);
process.stdout.write(JSON.stringify({afterOld,secondResult,afterSecond}));
""")
    after = result["afterOld"]
    assert after["oldGroup"] != after["newGroup"]
    assert after["oldPresent"] is False
    assert after["newPresent"] is after["newStillOwns"] is True
    assert after["newReleased"] is False
    assert after["refs"] == 2
    assert result["secondResult"] == {"ok": True, "data": {"current": True}}
    assert result["afterSecond"] == {"groups": 0, "pending": 0, "refs": 1}


def test_old_raw_reply_does_not_attach_again_or_release_a_new_attachment_group():
    result = _run(r"""
const capture = await attachBtapDebugger({tabId:42});
const first = executeManualScript(42, 'first()', {token:'first',policy:'manual',timeoutMs:2000});
await flush();
const old = pendingManualExecutions.get(42);
await cancelManualExecution(42, 'synthetic cancel', old);
await first;
await forceInvalidateDebuggerAttachment(capture.attachment, 'synthetic disconnect');
const replacement = await attachBtapDebugger({tabId:42});
// Deliberately reuse the label in the new connection: the connection identity,
// not just a convenient object-group string, must fence old cleanup.
remoteGroups.set(old.objectGroup, 'new-connection-sentinel');
const beforeEvents = events.length;
evaluations[0].resolve({result:{type:'object',objectId:'old-object'}});
await flush();
const after = {sentinel:remoteGroups.get(old.objectGroup),refs:replacement.attachment.refs,
  attached:chromeAttached,current:debuggerAttachments.get('tab:42') === replacement.attachment,
  lateCommands:events.slice(beforeEvents).map(event => event.method),
  attaches:events.filter(event => event.method === 'attach').length};
await detachBtapDebugger(replacement);
process.stdout.write(JSON.stringify(after));
""")
    assert result == {
        "sentinel": "new-connection-sentinel", "refs": 1, "attached": True,
        "current": True, "lateCommands": [], "attaches": 2,
    }


def test_late_reply_without_a_shared_lease_does_not_reopen_the_debugger():
    result = _run(r"""
const first = executeManualScript(42, 'first()', {token:'first',policy:'manual',timeoutMs:2000});
await flush();
const old = pendingManualExecutions.get(42);
await cancelManualExecution(42, 'synthetic cancel', old);
await first;
const beforeEvents = events.length;
evaluations[0].resolve({result:{type:'object',objectId:'old-object'}});
await flush();
process.stdout.write(JSON.stringify({groups:remoteGroups.size,attached:chromeAttached,
  tracked:debuggerAttachments.has('tab:42'),pending:pendingManualExecutions.size,
  lateCommands:events.slice(beforeEvents).map(event => event.method)}));
""")
    assert result == {"groups": 0, "attached": False, "tracked": False, "pending": 0, "lateCommands": []}


def test_late_cleanup_holds_its_reference_until_capture_can_safely_detach():
    result = _run(r"""
const capture = await attachBtapDebugger({tabId:42});
const first = executeManualScript(42, 'first()', {token:'first',policy:'manual',timeoutMs:2000});
await flush();
const old = pendingManualExecutions.get(42);
await cancelManualExecution(42, 'synthetic cancel', old);
await first;
releaseGate = deferred();
evaluations[0].resolve({result:{type:'object',objectId:'old-object'}});
await flush();
const duringCleanup = {groups:remoteGroups.size,refs:capture.attachment.refs};
await detachBtapDebugger(capture);
const afterCapture = {attached:chromeAttached,refs:capture.attachment.refs};
releaseGate.resolve();
await flush();
process.stdout.write(JSON.stringify({duringCleanup,afterCapture,
  afterCleanup:{attached:chromeAttached,groups:remoteGroups.size,
    refs:capture.attachment.refs,tracked:debuggerAttachments.has('tab:42')}}));
""")
    assert result["duringCleanup"] == {"groups": 1, "refs": 2}
    assert result["afterCapture"] == {"attached": True, "refs": 1}
    assert result["afterCleanup"] == {"attached": False, "groups": 0, "refs": 0, "tracked": False}


def test_normal_settlement_keeps_pending_owner_until_its_group_release_finishes():
    result = _run(r"""
const first = executeManualScript(42, 'first()', {token:'first',policy:'manual',timeoutMs:2000});
await flush();
const pending = pendingManualExecutions.get(42);
releaseGate = deferred();
evaluations[0].resolve({result:{type:'object',objectId:'normal-object'}});
await flush();
const replacement = await executeManualScript(42, 'second()', {token:'second',policy:'manual',timeoutMs:2000});
const duringRelease = {state:pending.state,pending:pendingManualExecutions.size,
  attached:chromeAttached,evaluations:evaluations.length,replacement};
releaseGate.resolve();
const outcome = await first;
process.stdout.write(JSON.stringify({duringRelease,outcome,
  after:{pending:pendingManualExecutions.size,attached:chromeAttached,groups:remoteGroups.size}}));
""")
    during = result["duringRelease"]
    assert during["state"] == "releasing"
    assert during["pending"] == during["evaluations"] == 1
    assert during["attached"] is True
    assert during["replacement"]["data"]["status"] == "busy"
    assert result["outcome"] == {"ok": True, "data": {"synthetic": True}}
    assert result["after"] == {"pending": 0, "attached": False, "groups": 0}
