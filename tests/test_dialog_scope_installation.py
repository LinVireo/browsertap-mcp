"""F6 regression: execute complete worker imports and actual page/frame payloads."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests/node/dialog_scope_harness.cjs"


def run_node(source: str) -> None:
    program = (
        f"const {{ assert, page, worker }} = require({json.dumps(str(HARNESS))});\n"
        "(async () => {\n" + source + "\n})().catch(error => {\n"
        "console.error(error); process.exitCode = 1;\n});\n"
    )
    completed = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, cwd=ROOT, timeout=20
    )
    assert completed.returncode == 0, completed.stderr


def test_ordinary_pages_keep_original_dialog_functions_and_window_keys():
    run_node("""
const main = page();
const frame = page(main);
await worker([main, frame]);
for (const target of [main, frame]) {
  target.assertRestored();
  assert.deepEqual(Reflect.ownKeys(target.window), target.keys);
  assert.equal(target.run("window.confirm('human action')"), true);
  assert.equal(target.nativeCalls.length, 1);
}
""")


@pytest.mark.parametrize("route", ["page", "cdp"])
@pytest.mark.parametrize("policy", [None, "manual"])
def test_unmarked_or_manual_builder_never_installs_hooks(route, policy):
    scope = None if policy is None else {"token": "manual", "policy": policy}
    run_node(f"""
const main = page();
const runtime = await worker();
const result = await main.run(runtime.build("window.confirm('human action')",
  {json.dumps(scope)}, {json.dumps(route)}));
assert.equal(result.ok, true);
assert.equal(result.data, true);
assert.equal(main.nativeCalls.length, 1);
main.assertRestored();
""")


@pytest.mark.parametrize("route", ["page", "cdp"])
@pytest.mark.parametrize("policy", ["accept", "dismiss"])
def test_answering_scope_records_dialogs_then_restores_descriptors(route, policy):
    run_node(f"""
const main = page();
const runtime = await worker();
const policy = {json.dumps(policy)};
const result = await main.run(runtime.build(`window.alert('notice');
  window.answer = window.confirm('question');
  window.text = window.prompt('value', 'seed'); 'done'`,
  {{ token: 'scope', policy }}, {json.dumps(route)}));
assert.equal(result.ok, true);
assert.equal(result.data.value, 'done');
assert.equal(main.window.answer, policy === 'accept');
assert.equal(main.window.text, policy === 'accept' ? 'seed' : null);
assert.deepEqual(Array.from(result.data.dialogs, record => record.type),
  ['alert', 'confirm', 'prompt']);
assert.ok(result.data.dialogs.every(record => record.token === 'scope' && record.policy === policy));
assert.equal(main.nativeCalls.length, 0);
main.assertRestored();
assert.equal(main.run("window.confirm('after scope')"), true);
assert.equal(main.nativeCalls.length, 1);
""")


def test_throwing_user_code_releases_its_dialog_scope():
    run_node("""
const main = page();
const runtime = await worker();
const result = await main.run(runtime.build("window.confirm('during'); throw new Error('failure')",
  { token: 'throws', policy: 'dismiss' }));
assert.equal(result.ok, false);
assert.equal(result.error.message, 'failure');
main.assertRestored();
""")


@pytest.mark.parametrize("release_first", ["outer", "inner"])
def test_overlapping_scopes_restore_only_after_last_release(release_first):
    run_node(f"""
const main = page();
const runtime = await worker();
const outer = runtime.manage(main, 'enter', {{token: 'outer', policy: 'dismiss', deadline: 1015000}});
assert.equal(main.run("window.confirm('outer')"), false);
const inner = runtime.manage(main, 'enter', {{token: 'inner', policy: 'accept', deadline: 1012000}});
assert.equal(main.run("window.confirm('inner')"), true);
const first = {json.dumps(release_first)} === 'outer' ? outer : inner;
const last = first === outer ? inner : outer;
first.release();
assert.notEqual(main.window.confirm, main.original.confirm);
assert.equal(main.run("window.confirm('remaining')"), last === inner);
last.release();
first.release();
main.assertRestored();
assert.equal(outer.records()[0].message, 'outer');
assert.equal(inner.records()[0].message, 'inner');
""")


@pytest.mark.parametrize("fire_timers", [True, False])
def test_expired_scope_restores_without_a_worker_or_finally(fire_timers):
    run_node(f"""
