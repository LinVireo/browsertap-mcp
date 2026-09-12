"""A complete worker must prepare every frame before dispatching caller code."""

from __future__ import annotations

import json

import pytest

from tests.test_dialog_scope_installation import run_node


@pytest.mark.parametrize("frame_order", ["top-first", "child-first"])
@pytest.mark.parametrize("block_eval", [False, True])
def test_child_dialogs_are_scoped_before_the_top_caller_in_every_route(
    frame_order, block_eval
):
    run_node(f"""
const main = page();
const child = page(main);
main.window.child = child.window;
const runtime = await worker([main, child], {{
  frameOrder: {json.dumps(frame_order)}, blockEval: {json.dumps(block_eval)},
}});
const outcome = await runtime.exec(`window.runs = (window.runs || 0) + 1;
  window.child.confirm('child during caller')`, 'dismiss');
assert.equal(outcome.type, 'result');
assert.equal(outcome.result.value, false);
assert.equal(main.window.runs, 1);
assert.equal(child.nativeCalls.length, 0);
assert.equal(runtime.cdpCalls.length, {1 if block_eval else 0});
main.assertRestored();
child.assertRestored();
""")


@pytest.mark.parametrize("frame_order", ["top-first", "child-first"])
def test_partial_frame_preparation_failure_never_dispatches_the_caller(frame_order):
    run_node(f"""
const main = page();
const child = page(main);
let installed = 0;
const runtime = await worker([main, child], {{
  frameOrder: {json.dumps(frame_order)},
  beforeFrame(request) {{
    if (request.args[0] === 'install' && installed++ === 1) {{
      throw new Error('synthetic frame became inaccessible');
    }}
  }},
}});
const outcome = await runtime.exec('window.runs = (window.runs || 0) + 1');
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, false);
assert.equal(outcome.error.may_have_executed, false);
assert.match(outcome.error.message, /prepar|install/i);
assert.equal(main.window.runs, undefined);
assert.equal(runtime.cdpCalls.length, 0);
assert.equal(runtime.injections.filter(request => request.args[0] === 'release').length, 1);
main.assertRestored();
child.assertRestored();
""")


@pytest.mark.parametrize("missing", ["all", "child", "top"])
def test_unconfirmed_preparation_never_dispatches_the_caller(missing):
    run_node(f"""
const main = page();
const child = page(main);
const missing = {json.dumps(missing)};
const runtime = await worker([main, child], {{
  afterInjection(request, entries) {{
    if (request.args[0] !== 'install') return entries;
    if (missing === 'all') return [];
    return entries.map(entry => entry.frameId === (missing === 'top' ? 91 : 1)
      ? {{ frameId: entry.frameId }} : entry);
  }},
}});
const outcome = await runtime.exec('window.runs = (window.runs || 0) + 1');
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, false);
assert.equal(outcome.error.may_have_executed, false);
assert.equal(main.window.runs, undefined);
assert.equal(runtime.cdpCalls.length, 0);
main.assertRestored();
child.assertRestored();
""")


def test_hung_preparation_times_out_without_dispatch_and_late_install_is_inert():
    run_node("""
const main = page();
const child = page(main);
let finish;
const gate = new Promise(resolve => { finish = resolve; });
const runtime = await worker([main, child], {
  frameOrder: 'top-first',
  async beforeFrame(request, _frame, index) {
    if (request.args[0] === 'install' && index === 1) await gate;
  },
});
const execution = runtime.exec('window.runs = (window.runs || 0) + 1');
await new Promise(resolve => setImmediate(resolve));
runtime.advance(3000);
assert.equal(runtime.expireTimers(3000), 1, 'preparation needs the caller deadline');
const outcome = await execution;
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, false);
assert.equal(outcome.error.may_have_executed, false);
assert.equal(main.window.runs, undefined);
main.assertRestored();
child.assertRestored();
finish();
await new Promise(resolve => setImmediate(resolve));
assert.equal(main.window.runs, undefined);
assert.equal(runtime.cdpCalls.length, 0);
assert.equal(runtime.injections.filter(request => request.args[0] === 'release').length, 1);
main.assertRestored();
child.assertRestored();
""")


