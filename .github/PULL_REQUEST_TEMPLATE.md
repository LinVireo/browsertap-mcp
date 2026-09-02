<!--
The checklist below is intentionally short. The full one lives in
CONTRIBUTING.md ("Pull request checklist") and is the canonical copy -- pasting
it here would create a second copy to keep in sync, and a stale checklist is
worse than a link. What is repeated here are only the items a reviewer cannot
verify by reading the diff.
-->

## What this changes

<!-- The behavior, not the files. If it fixes an issue, link it. -->

## Why

<!-- What was wrong or missing. For a bug fix, the root cause, not the symptom. -->

## How it was verified

<!--
Commands and their outcomes. "Tests pass" is not verification unless you say
which ones ran; the offline suite, the lint report, and the version/doc checks
are separate gates and a change can pass one while breaking another.

Paste the actual result. If something could not be run in your environment, say
so explicitly -- that is useful, and claiming it ran is not recoverable.
-->

```text

```

- [ ] Offline suite: `python -m pytest tests -q`
- [ ] Lint, JS lint, and type check: `python -m scripts.lint_report`
- [ ] Docs and version gates, if this touches either: `python -m scripts.versioning check`
- [ ] Live path exercised against a real browser, or explicitly not applicable

## Things a reviewer cannot see in the diff

- [ ] This preserves unrelated local work in the tree it was developed against.
- [ ] No generated artifacts, cookies, tokens, or profile data are included.
- [ ] Public documentation is updated in **both** languages, or this touches none.
- [ ] If a tool's signature or description changed, the tool-doc and coverage
      gates were re-run (`check_tool_docs`, `tool_coverage_report`).
- [ ] If the extension changed, the build stamp was regenerated and the
      extension was reloaded manually before any live claim.

<!--
By opening this pull request you agree to follow the Code of Conduct. The clause
that matters most here: redact cookies, tokens, history, and page screenshots
before attaching a reproduction.
-->
