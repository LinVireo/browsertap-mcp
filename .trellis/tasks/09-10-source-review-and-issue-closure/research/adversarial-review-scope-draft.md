# `/codex:adversarial-review` scope and baseline (drafted 2026-09-10, not sent)

Ready to send the moment the implementing session commits its batch. Everything
below is measured against this worktree, not assumed from the command's docs.

## Send precondition

The batch under review is **uncommitted** as of drafting: `git diff --shortstat`
= 51 files, +4227/-2139, and `HEAD` is still `de0ed83`
(`docs: split maintainer guidance into task-scoped references`, 2026-09-09
00:08:02 +0800). Sending now would review the wrong thing -- see the scope
measurement below for why working-tree mode is the wrong target here.

So: wait for the commit, then re-read `HEAD` before substituting the base ref.
`de0ed83` is only correct while nothing else lands. If the batch arrives as
several commits, the base stays `de0ed83`; it is the merge-base that matters,
not the count.

## The command

```
/codex:adversarial-review --background --base de0ed83 <focus text below>
```

Three arguments, each for a measured reason.

**`--base de0ed83`, not `--scope working-tree`.** In working-tree mode the
companion inlines every untracked non-ignored file whole, up to 24 KB each
(`scripts/lib/git.mjs:197-210`, `MAX_UNTRACKED_BYTES = 24 * 1024`). This tree
has **226** such files: 87 under `.trellis/`, 52 `.claude/`, 46 `.agents/`, 36
`tests/`, 4 `src/`, plus `BUGREPORT-2026-09-08.md`. That payload includes my own
defect analyses -- handing the reviewer my conclusions is the one thing an
independent review must not start from. Branch mode never touches untracked
files at all (`collectBranchContext`, `:262-290`).

**`--background`, not `--wait`.** The command's own rule is to wait only for
roughly 1-2 files. `main...HEAD` is 107 files / +17637-3559; even sol's batch
alone is 51 files.

**Do not expect the diff to be inlined either way.** `includeDiff` requires
`<= 2` changed files *and* `<= 256 KB`
(`DEFAULT_INLINE_DIFF_MAX_FILES = 2`, `:8-9`). Both scopes here blow past that,
so the payload degrades to commit log + diff stat + changed-file list, with the
prompt telling Codex to read the diff itself via read-only git commands. That is
fine -- it has the sandbox to do it -- but it means the **focus text is what
actually steers the review**, since it decides which files get opened. The
prompt weights `{{USER_FOCUS}}` heavily by construction
(`prompts/adversarial-review.md`, `<review_method>`).

## Focus text

One argument string, so it needs outer quotes on the command line and no inner
double quotes. Four angles, each chosen because it is checkable from the diff and
because **I did not verify it** in my own review pass:

> Focus on the reservation and command-envelope work in
> src/browsertap_mcp/pending_operations.py, src/browsertap_mcp/server.py and
> src/browsertap_mcp/browser_bridge.py. Four specific questions. First: a silent
> operation is now abandoned after a bounded grace window and its target
> reservation is released -- trace what happens when the browser's real reply
> arrives after that release, and whether a second operation can already hold the
> same target when it does. Second: observe_manual_execution admits a caller only
> when the recovery kind is handle_dialog, the requester matches, and the
> operation carries pending_execution -- all three guards are present, so the
> question is the resolution step inside the target loop, which reads the
> target's current holder rather than the operation that was pending when the
> recovery began; find an ordering of stacked or reused dialog recoveries where
> the current holder is not that operation and the requester still matches. Third: a
> recent fix split the extension command envelope into cmd and code fields and
> repaired four tools that were reading the wrong one -- enumerate every other
> caller of that envelope and say whether each reads the field its route actually
> populates. Fourth: capture bookkeeping now removes an entry once a terminal
> reply is seen -- show whether a non-terminal or duplicate reply can leave an
> entry that is never removed. Report the strongest defensible finding per
> question; say so plainly where the diff does not let you answer.

## Facts the reviewer may re-check but should not spend budget re-deriving

State these as context, not as conclusions to accept -- the prompt's stance is to
disprove, and these are exactly the kind of claim worth attacking:

- Independent full offline run on this batch: **2630 passed, 61 deselected,
  exit 0** (427.33s).
- The zero-length expiry window is closed: an `outcome_unknown` operation is
  still held at +69s and reports `abandoned` /
  `unknown_outcome_reservation_ttl` with the reservation released at +71s.
- `_prune()` runs before `TargetBusyError` is raised, and `read()` reports
  `reservation_held: False` for an abandoned operation while keeping
  `wire_result`.
- Mutation-checked: `tests/test_silent_operation_expiry.py` is the sole reader of
  the anchoring logic; two independent mutations fail 2 and 1 tests there while
  81 tests pass in the surrounding suites.

## Out of scope, and one thing already filed

- Everything under `.trellis/`, `.claude/`, `.agents/` is process bookkeeping,
  not the change. Branch mode keeps it out of the inlined payload, but Codex has
  read access to the worktree and may still open those files. A finding that
  restates a note found there is not an independent finding -- check provenance
  before counting it.
- The **`open_new_tab` recovery dead-end is already filed** with a patch design
  in `open-new-tab-recovery-deadend.md` in this directory. A rediscovery is not
  new information. Its zero-coverage branch (`server.py:3292`) predates this
  batch.
- `main...HEAD` carries 38 commits ahead of `origin/main`, most already reviewed.
  That is the reason for `--base de0ed83` rather than a branch-wide sweep.

## Interpreting the result

Review-only by contract: the command must not apply patches, and its output is
returned verbatim. Findings arrive as JSON with `line_start`/`line_end` and a
confidence score. Treat a finding as actionable only after reproducing it here --
the same rule that applies to any claim crossing between the two sessions, and
the reason `.trellis/agent-claims.json` says a claim is not proof.

