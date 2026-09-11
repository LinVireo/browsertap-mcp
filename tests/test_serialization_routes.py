"""Run the three shipped execution routes against the same page values."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from browsertap_mcp import server as S

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "src/browsertap_mcp/chrome_extension/background.js"
SERIALIZER = BACKGROUND.with_name("result_serialization.js")
EVAL_GUARD = BACKGROUND.with_name("guarded_eval.js")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


def _routes(code: str) -> dict:
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(__BACKGROUND__, 'utf8');
const serializerPath = __SERIALIZER__;
if (fs.existsSync(serializerPath)) {
  globalThis.smartProcessResult = eval(fs.readFileSync(serializerPath, 'utf8'));
}
globalThis.prepareGuardedEval = eval(fs.readFileSync(__EVAL_GUARD__, 'utf8'));
function section(start, end) {
  const offset = source.indexOf(start);
  if (offset < 0) throw new Error('missing start: ' + start);
  const limit = source.indexOf(end, offset);
  if (limit < 0) throw new Error('missing end: ' + end);
  return source.slice(offset, limit);
}
eval(section('function buildExecScript', '\nfunction buildPageScript'));
eval(section('function manualExecutionResult', '\n// --- Scoped, temporary CSP removal'));
const pendingManualExecutions = new Map();
const manualExecutionGenerations = new Map();
const dialogEventSequences = new Map();
let nextManualExecutionGeneration = 1;
function realm() {
  const context = vm.createContext({});
  vm.runInContext('globalThis.window = globalThis', context);
  return context;
}
const manualRealm = realm();
const mainRealm = realm();
const fallbackRealm = realm();
const objects = new Map();
const commands = [];
let attached = false;
let evaluations = 0;
async function attachBtapDebugger(target) { attached = true; return {target}; }
async function detachBtapDebugger() { commands.push({method:'detach'}); attached = false; }
async function waitForDefaultRuntimeExecutionContext() { return 101; }
function boundedCdpTimeout(value, fallback) { return Number(value) > 0 ? Number(value) : fallback; }
function remoteObject(value) {
  if (value === undefined) return {type:'undefined'};
  if (value === null) return {type:'object', subtype:'null', value:null};
  const type = typeof value;
  if (type === 'object' || type === 'function' || type === 'symbol') {
    const objectId = 'remote-' + objects.size;
    objects.set(objectId, value);
    return {type, objectId};
  }
  if (type === 'bigint') return {type, unserializableValue:String(value) + 'n'};
  if (type === 'number' && !Number.isFinite(value)) return {type, unserializableValue:String(value)};
  return {type, value};
}
async function sendDebuggerCommandWithTimeout(_lease, method, params, timeout) {
  if (!attached) throw new Error('debugger released before result conversion');
  commands.push({method, params, timeout});
  if (method === 'Page.enable' || method === 'Runtime.enable') return {};
  if (method === 'Page.getFrameTree') return {frameTree:{frame:{id:'main-frame'}}};
  if (method === 'Runtime.evaluate') {
    if (params.expression === 'void 0') return {result:{type:'undefined'}};
    evaluations += 1;
    const value = await vm.runInContext(params.expression, manualRealm, {timeout:2000});
    if (params.returnByValue) {
      return {result:{value:value === undefined ? undefined : JSON.parse(JSON.stringify(value))}};
    }
    return {result:remoteObject(value)};
  }
  if (method === 'Runtime.callFunctionOn') {
    if (!objects.has(params.objectId)) throw new Error('missing remote object');
    const converter = vm.runInContext('(' + params.functionDeclaration + ')', manualRealm);
    const value = converter.call(objects.get(params.objectId));
    return {result:{value:JSON.parse(JSON.stringify(value))}};
  }
  if (method === 'Runtime.releaseObjectGroup') { objects.clear(); return {}; }
  throw new Error('unexpected method: ' + method);
}
(async () => {
  const expression = buildExecScript(__CODE__,
    'return {ok:false,error:{name:e.name,message:e.message}};', null);
  const main = await vm.runInContext(expression, mainRealm, {timeout:2000});
  const fallback = await vm.runInContext(__FALLBACK__, fallbackRealm, {timeout:2000});
  const manual = await executeManualScript(42, __CODE__,
    {token:'synthetic-serialization', policy:'manual', timeoutMs:2000});
  process.stdout.write(JSON.stringify({main,fallback,manual,commands,evaluations,
    attached,pending:pendingManualExecutions.size,
    effects:{main:mainRealm.hits || 0,fallback:fallbackRealm.hits || 0,manual:manualRealm.hits || 0},
    markers:[mainRealm,fallbackRealm,manualRealm].map(context =>
      Object.getOwnPropertyNames(context).filter(key => key.startsWith('__btap_eval_')))}));
})().catch(error => { console.error(error.stack); process.exit(1); });
"""
    for key, value in {
        "__BACKGROUND__": str(BACKGROUND),
        "__SERIALIZER__": str(SERIALIZER),
        "__EVAL_GUARD__": str(EVAL_GUARD),
        "__CODE__": code,
        "__FALLBACK__": S._build_cdp_fallback_expression(code, "dismiss", 2.0),
    }.items():
        script = script.replace(key, json.dumps(value))
    result = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True,
        timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("new Set([1, 2])", [1, 2]),
        ("new Map([['key', 2n]])", [["key", "2"]]),
        ("({outerHTML:'<b>value</b>', nodeType:1})", "<b>value</b>"),
        ("(() => {const a={n:1}; a.self=a; return a;})()", {"n": 1, "self": "[Circular]"}),
        ("({value:3n})", {"value": "3"}),
        ("new TypeError('synthetic')", "[TypeError: synthetic]"),
        ("({cb:function named(){}})", {"cb": "[Function: named]"}),
        ("undefined", None),
        ("NaN", None),
        ("Infinity", None),
        ("3n", "3"),
        ("new Date(0)", "1970-01-01T00:00:00.000Z"),
        ("({get bad(){throw new Error('synthetic')}, good:1})",
         {"bad": "[unreadable: synthetic]", "good": 1}),
        ("({toJSON(){return {big:5n}}})", {"big": "5"}),
        ("(() => {const a={toJSON(){return a}}; return a;})()", "[Circular]"),
    ],
)
def test_main_fallback_and_raw_manual_have_one_value_contract(code, expected):
    result = _routes(code)
    for route in ("main", "fallback", "manual"):
        assert result[route].get("ok") is True, (route, result[route])
        assert "data" in result[route], (route, result[route])
        assert result[route]["data"] == expected, (route, result[route])
    assert result["evaluations"] == 1
    assert result["attached"] is False
    assert result["pending"] == 0


