"""The legacy Python CDP route must own and release its page dialog scope."""

from __future__ import annotations

import json

import pytest

from browsertap_mcp import server as S
from tests.test_dialog_scope_installation import run_node


@pytest.fixture
def expression(monkeypatch):
    # The shared page harness starts at 1,000,000 ms. Freeze the sender too so
    # tests can advance delivery and page timers without wall-clock sleeps.
    monkeypatch.setattr(S.time, "time", lambda: 1000.0)

    def build(code, policy="dismiss", timeout=2.0):
        return json.dumps(S._build_cdp_fallback_expression(code, policy, timeout))

    return build


@pytest.mark.parametrize("policy", ["accept", "dismiss"])
def test_python_cdp_answers_dialogs_and_restores_descriptors(expression, policy):
    script = expression(
        "window.alert('notice'); window.answer = window.confirm('question'); "
        "window.text = window.prompt('value', 'seed'); 'done'", policy,
    )
    run_node(f"""
const main = page();
const result = await main.run({script});
assert.equal(result.ok, true);
assert.equal(result.data.__btap_dialog_result, true);
assert.equal(result.data.value, 'done');
assert.deepEqual(Array.from(result.data.dialogs, record => record.type),
  ['alert', 'confirm', 'prompt']);
assert.equal(main.window.answer, {json.dumps(policy)} === 'accept');
assert.equal(main.window.text, {json.dumps(policy)} === 'accept' ? 'seed' : null);
assert.equal(main.nativeCalls.length, 0);
main.assertRestored();
assert.equal(main.run("confirm('human action')"), true);
assert.equal(main.nativeCalls.length, 1);
""")


def test_python_cdp_manual_preserves_native_dialogs_and_value_contract(expression):
    script = expression(
        "window.alert('notice'); [window.confirm('question'), window.prompt('value', 'seed')]",
        "manual",
    )
    run_node(f"""
const main = page();
const result = await main.run({script});
assert.equal(JSON.stringify(result), JSON.stringify({{ok: true, data: [true, 'seed']}}));
assert.equal(main.nativeCalls.length, 3);
main.assertRestored();
""")


@pytest.mark.parametrize("failure", [
    "new Error('failed')",
    "new SyntaxError('return count=' + window.hits)",
    "new EvalError('Refused to evaluate unsafe-eval')",
    "null",
])
def test_python_cdp_page_failure_restores_dialogs_without_replay(expression, failure):
    script = expression(
        "window.hits = (window.hits || 0) + 1; window.confirm('before failure'); "
        f"throw {failure}",
    )
    run_node(f"""
const main = page();
const result = await main.run({script});
assert.equal(result.ok, false);
assert.equal(typeof result.error.message, 'string');
assert.equal(main.window.hits, 1);
assert.equal(main.nativeCalls.length, 0);
main.assertRestored();
""")


@pytest.mark.parametrize("code", [
    "window.hits = (window.hits || 0) + 1; return window.confirm('body')",
    "window.hits = (window.hits || 0) + 1; await Promise.resolve(); return window.confirm('body')",
])
def test_python_cdp_async_body_uses_one_scope_and_one_caller(expression, code):
    script = expression(code)
    run_node(f"""
const main = page();
const result = await main.run({script});
assert.equal(result.ok, true);
assert.equal(result.data.value, false);
assert.equal(result.data.dialogs.length, 1);
assert.equal(main.window.hits, 1);
assert.equal(main.nativeCalls.length, 0);
main.assertRestored();
""")


@pytest.mark.parametrize("setup", ["locked_dialog", "controller_conflict"])
def test_python_cdp_install_failure_prevents_caller_and_rolls_back(expression, setup):
    script = expression("window.hits = (window.hits || 0) + 1; 42")
    setup_code = (
        "Object.defineProperty(main.window, 'prompt', {writable: false, configurable: false});"
        if setup == "locked_dialog" else
        "Object.defineProperty(main.window, '__btap_dialog_controller', "
        "{value: 'page owned', configurable: true});"
    )
    run_node(f"""
const main = page();
{setup_code}
const before = Object.getOwnPropertyDescriptors(main.window);
const result = await main.run({script});
assert.equal(result.ok, false);
assert.equal(main.window.hits, undefined);
for (const name of ['alert', 'confirm', 'prompt', '__btap_dialog_controller']) {{
  assert.deepEqual(Object.getOwnPropertyDescriptor(main.window, name), before[name], name);
}}
assert.equal(main.nativeCalls.length, 0);
""")


@pytest.mark.parametrize("policy", ["accept", "dismiss", "manual"])
def test_python_cdp_late_delivery_cannot_renew_deadline_or_run_caller(expression, policy):
    script = expression("window.hits = (window.hits || 0) + 1; 42", policy)
    run_node(f"""
const main = page();
main.time.advance(2000);
const result = await main.run({script});
assert.equal(result.ok, false);
assert.equal(result.error.name, 'TimeoutError');
assert.equal(result.error.dispatched, false);
assert.equal(result.error.may_have_executed, false);
assert.equal(main.window.hits, undefined);
main.assertRestored();
""")


@pytest.mark.parametrize("fire_timer", [True, False])
def test_python_cdp_pending_scope_expires_at_original_deadline(expression, fire_timer):
    script = expression(
        "window.confirm('during'); new Promise(resolve => {window.finish = resolve;})",
    )
    run_node(f"""
const main = page();
const pending = main.run({script});
assert.equal(main.nativeCalls.length, 0);
assert.notEqual(main.window.confirm, main.original.confirm);
main.time.advance(2000, {json.dumps(fire_timer)});
assert.equal(main.run("confirm('after deadline')"), true);
assert.equal(main.nativeCalls.length, 1);
main.assertRestored();
main.run('window.finish(42)');
const result = await pending;
assert.equal(result.ok, true);
assert.equal(result.data.value, 42);
assert.equal(result.data.dialogs.length, 1);
main.assertRestored();
""")


