# R6 successor integration

User authorization: “按你建议”, continuing the proposed integration, wait investigation, and remaining acceptance work.

## Scope

- Base: sealed r6 commit 44a092fc2ab1ab27a418e61a86b3d538b1f99a5c.
- Import committed F1/LOW-5 candidates and the small cross-platform prerequisites after reviewing their diffs.
- Review and reproduce the attributed read-only wait-probe release proposal; implement only a verified release boundary.
- Diagnose the original delayed wait reply separately from reservation retention and the already-fixed navigation deadline.
- Complete available Windows browser validation and prepare exact A7/macOS/Linux acceptance steps and evidence gaps.

## Acceptance

- I1: F1 retains one authenticated late terminal result without replay, reacquiring targets, changing the unknown receipt, extending retention, or affecting successor capture/recovery state.
- I2: LOW-5 cleans up only the failed socket's state and preserves replacement WebSocket/HTTP sessions and pending operation ownership.
- I3: r6 accepted-beforeunload navigation obeys the caller's remaining deadline; its regression remains passing.
- W1: A timed-out server-owned read-only probe releases its target without losing its receipt; arbitrary caller JS remains conservative.
- W2: Late completion cannot release a successor's target; unproven transport delay remains explicitly unproven.
- V1: Fresh focused/offline, lint/type, documentation, distribution/install and Windows live evidence identify this integration tree.
- V2: Independent review covers the new recovery semantics and goal alignment.
- M1: A7 and native macOS/Linux browser evidence are completed only where the required environment and observation are available.

SPEC trace: R2/R3/R5/R6/R7/R9 and A1-A7, preserving rev7 delivered_stub until its actual acceptance gaps close.

## Boundaries

Original r6 files and sealed artifacts remain unchanged. The cross-platform candidate worktree is read-only, including another session's staged navigation patch. No push, tag or publication. Preserve all unrelated author changes. Live resources have a separate exclusive claim in the original checkout.
