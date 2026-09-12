"""Script preparation must not dispatch a caller after its original deadline."""

from __future__ import annotations

import json

import pytest

from tests.test_dialog_scope_installation import run_node


@pytest.mark.parametrize("route", ["page", "cdp"])
@pytest.mark.parametrize("setup", ["timer_install", "dialog_getter"])
@pytest.mark.parametrize("async_body", [False, True])
def test_builder_scope_install_crossing_deadline_does_not_dispatch_caller(
    route, setup, async_body,
):
    code = "window.hits = (window.hits || 0) + 1; " + (
        "await Promise.resolve(); return window.confirm('late caller')"
        if async_body else "window.confirm('late caller')"
    )
    run_node(f"""
const main = page();
const runtime = await worker([main]);
let advanced = false;
function expire() {{
  if (!advanced) {{ advanced = true; runtime.advance(2000, false); }}
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
const scope = {{
  token: 'synthetic-install', policy: 'dismiss', timeoutMs: 2000,
  deadline: 1002000, startDeadline: 1002000,
}};
const result = await main.run(runtime.build({json.dumps(code)}, scope, {json.dumps(route)}));
assert.equal(advanced, true, 'installation must cross the deadline');
assert.equal(main.window.hits, undefined, 'expired preparation dispatched the caller');
assert.equal(main.nativeCalls.length, 0);
assert.equal(result.ok, false);
assert.equal(result.error.name, 'TimeoutError');
assert.equal(result.error.code, 'exec_timeout');
assert.equal(result.error.dispatched, false);
assert.equal(result.error.may_have_executed, false);
assert.equal(result.error.retryable, false);
for (const name of ['alert', 'confirm', 'prompt', 'onbeforeunload']) {{
  assert.deepEqual(Object.getOwnPropertyDescriptor(main.window, name), descriptors[name]);
}}
assert.deepEqual(Reflect.ownKeys(main.window).filter(key =>
  typeof key === 'string' && key.startsWith('__btap_')), []);
""")


@pytest.mark.parametrize("block_eval", [False, True])
@pytest.mark.parametrize("elapsed", [3000, 13000])
@pytest.mark.parametrize("async_body", [False, True])
def test_worker_scope_adoption_crossing_deadline_does_not_dispatch_caller(
    block_eval, elapsed, async_body,
):
    code = "window.hits = (window.hits || 0) + 1; " + (
        "await Promise.resolve(); return window.confirm('late caller')"
        if async_body else "window.confirm('late caller')"
    )
    run_node(f"""
const main = page();
const child = page(main);
const runtime = await worker([main, child], {{blockEval: {json.dumps(block_eval)}}});
const timer = main.window.setTimeout;
let timerCalls = 0;
let advanced = false;
main.window.setTimeout = (callback, delay) => {{
  const token = timer(callback, delay);
  timerCalls += 1;
  // The first timer prepares the frame; the second adopts that scope in
  // the real caller payload, after the worker's last pre-dispatch check.
  if (timerCalls === 2) {{
    advanced = true;
    runtime.advance({elapsed}, false);
  }}
  return token;
}};
const outcome = await runtime.exec({json.dumps(code)}, 'dismiss');
assert.equal(advanced, true, 'prepared scope adoption must cross the deadline');
assert.equal(main.window.hits, undefined, 'expired preparation dispatched the caller');
assert.equal(main.nativeCalls.length, 0);
assert.equal(child.nativeCalls.length, 0);
assert.equal(outcome.type, 'error');
assert.equal(outcome.error.name, 'TimeoutError');
assert.equal(outcome.error.code, 'exec_timeout');
assert.equal(outcome.error.dispatched, false);
assert.equal(outcome.error.may_have_executed, false);
assert.equal(outcome.error.retryable, false);
assert.equal(runtime.cdpCalls.length, {1 if block_eval else 0});
main.assertRestored();
child.assertRestored();
""")


@pytest.mark.parametrize("route", ["page", "cdp"])
@pytest.mark.parametrize("preparation", ["eval_guard", "async_body", "async_caller"])
@pytest.mark.parametrize("policy", ["dismiss", "manual"])
def test_builder_compilation_crossing_deadline_does_not_dispatch_caller(
    route, preparation, policy,
):
    code = "window.hits = (window.hits || 0) + 1; " + (
        "window.confirm('late caller')" if preparation == "eval_guard" else
        "await Promise.resolve(); return window.confirm('late caller')"
    )
    run_node(f"""
const main = page();
const runtime = await worker([main]);
let advanced = false;
main.window.expireCompilation = () => {{
  if (!advanced) {{ advanced = true; runtime.advance(2000, false); }}
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
        // Async preparation first validates the body without running it;
        // the second matching construction creates the actual caller.
        const desired = window.compileStage === 'async_caller' ? 2 : 1;
        if (matchingCompilations === desired) window.expireCompilation();
      }}
      return compiled;
    }},
  }});
  Object.defineProperty(prototype, 'constructor', {{value: slowConstructor}});
}})()`);
const scope = {{
  token: 'synthetic-compilation', policy: {json.dumps(policy)}, timeoutMs: 2000,
  deadline: 1002000, startDeadline: 1002000,
}};
const result = await main.run(runtime.build({json.dumps(code)}, scope, {json.dumps(route)}));
assert.equal(advanced, true, 'compilation must cross the deadline');
assert.equal(main.window.hits, undefined, 'expired preparation dispatched the caller');
assert.equal(main.nativeCalls.length, 0);
assert.equal(result.ok, false);
assert.equal(result.error.name, 'TimeoutError');
assert.equal(result.error.code, 'exec_timeout');
assert.equal(result.error.dispatched, false);
assert.equal(result.error.may_have_executed, false);
assert.equal(result.error.retryable, false);
main.assertRestored();
""")
