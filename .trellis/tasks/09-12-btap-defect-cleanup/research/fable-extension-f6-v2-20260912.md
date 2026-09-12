# F6 v2: frame preparation and execution boundary

Owner: `btap_runtime_identity`. Base: the frozen F6 v1 patch, on
`f63c4a7b9e4db1235b1f0177fe5122054fcab7ae`. This is an incremental offline
handoff to the coordinating agent; version, stamp, release and live validation
remain with that agent.

## Reproduced defects

- The reviewer's complete-worker probe called the same-origin child's
  `confirm()` from the top frame. F6 v1 passed with child-first scheduling, but
  top-first scheduling and CSP-to-CDP returned native `true`, with one native
  child dialog. Chrome does not guarantee child injection precedes the top.
- A caller that first increments a counter and then throws an `EvalError`
  containing CSP text ran three times in F6 v1: normal injection, CSP-relaxed
  injection and CDP. Both synchronous and top-level-await callers reproduced it.

## Changes

- Install the self-contained dialog helper directly with `chrome.scripting` in
  every target frame, and require serializable acknowledgements with matching
  token, policy and expiry plus exactly one top-frame marker before dispatching
  caller code. This installation does not use page `eval`.
- The top builder adopts its prepared lease, so its own `finally` still restores
  top-frame descriptors even when the worker's frame cleanup fails. Subframes
  retain only the original bounded scope until token cleanup or TTL expiry.
- Fix one worker deadline before preparation. Frame installation, scripting,
  CSP waiting, CDP attachment/evaluation and cleanup spend its remaining time.
  Late installation and late caller delivery cannot restart the scope or run a
  caller after the deadline. The existing scope grace remains a cleanup fallback.
- Keep preparation failures explicitly undispatched. Once a caller injection has
  been dispatched, missing results, API rejection and timeout remain unknown and
  are not replayed. The actual eval guard's `started()` evidence and the validated
  AsyncFunction dispatch boundary prevent caller-thrown CSP text from causing a
  retry; verified CSP failures before caller execution still fall back normally.
- Extend the full-worker API harness with independent frame scheduling, clock
  progression, serialization and transport fault injection. Require a completion
  receipt so a pending promise cannot silently pass when Node exits.

## Verification

Evidence is under `out/fable-extension-20260912/f6-v2/`:

- `red-final-v1-junit.xml`: the same final new regressions/harness against the
  frozen F6 v1 source: **19 failed, 4 passed**, no skips or collection errors.
- `green-final-candidate-junit.xml`: full worker regressions: **53 passed**,
  including all 30 F6 v1 behaviors and 23 preparation/execution cases.
- `related-final-junit.xml`: **612 passed**, including dialog policy, execution
  results, serialization, CDP budgets, CSP lifecycle, manual/debugger lifecycle,
  navigation, phase0 failure contracts, zombie handling, distribution and docs.
- `lint-final.json`: Ruff, ESLint and mypy clean and enforced.
- `frame-order-final-v1.json` / `frame-order-final-v2.json`: the original review
  input, with only the harness path parameterized. Both failing schedules become
  `false` with zero native child calls.
- `evalerror-runs-v1.json` / `evalerror-runs-v2.json`: sync and async callers go
  from three executions and one CDP call to one execution and zero CDP calls.

The v1 artifacts and source reference remain byte-frozen. This handoff does not
touch the root or live checkout, reload the browser, refresh user pages, reinstall
the package, commit, tag, publish or regenerate the extension stamp. Ordinary
pages and subsequently navigated documents still have native dialog functions;
the preparation acknowledgements describe the injected documents, not a
permanent hook for future frames or navigation.
