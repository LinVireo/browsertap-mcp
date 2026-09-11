# Knowledge reconciliation follow-up

Read-only follow-up by `codex-coverage-01a09080`, 2026-09-12. Canonical knowledge
edits and final dispositions remain with the cleanup coordinator.

- The project `INDEX.md` and the leading activation section of
  `source-review-2026-09-10.md` still describe the `db11d22` runtime as current,
  with 49 tools and no Reload required. Those are valid historical activation
  facts, but a new current-state entry must supersede them after this batch.
  Current source verification cannot establish the loaded extension's build.
- The coverage candidate note still points at isolated `3d4ed9c` and says its
  18 test files are uncommitted. The shared integration was accepted and
  committed as `e631522`; `.trellis/tasks/09-12-coverage-handoff` is now completed
  against your recorded acknowledgement. The configuration allocation is also
  completed as a reviewed patch handoff, independently of whole-project status.
- The old `source-review` headings saying the fork findings are unrepaired and
  F0-A/F1 are open are dated historical snapshots. Preserve their original
  evidence and point readers to the latest implementation/disposition table;
  do not infer current defects merely from those headings.
- `task-queue.md` remains the source for the numbered historical defects. The
  ledger's C01-C04/C16/M06 should close against the integrated r2 contracts,
  including the default-directory metadata boundary and structured malformed
  diagnosis. Avoid using the earlier superseded candidate or overlapping test
  counts as final evidence.
- The old `traps.md` duplicate-reader warning for
  `scripts/check_derived_notices.py` is superseded by its deletion in `f3edc7e`.
  This does not establish that unrelated logging/output contracts were tested.

The following are explicitly distinguished from reproduced defects in
`source-review-2026-09-10.md` (notably its product-policy paragraphs around
lines 132-137, 386, 449 and 580-582): upload/download path policy, default
lab/no-elicit policy, monitor latency preferences, result-file retention,
Q4 store-publication materials and Q5 capability grouping. Keep their actual
policy/backlog disposition visible; neither arbitrary cleanup of caller result
files nor silent removal of those records is part of fixing confirmed bugs.

The new unpaired-Unicode failure is a reproduced serialization defect and has a
separate active repair task (`09-12-result-unicode`), as documented in the peer
`unicode-result-repro-from-01a09080-20260912.md` receipt. It must not be confused
with the older undecided result-file-retention policy.

A7/native acceptance, macOS/Linux browser observations and historical wait
latency correlation need current evidence or an explicit remaining limitation.
The new narrow Windows implementation and offline tests alone do not close
those historical runtime acceptance conditions.
