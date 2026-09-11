# Required CI and platform-specific test selection

Update: the bounded r3 candidate is frozen. Parent final verification passed
750 tests (zero failures/errors/skips), Ruff, mypy src, diff/LF and source
stability; final independent acceptance is pending. Its complete original-seed
patch is `D:/coding/btap-result-unicode-20260912/out/result-unicode/r3/result-unicode.patch`,
SHA256 `cf6bb78178e6f89761902422037f24a3d3c90b2b85f09459e505849db7c6cb98`.
The manifest SHA256 is `2ca6f6f5fe9323754a5f7d6f5d96ba20fa0eca1a3ba7b355c637f73e6544d28b`.
This closes the local fixture correction when its review is accepted; the
shared-suite applicability audit below remains with the coordinator.

From `codex-coverage-01a09080`, 2026-09-12. This is an integration finding; shared
CI, evidence scripts and other owners' tests remain read-only in this thread.

The r2 server implementation passed this parent's complete 15-file affected
union: 750 passed, no failure/error/skip, 26.80 seconds. Ruff, mypy src, original
seed-relative diff and raw LF checks passed with all source SHA/mtime stable.
Evidence: `out/coverage-handoff-20260912/result-unicode/parent-final-r2/verification.json`,
SHA256 `fb3d2e39f6e702c2a0247af7478bfb608be4b12f12d87f6457a0b32a6699daa2`.

The independent checker also passed the r2 product path/wire scenarios, but we
both identified a required-CI conflict before final acceptance:

1. `.github/workflows/test.yml` uses Ubuntu and now invokes
   `python -m scripts.test_run_evidence --mode offline` as a required step.
2. That runner calls validate_test_run and returns failure for any nonzero
   JUnit skipped count or skipped execution phase. The informational acceptance
   step later in the workflow does not make this earlier required step optional.
3. The r2 result_file_directory fixture skips high/low surrogate directory
   cases on non-Windows hosts: two directory kinds times four result kinds,
   hence eight expected skips. Those would make the required Ubuntu step fail.

The original worker is preparing r3 with only a test-collection correction:
include the two real NTFS directory parameters only on Windows, keep ordinary
path cases and both portable alias probes on every platform, and remove the
fixture's expected platform skip. All 75 Windows cases and their assertions stay
intact. The r2 server SHA remains frozen; r1/r2 packages and review evidence stay
unchanged. The checker is recording the exact required-gate rejection separately.
The final delivery will be a complete original-seed-relative r3 patch; do not
integrate the r2 test file as final.

Please reconcile the same runner contract in your integration scope as well.
Current shared `tests/test_bookmark_backup.py` has non-Windows junction skips in
DIRECTORY_LINKS; `tests/test_screen_bounds.py` has an unreadable-display skip
that historically occurs on headless Linux. Other tests have conditional
environment skips (Node, symlink privilege, numeric IPv6) that must be checked
against what the required CI actually provides. These are not new product
failures or proof that a native platform run happened. Keep the formal gate
strict, and make applicability and unrun native verification explicit when
organizing the suite. Do not relabel a skip or missing browser as a pass.

This thread owns only its Unicode test correction; changes to your CI/evidence
contract or other test files remain yours. It will return the frozen r3 review,
then wait for your integration acknowledgement before committing its own Trellis
records as requested in root-unicode-r2-hold-ack-20260912.md.
