# Reviewed-source handoff and remaining boundaries

Active task: `.trellis/tasks/09-12-btap-defect-cleanup`.
Coordinator: `codex-resume-01a0881c`, native session
`01a09113-7fb4-71d1-b6ef-34cc2d6c9712`.
Original shared checkout: `D:/browsertap-mcp`, branch `local`.

## Current Fable follow-up (0.5.1)

The current isolated candidate is `D:/coding/btap-fable-fixes-20260912`, based
on f63c4a7. Follow [Fable integration](research/fable-final-integration-20260912.md)
for F1-F8, independent-review receipts, and the additional token/publication,
read-probe reservation and dialog execution fixes. Legacy Python CDP fallback
now installs/releases the controller in the current evaluation context. The
Python and extension builders recheck the original deadline immediately before
starting caller code, including time spent installing or compiling wrappers.

Final committed-tree gates, the next unused local `v0.5.1-rc.*` tag and canonical
checkout integration are not established by this tracked record. Their actual
outcomes belong in `out/fable-review-20260912/final-verification.json`, which
binds source HEAD/stamp, review inputs, complete-suite evidence, build and full
installation. The root worktree retains original failures and byte snapshots.
Canonical source was independently committed as `564e2509` and `6b391d88` while
root was integrating later fixes. Their frozen review found the intermediate
source bytes already covered by prior reviews, followed by version/record
updates. Preserve both commits by merging their history into the candidate,
then recheck that the original checkout is clean and its expected HEAD is an
ancestor before fast-forwarding. The former dirty-path stash plan no longer
applies. Unexpected source changes are reconciled before any transfer.

No new-build live run, bridge/MCP restart or manual extension Reload is claimed.
The external 0.5.0 Attempt 2 was 58 passed / 3 failed and did not establish user
tab preservation; it is retained as failed historical evidence. M01 full
retention, real A7, native Linux/macOS and historical wait latency remain open.
SPEC rev7 remains `delivered_stub`; a local candidate tag is not a release.

## Historical 0.5.0 cleanup

The following sections preserve the earlier tracked pre-seal handoff. Its later
results are in `out/bug-cleanup-20260912/final-verification.json` at f63c4a7 and
local `v0.5.0-rc.1`. Its pending language and extension stamp describe that
earlier candidate, not the current Fable follow-up.

## Integrated scope

The [defect ledger](research/defect-ledger-20260912.md) maps every confirmed
candidate to its disposition. C01-C26, M02-M06, the M01 `_Scope.close` handle
leak, and the FS-R1/ND-R1/ND-R2 review findings have implementations and scoped
regressions. Source version is 0.5.0 with 51 registered tools; the two explicit
native-file-dialog tools preserve opt-in, approval and ownership checks. The
seven removed generic OS-input tools remain removed.

All writers used bounded isolated copies or worktrees; only root applied source
patches to the original checkout. The external configuration/coverage/Unicode
peer owns and commits its own Trellis records. Its source and review artifacts,
earlier rejected revisions and root integration receipts remain preserved.

The final two integration receipts are:

- `out/bug-cleanup-20260912/root-unicode-r3-integration/integration.json`:
  accepted Unicode r3, exact candidate bodies/signatures, unrelated AST and root
  decorators preserved. Root result/docs/registration/version union: 291 passed.
- `out/bug-cleanup-20260912/root-adjacent-r2-integration/integration.json`:
  CLI/lock-key/resource-filename Unicode r2 and three-file CI applicability fix.
  Root adjacent-boundary/CLI/backup/geometry/close/stamp union: 184 passed.

Those suites overlap prior checks; counts must not be summed. Root's
`final-docs-run.json` and `final-version-run.json` both record exit 0.
The two bundled Skills' six validator checks passed in
`root-final-skills-r3.json`; BODY_VERBOSE is an advisory, not a failed gate.

Final independent delta review passed for all 351 frozen public files,
including the final Unicode/CI/documentation delta. Its report is
`out/bug-cleanup-20260912/final-fullscope-final-review.md`, bound by
`final-fullscope-final-manifest.json`. Root's separate artifact and source
verification receipt is `root-final-review-verification.json`.
The first two review reports and their original failed probes are immutable.

The first canonical attempt on `147c781` exposed one stale test inventory:
`tests/test_offline.py::test_every_tool_is_registered` omitted the two newly
registered native-dialog tools. It had 3965 passes and one failure; this is not
a passing canonical run. The isolated original assertion reproduced that
failure before the expected set was updated. The exact-set assertion and
product source are unchanged. `canonical.log`, `canonical-run.json`, the seven
files in `canonical-r1-evidence/` and `registry-red.*` preserve the failure.
`registry-green.*` and `final-registry-delta-review.md/.json` supply the bounded
follow-up verification; the final receipt identifies the later canonical run.
Root reproduced 28 registry tests passing. The supplement independently passed
four checks and confirmed that the other 350 public files were unchanged;
root verified its source and preserved-artifact hashes separately in
`root-registry-review-verification.json`.

## Candidate verification sequence

Finish the independent delta review and all owned record commits before the
source commit. Then run, on that exact clean commit:

```text
python -m scripts.finalize_change --bump none --skip-live
python -m scripts.check_install artifacts/dist --output out/bug-cleanup-20260912/full-install.json
python -m scripts.evidence_manifest --check
```

Use a fresh install environment; do not reinstall the shared editable venv.
The previous `build/` was preserved at
`out/bug-cleanup-20260912/build-before-final-seal/`; its independent receipt is
`build-preservation-reconciled.json`. The finalizer archives old canonical
artifacts recoverably. Never overwrite a failed run to make it appear passed.

The intended local tag is `v0.5.0-rc.1`, pointing to the sealed commit. Source
versions remain numeric because the repository versioner has that contract.
No formal `v0.5.0` tag, remote push, PyPI upload or Registry publication is
part of this handoff. `--skip-live` does not establish release readiness.

## Open work and runtime facts

- M01 full persistent-file retention remains open. `_Scope.close` now attempts
  all owned descriptor closes without replaying an uncertain close. Published
  v1 requester records remain durable death evidence; command files remain
  stable lock objects for old waiters. Unlink/TTL alone breaks those contracts.
  See `out/bug-cleanup-20260912/leases/m01-final-disposition.md`, including the
  unimplemented narrower Windows incomplete-requester cleanup candidate.
- Extension source stamp is `2752911b822fab7e`. This cleanup did not manually
  Reload the extension, restart the shared bridge, or certify a live MCP build.
  Source, installed metadata and loaded process identities are separate. Use
  actual diagnostics under a shared live claim before planning the required
  manual extension Reload and bridge/MCP restarts.
- Real A7 native Windows dialog inspection/cancellation and native macOS/Linux
  browser runs remain unverified. SPEC rev7 is still `delivered_stub`.
  Windows-only parameter collection and simulated POSIX/headless evidence do
  not constitute native Linux/macOS CI results.
- Historical wait latency still lacks correlated send/receive evidence. No
  root cause is claimed. Result-file retention and the pending product-policy
  decisions were not silently changed into cleanup work.
- Source identity covers package Python source and four imported JavaScript
  assets. It does not prove arbitrary bytecode, custom loaders, monkeypatches,
  code-object identity or malicious same-user ABA races.

The active task remains open for these explicit boundaries. The next session
starts with the final verification receipt and updated knowledge-vault project
index, rather than historical uncommitted/live claims from older builds.