def test_preparation_spends_the_existing_deadline_and_cannot_start_a_late_caller():
    run_node("""
const main = page();
const child = page(main);
const runtime = await worker([main, child], {
  afterInjection(request, entries) {
    if (request.args[0] === 'install') runtime.advance(3000);
    return entries;
  },
});
const outcome = await runtime.exec('window.runs = (window.runs || 0) + 1');
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, false);
assert.equal(outcome.error.may_have_executed, false);
assert.equal(main.window.runs, undefined);
assert.equal(runtime.cdpCalls.length, 0);
main.assertRestored();
child.assertRestored();
""")


def test_frame_scope_expiry_is_fixed_before_the_first_frame_is_prepared():
    run_node("""
const main = page();
const child = page(main);
let delayed = false;
const runtime = await worker([main, child], {
  rejectCleanup: true, frameOrder: 'top-first',
  beforeFrame(request, _frame, index) {
    if (!delayed && request.args[0] === 'install' && index === 1) {
      delayed = true;
      runtime.advance(1500);
    }
  },
});
const outcome = await runtime.exec("window.confirm('top')");
assert.equal(outcome.type, 'result');
assert.equal(delayed, true);
main.assertRestored();
assert.notEqual(child.window.confirm, child.original.confirm);
runtime.advance(11499);
assert.notEqual(child.window.confirm, child.original.confirm);
runtime.advance(1);
child.assertRestored();
""")


def test_csp_and_cdp_share_the_budget_already_spent_preparing_frames():
    run_node("""
const main = page();
const child = page(main);
main.window.child = child.window;
let prepareCount = 0;
let attempts = 0;
let cdpTimerDelays;
const runtime = await worker([main, child], {
  blockEval: true,
  afterInjection(request, entries) {
    if (request.args[0] === 'install') {
      prepareCount += 1;
      runtime.advance(1000);
    } else if (request.args[0] !== 'release') {
      attempts += 1;
      runtime.advance(500);
    }
    return entries;
  },
  beforeCdp() { cdpTimerDelays = runtime.pendingTimerDelays(); },
});
const outcome = await runtime.exec("window.child.confirm('CSP child')");
assert.equal(outcome.type, 'result');
assert.equal(outcome.result.value, false);
assert.equal(prepareCount, 1);
assert.equal(attempts, 2);
assert.equal(runtime.cdpCalls.length, 1);
assert.ok(cdpTimerDelays.includes(1000), JSON.stringify(cdpTimerDelays));
assert.equal(child.nativeCalls.length, 0);
main.assertRestored();
child.assertRestored();
""")


def test_debugger_attach_cannot_dispatch_after_the_shared_deadline():
    run_node("""
const main = page();
const runtime = await worker([main], {
  blockEval: true,
  beforeAttach() { runtime.advance(3000); },
});
const outcome = await runtime.exec('window.runs = (window.runs || 0) + 1');
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, false);
assert.equal(main.window.runs, undefined);
assert.equal(runtime.cdpCalls.length, 0);
main.assertRestored();
""")


@pytest.mark.parametrize("failure", ["rejected", "missing"])
def test_prepared_caller_with_an_unknown_result_is_not_replayed(failure):
    run_node(f"""
const main = page();
const child = page(main);
const runtime = await worker([main, child], {{
  frameOrder: 'top-first',
  afterInjection(request, entries) {{
    if (request.args[0] === 'install' || request.args[0] === 'release') return entries;
    if ({json.dumps(failure)} === 'rejected') throw new Error('result channel lost');
    return [];
  }},
}});
const outcome = await runtime.exec('window.runs = (window.runs || 0) + 1');
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, true);
assert.equal(outcome.error.may_have_executed, true);
assert.equal(main.window.runs, 1);
assert.equal(runtime.cdpCalls.length, 0);
assert.equal(runtime.injections.filter(request => request.args[0] === 'release').length, 1);
main.assertRestored();
child.assertRestored();
""")


def test_timed_out_caller_injection_cannot_execute_later_or_replay():
    run_node("""
const main = page();
const child = page(main);
let finish;
const gate = new Promise(resolve => { finish = resolve; });
const runtime = await worker([main, child], {
  async beforeFrame(request, _frame, index) {
    if (request.args[0] !== 'install' && request.args[0] !== 'release' && index === 0) await gate;
  },
});
const execution = runtime.exec('window.runs = (window.runs || 0) + 1');
await new Promise(resolve => setImmediate(resolve));
runtime.advance(3000);
assert.equal(runtime.expireTimers(3000), 1);
const outcome = await execution;
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, true);
assert.equal(outcome.error.may_have_executed, true);
finish();
await new Promise(resolve => setImmediate(resolve));
assert.equal(main.window.runs, undefined);
assert.equal(runtime.cdpCalls.length, 0);
main.assertRestored();
child.assertRestored();
""")


