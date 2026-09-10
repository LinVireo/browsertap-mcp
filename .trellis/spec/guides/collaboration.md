# Codex And Claude Code Collaboration

Trellis supplies shared task context and project conventions. The official
`codex@openai-codex` Claude Code plugin supplies Codex review and delegated work.

## Start And Scope

Read [AGENTS.md](../../../AGENTS.md), `AGENTS.local.md` when present, and
[the Trellis workflow](../../workflow.md). Use `trellis-start` for a new session
and `trellis-continue` for an active task. Record the task's intended files,
acceptance checks, and handoff evidence under `.trellis/tasks/`.

Default roles when Claude Code is available: Claude Code implements, Codex
independently reviews, and the current coordinating agent integrates verified
fixes. Claude Code is optional. With Codex only, run one Codex session as the
implementer and a second Codex session as the reviewer, or let one Codex session
dispatch `trellis-implement` followed by `trellis-check`. Explicit user
direction can change those roles. Review starts from the stated task and code
evidence.

## Concurrent Work

Give simultaneous writers separate Git worktrees and bounded file ownership.
Create new local checkouts in the operator's configured coding directory.
Use a clean reviewed baseline: a new worktree does not include this checkout's
uncommitted changes. Transfer only the intended patch when those changes matter.
Keep shared bridge/browser resources under one owner during live validation.

Trellis's `channel` CLI also supports live agent communication between Codex
workers. Use the `trellis-channel` skill when a task calls for live workers or
shared channels; the basic implement/review workflow does not require a
running channel. Two independent Codex sessions should still use separate
worktrees when both may write code.

## Review And Finish

In Claude Code, `/codex:review --background` requests a review;
`/codex:status` and `/codex:result` inspect that job. Specify the intended base,
commit, or change scope: a default uncommitted review may include earlier work.
Keep the review target stable until the result arrives, and validate each finding
against the current code before editing.

Run checks required by [CONTRIBUTING.md](../../../CONTRIBUTING.md). Record files
changed, commands and outcomes, open findings, and any untested behavior in the
handoff. Automatic journal/task commits are disabled in
[config.yaml](../../config.yaml); the coordinating agent follows the user's
commit and publication instructions. The plugin's automatic review gate remains
off unless the user asks to enable it.
