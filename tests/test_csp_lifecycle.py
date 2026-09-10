"""Exercise the extension's real CSP scope across asynchronous rule updates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BACKGROUND = (
    Path(__file__).resolve().parents[1]
    / "src/browsertap_mcp/chrome_extension/background.js"
)


def _run_csp(scenario: str, *, hold_startup: bool = False, hold_add: bool = False) -> dict:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("const CSP_RULE_BASE")
    end = source.index("\n// --- WebSocket client", start)
    harness = """
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const startupGate = deferred();
const addGate = deferred();
const workGate = deferred();
const rules = new Map();
const calls = [];
const entered = [];
const console = { log() {}, error(...args) { process.stderr.write(args.join(' ')); } };
const flush = () => new Promise(setImmediate);
const chrome = { declarativeNetRequest: {
  async getSessionRules() {
    const snapshot = [...rules.values()];
    await startupGate.promise;
    return snapshot;
  },
  async updateSessionRules(details) {
    calls.push(details);
    if (details.addRules) await addGate.promise;
    for (const id of details.removeRuleIds || []) rules.delete(id);
    for (const rule of details.addRules || []) rules.set(rule.id, rule);
  },
} };
function hasRule(tabId) {
  return [...rules.values()].some(rule => rule.condition.tabIds.includes(tabId));
}
"""
    if hold_startup:
        harness += "rules.set(90000, { id: 90000, condition: { tabIds: [99] } });\n"
    else:
        harness += "startupGate.resolve();\n"
    if not hold_add:
        harness += "addGate.resolve();\n"
    completed = subprocess.run(
        ["node", "-"],
        input=(
            harness + source[start:end] + "\n(async () => {\n" + scenario
            + "\n})().catch(error => { console.error(error.stack); process.exitCode = 1; });\n"
        ),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_startup_cleanup_cannot_remove_a_new_commands_rule():
    outcome = _run_csp("""
const pending = withCspOff(7, async () => {
  entered.push(hasRule(7));
  await workGate.promise;
  return 'done';
});
await flush();
const enteredBeforeCleanup = entered.length;
startupGate.resolve();
await flush();
const ruleDuringCommand = hasRule(7);
workGate.resolve();
const result = await pending;
process.stdout.write(JSON.stringify({
  enteredBeforeCleanup, ruleDuringCommand, entered, result, remaining: rules.size,
}));
""", hold_startup=True)
    assert outcome == {
        "enteredBeforeCleanup": 0,
        "ruleDuringCommand": True,
        "entered": [True],
        "result": "done",
        "remaining": 0,
    }


def test_same_tab_commands_both_wait_for_rule_installation():
    outcome = _run_csp("""
await flush();
const work = async () => {
  entered.push(hasRule(7));
  await workGate.promise;
};
const first = withCspOff(7, work);
await flush();
const second = withCspOff(7, work);
await flush();
const enteredBeforeInstall = entered.length;
addGate.resolve();
await flush();
const ruleDuringCommand = hasRule(7);
workGate.resolve();
await Promise.all([first, second]);
process.stdout.write(JSON.stringify({
  enteredBeforeInstall, ruleDuringCommand, entered, remaining: rules.size,
  adds: calls.filter(call => call.addRules).length,
  removals: calls.filter(call => !call.addRules).length,
}));
""", hold_add=True)
    assert outcome == {
        "enteredBeforeInstall": 0,
        "ruleDuringCommand": True,
        "entered": [True, True],
        "remaining": 0,
        "adds": 1,
        "removals": 1,
    }


def test_nested_scope_keeps_the_outer_rule_until_it_returns():
    outcome = _run_csp("""
await withCspOff(7, async () => {
  entered.push(hasRule(7));
  await withCspOff(7, async () => { entered.push(hasRule(7)); });
  entered.push(hasRule(7));
});
process.stdout.write(JSON.stringify({ entered, remaining: rules.size, calls: calls.length }));
""")
    assert outcome == {"entered": [True, True, True], "remaining": 0, "calls": 2}


def test_another_tab_finishing_does_not_remove_the_active_rule():
    outcome = _run_csp("""
const first = withCspOff(7, async () => { await workGate.promise; });
await flush();
await withCspOff(8, async () => { entered.push(hasRule(7), hasRule(8)); });
entered.push(hasRule(7), hasRule(8));
workGate.resolve();
await first;
process.stdout.write(JSON.stringify({ entered, remaining: rules.size }));
""")
    assert outcome == {"entered": [True, True, True, False], "remaining": 0}
