# Unicode r2 status: r1 held, renewed verification running

Update: r2 product and parent checks passed, but an expected non-Windows skip
conflicts with the required CI evidence runner. A test-only r3 is being prepared;
see `ci-platform-selection-from-01a09080-20260912.md`. Final delivery is pending.

From `codex-coverage-01a09080`, 2026-09-12. Shared source remains coordinator-owned.

The original r1 candidate and its old review are not the final delivery. The
parent subsequently reproduced an actual NTFS result-file path containing an
unpaired surrogate, which made the complete JSONRPC wire serialization fail.
Evidence and the r1 review addendum are in
`out/coverage-handoff-20260912/result-unicode/result-path-unicode-full-wire-probe.json`
and `out/coverage-handoff-20260912/result-unicode/review-r2/r1-blocker-addendum.md`.

The isolated r2 product files are now frozen:

- server.py SHA256:
  `8c5e8494a759f11e096a3b4eac8095abe1eb9c56aaf15bd09a09576e08ce74c2`.
- tests/test_result_unicode.py SHA256:
  `f00e5007d2e5977176958be28e2971823cafc8e57556546837074236091f3821`.

This revision uses an explicit `result_file_encoding="json"` only when the path
itself contains non-scalar UTF-16 units. Decode that result_file with JSON once
before opening it. Normal Unicode and ordinary backslash paths stay unchanged.
The marker is also part of replaced-descriptor cleanup. No execution or replay
semantics have changed.

The parent is running the fresh 15-file union and quality checks in
`out/coverage-handoff-20260912/result-unicode/parent-final-r2/`. The existing
checker is independently reviewing the real NTFS/file/wire boundary under
`review-r2/`. The implementation worker is exporting the complete two-file patch
relative to original seed `390afba80d63bff1e3848a53e2d38faa4b541f26` into
`D:/coding/btap-result-unicode-20260912/out/result-unicode/r2/`. Wait for the final
receipt before treating these running checks as PASS.

The read-only view of your prepared `out/bug-cleanup-20260912/integrate_final_unicode.py`
still binds the r1 manifest and `review/`. Please replace those inputs with the
eventual r2 manifest, patch, and `review-r2/` final report before execution. The
shared server has not received the r1 helper or new test at this checkpoint.
The script has source-hash guards, but its frozen inputs still need updating.

This thread's own Trellis records are being prepared for an exact-path commit;
the pre-r2 audit found 37 owned records and six files requiring mechanical LF
normalization; this status receipt adds one more record. The final commit will
follow the accepted r2 handoff. Public
guidance, knowledge, shared integration, canonical validation and version/local
tag remain with you.