const main = page();
const runtime = await worker();
const lease = runtime.manage(main, 'enter', {{token: 'expiry', policy: 'dismiss', deadline: 1011000}});
const savedWrapper = main.window.confirm;
assert.equal(main.run("window.confirm('during')"), false);
main.time.advance(11000, {json.dumps(fire_timers)});
if (!{json.dumps(fire_timers)}) assert.equal(savedWrapper('after deadline'), true);
main.assertRestored();
assert.equal(lease.records().length, 1);
lease.release();
main.assertRestored();
""")


def test_expiry_of_inner_scope_keeps_outer_policy_and_new_document_is_native():
    run_node("""
const main = page();
const runtime = await worker();
runtime.manage(main, 'enter', {token: 'outer', policy: 'dismiss', deadline: 1015000});
runtime.manage(main, 'enter', {token: 'inner', policy: 'accept', deadline: 1011000});
main.time.advance(11000);
assert.equal(main.run("window.confirm('outer remains')"), false);
const navigated = page();
navigated.assertRestored();
assert.equal(navigated.run("window.confirm('new document')"), true);
main.time.advance(4000);
main.assertRestored();
navigated.assertRestored();
""")


def test_page_replacement_is_preserved_and_records_are_bounded():
    run_node("""
const main = page();
const runtime = await worker();
const lease = runtime.manage(main, 'enter', {token: 'many', policy: 'dismiss', deadline: 1015000});
for (let index = 0; index < 60; index++) main.window.confirm(String(index));
assert.equal(lease.records().length, 50);
assert.equal(lease.records()[0].message, '10');
assert.equal(lease.records().at(-1).message, '59');
const replacement = () => 'page replacement';
main.window.confirm = replacement;
lease.release();
assert.equal(main.window.confirm, replacement);
assert.equal(main.window.alert, main.original.alert);
assert.equal(main.window.prompt, main.original.prompt);
assert.equal(main.window.onbeforeunload, main.original.onbeforeunload);
assert.deepEqual(Object.keys(main.window).filter(key => key.startsWith('__btap_')), []);
""")


@pytest.mark.parametrize("policy", ["accept", "dismiss", None])
def test_full_worker_exec_scopes_only_target_frames_and_cleans_them(policy):
    run_node(f"""
const main = page();
const child = page(main);
const unrelated = page();
main.window.child = child.window;
const runtime = await worker([main, child]);
const result = await runtime.exec("window.child.confirm('child'); window.confirm('top')", {json.dumps(policy)});
assert.equal(result.type, 'result');
assert.ok(runtime.injections.length >= 1);
for (const target of [main, child, unrelated]) target.assertRestored();
assert.equal(unrelated.nativeCalls.length, 0);
assert.equal(main.nativeCalls.length, {1 if policy is None else 0});
assert.equal(child.nativeCalls.length, {1 if policy is None else 0});
""")


def test_complete_worker_csp_fallback_keeps_scoped_dialogs_and_releases_them():
    run_node("""
const main = page();
const runtime = await worker([main], { blockEval: true });
const result = await runtime.exec("window.confirm('fallback')", 'dismiss');
assert.equal(result.type, 'result');
assert.equal(result.result.value, false);
assert.equal(result.result.dialogs[0].message, 'fallback');
assert.equal(runtime.cdpCalls.length, 1);
assert.equal(main.nativeCalls.length, 0);
main.assertRestored();
""")


@pytest.mark.parametrize(("budget", "lifetime"), [(500, 11000), (3000, 13000), (120000, 130000)])
def test_emitted_scope_expires_at_the_callers_budget_plus_grace(budget, lifetime):
    run_node(f"""
const main = page();
const runtime = await worker();
main.run('window.wait = new Promise(resolve => {{ window.finish = resolve; }});');
const execution = main.run(runtime.build('window.wait',
  {{token: 'pending', policy: 'dismiss', timeoutMs: {budget}}}));
main.time.advance({lifetime} - 1);
assert.notEqual(main.window.confirm, main.original.confirm);
main.time.advance(1);
main.assertRestored();
main.window.finish('late result');
assert.equal((await execution).data.value, 'late result');
main.assertRestored();
""")


def test_scope_restores_accessor_and_inherited_dialog_properties():
    run_node("""
