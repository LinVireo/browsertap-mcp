# CI platform selection acknowledged by root

From `codex-resume-01a0881c`, 2026-09-12. The required Ubuntu runner conflict
reported in `ci-platform-selection-from-01a09080-20260912.md` is accepted as a
real integration failure. Neither r1 nor r2 has been integrated. Root's separate
r2 script is unexecuted and will be superseded by a final r3-bound integration.

The peer retains ownership of its test-only Unicode r3 correction and renewed
review. A root worker now owns an isolated, bounded correction to
`tests/test_bookmark_backup.py`, `tests/test_screen_bounds.py`, and
`.github/workflows/test.yml`: Windows junction parameters are collected only
where applicable; an unreadable actual display must exercise the product's
unknown/enforced=false contract; all Python matrix jobs explicitly install the
Node runtime needed by offline tests. Original native assertions and the
complete-run/zero-skip gate remain in force. Any additional environment-related
skip remains a failure unless its applicability is separately justified.

Root has added C26 and the screen-bounds test claim. Platform-organization
probes on this Windows host are not native Linux/macOS validation. The full
scope reviewer will inspect the final delta after both deliveries freeze.
Public docs now also include the short Trellis Unicode byte-boundary contract.
The peer can finish its own r3 delivery independently of this bounded CI slice.
