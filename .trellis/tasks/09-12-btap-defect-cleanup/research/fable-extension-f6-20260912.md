# F6: default MAIN-world dialog fingerprint

Owner: `btap_runtime_identity` (`trellis-implement`). Base:
`f63c4a7b9e4db1235b1f0177fe5122054fcab7ae`. Work is limited to the isolated
extension worktree, injection configuration, and directly related tests.

## Reproduction and scope

Executing the complete baseline `disable_dialogs.js` under Node changes the
identities of `window.alert`, `window.confirm`, and `window.prompt` immediately.
The wrapper source contains `activeScope`, even before an automation command.
`onbeforeunload` is unchanged. **No `__btap_*` window property is created by the
default script alone.** The command preamble creates the scope/record/mirror
properties; they remain after command cleanup. These are two separate defects.

Chrome's current `RegisteredContentScript` reference lists `allFrames`, `css`,
`excludeMatches`, `id`, `js`, `matchOriginAsFallback`, `matches`,
`persistAcrossSessions`, `runAt`, and `world`. It has no tab filter. Source read
2026-09-12: https://developer.chrome.com/docs/extensions/reference/api/scripting#type-RegisteredContentScript
Therefore dynamic URL registration cannot implement per-tab consent. This fix
uses the existing `chrome.scripting.executeScript` target instead.

## Implementation plan

- Remove the default MAIN-world content script entry. Keep the isolated content
  script and all manifest identity fields unchanged.
- Load a self-contained dialog scope helper in the worker. Embed it in answering
  command builders only; `manual` and unmarked monitoring keep native dialogs.
- Store scope records and original property descriptors in a temporary
  controller closure. Release the exact lease in `finally`; release each command
  token in all injected frames before sending its outcome. Overlapping scopes
  remain independent, including out-of-order completion.
- Arm an expiry timer and also check deadlines at each dialog call. The last
  scope restores owned dialog descriptors and removes its controller property.
  Preserve a function the page replaced while a scope was active.
- Do not change `onbeforeunload`, navigation protocol handling, manual debugger
  ownership, execution replay decisions, or timeout budgets. A newly navigated
  document starts without MAIN-world hooks.

This is removal of default interference, not an anti-tampering claim: a page
can observe or modify its own MAIN world during explicit automation. No domain
blacklist, random property name, or manifest key is involved.

## Verification contract

New Node tests evaluate the **complete** worker and its real `importScripts`
dependencies, then execute the emitted JavaScript in page/frame VM realms.
Chrome APIs are explicit offline fakes; they never connect to the local bridge
or browser. Cover default identities, both answering policies, manual/monitor,
success/error cleanup, overlap, TTL including throttled timers, descriptor
restoration, frame cleanup, new-document isolation, and existing CSP/CDP routing.
Old source-slice tests remain supplementary, not F6's behavioral evidence.

Evidence goes under `out/fable-extension-20260912`. Root owns build-stamp
regeneration, documentation reconciliation, final release gates and live tests.
The original tree and post-Reload browser verification remain untouched.

## Implemented result

The answering helper is now loaded by the worker and serialized only for an
answering execution. A temporary `__btap_dialog_controller` coordinates active
leases. Its closure holds original descriptors and at most 50 records per lease.
The top-frame builder releases its lease in `finally`; worker cleanup releases
the token across frames before publishing the result. That cleanup has a 1-second
response cap, so a lost cleanup response cannot hide an already-known outcome.
The frame-local deadline timer and dialog-call check remain the fallback.

Descriptor handling covers normal data properties, accessors and inherited
functions. Failed installation rolls back earlier wrapper writes before caller
code executes. A conflicting page property is preserved and causes an explicit
error. A page's later replacement of a dialog function is retained at cleanup.
Duplicate-token leases and out-of-order release do not remove unrelated scopes.

Validation: 30 complete-worker Node behavior cases passed; the final related
union passed 423 tests. `scripts.lint_report` is clean with Ruff, ESLint and mypy
all enforced; Node syntax and `git diff --check` also passed. The initial 20-case
regression failed against the baseline; final-source red/green evidence and
per-file hashes are exported in the handoff directory.

## Remaining runtime boundary

There is no live-browser result for this patch. Node VM realms exercise the
actual JavaScript but do not establish Chromium document-start scheduling,
native dialog UI or real cross-origin frame behavior. Existing all-frame
dispatch ordering and manual/navigation protocol handling are unchanged.

An extension Reload cannot remove an old MAIN-world wrapper already installed
in an existing document. Its original function is hidden in the old IIFE's
closure; replacing it with a guessed native could overwrite page behavior.
Those documents need ordinary navigation/refresh after the extension update to
get the new default. This patch does not reload or navigate user tabs. Root must
regenerate the stamp after integrating all extension changes and keep the
manual extension Reload/live-verification boundary explicit.