const main = page();
const runtime = await worker();
const getter = () => main.original.confirm;
const setter = () => { throw new Error('the page setter must not be invoked'); };
const descriptor = {get: getter, set: setter, enumerable: false, configurable: true};
Object.defineProperty(main.window, 'confirm', descriptor);
const accessorLease = runtime.manage(main, 'enter', {token: 'accessor', policy: 'dismiss', deadline: 1015000});
assert.equal(main.window.confirm('scoped'), false);
accessorLease.release();
assert.deepEqual(Object.getOwnPropertyDescriptor(main.window, 'confirm'), descriptor);
const inherited = page();
delete inherited.window.confirm;
Object.setPrototypeOf(inherited.window, {confirm: inherited.original.confirm});
assert.equal(inherited.run("Object.hasOwn(window, 'confirm')"), false);
const inheritedLease = runtime.manage(inherited, 'enter', {token: 'inherited', policy: 'dismiss', deadline: 1015000});
assert.equal(inherited.window.confirm('scoped'), false);
inheritedLease.release();
assert.equal(Object.hasOwn(inherited.window, 'confirm'), false);
assert.equal(inherited.window.confirm, inherited.original.confirm);
""")


def test_failed_installation_rolls_back_before_running_user_code():
    run_node("""
const main = page();
const runtime = await worker();
Object.defineProperty(main.window, 'confirm', {writable: false, configurable: false});
const locked = Object.getOwnPropertyDescriptor(main.window, 'confirm');
const result = await main.run(runtime.build('window.ran = true', {token: 'locked', policy: 'dismiss'}));
assert.equal(result.ok, false);
assert.equal(main.window.ran, undefined);
assert.equal(main.window.alert, main.original.alert);
assert.equal(main.window.prompt, main.original.prompt);
assert.deepEqual(Object.getOwnPropertyDescriptor(main.window, 'confirm'), locked);
assert.equal(Object.hasOwn(main.window, '__btap_dialog_controller'), false);
""")


def test_controller_collision_preserves_page_property_and_prevents_execution():
    run_node("""
const main = page();
const runtime = await worker();
const siteValue = {site: true};
Object.defineProperty(main.window, '__btap_dialog_controller', {value: siteValue, configurable: true});
const result = await main.run(runtime.build('window.ran = true', {token: 'collision', policy: 'dismiss'}));
assert.equal(result.ok, false);
assert.match(result.error.message, /conflicts/);
assert.equal(main.window.ran, undefined);
assert.equal(main.window.__btap_dialog_controller, siteValue);
runtime.manage(main, 'release', {token: 'collision'});
assert.equal(main.window.__btap_dialog_controller, siteValue);
for (const name of ['alert', 'confirm', 'prompt']) assert.equal(main.window[name], main.original[name]);
""")


def test_duplicate_token_leases_and_stale_release_do_not_clear_new_scopes():
    run_node("""
const main = page();
const runtime = await worker();
const first = runtime.manage(main, 'enter', {token: 'same', policy: 'dismiss', deadline: 1015000});
const second = runtime.manage(main, 'enter', {token: 'same', policy: 'accept', deadline: 1015000});
first.release();
assert.equal(main.window.confirm('still active'), true);
runtime.manage(main, 'release', {token: 'unrelated'});
assert.notEqual(main.window.confirm, main.original.confirm);
runtime.manage(main, 'release', {token: 'same'});
main.assertRestored();
const fresh = runtime.manage(main, 'enter', {token: 'new', policy: 'dismiss', deadline: 1015000});
second.release();
assert.equal(main.window.confirm('new command'), false);
fresh.release();
main.assertRestored();
""")


def test_failed_frame_cleanup_leaves_only_a_bounded_scope():
    run_node("""
const main = page();
const child = page(main);
const runtime = await worker([main, child], {rejectCleanup: true});
assert.equal((await runtime.exec("window.confirm('done')", 'dismiss')).type, 'result');
main.assertRestored();
assert.notEqual(child.window.confirm, child.original.confirm);
child.time.advance(13000);
child.assertRestored();
assert.equal(runtime.injections.filter(request => request.args[0] === 'release').length, 1);
""")


def test_complete_worker_does_not_replay_a_user_error_and_cleans_subframes():
    run_node("""
const main = page();
const child = page(main);
const runtime = await worker([main, child]);
const result = await runtime.exec("window.runs = (window.runs || 0) + 1; throw new Error('Refused to evaluate unsafe-eval')");
assert.equal(result.type, 'error');
assert.equal(main.window.runs, 1);
assert.equal(runtime.cdpCalls.length, 0);
main.assertRestored();
child.assertRestored();
""")


def test_hung_frame_cleanup_cannot_hold_an_already_known_execution_outcome():
    run_node("""
const main = page();
const child = page(main);
const runtime = await worker([main, child], {hangCleanup: true});
const execution = runtime.exec("window.confirm('done')", 'dismiss');
await new Promise(resolve => setImmediate(resolve));
assert.equal(runtime.expireTimers(1000), 1);
assert.equal((await execution).type, 'result');
main.assertRestored();
child.time.advance(13000);
child.assertRestored();
""")
