# Integrate coverage tests through shared BTAP Trellis

## Goal

Integrate the existing candidate coverage tests into the original checkout with
shared Trellis ownership, then verify them against the current diagnostics code.

## Requirements

- Work from the original checkout, `D:/browsertap-mcp`, with its Trellis task
  context and claims loaded. This continues the already authorized coverage work
  after the user's 2026-09-12 correction about the shared project directory.
- Bind only the current Codex thread, `01a09080-c3ed-7560-b872-8950f5972a6a`.
  Preserve the shared developer identity and other sessions' task pointers.
- Respect `codex-resume-01a0881c` ownership of the setup-diagnostics repair.
  This session owns its task artifacts, peer research messages,
  `out/coverage-handoff-20260912`, and the 18 test paths listed in
  `agent-claims.json`. Product source and diagnostics tests stay with their owner.
- Bring the existing 18-file test patch and its final evidence into the original
  checkout as a reviewable handoff, preserving the candidate files.
- Keep the candidate result bound to source HEAD `3d4ed9c`. The original tree is
  at `db11d22` plus concurrent diagnostics edits, so its coverage must be measured
  separately after integration.
- Use a new file under the diagnostics task's `research/` directory for the peer
  message. An outgoing file is not proof that the other session has read it.

## Acceptance Criteria

- [x] The current task resolves from this thread's session pointer.
- [x] Other agents' claim entries and existing dirty files are preserved.
- [x] All 18 candidate test hashes still match the final verification artifact.
- [x] The exported patch and copied evidence have SHA-256 records.
- [x] A read-only patch application check records compatibility with the current
  shared working tree, including any files requiring adaptation.
- [x] The peer handoff names the patch, evidence, ownership, and next dependency.
- [x] The 18-file patch is applied/adapted in the original checkout without
  changing the 11 frozen diagnostics-owned files.
- [x] The affected tests, Ruff checks, diff checks, and LF checks pass on the
  original checkout.
- [x] An independent Trellis check reviews the integrated test delta and any
  adaptation findings are resolved.
- [x] The diagnostics owner receives a readiness record with final test hashes
  and can bind combined coverage and required checks to the same tree.

## Integration authorization and boundary

The user said continue after the coordination repair. The diagnostics owner
confirmed its stable snapshot and invited this integration in
`setup-verification-from-01a09113-20260912.md`. Preparation is complete; the
integration follows `design.md` and `implement.md`.

The coverage task owns affected-test verification and review. The diagnostics
owner owns whole-tree coverage and packaging after the readiness handoff.
Product-source issues must be sent to that owner. No live calls, bridge restart,
extension reload, product commit, or publication belongs to this coverage task.
