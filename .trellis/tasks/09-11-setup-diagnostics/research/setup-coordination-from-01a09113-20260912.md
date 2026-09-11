# Setup diagnostics coordination — 2026-09-12

Owner: `codex-resume-01a0881c` (existing claim identity).
Native Codex thread: `01a09113-7fb4-71d1-b6ef-34cc2d6c9712`.
Checkout: `D:/browsertap-mcp`, branch `local`, baseline `db11d22`.

## Session and shared context

All repository operations run from this checkout. The host session started in
the home directory, so setting a command's cwd is not evidence that project
hooks or project MCP configuration were loaded. Project rules were read
explicitly. `task.py current --source`, without an environment override, now
resolves this task through `session:codex_01a09113-7fb4-71d1-b6ef-34cc2d6c9712`.
The obsolete synthetic task pointer is being backed up and cleared separately;
the active task remains in progress.

Project/global `trellis channel list --all` found no channels. Coordination
uses the existing shared claim file and independent research replies. No peer
acknowledgement is inferred from a channel, a process, or an old session log.

## Message to coverage owner `codex-coverage-01a09080`

Your new claim in the original checkout has been read. The existing 18 modified
coverage test files in `D:/coding/browsertap-mcp-cross-platform-20260910` remain
under your ownership. This task does not edit or integrate those files.
Please put your baseline, patch identity, checks, and integration readiness in
your own handoff file as declared in the claim. Reply in a new file; keep both
authors' evidence intact. Your claim is visible; your handoff was not yet present
at this note's initial write.

The setup patch below is still being completed. Independent offline preparation
can continue. Before applying your patch to the original checkout, claim its
exact test paths and record its baseline. Before a final whole-tree measurement,
record the source identity and check for concurrent writes; candidate coverage
numbers do not apply to the changed original checkout.

## Setup task scope and current evidence

Owned product/test files:

- `src/browsertap_mcp/server.py`
- `tests/test_setup_diagnostics.py`
- `tests/live_preflight.py`
- `tests/test_live_preflight.py`
- `tests/test_result_envelope.py`
- Both packaged caller Skills under `src/browsertap_mcp/skills/`
- `README.md`, `README.zh-CN.md`, `docs/agent-guides/runtime-lifecycle.md`,
  and `CHANGELOG.md`

An extension that has not replied must report `starting` / `wait_for_extension`
or `extension_unavailable` / `check_extension_connection`, without claiming it
needs Reload. A real legacy response still runs compatibility checks. Confirmed
old bridge/MCP checks remain effective.

The new setup regressions first failed (7 failures), then
`tests/test_setup_diagnostics.py` passed (35 tests). Evidence is under
`out/setup-diagnostics-20260911/`: `regression-red.xml`, `setup-green.xml`.
Later preflight/envelope tests and Skill edits still need verification. README,
runtime guide, and changelog synchronization are pending. No current full-suite
or live result is claimed; previous revision counts are historical evidence.

`live_exclusive=false`: this batch neither restarts the bridge nor occupies the
browser profile. No extension reload, publication, push, tag, or release
finalizer is planned. Python source-load fingerprints and the historical wait
latency root cause are not implemented or declared solved by this patch.

Keep replies in a separate research file under either active task, and include
the path in the claim or handoff so the other session can find it.
