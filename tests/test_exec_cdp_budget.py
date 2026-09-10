"""The CSP/CDP exec fallback must spend the caller's budget, not its own ceiling.

`runCdpExecFallback` used to bound `Runtime.evaluate` with a fixed
DEFAULT_CDP_TIMEOUT_MS, so `execute_js(timeout=60)` on a CSP page failed at 20s
with a deadline the caller never chose and could not see in the reply. These
tests pin the three properties that made that bug possible: the forwarded budget
is honoured, an absent budget keeps the old ceiling, and the one retry shares the
budget as a deadline instead of restarting it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

BACKGROUND = (
    Path(__file__).resolve().parents[1]
    / "src/browsertap_mcp/chrome_extension/background.js"
)


def _run_fallback(scenario: str, *, first_attempt: str = "ok") -> dict:
    """Slice the real fallback plus the real timeout clamp it depends on."""
    source = BACKGROUND.read_text(encoding="utf-8")
    clamp_start = source.index("const MAX_CDP_TIMEOUT_MS")
    clamp_end = source.index("\nfunction clearDebuggerTabState", clamp_start)
    fallback_start = source.index("async function runCdpExecFallback")
    fallback_end = source.index("\nasync function navigateWithDialogPolicy", fallback_start)
    harness = """
const console = { log() {}, error(...a) { process.stderr.write(a.join(' ')); } };
const requestedTimeouts = [];
const attachedAt = [];
let now = 1000;
// The retry path awaits a real sleep. Fire it immediately: the clock this test
// measures is the injected `now`, so a stub that never ran the callback simply
// hung the scenario and produced an empty result rather than a failed assertion.
function setTimeout(callback, delay) { setImmediate(callback); return { delay }; }
async function attachBtapDebugger(target) {
  attachedAt.push(now);
  return { attachment: { target }, generation: 1, released: false };
}
async function detachBtapDebugger() {}
const firstAttempt = %s;
async function sendDebuggerCommandWithTimeout(_lease, method, _params, timeoutMs) {
  requestedTimeouts.push({ method, timeoutMs });
  // Every attempt burns wall clock, so a per-attempt budget and a shared
  // deadline give different answers on the retry.
  now += 5000;
  if (requestedTimeouts.length === 1 && firstAttempt !== 'ok') {
    const error = new Error('cdp_timeout: attach lost before dispatch');
    error.code = firstAttempt;
    error.dispatched = false;
    throw error;
  }
  return { result: { value: { ok: true, data: 'ran' } } };
}
""" % json.dumps(first_attempt)
    # Date.now is what the fallback reads for its deadline; drive it explicitly so
    # the assertions are about arithmetic rather than about how fast node is.
    harness += "const Date = { now: () => now };\n"
    # The fallback closes over this constant, which is declared ~2400 lines above
    # the slice. Copy the real declaration in rather than restating its value:
    # a literal here would keep passing after the shipped default changed.
    default_start = source.index("const DEFAULT_CDP_TIMEOUT_MS")
    harness += source[default_start:source.index("\n", default_start)] + "\n"
    completed = subprocess.run(
        ["node", "-"],
        input=(
            harness
            + source[clamp_start:clamp_end]
            + source[fallback_start:fallback_end]
            + "\n(async () => {\n"
            + scenario
            + "\n})().catch(error => { console.error(error.stack); process.exitCode = 1; });\n"
        ),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_forwarded_budget_replaces_the_built_in_ceiling():
    """A caller asking for 60s must not be cut off at the 20s default."""
    outcome = _run_fallback(
        """
        const result = await runCdpExecFallback(7, 'wrapped', 60000);
        process.stdout.write(JSON.stringify({ result, requestedTimeouts }));
        """
    )
    assert outcome["result"] == {"ok": True, "data": "ran"}
    assert [entry["timeoutMs"] for entry in outcome["requestedTimeouts"]] == [60000]


def test_absent_budget_keeps_the_previous_default():
    """An older bridge sends no budget; that must not become an unbounded wait."""
    source = BACKGROUND.read_text(encoding="utf-8")
    default_line = source[source.index("const DEFAULT_CDP_TIMEOUT_MS"):]
    expected = int(default_line.split("=", 1)[1].split(";", 1)[0].strip())
    outcome = _run_fallback(
        """
        const result = await runCdpExecFallback(7, 'wrapped');
        process.stdout.write(JSON.stringify({ result, requestedTimeouts }));
        """
    )
    assert [entry["timeoutMs"] for entry in outcome["requestedTimeouts"]] == [expected]


def test_retry_shares_one_deadline_instead_of_restarting_the_budget():
    """The undispatched retry must not hand the caller twice the wait it asked for."""
    outcome = _run_fallback(
        """
        const result = await runCdpExecFallback(7, 'wrapped', 30000);
        process.stdout.write(JSON.stringify({ result, requestedTimeouts, attachedAt }));
        """,
        first_attempt="debugger_detached",
    )
    assert outcome["result"] == {"ok": True, "data": "ran"}
    requested = [entry["timeoutMs"] for entry in outcome["requestedTimeouts"]]
    assert len(requested) == 2, requested
    # Attempt one gets the whole budget; attempt two only what is left of it, so
    # the two together can never exceed what the caller allowed.
    assert requested[0] == 30000
    elapsed = outcome["attachedAt"][1] - outcome["attachedAt"][0]
    assert requested[1] == 30000 - elapsed
    assert sum(requested) < 2 * 30000


@pytest.mark.parametrize("timeout", [3.0, 45.0])
def test_bridge_forwards_the_remaining_budget_on_the_exec_wire(timeout):
    """The half above only matters if the bridge actually sends a budget.

    The extension cannot widen a deadline it was never told about, so this pins
    the producer: every exec payload carries `timeoutMs`, and it tracks the
    caller's timeout rather than a constant.
    """
    operations = pytest.importorskip("tests.test_pending_bridge_operations")
    bridge = operations.make_bridge()
    bridge.execute_js("1+1", timeout=timeout, requester_id="a")
    assert len(bridge.sent) == 1
    sent = bridge.sent[0]
    assert "timeoutMs" in sent, sent
    budget_ms = sent["timeoutMs"]
    assert isinstance(budget_ms, int)
    # The budget is what is *left* when the payload is built, so it is at or
    # below the request and never a different order of magnitude from it.
    assert 0 < budget_ms <= int(timeout * 1000)
    assert budget_ms > int(timeout * 1000) * 0.5
