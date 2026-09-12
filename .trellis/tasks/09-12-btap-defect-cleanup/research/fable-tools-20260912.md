# F3 / F5 / F7 server logging / F8 implementation

Owner: btap_observation_fix (trellis-implement). Worktree:
D:/coding/btap-fable-tools-20260912, branch codex/fable-tools-20260912.
Base: f63c4a7b9e4db1235b1f0177fe5122054fcab7ae.

Only server.py, a private annotations helper and focused tests are owned here.
The original D:/browsertap-mcp source is frozen for the live owner. No live
calls, shared installation, Reload, version/stamp changes or commits.
Root owns integration, public documentation, review and final acceptance.

## Reproduction and design

- Actual FastMCP list_tools advertises 51 tools with annotations=None.
  Metadata must cover all parameter paths: scan_page(extra_js), wait_for(js),
  get_console_messages(clear), screenshot(save_path), native inspection markers
  and JS-result file export cannot inherit a read classification from their
  names or default parameters. Hints do not enforce approval or concurrency.
- Physical approval currently returns only bool and the MCP diagnostics omit
  its cause. Use a per-request immutable decision, explicit reasons and real
  MCP call_tool tests. Preserve safe/lab/cache, native ticket cleanup, physical
  lease, focus and quiet gates. External task cancellation must propagate.
- Server logs interpolate cleanup exceptions, an invalid environment value,
  permission/origin and physical approval summaries. Replace only logging
  payloads with fixed operation/category, reason and exception type. Keep the
  original tool error receipt.
- Lead execute_js and raw CDP descriptions with effects and targeting.
  Preserve async handles, uncertainty and complete file/JSON decoding rules.
  Root adds the separately reviewed F4 CDP method policy.

Baseline SHA-256/mtime are in out/fable-tools-20260912/baseline.json.
Red/green evidence and final handoff will be recorded in that same directory.

## Implemented and verified

- All 51 registrations now receive four explicit boolean ToolAnnotations.
  The private matrix covers caller JS in scan_page/wait_for, optional buffer
  clearing and output files. The same five mixed-effect tools are corrected
  in the internal capability inventory; get_setup_status's description now
  accurately describes its summary counts/groups, not nonexistent entry data.
- Immutable physical approval decisions expose reason=elicitation_unsupported,
  declined, timeout, cancelled or error. Actual MCP results retain reason at
  top level, in legacy and diagnostics. URL-only/no elicitation support is
  detected before prompting; METHOD_NOT_FOUND and NotImplementedError are
  distinct from ordinary errors. Strict boolean approval, session cache and
  lab/no-elicit shortcuts are preserved.
- Failed approval never reaches physical lease/quiet/focus/input. Native
  attempts still consume their ticket and clean the marker. If the ticket
  expires during approval, its original ticket-expiry error survives beside
  the approval reason. External cancellation propagates and cleans the claim.
- Server logging now uses fixed category/reason/exception type without raw
  cleanup exceptions, invalid environment values, permission/origin or action
  summaries. MCP primary-error content remains intact. Non-finite approval
  timeouts fall back to the bounded default.
- execute_js description shrank from 1623 to 1289 characters with effect/target
  guidance first and complete async, uncertainty and JSON-result contracts.
  CDP descriptions include ROOT's companion F4 guard (not implemented in this
  worktree): lab AND BROWSERTAP_ALLOW_UNSAFE_CDP=1; safe always blocks;
  raw_cdp_blocked is undelivered with retry_safe=false and retryable=false.

Evidence: initial red 58 failed/4 passed; native/approval group 342 passed;
MCP boundary/Unicode/registration group 368 passed; final reason/annotation/
logging/cancellation group 70 passed. These groups overlap and are not summed.
Ruff, mypy (19 shipped sources) and tool docs (51 tools) pass. The subsequent
inventory correction passed 80 focused tests with separate red/green evidence;
its first test attempt
used the wrong summary key and is retained as a harness error, not a bug proof.
Other transient Ruff/mypy corrections and a mistyped pytest path are retained
in out/. Only inventory-red-r2 is the valid inventory reproduction.

Root owns full integrated offline/build/install verification, independent
review, version/tag changes, public documents and the knowledge-vault update.
No source or shared installation in the live checkout was changed here.