@pytest.mark.parametrize("setup", ["timer_install", "dialog_getter"])
@pytest.mark.parametrize("async_body", [False, True])
def test_python_cdp_scope_install_crossing_deadline_does_not_dispatch_caller(
    expression, setup, async_body,
):
    code = "window.hits = (window.hits || 0) + 1; " + (
        "await Promise.resolve(); return window.confirm('late caller')"
        if async_body else "window.confirm('late caller')"
    )
    script = expression(code)
    run_node(f"""
const main = page();
let advanced = false;
function expire() {{
  if (!advanced) {{ advanced = true; main.time.advance(2000, false); }}
}}
if ({json.dumps(setup)} === 'timer_install') {{
  const timer = main.window.setTimeout;
  main.window.setTimeout = (callback, delay) => {{
    const token = timer(callback, delay);
    expire();
    return token;
  }};
}} else {{
  Object.defineProperty(main.window, 'alert', {{
    configurable: true, enumerable: true,
    get() {{ expire(); return main.original.alert; }},
  }});
}}
const descriptors = Object.getOwnPropertyDescriptors(main.window);
const result = await main.run({script});
assert.equal(advanced, true, 'scenario did not cross the deadline during installation');
assert.equal(result.ok, false);
assert.equal(result.error.name, 'TimeoutError');
assert.equal(result.error.dispatched, false);
assert.equal(result.error.may_have_executed, false);
assert.equal(main.window.hits, undefined);
assert.equal(main.nativeCalls.length, 0);
for (const name of ['alert', 'confirm', 'prompt']) {{
  assert.deepEqual(Object.getOwnPropertyDescriptor(main.window, name), descriptors[name]);
}}
assert.deepEqual(Reflect.ownKeys(main.window).filter(key =>
  typeof key === 'string' && key.startsWith('__btap_')), []);
""")


@pytest.mark.parametrize("preparation", ["eval_guard", "async_body", "async_caller"])
@pytest.mark.parametrize("policy", ["dismiss", "manual"])
def test_python_cdp_compile_preparation_crossing_deadline_does_not_dispatch_caller(
    expression, preparation, policy,
):
    code = "window.hits = (window.hits || 0) + 1; " + (
        "window.confirm('late caller')" if preparation == "eval_guard" else
        "await Promise.resolve(); return window.confirm('late caller')"
    )
    script = expression(code, policy)
    run_node(f"""
const main = page();
let advanced = false;
main.window.expireCompilation = () => {{
  if (!advanced) {{ advanced = true; main.time.advance(2000, false); }}
}};
main.window.compiledCallerText = {json.dumps(code)};
main.window.compileStage = {json.dumps(preparation)};
main.run(`(() => {{
  const prototype = window.compileStage === 'eval_guard'
    ? Function.prototype : Object.getPrototypeOf(async function() {{}});
  const native = prototype.constructor;
  let matchingCompilations = 0;
  const slowConstructor = new Proxy(native, {{
    construct(target, args, newTarget) {{
      const compiled = Reflect.construct(target, args, newTarget);
      if (args.at(-1) === window.compiledCallerText) {{
        matchingCompilations += 1;
        // The first matching async construction is the compile-only body
        // probe. The second is the actual function, still before invocation.
        const desired = window.compileStage === 'async_caller' ? 2 : 1;
        if (matchingCompilations === desired) window.expireCompilation();
      }}
      return compiled;
    }},
  }});
  Object.defineProperty(prototype, 'constructor', {{value: slowConstructor}});
}})()`);
const result = await main.run({script});
assert.equal(advanced, true, 'scenario did not cross the deadline during compilation');
assert.equal(result.ok, false);
assert.equal(result.error.name, 'TimeoutError');
assert.equal(result.error.dispatched, false);
assert.equal(result.error.may_have_executed, false);
assert.equal(main.window.hits, undefined);
assert.equal(main.nativeCalls.length, 0);
main.assertRestored();
""")


def test_python_cdp_cleanup_preserves_later_page_replacement(expression):
    script = expression(
        "window.confirm('before replacement'); "
        "window.pageConfirm = () => 'page replacement'; window.confirm = window.pageConfirm; 42",
    )
    run_node(f"""
const main = page();
const result = await main.run({script});
assert.equal(result.ok, true);
assert.equal(main.nativeCalls.length, 0);
assert.equal(main.window.confirm, main.window.pageConfirm);
assert.equal(main.run('confirm()'), 'page replacement');
assert.equal(main.window.alert, main.original.alert);
assert.equal(main.window.prompt, main.original.prompt);
assert.equal(Object.hasOwn(main.window, '__btap_dialog_controller'), false);
""")


def test_python_cdp_overlapping_scopes_release_only_their_own_lease(expression):
    first = expression("new Promise(resolve => {window.finish = resolve;})", "accept")
    second = expression("window.confirm('inner')", "dismiss")
    run_node(f"""
const main = page();
const pending = main.run({first});
const inner = await main.run({second});
assert.equal(inner.data.value, false);
assert.equal(main.run("confirm('outer')"), true);
assert.equal(main.nativeCalls.length, 0);
main.run('window.finish(42)');
const outer = await pending;
assert.equal(outer.data.value, 42);
assert.equal(outer.data.dialogs.length, 1);
assert.equal(outer.data.dialogs[0].message, 'outer');
main.assertRestored();
""")
