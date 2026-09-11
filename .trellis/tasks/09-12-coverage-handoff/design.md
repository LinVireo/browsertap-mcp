# Coverage integration design

Apply the preserved candidate test delta to the original checkout using the
reviewable patch in `out/coverage-handoff-20260912/coverage-candidate.patch`.
The patch has 18 test paths, all explicitly claimed by this session.

The setup owner froze 11 separate files against original HEAD `db11d22`.
Their per-file identities are in
`out/setup-diagnostics-20260911/pre-coverage-integration.json`. Verify those
identities before and after test edits; preserve any newly announced peer change.
The complete source identity will change when the tests change, so a new combined
snapshot is required after readiness.

Adapt only test fixtures and assertions whose original assumptions differ from
the current valid contracts. Preserve failure/cleanup assertions and the existing
three command-scope regressions. Do not weaken assertions, invent invalid
production states, change coverage exclusions, or edit product source to improve
the percentage. Report product-source findings to the diagnostics owner.

The implementer runs affected offline checks. An independent checker reviews
the delta before the root sends a new readiness file. The diagnostics owner then
runs combined full coverage and package checks, using a fresh coverage database.
Candidate evidence is retained as a historical comparison, never merged into the
changed tree's coverage database.

Keep pre-integration test copies and raw hashes in the owned output directory
for audit and scoped recovery. Do not restore or reset the shared tree as a whole,
and do not edit another agent's task or research record.
