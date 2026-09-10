"""What `execute_js` does with each shape of caller code that returns a value.

The wrapper `buildExecScript` builds picks between three strategies -- an
AsyncFunction when the last line starts with `return`, a plain `eval` otherwise,
and an AsyncFunction retry when that eval throws a SyntaxError naming `return` or
`await`. Which branch a script lands in is invisible to the caller, so a branch
that drops the value fails silently: the tool answers `null` and reports success.

That is not hypothetical. A 2026-08-28 session lost hours to multi-statement and
IIFE scripts returning `null` through this layer, and the shapes were only
confirmed working again by hand on 2026-09-08 -- with nothing in the suite to
keep them working. These tests are that gate. Each case names the shape and
asserts the value, so a refactor of the branch logic cannot quietly swallow a
return value again.
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
    / "src/browsertap_mcp/chrome_extension/background.js"
)


def _run_exec(code: str) -> dict:
    """Build the real exec script for `code` and run it, returning its envelope.

    The script runs in node's own realm rather than a `vm` context: the wrapper
    reaches for AsyncFunction, Promise and an indirect `eval`, and a hand-built
    context that forgets one of those fails in a way that looks like the wrapper
    is broken. Only `window`/`document` are supplied, which is all the preamble
    touches when no dialog scope is active.
    """
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function buildExecScript")
    end = source.index("\nfunction buildPageScript", start)
    builder = source[start:end]
    script = (
        "globalThis.window = globalThis;\n"
        "globalThis.document = { createElement() { return { style: {}, remove() {},"
        " textContent: '' }; }, body: { appendChild() {} },"
        " documentElement: { appendChild() {} } };\n"
        + builder
        + "\nconst expression = buildExecScript("
        + json.dumps(code)
        + ", \"return { ok: false, error: { name: e.name, message: e.message } };\", null);\n"
        "(async () => {\n"
        "  const envelope = await eval(expression);\n"
        "  process.stdout.write(JSON.stringify(envelope));\n"
        "})().catch(error => { console.error(error && error.stack); process.exit(1); });\n"
    )
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
        completed = subprocess.run(
            ["node", path], capture_output=True, text=True, timeout=20, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def test_the_builder_this_file_tests_is_the_one_that_ships():
    """Both slice anchors must still exist, or every case below is vacuous."""
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function buildExecScript")
    end = source.index("\nfunction buildPageScript", start)
    builder = source[start:end]
    # The three dispatch branches the cases below are here to pin.
    assert "AsyncFunction" in builder
    assert "lastLine.startsWith('return')" in builder
    assert "smartProcessResult" in builder


# Each entry is (label, caller code, expected value). The labels are the shapes a
# caller actually writes; the reason they are grouped in one table is that the
# branch chosen inside the wrapper differs between them while the contract --
# "the value I returned is the value I get" -- does not.
RETURN_SHAPES = [
    ("async_iife_one_line", "(async()=>{const a=1+1; await Promise.resolve(); return a;})()", 2),
    (
        "async_iife_multi_line",
        "(async () => {\n  const a = 1 + 1;\n  await Promise.resolve();\n  return a;\n})()",
        2,
    ),
    ("sync_iife", "(() => { const a = 40; return a + 2; })()", 42),
    ("bare_expression", "41 + 1", 42),
    ("multi_statement_explicit_return", "const a = 20;\nconst b = 22;\nreturn a + b;", 42),
    ("single_line_statements_then_return", "const a = 20; const b = 22; return a + b;", 42),
    ("top_level_await_then_return", "const a = await Promise.resolve(21);\nreturn a * 2;", 42),
    ("trailing_expression_after_statements", "const a = 20;\nconst b = 22;\na + b", 42),
    ("promise_returning_expression", "Promise.resolve(42)", 42),
    ("object_literal_result", "(async () => ({ n: 42 }))()", {"n": 42}),
    ("array_result", "(async () => [1, 2, 3])()", [1, 2, 3]),
]


@pytest.mark.parametrize(
    "code,expected",
    [pytest.param(code, expected, id=label) for label, code, expected in RETURN_SHAPES],
)
def test_return_value_survives_every_supported_code_shape(code, expected):
    envelope = _run_exec(code)
    assert envelope["ok"] is True, envelope
    assert envelope["data"] == expected


def test_a_script_returning_nothing_is_not_reported_as_a_failure():
    """`undefined` is a legitimate answer and must not be confused with an error."""
    envelope = _run_exec("const a = 1 + 1;")
    assert envelope["ok"] is True, envelope
    assert envelope.get("data") is None


def test_a_thrown_error_still_reaches_the_error_handler():
    """The reverse direction: the branch logic must not swallow a real throw.

    Without this, a wrapper change that routed everything through a branch which
    returns `undefined` on failure would make every test above pass while errors
    silently became successful `null` results.
    """
    envelope = _run_exec("(async () => { throw new TypeError('boom'); })()")
    assert envelope["ok"] is False, envelope
    assert envelope["error"]["name"] == "TypeError"
    assert "boom" in envelope["error"]["message"]


def test_a_syntax_error_in_caller_code_is_reported_not_returned_as_a_value():
    envelope = _run_exec("const = ;")
    assert envelope["ok"] is False, envelope
    assert "SyntaxError" in envelope["error"]["name"]
