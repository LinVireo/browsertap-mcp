# R6 successor integration closure

Source commit: `7d7d5d2c2e44d4fc7675559648fdf87167985fc4` on
`codex/r6-integration-20260911`, based on sealed r6 `44a092f`. Version remains
0.4.20. This closes the authorized local integration and prepares the remaining
native acceptance; it does not complete the whole product contract or publish a
release. SPEC rev7 remains `delivered_stub`, with source provenance.

## Changes and acceptance

| Item | Result and evidence |
| --- | --- |
| I1 | F1 retains authenticated late terminal evidence without replay or renewing receipt retention; covered by late-result and recovery contracts. |
| I2 | LOW-5 removes only the failed connection's state and preserves replacement sessions and operation owners; disconnect-cleanup contracts pass. |
| I3 | The sealed r6 remaining-navigation-deadline implementation is preserved and its regression passes. |
| W1 | Only server-generated selector/text/URL probes use the private read-only marker and release an eligible ordinary lease at the existing bridge deadline. Public caller JS cannot select that behavior. |
| W2 | Retained receipts keep ownership and deadlines; a late reply cannot release a successor lease. Unusable wait results return the original receipt instead of dispatching another probe or replaying caller JS. |
| V1 | Fresh offline, focused, Windows live, lint/type/docs, distribution and full-install results identify this integration. Exact scopes are below. |
| V2 | Independent r2 review is PASS with no findings; the current bound gate passed and checkpoint is satisfied. |
| M1 | Available Windows checks are complete. Native macOS/Linux and A7 evidence remain pending, with an executable acceptance manual. |

The imported candidate commits are recorded in the source commit message. The
original r6 checkout and its sealed artifacts remain preserved. The independent
cross-platform checkout was observed at `3d4ed9c` during closure; changes made by
its author are separate from this integration and its CI is not our evidence.

## Verification

The current result files are under `out/integration-20260911/r2/` unless stated
otherwise:

- `offline-junit.xml`: 2809 passed, zero failures/errors/skips; 61 live cases
  deselected. `offline-coverage.json`: 95.51940639269407% combined coverage.
- `../long-wait-green-2-junit.xml`: 93 focused contracts passed. The new replay
  regression cases first failed 12 times before the collector repair.
- `../windows-live-junit.xml`: 61 full Windows live cases passed before the
  final collector repair. `windows-wait-live-junit.xml`: all 10 affected wait
  live cases passed after that repair. Unchanged paths reuse the earlier run.
- `windows-wait-live-preflight.json`: enforced extension `matches_tree`, stamp
  `cf99c8ef9dc76da6`; one owned tab registered and released, zero outstanding.
- `lint.json`: Ruff, ESLint and mypy clean; JavaScript and type checks enforced.
- `../tool-docs-r2.md`: 49/49 tool docs, `docs_ok=True`. The default offline
  `tool-coverage.json` reports 49 valid contracts, 32 fully verified tools and
  no failed evidence; its live evidence is deferred.
- `build.log`, `distribution.log`, `twine.log`: new wheel and sdist built and
  passed distribution and strict metadata checks.
- `install-full.json`: full fresh-environment installation passed, CLI proved,
  no problems or notes. `secret-scan-source.json` is empty; staged source and
  whitespace checks passed before the source commit.

The r1 full-live preflight was not separately snapshotted before the r2 helper
replaced the ad hoc integration copy. Its 61-case JUnit/log, runtime observation
and independent r1 reviewer observations remain. The r2 preflight above proves
only the actual 10-case run. The original r6 sealed evidence was not overwritten.

## Independent review

Review `r6-integration-20260911-r2` binds the 323-file source subject
`d686f961e276034c0858582ffecfb05ec931b659470bb6aee0543737814efffc`.
Task and journal bookkeeping are excluded from that source subject.

R1 found a P1 long-wait replay path and P3 misleading result-tool description.
The collector now queries only `in_progress` receipts, decodes `success`, and
returns all other states with the original operation ID and actual reservation
state. Released guidance no longer mislabels caller JS as read-only. R2
independently rechecked these repairs, package bytes and the recorded checks.

The original BLOCK record remains in `out/reviews/archive/`; its request was
archived unchanged when the new subject was opened. The r2 result is in
`out/review-inputs/r6-integration-20260911-r2/result.json`, and the bound review
record is `out/reviews/r6-integration-20260911-r2.json`. The satisfied checkpoint
is `out/review-request.json`. Final closure recomputes the gate into
`out/integration-20260911/r2/review-gate-final.json`.

## Runtime and remaining work

The integration bridge uses a process-local source path. The existing editable
installation and persistent host MCP configuration still point at their previous
source; validation used fresh MCP processes. No extension reload is needed.
The task's source/live claim is released during final handoff.

The bounded timing experiment in `out/integration-20260911/wait-run2/` exercised
14 waits and 14 navigations, including two late receipts collected after a new
same-tab read. It demonstrates release and collection without replay, but does
not establish the historical delay's cause: extension reply-send and bridge
WebSocket receive timestamps are missing. Add correlated instrumentation if the
delay recurs; do not infer a cause or continue unbounded sampling.

`manual-native-acceptance.md` documents the missing registered A7 capability and
native macOS/Linux procedure. A7 requires a product decision about implementing
a narrow capability or revising and reapproving the contract. The native test
machine question remains unanswered. No attestation is inferred from CI.

After task and journal commits, `out/integration-20260911/native-validation/`
receives the final Git bundle, package copies, SPEC/PLAN context and hash
manifest. The final machine-readable `verification.json`, `delivery-report.md`
and `continuation-state.json` bind those files, commits, knowledge update and
claim release. These local artifacts are not uploaded. No push, tag, publication
or release finalizer is part of this task.
