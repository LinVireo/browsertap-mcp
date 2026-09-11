# Setup diagnostics without a completed extension handshake

## Goal and scope

Repair the false `stale_extension` result observed after restarting the bridge.
Work in the original checkout. Keep this change local to setup diagnostics and
their callers; preserve the extension's manual reload guards and wait behavior.

## Evidence and decision

A deterministic offline probe on `db11d22` reports
`reload_extension_required=true` for `starting`, `ext_never_registered`, and
`sw_slept_or_dropped` when `bridge_status` cannot obtain any extension response.
The initial activation evidence records the same transition from `starting` to
`stale_extension`, followed by recovery without a reload.

Track whether extension runtime information was actually obtained. An absent
response remains unknown; a valid legacy response missing required fields still
supports the existing compatibility checks. Publish an unavailable connection
state after the startup grace period. Keep confirmed bridge/MCP version skew
ahead of waiting, and allow a successful fallback probe to complete startup.

## Acceptance

- Missing runtime status never alone requests an extension reload, during or
  after startup; it never reports healthy either.
- A successful fallback probe can recover from the earlier starting snapshot.
- Real version, protocol, capability and worker stamp mismatches retain their
  current recovery actions, including legacy responses with absent fields.
- MCP result envelopes and live preflight preserve the new diagnostic state.
- Tool description, both READMEs, both packaged Skills and changelog agree.
- Focused regression checks, required offline/lint/docs/version/package checks
  pass; fresh MCP verification identifies the checkout and observed behavior.

## Ownership and limits

Owner: `codex-resume-01a0881c`; exact paths are in `agent-claims.json`.
Recheck hashes before edits and acquire the live slot before bridge operations.
Other agents' records and worktrees remain under their current ownership.
Record evidence under `out/setup-diagnostics-20260911` and an independent
research handoff here. No release finalizer, push, tag or publication.

Python same-version source identity is an investigation item. Historical wait
latency needs measured evidence and is not claimed fixed by this change.
The existing rev7 `delivered_stub` and native-platform gaps remain unchanged.

## Completed baseline

Implementation `21ad974` and accepted coverage integration `e631522` complete
this setup-diagnostics slice. Root's combined-r2 verification records 3081
offline passes, unchanged validation inputs, required static/documentation and
package checks, and a full dependency installation proving the CLI. Evidence:
`out/setup-diagnostics-20260911/combined-r2/verification.json`. The fresh MCP
observation is recorded separately; the current host session was not reconnected.
Remaining source-identity work and runtime acceptance limits continue under
`09-12-btap-defect-cleanup` and do not reopen this completed baseline.