@pytest.mark.parametrize("prefix", ["", "await Promise.resolve();"])
def test_a_started_callers_evalerror_is_not_a_csp_retry_signal(prefix):
    run_node(f"""
const main = page();
const child = page(main);
const runtime = await worker([main, child]);
const outcome = await runtime.exec(`{prefix} window.runs = (window.runs || 0) + 1;
  throw new EvalError('Refused to evaluate unsafe-eval due to content security policy')`);
assert.equal(outcome.type, 'error');
assert.equal(main.window.runs, 1);
assert.equal(runtime.cdpCalls.length, 0);
main.assertRestored();
child.assertRestored();
""")


def test_csp_before_the_inner_eval_still_falls_back_once_with_child_scopes():
    run_node("""
const main = page();
const child = page(main);
main.window.child = child.window;
const runtime = await worker([main, child], {blockInnerEval: true, frameOrder: 'top-first'});
const outcome = await runtime.exec(`window.runs = (window.runs || 0) + 1;
  window.child.confirm('child after inner CSP')`);
assert.equal(outcome.type, 'result');
assert.equal(outcome.result.value, false);
assert.equal(main.window.runs, 1);
assert.equal(child.nativeCalls.length, 0);
assert.equal(runtime.cdpCalls.length, 1);
main.assertRestored();
child.assertRestored();
""")


def test_cleanup_uses_only_the_time_remaining_on_the_command_deadline():
    run_node("""
const main = page();
const child = page(main);
const runtime = await worker([main, child], {
  hangCleanup: true,
  afterInjection(request, entries) {
    if (request.args[0] !== 'install' && request.args[0] !== 'release') runtime.advance(2500);
    return entries;
  },
});
const execution = runtime.exec("window.confirm('finished')");
await new Promise(resolve => setImmediate(resolve));
assert.equal(runtime.expireTimers(1000), 0, 'cleanup must not restart its full 1s cap');
runtime.advance(500);
assert.equal(runtime.expireTimers(500), 1);
const outcome = await execution;
assert.equal(outcome.type, 'result');
assert.equal(outcome.result.value, false);
assert.equal(runtime.injections.filter(request => request.args[0] === 'release').length, 1);
main.assertRestored();
runtime.advance(10000);
child.assertRestored();
""")


def test_cdp_does_not_round_a_short_remaining_budget_up_to_one_hundred_ms():
    run_node("""
const main = page();
let cdpTimerDelays;
const runtime = await worker([main], {
  blockEval: true,
  afterInjection(request, entries) {
    if (request.args[0] === 'install') runtime.advance(2950);
    return entries;
  },
  beforeCdp() { cdpTimerDelays = runtime.pendingTimerDelays(); },
});
const execution = runtime.exec('window.runs = (window.runs || 0) + 1');
await new Promise(resolve => setImmediate(resolve));
assert.ok(cdpTimerDelays.includes(50), JSON.stringify(cdpTimerDelays));
assert.equal(cdpTimerDelays.includes(100), false);
assert.equal(runtime.expireTimers(50), 1, 'only the remaining async-tab grace is pending');
const outcome = await execution;
assert.equal(outcome.type, 'result');
assert.equal(main.window.runs, 1);
assert.equal(runtime.cdpCalls.length, 1);
main.assertRestored();
""")


def test_hung_csp_setup_cannot_run_a_late_caller_after_cleanup():
    run_node("""
const main = page();
const child = page(main);
let finish;
const gate = new Promise(resolve => { finish = resolve; });
const runtime = await worker([main, child], {blockEval: true});
runtime.chrome.declarativeNetRequest.updateSessionRules = async request => {
  if (request.addRules?.length) await gate;
};
const execution = runtime.exec('window.runs = (window.runs || 0) + 1');
await new Promise(resolve => setImmediate(resolve));
runtime.advance(3000);
assert.equal(runtime.expireTimers(3000), 1);
const outcome = await execution;
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.dispatched, true);
assert.equal(outcome.error.may_have_executed, true);
finish();
await new Promise(resolve => setImmediate(resolve));
assert.equal(main.window.runs, undefined);
assert.equal(runtime.cdpCalls.length, 0);
assert.equal(runtime.injections.filter(request => request.args[0] === 'release').length, 1);
main.assertRestored();
child.assertRestored();
""")