def test_manual_converts_and_releases_objects_before_detaching_without_rewriting_code():
    code = "new Set([2n])"
    result = _routes(code)
    commands = result["commands"]
    evaluation = [c for c in commands if c["method"] == "Runtime.evaluate"][-1]
    assert evaluation["params"]["expression"] == code
    assert evaluation["params"]["returnByValue"] is False
    assert evaluation["params"]["replMode"] is True
    methods = [c["method"] for c in commands]
    assert methods.index("Runtime.callFunctionOn") < methods.index("Runtime.releaseObjectGroup")
    assert methods.index("Runtime.releaseObjectGroup") < methods.index("detach")
    conversion = next(c for c in commands if c["method"] == "Runtime.callFunctionOn")
    assert 0 < conversion["timeout"] <= 2000
    assert conversion["params"]["returnByValue"] is True


def test_iterable_cap_bounds_enumeration():
    code = """(() => {
      function* values() {
        for (let i=0; ; i++) {
          if (i > 200) throw new Error('enumerated past bounded lookahead');
          yield i;
        }
      }
      return values();
    })()"""
    result = _routes(code)
    expected = list(range(200)) + ["[more items; limit 200]"]
    for route in ("main", "fallback", "manual"):
        assert result[route] == {"ok": True, "data": expected}


