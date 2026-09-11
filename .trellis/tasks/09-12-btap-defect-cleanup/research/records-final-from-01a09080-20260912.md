# Final scoped records and integration reconciliation

From `codex-coverage-01a09080`, 2026-09-12. The coordinator's
`root-unicode-r3-accepted-20260912.md` acknowledges actual Unicode r3 integration.
All three allocated tasks in this thread are complete: coverage integration,
configuration diagnostics and Unicode result handling. The overall overnight
cleanup remains with `codex-resume-01a0881c`.

This parent's final read-only integration comparison is
`out/coverage-handoff-20260912/result-unicode/main-integration-readback-r3.json`,
SHA256 `3baadd9fc9eba87f8736828b077b28425360fa4aa48a5ab90d61f1a1b7dc9726`.
It confirms the main test is the exact frozen r3 file; the four replaced and
four added server definitions match the accepted candidate; unrelated module
AST and all existing coordinator decorators are preserved. Main server SHA256
at comparison is `9ee48cba06f4a77ef01ed7dc0161e41287f28636d3b742cb5336e2f4b84e3e62`.
This is a bounded source comparison, not a new whole-project test run.

Public descriptions and both README/caller-Skill pairs now state the conditional
path decode. C21's Python/MCP boundary and C22's completed-result I/O fallback
are reconciled through the final r3 handoff. C01-C04/C16/M06 were accepted in
the previous configuration handoff; coverage source was committed as `e631522`.
The coordinator retains final public contract checks, the canonical full suite,
full isolated install, source commit, version/local RC tag and knowledge update.
Its separate C23-C26 fixes are outside this thread's implementation ownership.

The exact owned record set is the complete content of these three directories:

- `.trellis/tasks/09-12-coverage-handoff/`
- `.trellis/tasks/09-12-config-diagnostics/`
- `.trellis/tasks/09-12-result-unicode/`

It additionally includes only this thread's `*from-01a09080-*.md` files under
the setup-diagnostics and defect-cleanup research directories. The final audit
normalizes only these records to LF and validates their JSON/JSONL. Staging,
secret scanning and the local commit use this exact set, preserving all source,
public documents and other agents' records. No push or publication is involved.

The immutable commit outcome, exact path list, pre/post source and index checks,
validation and secret-scan results will be recorded at
`out/coverage-handoff-20260912/records/commit.json`. The commit hash belongs in
that ignored receipt so this tracked note does not create a self-reference or
leave another dirty receipt after the source owner freezes the tree. After the
commit, only this thread's active task pointer and claim are released.

Knowledge reconciliation must retain the remaining runtime boundaries: the new
source needs manual extension Reload and fresh appropriate Python processes;
offline checks do not prove A7 or Linux/macOS native behavior. M01's fd leak was
repaired, while full v1 requester-proof/stable-lock-file retention remains open.
The previous `db11d22` / 49-tool / no-Reload knowledge entry is historical once
the new source is committed; the coordinator owns its current-state replacement.
No blanket all-bugs-closed or release-ready claim follows from this record.
