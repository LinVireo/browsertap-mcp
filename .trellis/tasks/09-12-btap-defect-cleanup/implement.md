# Execution slices

1. Receive the final coverage root receipt; verify the frozen setup and coverage manifests and run the combined baseline.
2. Complete the read-only inventory; reproduce candidate failures and write the defect ledger.
3. Assign non-overlapping repairs in isolated worktrees, or implement sequentially if the peer is unavailable. Record each failing regression before the repair and its passing result afterward.
4. Review each repair against the stated contracts; integrate only the reviewed scope and preserve unrelated changes.
5. Run final whole-tree verification, fresh packaging/full installation and the runtime checks supported by the actual loaded builds.
6. Apply warranted version/local tag changes, retain accurate release limitations, reconcile Trellis and knowledge records, and commit the authorized work.

The diagnostics/coverage handoff was integrated as `21ad974` plus `e631522` and
its combined-r2 baseline passed 3081 offline tests and the required checks.
Subsequent cleanup slices are tracked in `research/defect-ledger-20260912.md`;
the baseline evidence remains separately preserved.

Before the canonical seal, commit the reviewed source, public documentation
and each agent's own records, then run the repository finalizer with the version
already synchronized and `--skip-live`. The final whole-tree and installation
results belong in `out/bug-cleanup-20260912/final-verification.json`, so recording
them does not change the tracked tree after it has been sealed. A local RC tag
must point to that exact commit. Runtime/manual Reload and native-platform gaps
remain visible even when the offline candidate is complete.