@pytest.mark.parametrize("keyword", ["return", "await"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("strict", [False, True])
def test_runtime_syntax_error_does_not_replay_side_effects(keyword, asynchronous, strict):
    code = (
        ('"use strict";\n' if strict else '')
        + ("(async () => {" if asynchronous else "(() => {")
        + "globalThis.hits = (globalThis.hits || 0) + 1;"
        + f"throw new SyntaxError('{keyword} count=' + globalThis.hits);"
        + "})()"
    )
    result = _routes(code)
    assert result["effects"] == {"main": 1, "fallback": 1, "manual": 1}
    for route in ("main", "fallback", "manual"):
        assert result[route]["ok"] is False
        assert result[route]["error"]["message"] == f"{keyword} count=1"
    assert result["markers"] == [[], [], []]


def test_nested_eval_parse_error_is_a_runtime_failure_of_the_outer_script():
    result = _routes("globalThis.hits = (globalThis.hits || 0) + 1; eval('return 7')")
    assert result["effects"] == {"main": 1, "fallback": 1, "manual": 1}
    for route in ("main", "fallback", "manual"):
        assert result[route]["ok"] is False
        assert result[route]["error"]["name"] == "SyntaxError"
    assert result["markers"] == [[], [], []]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("", None),
        ("// only a comment", None),
        ("/* only a comment */", None),
        ("var local = 7;", None),
        ("let local = 7;", None),
        ("const local = 7;", None),
        ('"use strict"', "use strict"),
        ('"use strict"; // only a directive', "use strict"),
        ('"use strict"; var local = 7;', "use strict"),
        ('"use strict"; let local = 7;', "use strict"),
        ('"other"; "use strict"; const local = 7;', "use strict"),
        ('"use strict"; (function(){return this === undefined})()', True),
        ('/* comment */ "use strict" // comment\n; (function(){return this === undefined})()', True),
        ('"use\\x20strict"; (function(){return this === undefined})()', False),
        ('"use strict"\n[0]; (function(){return this === undefined})()', False),
        ('"use strict"\n+ ""; (function(){return this === undefined})()', False),
        ('eval("var local = 7"); typeof local', "number"),
        ('"use strict"; eval("var local = 7"); typeof local', "undefined"),
        ('let local = 41; eval("local + 1")', 42),
        ('"use strict"; (0, eval)("var remote = 42"); globalThis.remote', 42),
        ('let globalThis = 17; globalThis', 17),
        ('let eval = 17; eval', 17),
        ('let await = value => value; await(42)', 42),
        ('var local = 42; (() => local)()', 42),
        ('let returnValue = 42;\nreturnValue', 42),
        ('let return$Value = 42;\nreturn$Value', 42),
        ('let returnValue = 42;\nreturn\\u0056alue', 42),
        ('#!synthetic\n42', 42),
        ('#!synthetic\u202842', 42),
        ('#!synthetic\u202942', 42),
        ('Object.getOwnPropertyNames(this).filter(key => key.startsWith("__btap_eval_"))', []),
    ],
)
def test_guard_preserves_directives_completion_values_and_eval_scopes(code, expected):
    result = _routes(code)
    for route in ("main", "fallback", "manual"):
        assert result[route] == {"ok": True, "data": expected}, (route, result[route])
    assert result["markers"] == [[], [], []]


def test_indirect_eval_preserves_manual_global_and_wrapper_local_scopes():
    result = _routes('let local = 41; (0, eval)("typeof local")')
    assert result["main"] == {"ok": True, "data": "undefined"}
    assert result["fallback"] == {"ok": True, "data": "undefined"}
    assert result["manual"] == {"ok": True, "data": "number"}
    assert result["markers"] == [[], [], []]


def test_await_call_ambiguity_does_not_replay_an_already_started_script():
    result = _routes('globalThis.hits = (globalThis.hits || 0) + 1; await(42)')
    assert result["effects"] == {"main": 1, "fallback": 1, "manual": 1}
    for route in ("main", "fallback", "manual"):
        assert result[route]["ok"] is False
        assert result[route]["error"]["name"] == "ReferenceError"
        assert result[route]["error"]["message"] == "await is not defined"
    assert result["markers"] == [[], [], []]


@pytest.mark.parametrize(
    "code",
    [
        '"use strict"; globalThis.hits = 1; with ({}) {}',
        '"use strict"; globalThis.hits = 1; var eval = 7;',
        'globalThis.hits = 1; new.target',
        'await (globalThis.hits = 1); )',
    ],
)
def test_compile_only_probe_does_not_relax_script_early_errors(code):
    result = _routes(code)
    assert result["effects"] == {"main": 0, "fallback": 0, "manual": 0}
    for route in ("main", "fallback", "manual"):
        assert result[route]["ok"] is False
        assert result[route]["error"]["name"] == "SyntaxError"
    assert result["markers"] == [[], [], []]


@pytest.mark.parametrize(
    "code",
    [
        'globalThis.hits = (globalThis.hits || 0) + 1; return 42;',
        '"use strict";\nglobalThis.hits = (globalThis.hits || 0) + 1;\nreturn 42;',
        'globalThis.hits = (globalThis.hits || 0) + 1;\nawait Promise.resolve(42)',
        '"use strict";\nglobalThis.hits = (globalThis.hits || 0) + 1;\nawait Promise.resolve(42)',
        'const value = await Promise.resolve(21);\nvalue * 2',
    ],
)
def test_top_level_return_and_await_keep_their_value_without_replaying(code):
    result = _routes(code)
    for route in ("main", "fallback"):
        assert result[route] == {"ok": True, "data": 42}, (route, result[route])
    expected_effects = 0 if code.startswith("const value") else 1
    assert result["effects"]["main"] == expected_effects
    assert result["effects"]["fallback"] == expected_effects
    # This harness's manual VM parses Script grammar, unlike Chrome's native
    # REPL mode. It proves the original text is sent once, not top-level await.
    assert result["evaluations"] == 1
    assert result["markers"] == [[], [], []]


