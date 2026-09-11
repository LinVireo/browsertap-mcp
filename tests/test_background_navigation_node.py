"""Drive navigation deadlines and timer ownership with a deterministic clock."""

from __future__ import annotations

import json

import pytest

from tests.test_dialog_policy import BACKGROUND, _run_node_harness


def _navigation_clock(mode: str, *, timeout_ms: int = 60000, policy: str = "dismiss") -> dict:
    return _run_node_harness(
        r"""
const fs = require('fs');
const source = fs.readFileSync(BACKGROUND_PATH, 'utf8');
const mode = MODE;
let now = 1000000;
Date.now = () => now;
const timers = new Map();
const browserEvents = new Map();
let nextTimer = 1;
globalThis.setTimeout = (fn, delay, ...args) => {
  const id = nextTimer++;
  timers.set(id, { at: now + delay, delay, fn: () => fn(...args) });
  return id;
};
globalThis.clearTimeout = id => timers.delete(id);
const browserEvent = (delay, fn) => {
  browserEvents.set(nextTimer++, { at: now + delay, fn });
};
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
function validDialogPolicy(policy) {
  return ['dismiss', 'accept', 'manual'].includes(policy);
}
function currentProtocolDialog(tabId) { return protocolDialogStates.get(tabId) || null; }
function rememberProtocolDialog(tabId, params) {
  const dialog = { ...params, openedAt: Date.now() };
  protocolDialogStates.set(tabId, dialog);
  return dialog;
}
function debuggerTargetKey(target) { return `tab:${target.tabId}`; }
let currentUrl = 'https://old.example/';
const calls = [];
function openDialog() {
  handleDebuggerEvent({ tabId: 42 }, 'Page.javascriptDialogOpening', {
    type: 'beforeunload', message: 'Leave?', url: currentUrl,
  });
}
const chrome = {
  debugger: {
    onEvent: { addListener() {} },
    onDetach: { addListener() {} },
    attach() { return Promise.resolve(); },
    detach() { return Promise.resolve(); },
    sendCommand(_target, method) {
      calls.push(method);
      if (method !== 'Page.navigate') return Promise.resolve({});
      if (mode === 'cancel') {
        browserEvent(100, () => { void handleDebuggerDetach({ tabId: 42 }); });
      }
      if (['accept', 'manual'].includes(mode)) browserEvent(10, openDialog);
      if (mode === 'grace_dialog') browserEvent(25, openDialog);
      if (mode === 'error') return Promise.reject(new Error('Page.navigate refused'));
      if (['hang', 'manual', 'cancel'].includes(mode)) return new Promise(() => {});
      if (['long', 'accept'].includes(mode)) {
        return new Promise(resolve => browserEvent(mode === 'long' ? 35000 : 400, () => {
          currentUrl = 'https://new.example/';
          resolve({ frameId: 'main' });
        }));
      }
      currentUrl = 'https://new.example/';
      return Promise.resolve({ frameId: 'main' });
    },
  },
  tabs: {
    onRemoved: { addListener() {} },
    async get() { return { url: currentUrl, pendingUrl: '', title: 'Synthetic page' }; },
  },
};
eval(source.slice(
  source.indexOf('function handleDebuggerEvent'),
  source.indexOf('async function handleExtMessage'),
));
async function flushMicrotasks() {
  // Each dispatch/finally chain gets to settle before virtual time advances.
  for (let index = 0; index < 100; index++) await Promise.resolve();
}
(async () => {
  let result;
  let complete = false;
  const task = navigateWithDialogPolicy({
    tabId: 42, url: 'https://new.example/', beforeunload: POLICY, timeoutMs: TIMEOUT_MS,
  }).then(value => { result = value; complete = true; });
  for (let step = 0; step < 100; step++) {
    await flushMicrotasks();
    if (complete) break;
    const ready = [
      ...[...timers].map(([id, item]) => ({ id, item, owner: timers })),
      ...[...browserEvents].map(([id, item]) => ({ id, item, owner: browserEvents })),
    ].sort((left, right) => left.item.at - right.item.at || left.id - right.id)[0];
    if (!ready) throw new Error('navigation stuck without a pending event');
    now = ready.item.at;
    ready.owner.delete(ready.id);
    ready.item.fn();
  }
  if (!complete) throw new Error('virtual navigation exceeded its event limit');
  await task;
  await flushMicrotasks();
  process.stdout.write(JSON.stringify({
    result, calls, elapsedMs: now - 1000000,
    pendingAtEnd: pendingNavigations.has(42),
    liveTimers: [...timers.values()].map(item => ({ delay: item.delay, at: item.at - 1000000 })),
  }));
})().catch(error => { console.error(error); process.exit(1); });
""".replace("BACKGROUND_PATH", json.dumps(str(BACKGROUND)))
        .replace("MODE", json.dumps(mode))
        .replace("POLICY", json.dumps(policy))
        .replace("TIMEOUT_MS", str(timeout_ms))
    )


def test_navigation_can_complete_after_thirty_seconds_within_the_caller_budget():
    outcome = _navigation_clock("long")
    assert outcome["result"]["ok"] is True, outcome
    assert outcome["result"]["data"]["navigation"] == {"frameId": "main"}
    assert 35000 <= outcome["elapsedMs"] < 60000
    assert outcome["calls"].count("Page.navigate") == 1
    assert outcome["pendingAtEnd"] is False
    assert outcome["liveTimers"] == []


@pytest.mark.parametrize("mode,policy,status", [
    ("success", "dismiss", "ok"),
    ("error", "dismiss", "navigation_failed"),
    ("grace_dialog", "dismiss", "blocked_by_beforeunload"),
    ("accept", "accept", "ok"),
])
def test_completed_navigation_clears_all_of_its_race_timers(mode, policy, status):
    outcome = _navigation_clock(mode, policy=policy)
    assert outcome["result"]["ok"] is True, outcome
    assert outcome["result"]["data"]["status"] == status
    assert outcome["calls"].count("Page.navigate") == 1
    assert outcome["pendingAtEnd"] is False
    assert outcome["liveTimers"] == []


@pytest.mark.parametrize("mode,code", [("hang", "cdp_timeout"), ("cancel", "debugger_detached")])
def test_timeout_and_cancellation_keep_dispatch_evidence_and_clear_timers(mode, code):
    outcome = _navigation_clock(mode, timeout_ms=2000)
    assert outcome["result"]["ok"] is False
    assert outcome["result"]["error"]["code"] == code
    assert outcome["result"]["error"]["dispatched"] is True
    assert outcome["elapsedMs"] <= 2000
    assert outcome["calls"].count("Page.navigate") == 1
    assert outcome["pendingAtEnd"] is False
    assert outcome["liveTimers"] == []


def test_manual_dialog_keeps_only_the_navigation_and_retention_watchdogs():
    outcome = _navigation_clock("manual", policy="manual")
    assert outcome["result"]["ok"] is True
    assert outcome["result"]["data"]["status"] == "blocked_by_dialog"
    assert outcome["result"]["data"]["pending_execution"] is True
    assert outcome["pendingAtEnd"] is True
    assert sorted(item["delay"] for item in outcome["liveTimers"]) == [60000, 120000]
