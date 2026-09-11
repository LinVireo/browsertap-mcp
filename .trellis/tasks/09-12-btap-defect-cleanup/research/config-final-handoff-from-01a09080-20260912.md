# Configuration repair: final r2 release for integration

From `codex-coverage-01a09080`, native thread
`01a09080-c3ed-7560-b872-8950f5972a6a`, 2026-09-12.

**Final independent check PASS. The C04 hold is lifted for r2 only.** This
receipt supersedes the earlier candidate and hold receipts. The old
`final-handoff/` package must not be applied.

Use the frozen package at
`D:/coding/btap-config-20260912/out/config-repair/final-handoff-r2/`:

- `config-diagnostics.patch`: 12 source/test files, 70,162 bytes.
- SHA256: `9dd3b9ecb2ca7b7d60300d18cdbb78ce1643bbeb01423d95b8d572206f3c16d9`.
- `manifest.json`: exact seed-before and frozen-after hashes, size, and mtime.
- `verification.json`: final test results and scope limits.
- `HANDOFF.md`: fix contracts and sequential integration notes.

The delta is relative to seed tree
`e44b92a9f5fff955d1c86939e162d3f2db1ee604`, not the isolated worktree HEAD.
It excludes the frozen setup and coverage baseline. Apply the bounded patch
against the current main checkout; copying whole source files would overwrite
newer coordinator changes. Preserve runtime identity, receipt safety (including
the missing-result wire-frame refusal), and native-dialog integration in
`browser_bridge.py` and `server.py`.

## Fixes and reproduced validation

C01-C04, C16 and M06 are covered. The final C04 correction includes both explicit
and default state directories: unreadable directory metadata is reported as
`state_dir_exists=null`; an unknown canonical directory is not treated as
missing and cannot silently redirect token selection to legacy. Existing token
creation, legacy-selection and manual-reload protections remain in force.

This thread ran the final affected union: **789 passed, 0 failures/errors/skips,
64.76 seconds**, plus **17/17 fresh-process entrypoint probes**, Ruff `src tests`,
and mypy for 15 source files. The probes created no state directory. All 12
source/test hashes and mtimes stayed fixed before and after final verification.
These are affected-suite results, not a project-wide count.

Independent reviewer `/root/trellis_check_coverage` completed full delta review,
20 boundary probes, the four new permission regressions, explicit/default
permission reproductions, and final package/seed/reverse-apply/index checks.
It reused its separately verified connection-age review after SHA/AST checks.
All passed; overlapping test sets must not be added together.

The final shared report is
`out/coverage-handoff-20260912/config-repair/combined-review.md`.
Report SHA256:
`59721d20e3ccaca87cd8ae215b7d9d067707c1ee01a44cc2e20ed2e4a6ad2522`.
Its `combined-review-evidence/archive-manifest.json` seals 23 report/evidence
files, all read back by the reviewer; manifest SHA256:
`83feb8ec077768c9f6e47454c009560c2aff3d9b4d2577ab1bd008ec34415ace`.

## Ownership and completion

Implementation and independent review are complete. Both isolated configuration
worktrees remain frozen as recoverable evidence; this thread releases source
ownership to the coordinator and retains only its own Trellis/evidence records
and integration follow-up. No shared product file or other claim was changed
for this handoff.

Please reproduce affected integration checks after applying. Public guidance is
listed in
`.trellis/tasks/09-12-config-diagnostics/research/public-guidance-needed.md`,
including the two final C04 directory rules. Public docs, canonical knowledge,
final whole-project gates, version and local tag remain coordinator-owned.
Python loopback checks do not establish extension IPv6 support. This slice did
not restart the shared bridge, run shared live tests, reload the extension,
push, or publish. The complete user request remains active with the coordinator.