@pytest.mark.parametrize(
    ("code", "expected", "effects"),
    [
        pytest.param(
            'await Promise.resolve(globalThis.hits = 1); '
            'await Promise.resolve(globalThis.hits += 1);',
            None, 2, id="two_await_statements_on_one_line",
        ),
        pytest.param(
            '"use strict"; await Promise.resolve(globalThis.hits = 1);',
            None, 1, id="directive_and_await_on_one_line",
        ),
        pytest.param(
            'await Promise.resolve(globalThis.hits = 1); '
            'const last = await Promise.resolve(globalThis.hits += 1);',
            None, 2, id="await_and_declaration_on_one_line",
        ),
        pytest.param(
            'const value = await Promise.resolve(21)\nvalue * 2',
            42, 0, id="final_expression_after_asi",
        ),
        pytest.param(
            'await Promise.resolve(\n21\n).then(value => value * 2)',
            42, 0, id="complete_multiline_await_expression",
        ),
        pytest.param(
            'await Promise.resolve(42); // trailing comment',
            42, 0, id="complete_expression_with_trailing_comment",
        ),
        pytest.param(
            'const value = await Promise.resolve(21);\nvalue * 2; /* trailing comment */',
            42, 0, id="final_expression_with_trailing_comment",
        ),
        pytest.param(
            'globalThis.hits = 0;\n'
            '(await Promise.resolve(value => {globalThis.hits++; return value}))\n(42)',
            None, 1, id="continued_call_has_no_statement_boundary",
        ),
        pytest.param(
            'globalThis.hits = 0;\n'
            '(await Promise.resolve(value => {globalThis.hits++; return value})) // ;\n(42)',
            None, 1, id="line_comment_semicolon_is_not_a_statement_boundary",
        ),
        pytest.param(
            'globalThis.hits = 0;\n'
            '(await Promise.resolve(value => {globalThis.hits++; return value}))\n'
            '/* continuation */(42)',
            None, 1, id="comment_does_not_split_a_continued_call",
        ),
        pytest.param(
            'let\nvalue = await Promise.resolve(globalThis.hits = 1)',
            None, 1, id="let_continuation_keeps_declaration_semantics",
        ),
        pytest.param(
            'await Promise.resolve(globalThis.hits = 1);\n/* declaration */ class Final {}',
            None, 1, id="comment_before_class_declaration",
        ),
        pytest.param(
            'await Promise.resolve(globalThis.hits = 1);\n/* declaration */ function final() {}',
            None, 1, id="comment_before_function_declaration",
        ),
        pytest.param(
            'await Promise.resolve(globalThis.hits = 1);\nasync/* declaration */function final() {}',
            None, 1, id="comment_inside_async_function_declaration",
        ),
        pytest.param(
            '/* declaration */ class Final extends '
            '(await Promise.resolve((globalThis.hits = 1, Object))) {}',
            None, 1, id="whole_body_class_declaration",
        ),
        pytest.param(
            '/* block */ { value: await Promise.resolve(globalThis.hits = 1) }',
            None, 1, id="whole_body_block_keeps_statement_semantics",
        ),
        pytest.param(
            'await Promise.resolve(globalThis.hits = 1);\n{ value: 42 }',
            None, 1, id="final_block_keeps_statement_semantics",
        ),
        pytest.param(
            'await (globalThis.hits = 1); await (globalThis.hits += 1); return 42;',
            42, 2, id="explicit_return_after_multiple_statements",
        ),
        pytest.param(
            '"use strict"; await (globalThis.hits = 1); '
            'return (function(){ return this === undefined })()',
            True, 1, id="explicit_return_keeps_strict_mode",
        ),
    ],
)
def test_async_body_preserves_statement_order_and_unambiguous_values(code, expected, effects):
    result = _routes(code)
    for route in ("main", "fallback"):
        assert result[route] == {"ok": True, "data": expected}, (route, result[route])
        assert result["effects"][route] == effects
    assert result["evaluations"] == 1
    assert result["markers"] == [[], [], []]
