# Implementation and handoff

1. Verify/copy the frozen setup and coverage source into the isolated worktree;
   record per-file seed bytes and the source identity in ignored out artifacts.
2. Load the allocated audit reports and relevant specs. Add meaningful failing
   regressions for C01-C04, C16 and M06 as applicable. Keep all filesystem/network
   effects within test fixtures; no bridge restart or shared browser work.
3. Implement the bounded source changes. The implement sub-agent owns the
   isolated source while writing; root independently examines uncovered failure
   cases and coordinates with the separate root identity worker.
4. Run affected tests, required lint/type checks, diff and LF checks. Stop writes
   and hand the whole own delta to an independent check sub-agent.
5. Resolve review findings, rerun affected checks when changed, create an exact
   seed-relative patch and file manifest, and send the coordinator a new shared
   research receipt. Root coordinator integrates and runs final shared checks.
