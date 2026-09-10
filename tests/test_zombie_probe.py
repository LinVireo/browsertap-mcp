"""A timed-out page script is probed, and terminated only when provably running.

After `Runtime.evaluate` hits `cdp_timeout` the extension detaches the debugger,
which does not stop the script: a synchronous loop keeps the main thread pinned,
a pending fetch keeps running. The caller was told `outcome_unknown` and nothing
else. `Runtime.terminateExecution` would end a pinned script, but it terminates
the running script *or, if none is running, the next one* -- so it may only be
issued after a sentinel evaluate has proved the thread is pinned.

The probe runs on the attachment whose evaluate timed out, before that
attachment is invalidated: measured in a real Chrome, a fresh attach to a pinned
renderer cannot deliver the interrupt (its renderer-side session is created on
the pinned thread), so `terminateExecution` on it never answered.
"""

from __future__ import annotations

import json

import pytest

from browsertap_mcp import browser_bridge as bridge_module
from browsertap_mcp import server as S
from tests.test_phase0_recovery import _cdp_exec_fallback_source, _run_node_script

# The scripted debugger answers each raw sendCommand by method name, in order:
# 'ok' resolves, 'pinned' never settles (the timer wins), 'fail' rejects. The
# outer evaluate is scripted under "outer": 'ok', 'timeout' (which runs the
# beforeInvalidate hook exactly as the real sender does), 'timeout_nohook' (an
# older sender that ignores the hook) or 'fail'.
_HARNESS = """
console.log = () => {{}};
const DEFAULT_CDP_TIMEOUT_MS = 5000;
const script = {script};
const sent = [];
let attaches = 0;
let detaches = 0;
let hookRuns = 0;
function next(kind) {{
  const queue = script[kind] || [];
  return queue.length ? queue.shift() : 'ok';
}}
const chrome = {{ debugger: {{
  sendCommand(target, method, params) {{
    sent.push(method);
    const step = next(method);
    if (step === 'ok') return Promise.resolve({{ result: {{ value: 1 }} }});
    if (step === 'fail') return Promise.reject(new Error('boom'));
    return new Promise(() => {{}});  // pinned: never settles
  }},
}} }};
async function attachBtapDebugger({{ tabId }}) {{
  attaches += 1;
  return {{ attachment: {{ target: {{ tabId }} }} }};
}}
async function sendDebuggerCommandWithTimeout(lease, method, params, timeoutMs, minimum, dispatchState, beforeInvalidate) {{
  sent.push(method);
  if (dispatchState) dispatchState.dispatched = true;
  const step = next('outer');
  if (step === 'ok') return {{ result: {{ value: {{ ok: true, data: 'ran' }} }} }};
  const timedOut = step === 'timeout' || step === 'timeout_nohook';
  const error = new Error(timedOut ? `cdp_timeout: ${{method}} exceeded ${{timeoutMs}}ms` : 'boom');
  if (timedOut) {{ error.code = 'cdp_timeout'; error.method = method; }}
  if (step === 'timeout' && typeof beforeInvalidate === 'function') {{
    hookRuns += 1;
    Object.assign(error, await beforeInvalidate(script.noAttachment ? null : lease.attachment) || {{}});
  }}
  error.dispatched = true;
  throw error;
}}
async function detachBtapDebugger() {{ detaches += 1; }}
function currentProtocolDialog() {{ return script.dialog || null; }}
{source}
(async () => {{
  const res = await runCdpExecFallback(11, 'while(true){{}}');
  process.stdout.write(JSON.stringify({{ res, sent, attaches, detaches, hookRuns }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""


def _outcome(script: dict) -> dict:
    return _run_node_script(_HARNESS.format(
        script=json.dumps(script), source=_cdp_exec_fallback_source(),
    ))


def test_a_pinned_page_is_terminated_and_reported_killed():
    outcome = _outcome({
        "outer": ["timeout"],
        "Runtime.evaluate": ["pinned", "ok"],
        "Runtime.terminateExecution": ["ok"],
    })

    error = outcome["res"]["error"]
    assert error["code"] == "cdp_timeout"
    assert error["dispatched"] is True
    assert error["zombie"] == "killed"
    assert "terminateExecution" in error["zombie_detail"]
    # Outer evaluate, sentinel (pinned), terminate, sentinel (answers).
    assert outcome["sent"] == [
        "Runtime.evaluate", "Runtime.evaluate", "Runtime.terminateExecution", "Runtime.evaluate",
    ]


def test_the_probe_uses_the_live_attachment_not_a_fresh_one():
    # The whole reason the probe is a hook: only the session already set up in
    # the renderer can deliver the interrupt to a pinned main thread.
    outcome = _outcome({
        "outer": ["timeout"],
        "Runtime.evaluate": ["pinned", "ok"],
        "Runtime.terminateExecution": ["ok"],
    })

    assert outcome["hookRuns"] == 1
    assert outcome["attaches"] == 1


def test_a_responsive_page_is_not_terminated():
    # The script finished, or is idle awaiting a fetch. There is nothing that
    # could be killed safely -- terminateExecution would take the page's *next*
    # script instead.
    outcome = _outcome({"outer": ["timeout"], "Runtime.evaluate": ["ok"]})

    error = outcome["res"]["error"]
    assert error["zombie"] == "not_blocking"
    assert "Runtime.terminateExecution" not in outcome["sent"]


def test_a_page_that_stays_pinned_after_termination_is_reported_still_running():
    outcome = _outcome({
        "outer": ["timeout"],
        "Runtime.evaluate": ["pinned", "pinned"],
        "Runtime.terminateExecution": ["ok"],
    })

    assert outcome["res"]["error"]["zombie"] == "still_running"
    assert "remains pinned" in outcome["res"]["error"]["zombie_detail"]


def test_an_unanswered_terminate_is_reported_still_running_not_killed():
    # The measured real-Chrome failure shape: the kill never answers.
    outcome = _outcome({
        "outer": ["timeout"],
        "Runtime.evaluate": ["pinned"],
        "Runtime.terminateExecution": ["pinned"],
    })

    error = outcome["res"]["error"]
    assert error["zombie"] == "still_running"
    assert "did not answer" in error["zombie_detail"]
    # No second sentinel: the kill was never acknowledged, so there is nothing
    # to confirm.
    assert outcome["sent"].count("Runtime.evaluate") == 2


def test_a_rejected_terminate_is_reported_still_running():
    outcome = _outcome({
        "outer": ["timeout"],
        "Runtime.evaluate": ["pinned"],
        "Runtime.terminateExecution": ["fail"],
    })

    error = outcome["res"]["error"]
    assert error["zombie"] == "still_running"
    assert "failed (boom)" in error["zombie_detail"]


def test_a_page_pinned_by_a_native_dialog_is_never_terminated():
    # alert() blocks the renderer inside whichever script called it -- possibly
    # the page's own, opened before the caller's evaluate ever ran. That script
    # is not the caller's to kill; the dialog tools own the case.
    outcome = _outcome({
        "outer": ["timeout"],
        "dialog": {"type": "alert", "message": "hi"},
        "Runtime.evaluate": ["pinned"],
    })

    error = outcome["res"]["error"]
    assert error["zombie"] == "blocked_by_dialog"
    assert "handle_dialog" in error["zombie_detail"]
    assert "Runtime.terminateExecution" not in outcome["sent"]


def test_a_failed_sentinel_is_reported_unknown():
    outcome = _outcome({"outer": ["timeout"], "Runtime.evaluate": ["fail"]})

    error = outcome["res"]["error"]
    assert error["zombie"] == "unknown"
    assert "boom" in error["zombie_detail"]
    assert "Runtime.terminateExecution" not in outcome["sent"]


def test_a_missing_attachment_is_reported_unknown():
    outcome = _outcome({"outer": ["timeout"], "noAttachment": True})

    error = outcome["res"]["error"]
    assert error["zombie"] == "unknown"
    assert "no live attachment" in error["zombie_detail"]
    assert outcome["sent"] == ["Runtime.evaluate"]


def test_a_sender_that_ignores_the_hook_still_yields_a_verdict():
    # Absent would read as "the extension has no opinion", which is the old
    # bug in a new coat. An older sender gets an explicit unknown instead.
    outcome = _outcome({"outer": ["timeout_nohook"]})

    error = outcome["res"]["error"]
    assert error["code"] == "cdp_timeout"
    assert error["zombie"] == "unknown"
    assert "did not run the zombie probe" in error["zombie_detail"]
    assert outcome["hookRuns"] == 0


def test_no_probe_runs_for_a_failure_that_is_not_a_timeout():
    outcome = _outcome({"outer": ["fail"]})

    error = outcome["res"]["error"]
    assert "zombie" not in error
    assert outcome["hookRuns"] == 0
    assert outcome["sent"] == ["Runtime.evaluate"]


@pytest.mark.parametrize(
    "zombie", ["killed", "not_blocking", "still_running", "blocked_by_dialog", "unknown"],
)
def test_every_verdict_leaves_retry_unsafe(zombie):
    # Even `killed` does not undo what the script did before the kill.
    detail = {"error": {
        "message": "CDP fallback failed: cdp_timeout: Runtime.evaluate exceeded 20000ms",
        "code": "cdp_timeout", "dispatched": True, "zombie": zombie, "zombie_detail": "x",
    }}

    code, retry_safe, diagnostics = bridge_module._page_execution_metadata(detail, script="x")

    assert code == "cdp_timeout"
    assert retry_safe is False
    assert diagnostics["zombie"] == zombie
    assert diagnostics["zombie_detail"] == "x"


def test_the_mcp_envelope_carries_the_verdict():
    diagnostics = S._result_diagnostics(
        {"status": "error", "code": "cdp_timeout", "zombie": "killed", "zombie_detail": "freed"},
        tool="execute_js",
    )

    assert diagnostics["zombie"] == "killed"
    assert diagnostics["zombie_detail"] == "freed"


def test_success_carries_no_zombie_field():
    outcome = _outcome({"outer": ["ok"]})

    assert outcome["res"] == {"ok": True, "data": "ran"}
    assert outcome["sent"] == ["Runtime.evaluate"]


def _timeout_result(**extra):
    return {"success": False, "data": {"error": {
        "message": "CDP fallback failed: cdp_timeout: Runtime.evaluate exceeded 5945ms",
        "code": "cdp_timeout", "dispatched": True, **extra,
    }}}


def test_a_killed_script_releases_its_tab():
    # Measured 2026-09-10: `zombie: killed` came back and the next call on the
    # same tab still got target_busy -- a reservation guarding a reply that
    # could never arrive.
    assert bridge_module._execution_may_continue(_timeout_result(zombie="killed")) is False


@pytest.mark.parametrize("zombie", ["not_blocking", "still_running", "blocked_by_dialog", "unknown"])
def test_every_other_verdict_keeps_the_outcome_open(zombie):
    assert bridge_module._execution_may_continue(_timeout_result(zombie=zombie)) is True


def test_a_timeout_without_a_verdict_still_keeps_the_outcome_open():
    assert bridge_module._execution_may_continue(_timeout_result()) is True
