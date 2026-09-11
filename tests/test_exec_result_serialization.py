"""Behaviour tests for the shared converter all `execute_js` routes inject.

`smartProcessResult` lives in a standalone linted JavaScript resource. The
worker embeds its function source and the Python fallback reads the same file.

The harness runs the real function text under node against fakes rather than a
real DOM. That is what lets the assertions here be about the *classification*:
a fake can be an object whose `Object.prototype.toString` tag is `Window`, or a
collection that is iterable but not array-like, or a getter that throws -- all
states a real page produces and none a browser can be asked to produce on
demand.

Every test below names the failure the old shape had, because each of them was
silent: the value simply arrived as `{}` or as `[Object]` and nothing anywhere
reported a loss.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)

BACKGROUND = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "browsertap_mcp"
    / "chrome_extension"
    / "background.js"
)


def _converter_source() -> str:
    return BACKGROUND.with_name("result_serialization.js").read_text(encoding="utf-8")


def _run(expression: str, setup: str = "") -> object:
    """Evaluate `smartProcessResult(<expression>)` under node and return its JSON."""
    script = (
        _converter_source()
        + "\n"
        + setup
        + "\nconst __out = smartProcessResult("
        + expression
        + ");\n"
        + "process.stdout.write(JSON.stringify({value: __out}));\n"
    )
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
        completed = subprocess.run(["node", path], capture_output=True, text=True, timeout=10)
        if completed.returncode:
            raise AssertionError(f"node harness failed: {completed.stderr.strip()}")
        return json.loads(completed.stdout)["value"]
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def test_the_fragment_this_file_tests_is_the_one_that_ships():
    body = _converter_source()
    assert "function smartProcessResult(" in body
    assert "Object.prototype.toString.call(result)" in body
    background = BACKGROUND.read_text(encoding="utf-8")
    assert "importScripts('result_serialization.js')" in background
    assert "globalThis.smartProcessResult.toString()" in background
    assert body.rstrip().endswith("smartProcessResult;")


def test_primitives_and_null_pass_through_untouched():
    assert _run("42") == 42
    assert _run("'hi'") == "hi"
    assert _run("true") is True
    assert _run("null") is None


def test_a_bigint_is_stringified_because_json_refuses_it():
    """`JSON.stringify(1n)` throws, and the old shape lost the whole result to it."""
    assert _run("10n ** 25n") == "10000000000000000000000000"


def test_an_element_becomes_its_outer_html():
    assert _run("{outerHTML: '<b>x</b>', nodeType: 1}") == "<b>x</b>"


def test_a_node_without_outer_html_describes_itself_instead_of_becoming_an_empty_object():
    """A text node, a comment or a document used to arrive as `{}`.

    `nodeType === 1` was the only node the old converter recognised, and
    everything else fell through to `JSON.stringify`, where a DOM node has no
    enumerable properties -- so the model was handed `{}` with no indication
    that a node had been there at all.
    """
    setup = "const t = {nodeType: 3, nodeValue: 'hello'};"
    assert _run("t", setup) == "[Object: hello]"


def test_a_window_is_named_by_its_href_and_a_cross_origin_one_says_so():
    """The class tag is what survives a realm boundary; `instanceof` does not."""
    setup = """
    class Window { get location() { return {href: 'https://example.test/a'}; } }
    Object.defineProperty(Window.prototype, Symbol.toStringTag, {value: 'Window'});
    const w = new Window();
    class Foreign { get location() { throw new Error('cross-origin'); } }
    Object.defineProperty(Foreign.prototype, Symbol.toStringTag, {value: 'Window'});
    const f = new Foreign();
    """
    assert _run("w", setup) == "[Window: https://example.test/a]"
    assert _run("f", setup) == "[Window: cross-origin]"


def test_an_error_keeps_its_name_and_message():
    """`Object.keys(new TypeError('x'))` is empty, so this used to be `{}`."""
    assert _run("new TypeError('bad input')") == "[TypeError: bad input]"


def test_any_iterable_collection_converts_not_just_the_three_that_were_named():
    """This is the defect the whole rewrite turns on.

    The old converter had one branch for jQuery, one for `NodeList` and
    `HTMLCollection`, and one for array-likes whose first slot held an element.
    A `Set` matched none of them, so a set of elements reached
    `JSON.stringify` and arrived as `{}`.
    """
    setup = "const s = new Set([{outerHTML: '<i>1</i>'}, {outerHTML: '<i>2</i>'}]);"
    assert _run("s", setup) == ["<i>1</i>", "<i>2</i>"]

    setup = "const m = new Map([['k', {outerHTML: '<i>v</i>'}]]);"
    assert _run("m", setup) == [["k", "<i>v</i>"]]

    setup = "function* g() { yield {outerHTML: '<i>g</i>'}; }"
    assert _run("g()", setup) == ["<i>g</i>"]


def test_a_collection_whose_first_slot_is_empty_still_converts():
    """The old array-like branch required `result[0]` to be an element.

    A collection that starts with a hole -- or with anything that is not an
    element -- fell through to `JSON.stringify` and lost every element after it.
    """
    setup = """
    const c = {length: 2, 0: undefined, 1: {outerHTML: '<b>second</b>'}};
    Object.defineProperty(c, Symbol.toStringTag, {value: 'HTMLCollection'});
    """
    assert _run("c", setup) == [None, "<b>second</b>"]


def test_the_item_cap_applies_to_every_collection_not_just_array_likes():
    """The cap used to sit on the branch least likely to be large.

    Only the array-like path stopped at 100; the `NodeList` and jQuery paths
    were unbounded, so `querySelectorAll('div')` on a large page serialised
    every element's `outerHTML` into one payload.
    """
    setup = "const many = Array.from({length: 250}, (_, i) => ({outerHTML: '<b>' + i + '</b>'}));"
    out = _run("many", setup)
    # 200 converted items plus one line saying what was dropped -- a silent
    # truncation would read as "that is all there was".
    assert len(out) == 201
    assert out[0] == "<b>0</b>"
    assert out[199] == "<b>199</b>"
    assert out[200] == "[50 more of 250]"


def test_a_plain_object_carrying_a_length_key_keeps_its_keys():
    """`{length: 2}` is not a collection, and turning it into one loses the data.

    This is why the array-like fallback is gated on the class tag: `Array.from`
    would answer `[undefined, undefined]` for it.
    """
    assert _run("{length: 2, name: 'x'}") == {"length": 2, "name": "x"}


def test_a_cycle_is_marked_and_the_rest_of_the_result_survives():
    """`JSON.stringify` throws on a cycle, and the old shape returned a string.

    One bad edge discarded the entire result -- every sibling key included --
    and replaced it with a message the caller could not act on.
    """
    setup = "const a = {name: 'root', child: {}}; a.child.parent = a;"
    assert _run("a", setup) == {"name": "root", "child": {"parent": "[Circular]"}}


def test_a_throwing_getter_loses_its_own_value_and_nothing_else():
    setup = """
    const o = {good: 1};
    Object.defineProperty(o, 'bad', {enumerable: true, get() { throw new Error('nope'); }});
    """
    out = _run("o", setup)
    assert out["good"] == 1
    assert out["bad"] == "[unreadable: nope]"


def test_a_date_still_arrives_as_a_string_without_date_being_named():
    """`toJSON` is the engine's own hook, so Date needs no branch of its own."""
    assert _run("new Date(0)") == "1970-01-01T00:00:00.000Z"


def test_depth_is_bounded_so_a_deep_structure_cannot_blow_the_payload():
    setup = """
    let deep = {leaf: true};
    for (let i = 0; i < 12; i++) deep = {next: deep};
    """
    out = _run("deep", setup)
    for _ in range(6):
        assert isinstance(out, dict), out
        out = out["next"]
    assert out == "[Object: depth limit]"


def test_a_function_is_named_rather_than_dropped():
    """`JSON.stringify` omits functions entirely, so a caller saw no key at all."""
    assert _run("{cb: function namedCallback() {}}") == {
        "cb": "[Function: namedCallback]"
    }


def test_a_truncated_generator_is_closed_after_one_bounded_lookahead():
    setup = """
    let closed = false;
    let steps = 0;
    function* g() {
      try { while (true) { steps++; yield steps; } }
      finally { closed = true; }
    }
    """
    assert _run("(() => { smartProcessResult(g()); return {closed, steps}; })()", setup) == {
        "closed": True, "steps": 201,
    }


def test_an_own_proto_key_stays_data():
    assert _run('JSON.parse(\'{"__proto__":{"value":1},"x":2}\')') == {
        "__proto__": {"value": 1}, "x": 2,
    }
