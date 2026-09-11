# Final accepted Unicode r3 handoff

From `codex-coverage-01a09080`, 2026-09-12. The frozen r3 two-file slice is
accepted for integration. Independent review is PASS with no open slice
blocker. This supersedes the r1 path hold and r2 platform-collection hold for
this candidate; their immutable reports remain historical evidence.

The coordinator has not applied an earlier server revision. Apply the complete
original-seed patch, not the r2-to-r3 increment. The prepared
`out/bug-cleanup-20260912/integrate_server_unicode_r3.py` can now validate its
review gate and integrate sequentially. The HTTP patch is already integrated
and is not part of this delivery.

| Identity | Value |
| --- | --- |
| Source | `D:/coding/btap-result-unicode-20260912` |
| Seed tree | `390afba80d63bff1e3848a53e2d38faa4b541f26` |
| r3 tree | `5bf2e78591c46c714aba5ae4c2400d6363bc82f0` |
| server.py SHA256 | `8c5e8494a759f11e096a3b4eac8095abe1eb9c56aaf15bd09a09576e08ce74c2` |
| tests/test_result_unicode.py SHA256 | `f714a66a5dae10fa581e052bb96f6f03b4260edfa1f332f14dc617129a300625` |
| Complete patch SHA256 | `cf6bb78178e6f89761902422037f24a3d3c90b2b85f09459e505849db7c6cb98` |
| Package manifest SHA256 | `2ca6f6f5fe9323754a5f7d6f5d96ba20fa0eca1a3ba7b355c637f73e6544d28b` |
| Independent review manifest SHA256 | `222993aeb535a56bc6179d3de44167a2677b8abceb956d7f3f6c71aa00d5fe4f` |
| Parent final verification SHA256 | `4b59b5ccea7c44a578262420699552ae8338b63748e30e923e6951c026715f78` |
| Parent review readback SHA256 | `2cd81bf719ddcfc0c219622c7a488ec43410c32463b7a310ee4e6502f28135ee` |

The complete patch, manifest and `HANDOFF.md` are under the source's
`out/result-unicode/r3/`. Shared evidence is under
`out/coverage-handoff-20260912/result-unicode/`:

- `review-r3/independent-review.md` and
  `review-r3/review-evidence-manifest.json`: renewed independent PASS.
- `parent-final-r3/verification.json`: this parent actually ran 750 affected
  tests with zero failures/errors/skips, plus Ruff, mypy src, diff/LF and
  unchanged seed/source SHA and mtime. Test counts overlap and are not added.
- `review-readback-r3.json`: this parent independently read back all 16 final
  review artifacts, all 14 package artifacts, both frozen source files, four
  delivery identities, all five parent logs and the review JUnit. Hash, size,
  mtime and complete artifact-set checks passed. It did not rerun the suites.
- `package-preflight-r3.json`: earlier original-seed delta/reverse-apply and
  shared-tree read-only apply checks. The integration must still check its
  current source before writing.

r3 only changes test parameter collection relative to r2; server bytes are
identical. Windows retains the same full 75-node suite. The module-local POSIX
collection contains 67 nodes, omitting only eight Windows NTFS directory
combinations while keeping ordinary directories and both portable path probes.
This is collection evidence on Windows, not a native Linux/macOS run. The
review reuses r2 path/wire evidence only for the unchanged product source and
keeps the old r2 overall BLOCKED report intact. Shared CI changes remain the
coordinator's separately acknowledged C26 slice.

## Public contract and reconciliation

The delivery closes C21's Python/MCP result boundary and C22's completed-result
file-write failure, including synchronous, completed-poll and late results.
It retains original success/error, operation identity and no-replay decisions.

Read the r3 `HANDOFF.md` together with the unchanged r2 contract details. Only
an actual surrogate in the filesystem path adds `result_file_encoding="json"`:
decode that field once, then open the resulting path and decode the file JSON.
Normal paths stay unchanged. `result_file_scope` and `result_json_scope` are
`js-value`, `envelope` or `mcp-call-result`; the last archives the complete native
result after normal envelope adaptation. Its original structuredContent is in
the adapted data/legacy. On I/O failure, `result_json` retains complete ASCII
JSON; envelope/native headers reference it through `result_json_ref` without
duplicating the body. Stale file/path markers are cleared. Encoded error
message/code fields carry their own markers. File retention is unchanged.

This parent read the prepared MCP descriptions, both README sections and both
packaged caller Skills: all describe the two-step path decode. The coordinator
owns their final reconciliation with integrated code and `check_tool_docs`.
Preserve its current descriptions, native glue and unrelated AST definitions.

Please acknowledge actual r3 integration through your own research file. Then
this thread will finish and freeze its three owned Trellis tasks, normalize only
its records to LF, validate their contexts, scan and commit only its exact
record paths, and report the commit through ignored evidence. It will not stage
the coordinator's source or records. Canonical whole-project evidence, knowledge
reconciliation, the version/local RC tag and unavailable native/live acceptance
remain coordinator-owned; this handoff does not declare the overall task done.
