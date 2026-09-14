# AGENTS.md

Implementation entry point for agents changing this repository. General setup,
coding checks, and release gates live in [CONTRIBUTING.md](.github/CONTRIBUTING.md)
([简体中文](.github/CONTRIBUTING.zh-CN.md)). Public tool parameters remain in the
`## Tools` table in [README.md](README.md) and
[README.zh-CN.md](README.zh-CN.md).

Read the detailed guide for the area being changed before editing that area.
The guides preserve implementation constraints and the failures behind them;
routine tasks do not need every guide.

## 1. Three processes, three different ways a change takes effect

Read [runtime lifecycle](docs/agent-guides/runtime-lifecycle.md) before changing
startup, process state, extension identity, or injected page scripts.

- MCP Python changes need a fresh MCP process; bridge changes need a bridge
  restart; extension source changes need the browser's manual extension reload.
- Use the reported action and build-stamp verdict to identify stale components.
  A matching version number alone does not prove matching code.
- Preserve the manual-reload and self-disable guards. Page scripts are separate
  from extension code and keep bare entry-point references, not calls.

## 2. Changing a tool signature or a default touches four places

Read [tool contracts](docs/agent-guides/tool-contracts.md) before changing any
public tool name, parameter, default, behavior, or packaged caller guidance.

Update the tool description, both README tool tables, and both caller Skills:
- `src/browsertap_mcp/skills/browsertap-default/SKILL.md`
- `src/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md`

Run `python -m scripts.check_tool_docs`. Installed-copy comparison is a separate
check and requires an actual mirror directory.

## 3. Tab ids are not stable -- never remember one

Read [tabs and input](docs/agent-guides/tabs-and-input.md) before changing target
resolution, reconnect handling, lifecycle generations, or tab cleanup.

Explicit targets must not silently move to another tab. An omitted dead default
can be reselected according to the existing policy. Preserve ownership and
generation evidence; an unreadable generation store is not an empty store.
Tests use synthetic identities rather than real tab IDs.

## 4. Physical input acts on the *visible* tab

Read [tabs and input](docs/agent-guides/tabs-and-input.md) before modifying input,
focus, hit testing, quiet-input checks, or debugger attachment.

Keep page-level CDP input in the selected background tab. The remaining physical
fallback needs the intended tab visible on screen. Preserve the single-batch
focus round trip and selector hit test. After an uncertain dispatch, inspect
state instead of replaying the input.

## 5. MV3 keepalive and alarms

Read [runtime lifecycle](docs/agent-guides/runtime-lifecycle.md) before changing
worker liveness, timer cadence, alarms, or generation persistence. Keep the
live-worker timer and post-collection recovery mechanisms distinct; preserve
the recorded timing and startup failure cases.

## 6. The HTTP port is token-authenticated

Read [transport authentication](docs/agent-guides/transport-auth.md) before
changing authentication, rejection responses, socket ownership, or reconnect
grace periods. [SECURITY.md](.github/SECURITY.md) and
[troubleshooting](docs/TROUBLESHOOTING.md) remain the operator references.

Drain rejected HTTP request bodies before returning an authentication error.
Preserve client-to-socket ownership and redact browser data at logging sites.

## 7. Running the tests

Read [validation contracts](docs/agent-guides/validation.md) when changing tests,
preflight, lint coverage, artifact validation, or browser cleanup.

For changes limited to this guide and its references, start with:
`python -m pytest tests/test_documentation_contract.py -q`.
If a source-distribution reference moves, update `MANIFEST.in` and inspect a
fresh source archive for every referenced guide.

For code changes, run the affected tests and the checks required by
[CONTRIBUTING.md](.github/CONTRIBUTING.md). Run the release finalizer only when preparing
a release candidate. Preserve existing sealed artifacts.

Live tests must identify the running build and clean up only tabs they own.
User tab activity is context, not permission to close those tabs.

## 8. Odds and ends

The detailed notes are grouped by the implementation they constrain:
- Process stubs, spawn locks, log rotation, and worker eviction:
  [runtime lifecycle](docs/agent-guides/runtime-lifecycle.md).
- Logging redaction and rejected connections:
  [transport authentication](docs/agent-guides/transport-auth.md).
- Debugger attachment and input recovery:
  [tabs and input](docs/agent-guides/tabs-and-input.md).
- Lint target coverage, LF line endings, and complete evidence file sets:
  [validation contracts](docs/agent-guides/validation.md).

## 9. Machine-specific notes

Anything that only makes sense on one machine -- absolute paths, which browser
profile is in use, how a particular skill manager is wired -- belongs in an
untracked `AGENTS.local.md`, not here. This file must stay valid for someone who
just cloned the repository, and `python -m pytest tests/test_documentation_contract.py`
fails if an absolute path leaks into a published document.

When that file exists, read it for local interpreter, manager, or live-test setup.
