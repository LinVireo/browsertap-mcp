# Additional confirmed result-encoding defect

Current status: r1 is held for a subsequently reproduced real NTFS path/wire
failure. Frozen r2 is now under renewed parent and independent verification;
see `unicode-r2-status-from-01a09080-20260912.md`. Do not use the old r1 PASS as
the final integration receipt.

From `codex-coverage-01a09080`, 2026-09-12, following the final configuration handoff.

The configuration r2 delta is now present in the shared tree: all ten
nonoverlapping files match the manifest exactly. A read-only check of the two
merged Python files is under way; this is separate from your integration tests.

While checking the historical large-result note, I reproduced a new Python
boundary failure in the current shared `server.py`. JavaScript permits unpaired
UTF-16 surrogates (for example `String.fromCharCode(0xD800)`); JSON transports
them with an escape. `_externalize_execute_js_result()` invokes
`_serialize_execute_js_value()` even for small values. `json.dumps` with
`ensure_ascii=False`, followed by strict UTF-8 encoding, raises
`UnicodeEncodeError`. The execution itself may already have succeeded.

The reproduction imported the current shared module and called the real
externalizer with synthetic successful values. Ordinary Chinese/emoji passed;
a lone high surrogate, a nested lone low surrogate, and a 25,001-character value
ending in a lone surrogate all failed at UTF-8 encoding. No browser, bridge,
actual user value, or file cleanup was involved. The new JavaScript serializer
preserves these string values, so its common-result contract does not close
this Python boundary.

I am preparing a bounded follow-up under a new isolated worktree/task, leaving
shared source and both sealed configuration worktrees unchanged. The investigation
must include the actual MCP/Pydantic serialization boundary: merely changing
`.encode()` to a lossy error handler, or fixing file output while inline
structured output still raises, is insufficient. The correction must preserve
the value without replacing characters or implying the script can be replayed.
I will return a small independently reviewed patch for your sequential merge.

The original temporary-file retention note is a separate product-policy item.
Files are currently returned as caller-readable result artifacts; this slice
will not invent a retention policy or delete existing result files. Please keep
that distinction in the final ledger/knowledge reconciliation rather than
silently closing the historical note as fixed.

## Actual MCP confirmation

The follow-up task is `.trellis/tasks/09-12-result-unicode`, activated only for
this thread. The existing implementation worker is preparing the isolated patch;
the existing independent checker will review it afterward.

`out/coverage-handoff-20260912/result-unicode/initial-mcp-boundary.json` records
two additional failures through a newly registered synthetic `@mcp.tool` in a
fresh process. A successful nested value fails inside FastMCP conversion with
`ToolError`; a failed result containing an unpaired surrogate in its error text
produces `PydanticSerializationError` when the returned MCP model is serialized.
The main source SHA stayed
`370cac32364181aca7ee35861b344e19f593a98398a5025a428dd7712d59eeff`
through these probes. No live driver or browser was used.

Therefore the bounded Python repair also needs the general result adapter's
success/error path, not only JS file encoding. The scope remains serialization
and a new regression file; current C10 builder/route and native-dialog work stay
with their owners. A fallback must preserve the complete value and existing
isError/retry classification without changing the SDK or silently replacing
characters. Caller-visible exceptional representation guidance will accompany
the final handoff.

## Completed-result file-write failure

The parent independently reproduced a second defect at this same boundary:
even an ordinary 25,000-character ASCII result becomes a retryable transport
failure if its temporary-file write raises OSError. The actual low-level
get_execute_js_result handler received a successful synthetic receipt with
retry_safe=false, but returned ok=false, isError=true, transport_error and
retryable=true, dropping the operation id and recoverable value. The query ran
once; no browser or bridge was involved.

Evidence: `out/coverage-handoff-20260912/result-unicode/ordinary-large-write-failure.json`.
The isolated candidate SHA stayed
`1aa63f12fb9ac8e77e397f364227c3a64c4b9d82ecdb8d79cb3802064e745f97`
through this probe. This is a new reproduced failure, not a passing result or
a final candidate identity.

The implementation worker is correcting both result-export failures in the
same bounded two-file slice; the checker will independently verify the complete
MCP boundary afterward. Plain large values, Unicode values and late receipts
must preserve their original verdict and complete value on disk failure. The
reviewed handoff will specify any inline JSON fallback and its explicit scope.
All shared source remains coordinator-owned; own task/research records will be
normalized to LF and committed explicitly before the final whole-project seal.

## Frozen candidate; parent verification complete, independent review running

The bounded candidate is frozen in `D:/coding/btap-result-unicode-20260912`.
Its seed is `390afba80d63bff1e3848a53e2d38faa4b541f26`; the delta tree is
`3e01160c01726ba1f32a8d1e6aa77edb7389347b`. The two candidate SHA256 values are:

- `src/browsertap_mcp/server.py`:
  `4104b1a75d89ca40cf0decd7b34f5f7591d33fd1a890cd2de188de26b089cad5`.
- `tests/test_result_unicode.py`:
  `58cc20624d1b181d96dc4d069893310e59412ab4fa21055fb6bdc7be4377e23f`.

This thread's parent independently ran the 15-file affected union:
**732 passed, 0 failures/errors/skips**. Ruff, mypy src, seed-relative diff and
raw LF checks passed. All 386 seed paths plus the new test stayed SHA/mtime
stable throughout. Evidence:
`out/coverage-handoff-20260912/result-unicode/parent-final-r1/verification.json`.
This is a scoped result and overlaps the implementation worker's suite.

The existing checker is now performing independent semantic and real SDK wire
review. This is not yet the final integration approval; shared source remains
coordinator-owned. The candidate's `out/result-unicode/HANDOFF.md` already gives
the proposed public guidance for js-value/envelope/mcp-call-result scopes,
ASCII inline JSON fallback on file failure, and explicitly JSON-encoded unsafe
error fields. Final reviewed patch and task-record commit will follow.

## HOLD: actual result-file path can reintroduce an unpaired surrogate

Do not integrate the r1 candidate as final. After the earlier scoped checks,
the parent created an exclusively owned Windows temporary subdirectory whose
name contains an unpaired high surrogate. The actual result writer successfully
wrote both a small surrogate value and an ordinary 25,000-character ASCII value
there. In both cases the returned result_file path reintroduced that surrogate,
and the real get_execute_js_result MCP response failed at
JSONRPCMessage.model_dump_json with PydanticSerializationError. Execution had
already completed. The parent cleaned up only its own temporary probe files;
candidate source SHA remained unchanged.

Full-wire evidence:
`out/coverage-handoff-20260912/result-unicode/result-path-unicode-full-wire-probe.json`.
The similarly named result-path-unicode-probe.json only checked model_dump in
JSON mode, which is insufficient to prove successful wire serialization.

The existing implementation worker is preparing r2 in the same isolated two
files. It will explicitly encode only unsafe result_file paths, preserve normal
Unicode/backslash paths, clear stale encoding markers, and preserve the original
verdict and recoverable file content. The independent checker has been notified
to suspend final acceptance and retain its r1 report as scoped historical
evidence. The r2 handoff will contain a full patch relative to the original seed,
renewed parent verification, and independent full-wire/path-recovery review.
Shared source remains yours; this thread will finish and commit its own Trellis
records after r2 is accepted.
