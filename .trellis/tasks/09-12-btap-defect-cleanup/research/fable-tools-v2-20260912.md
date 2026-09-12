# Tools v2 increment: site approval reasons and setup effects

This increment depends on the earlier `fable-tools-20260912` delivery. Its
artifacts are preserved; this note describes only changes after that baseline.

## Changes

- Site-permission and physical-input approval now use one immutable
  `_ApprovalDecision` and one form-approval implementation. An unsuccessful
  approval records `elicitation_unsupported`, `declined`, `timeout`, `cancelled`
  or `error`, including through the real MCP adapter's top-level, `legacy` and
  `diagnostics` fields. The site tool keeps `requires_user_action` as its error
  code and never dispatches a permission grant after an unsuccessful approval.
- Form capability detection supports legacy `elicitation={}` and modern
  `form={}`. Missing or URL-only capabilities do not receive a form request.
  `METHOD_NOT_FOUND` and `NotImplementedError` mean unsupported; malformed or
  non-boolean acceptance is an error. Task cancellation propagates and restores
  an explicit temporary target. Logs contain fixed categories, reasons and
  exception types rather than permission origins or raw exception payloads.
- Safe mode still asks on every allow. Lab's explicit no-elicitation mode and
  per-session cache remain; physical and site approvals use separate caches.
  `block` and `ask` do not require an allow approval. Lease creation, duration,
  normalization and restoration behavior are unchanged.
- F2's Windows token diagnostics can tighten an existing token file ACL.
  `get_setup_status` therefore advertises `(readOnlyHint=false,
  destructiveHint=false, idempotentHint=true, openWorldHint=true)` and its
  coarse capability-inventory effect is `mixed`. Ordinary read tools retain
  their read-only hints; routine cache/transport activity does not imply a
  business-state write. The setup description discloses ACL hardening.

## Evidence

- New reason/effect regressions against v1: **42 failed, 26 passed**. These
  include missing reason fields, absent form-capability checks and the old
  read-only setup hint.
- Combined site, physical, native-dialog, reason, log and annotation tests:
  **404 passed** on asyncio/trio where applicable. The successful check receipt
  confirms all recorded source hashes stayed unchanged during execution.
- Focused Ruff: passed. `mypy src`: passed, 19 source files. Tool-document
  check: passed, 51 registered tools. `git diff --check`: passed.
- Offline MCP inventory: 51 tools; true hint counts are read-only 10,
  destructive 37, idempotent 12, open-world 48.

The checks mock permission dispatch and exercise the actual FastMCP adapter.
They make no live grants, browser calls, bridge restarts or extension Reloads.
Native token ACL behavior belongs to the F2 worker/root tree and was inspected
to classify setup effects; this tools-only checkout does not contain F2.

## Integration

Apply only `out/fable-tools-20260912-v2/changes-v1-to-v2.patch` after tools v1.
Its manifest binds every changed file to v1 and v2 SHA256 digests. The earlier
v1 patch, handoff, file manifest and evidence remain unchanged.

Root owns both-language README/caller-guide integration, whole-tree/release
gates, version/tag decisions and any runtime acceptance. Add the same site
approval reasons and the setup ACL effect to that documentation. No version,
shared installation, live state, commit, tag or knowledge-vault page was changed
by this increment.
