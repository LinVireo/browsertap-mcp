# Clear remaining BTAP defects and reconcile recorded bugs

## Goal

Inventory current project and knowledge-vault bug records, reproduce unresolved defects, fix and verify bounded slices in the original shared checkout, reconcile stale records, and update version/local tags when justified by completed checks; user is unavailable for manual extension Reload.

## Requirements

- Reconcile every remaining defect recorded for BTAP in the project and knowledge vault against the current source; retain provenance and distinguish already-fixed records from reproducible failures.
- Fix reproducible implementation defects with bounded ownership and meaningful regression coverage. Preserve authentication, explicit tab targeting, input ownership, and non-replay behavior after uncertain delivery.
- Complete the diagnostics/coverage handoff first and preserve its evidence as the pre-cleanup baseline.
- Coordinate with the other Codex session through Trellis research receipts and claims. Concurrent implementation uses isolated worktrees; shared browser/live resources have one owner. If the peer is inactive, attempt recovery or finish its bounded work locally.
- Run appropriate focused checks per slice, then the required whole-tree, static, package/install and available runtime verification against the final source identity.
- Update version and local tags when warranted by the final change. Preserve existing tags and sealed artifacts; no remote push or publication is authorized.
- Reconcile the project knowledge pages and Trellis task records using verified facts, without erasing historical decisions or changing unrelated knowledge pages.

## Acceptance Criteria

- [ ] A finite defect ledger covers the relevant project and knowledge-vault sources; every item has a current disposition and evidence.
- [ ] Confirmed source defects have fixes and passing regression checks; historical records identify the fixes that supersede them.
- [ ] Independent review findings are resolved or explicitly reported with reproducible evidence.
- [ ] Final offline tests, lint/type checks, tool documentation and package/full-install checks pass for the recorded source identity.
- [ ] Available runtime checks identify their actual loaded build. Any extension Reload or unavailable native-platform requirement remains visible and is not relabeled as passed.
- [ ] Version/tag decisions, commits, knowledge validation and residual limitations are recorded accurately.

## Notes

- The user may be unavailable for manual extension Reload. Preserve the repository's manual-reload protection; perform all independent work before reporting that runtime limitation.
- Existing formal acceptance gaps (desktop capability registry, native-platform runs, lifecycle delivery evidence) must be investigated separately from source defects. Do not weaken the contract or fabricate verification to clear them.
