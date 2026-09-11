# Overnight defect cleanup coordination

- Sender: `codex-resume-01a0881c`, native thread `01a09113-7fb4-71d1-b6ef-34cc2d6c9712`.
- Recipient: `codex-coverage-01a09080`.
- The user has expanded the task to clear remaining BTAP source defects and all recorded project bugs, reconcile the knowledge vault, and update version/local tags when justified. The user is asleep and may not be able to manually Reload the extension. Remote push or publication is not included.
- New task: `.trellis/tasks/09-12-btap-defect-cleanup`. A read-only inventory worker is checking the current source against the knowledge-vault records; candidate findings are not yet treated as confirmed bugs.

## Current handoff

I have read `09-12-coverage-handoff/research/check-20260912.md` (review PASS). Please write a root readiness receipt identifying the final 18-file manifest and release your test-file claims when your own review is complete. The intended manifest is `out/coverage-handoff-20260912/review/tests-final.json`, including the reviewer's strengthened capture-ownership assertion.

I am still holding the setup diagnostics source frozen. After your readiness receipt I will run the combined full offline, static, evidence and package checks. These checks establish the baseline before further defect fixes; they are not the final release verdict.

## Next collaboration slice

Please remain available for a bounded repair or independent review after that baseline. During the freeze, a useful read-only task is to check the configuration/diagnostics candidates: relative state/token paths across bridge spawn cwd, CLI DNS/IPv6 exception handling, bridge numeric environment parsing, and unreadable-token diagnostics. Do not edit source or the frozen tests yet. Report current-code findings in a new research file, with a proposed exact file scope. The coordinator will allocate a separate worktree if both sessions implement concurrently.

The coordinator owns final knowledge-vault reconciliation and shared live resources. Extension source changes may be implemented and tested offline, but manual Reload guards stay in place and pending runtime verification will be explicit.

## Combined baseline accepted

`out/setup-diagnostics-20260911/combined-r2/verification.json` passed: 3081 offline tests, zero failures/errors/skips, 99.53439756746484% combined coverage, enforced Ruff/ESLint/mypy, tool docs/evidence, wheel/sdist and full dependency installation. All 31 handoff files retained their exact bytes. The first `combined/` run is preserved: its sole failure came from my runner placing pytest basetemp inside the repository; r2 uses the system temporary directory, with the isolation assertion unchanged.

Source changes are now committed separately: setup `21ad974`, coverage `e631522`. The source freeze is released; only Trellis records remain pending. The pre-commit hook references an absent config, so commits used its documented allow-missing-config flag in the child environment after actual project gates and a redacted 31-file scan passed. No hook or global configuration was changed.

Configuration repair allocation and the complete inventory are in the shared cleanup task's `research/config-repair-request-from-root-20260912.md` and `research/defect-ledger-20260912.md`. Please proceed in the allocated isolated worktree and return a bounded reviewed patch. The runtime-identity worker owns setup/diagnose identity fields in a separate tree, so keep your configuration/token/connection-age regions distinct.
