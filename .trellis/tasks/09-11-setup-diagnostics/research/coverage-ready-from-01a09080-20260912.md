# Coverage integration ready for combined verification

Sender: `codex-coverage-01a09080`, native thread
`01a09080-c3ed-7560-b872-8950f5972a6a`. Date: 2026-09-12.

Independent Trellis check is **PASS**, and this root has reconciled the final
source bytes and recorded evidence. The 18-test-file write claims are released
to you. Both local implement/check workers stopped writing tests.

Use **`out/coverage-handoff-20260912/review/tests-final.json`**, not the earlier
`integration/tests-final.json`. Your prepared runner already accepts this as
its fourth argv item (`init <this-ready-path> <review-manifest-path>`).

- Review: `.trellis/tasks/09-12-coverage-handoff/research/check-20260912.md` and
  `out/coverage-handoff-20260912/review/report.json`.
- Root reconciliation: `out/coverage-handoff-20260912/reconciliation/post-review-evidence.json`.
- Final affected-test JUnit: `out/coverage-handoff-20260912/review/affected-tests-final.junit.xml`;
  1222 passed, zero failure/error/skip, executed by the checker after its repair.
- Final delta: 18 files, +2434/-32. Wait failure receipts match the current
  contract; capture cleanup now proves which tab retains ownership. Fault
  injection rejected wrong-tab cleanup. No production changes from this batch.
- Root verified 25 review artifacts, 18 tests and original backups, 11 setup
  files (hash and mtime preserved), and 15 Python sources.
- Final patch: `out/coverage-handoff-20260912/review/integrated-tests-final.patch`.

All whole-tree coverage, static, tool-evidence and package/install checks are
yours. No duplicate full run is running here. Our own Trellis updates are now
finished for this freeze; new findings go only to ignored `out/` until your
measurement finishes. A post-ready snapshot will be stored at
`out/coverage-handoff-20260912/reconciliation/ready-source.json`; the runner's
own before/after snapshot remains authoritative for its checks.

## Overnight scope accepted

Read `overnight-scope-from-01a09113-20260912.md`. Your newly created
`09-12-btap-defect-cleanup` is the shared cleanup task; I will not create a
competing parent task. You own final knowledge-vault reconciliation (including
the BTAP index and source-review page) and shared live resources.

This root takes the proposed bounded, read-only configuration/diagnostics audit:
relative state/token paths across spawn cwd, CLI DNS/IPv6 exceptions, numeric
environment parsing and unreadable-token diagnostics. One reused local worker
handles relative paths; I handle the remaining candidates. Outputs go to
`out/coverage-handoff-20260912/residual-inventory/`. I will send a new research
receipt after your combined source freeze ends and propose exact repair files.

User authorization includes fixing remaining project/recorded bugs and justified
version/local-tag changes while they sleep. No manual extension Reload is
assumed. Keep the actual native-platform, A7, runtime and release evidence
boundaries; old candidate percentages are not combined-tree measurements.
