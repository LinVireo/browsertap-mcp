# Changelog

Notable user-facing changes to `browsertap-mcp` are recorded here. This file
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.1] - 2026-08-22

### Security

- The bridge log no longer records page URLs verbatim. `bridge.log` lives for the
  life of an install and is the file operators are asked to attach to a bug
  report, so an OAuth callback, a signed download link or a search query used to
  be written to disk and handed out with it. URLs are now redacted at the logging
  call rather than by a scrubber afterwards: scheme, host and a truncated path
  survive, query strings and fragments become `?...`/`#...`, credentials in the
  authority are dropped, and `file:`, `data:`, `blob:` and `javascript:` URLs are
  reduced to `<scheme>:<redacted>` because for those the location is the content.
  A caller's `wait_for_url` pattern gets the same treatment. What the log may and
  may not contain is now stated in `SECURITY.md`, and the offline suite scans the
  module for a log line that passes a URL straight through.

### Fixed

- An automatic tab pick no longer lands on a page Chrome refuses to script. The
  extensions gallery is an ordinary `https://` page, so a content script
  registers a session there and the tab joined every automatic pick like any
  other, while each injection came back `The extensions gallery cannot be
  scripted.` with nothing dispatched -- reported as a bridge fault rather than a
  bad target. Both automatic paths (the implicit default and the failover escape
  hatch) now skip gallery, `chrome://`, `edge://`, `devtools://`, `view-source:`
  and extension pages, on the same terms as the settle filter: a browser showing
  nothing but such pages behaves exactly as before rather than refusing, and a
  tab the caller names explicitly still gets the real error.
- `page_click` proves in the page that the point it is about to click belongs to
  the target, instead of dispatching and reporting success either way. A cookie
  banner, a modal backdrop or a sticky header over the element left
  `Input.dispatchMouseEvent` returning success while the click went to the
  overlay, and an element below the fold was clicked at negative viewport
  coordinates -- neither was distinguishable from a real click in the result. In
  selector mode the point is now hit-tested with `document.elementFromPoint`
  inside the same resolver round trip: a target below the fold is scrolled into
  view once and re-probed (`scrolled_into_view`), a point owned by something else
  refuses with `obscured` and `occluded_by` naming it, and a point still off
  screen refuses with `outside_viewport`. Both refusals dispatch nothing and carry
  a `next_action`. A verified click reports `hit_verified: true`. Coordinate mode
  is unchanged: coordinates name a pixel, not an element.
- The bridge rotates its own log while it is running. The 5 MB cap was only ever
  checked when a bridge was spawned, and the handle opened there becomes the
  daemon's stdout for the whole life of the process -- so a bridge that stayed up
  for weeks grew its log without bound and restarting it was what appeared to
  "fix" the size. The daemon now checks every five minutes and rotates in place
  by copying to `bridge.log.old` and truncating its own descriptor, which is what
  Windows allows on a file that still has an open handle.
- A `401` from `/link` can be diagnosed without guessing. `get_setup_status` and
  `browsertap doctor` report `state_paths` -- the `state_dir`, `token_file`,
  `auth_enabled` and a truncated `sha256:` token fingerprint that the reporting
  process actually resolved, plus whether the directory came from the
  environment, the default, or a pre-0.4.0 `~/.agent-browser-mcp` -- and the
  running daemon reports its own. When the two disagree the result adds
  `state_paths_disagreement`, naming each differing field for `this_process` and
  for `bridge`, and separates the two causes that need different fixes: a daemon
  holding a token from before the file changed needs one bridge restart, whereas
  differing paths mean the processes have different environments and a restart
  will not help. The token value itself is never printed.
- The VS Code snippet in `README.md` registered `command: browsertap-mcp`, which
  is the distribution name and not an executable; the console script is
  `browsertap`.

### Added

- `README.md` opens with a three-command install, and says plainly that step 2 --
  loading the unpacked extension by hand -- is manual and is the slow one,
  because there is no Web Store listing yet.
- The offline gates run on Windows as well as Linux. Every OS-specific failure
  this product has is a Windows one (a rejected request with an unread body being
  reset by wsgiref, the `SO_EXCLUSIVEADDRUSE` host lock, `pythonw.exe`'s
  forwarding stub, a minimised window that accepts focus calls, a log file that
  cannot be renamed while it is open), and `bridge.py` -- where most of that
  lives -- is the least covered module in the package.
- `trio` is a declared development dependency. anyio's pytest plugin parametrises
  over the backends it can import, so whether trio was installed silently decided
  whether 51 `[trio]` variants of the async tests existed at all; MCP's server
  stack is anyio's, so a client may well host it on trio. The suite now asserts
  both backends are present rather than passing with half the matrix missing.
- `scripts/check_install.py` answers the question `check_distribution` cannot:
  not what is inside the archive, but what a stranger has after `pip install`.
  The wheel goes into a throwaway environment with no repository on the path and
  the console script, the packaged skills and the extension files are exercised
  there. It runs in the offline gates, in the release workflow before publishing,
  and in `finalize_change` (layout only there, since that has to work offline).
- `scripts/check_release_tag.py` fails a release whose git tag names a different
  commit than the tree being published, before anything is installed or built.
  A TestPyPI rehearsal may run from an untagged branch, so there a missing tag is
  a note while a misplaced one is still a failure.
- The release workflow audits the dependency closure the wheel actually installs
  against the advisory database and uploads a CycloneDX SBOM of it, as a separate
  artifact rather than into `dist/`. A new `supply-chain.yml` runs the same audit
  on a schedule, informationally.
- The live suite checks its own preconditions instead of relying on the reader.
  It samples the tab list twice before the first live test and skips the whole
  layer when the browser is in use, since a tab that is still loading makes the
  failover test fail for reasons that have nothing to do with the change under
  test; a skip is a live-gate failure in the acceptance report rather than a
  pass. At the end it compares the inventory again, so a fixture that leaks a tab,
  closes one of the user's, or navigates the page they were reading fails in
  teardown. Both verdicts are written to `artifacts/live-preflight.json` and
  uploaded with the junit results. `BTAP_LIVE_ALLOW_BUSY_BROWSER=1` overrides the
  skip and is recorded in the report.
- `_pick_failover_session` skips a tab that registered within
  `FAILOVER_SETTLE_SECONDS`, which is the product-side half of the same problem:
  the escape hatch used to pick the most recently registered tab, and a tab that
  is mid-navigation loses its CDP debugger with no retry behind it.

## [0.4.0] - 2026-08-21

### Changed

- Renamed the project from `agent-browser-mcp` to `browsertap-mcp`. The
  distribution is `browsertap-mcp`, the Python module is `browsertap_mcp`, and the
  console script is `browsertap`: the distribution name carries the `-mcp` suffix
  that an index search filters on, while the command drops it, because the command
  is the part typed daily and this package is already more than an MCP server --
  `browsertap bridge --restart`, `browsertap doctor` and `browsertap skill-path`
  are not MCP calls. Nothing had been published to PyPI under the old name, so no
  release is being superseded and no name is being burned; the module was renamed
  with the distribution so that an installed `browsertap-mcp` is never imported as
  something else.
- MCP clients register the server as `browsertap` running `command: browsertap`.
  The shipped examples, the README snippets and `browsertap print-hermes-config`
  all agree on that spelling now. An existing client entry keeps pointing at the
  old executable name and has to be re-added; the tool names a client derives from
  its own entry key change with it.
- `AGENT_BROWSER_TMWD_HOST` and `AGENT_BROWSER_TMWD_PORT` are now
  `BROWSERTAP_BRIDGE_HOST` and `BROWSERTAP_BRIDGE_PORT`. They were named after the
  driver class this release deletes, so carrying `TMWD` into the new namespace
  would have preserved a word that no longer names anything. Every other variable
  is a straight prefix swap, `AGENT_BROWSER_` to `BROWSERTAP_`.
- All pre-0.4.0 `AGENT_BROWSER_*` variables still work. `browsertap_mcp.paths`
  copies each one onto its new name when the new name is unset, and the package
  `__init__` runs that before any submodule is imported -- which is the only point
  early enough, because `server` reads the bridge host and port at *import* time
  and an entry-point-level call would run after the value it is meant to supply
  had already been read.
- Per-user state moved from `~/.agent-browser-mcp` to `~/.browsertap`. An install
  that predates the rename keeps using the old directory as long as it exists and
  the new one does not, so the persistent bridge token stays valid and a running
  daemon and a fresh server do not end up authenticating against different files.
  The old directory is used in place, never renamed: a live bridge holds
  `bridge.log` open and Windows refuses to rename a directory containing an open
  handle, so a migration-by-move would fail in exactly the case that matters.
- `doctor` reports `bridge_host`, `bridge_ws_port` and `bridge_http_port` instead
  of `tmwebdriver_host`, `tmwebdriver_ws_port` and `tmwebdriver_http_port`. Same
  values, named after the component that still exists.
- The unpacked extension is now **BrowserTap Bridge**, and the internal `abm` /
  `ABM` marker became `btap` / `BTAP` -- extension storage keys, the WebSocket
  subprotocol, injected page globals, and the connection badge text. Reload the
  extension after upgrading: an unpacked extension's ID is derived from its
  directory path, and the package directory changed, so Chrome treats it as a new
  extension. That also resets what the old extension had stored -- the badge
  preference, the remembered bridge port, and any granted permission leases. The
  bridge does not pin extension IDs, so nothing else has to be reconfigured.
- The two packaged agent skills are now `browsertap-default` and
  `browsertap-bridge-recovery`. `browsertap skill-path` still prints the directory
  that holds them.

### Fixed

- `versioning check-bump` can read a baseline from before the rename. It resolved
  the version source at one hardcoded package path, so any base ref older than the
  rename commit failed with `path exists on disk, but not in <ref>` -- the gate
  would have crashed on precisely the change set it exists to validate. It now
  tries the pre-0.4.0 path as a fallback.
- `test_public_maintenance_commands_use_module_invocation` no longer reads a file
  under `docs/superpowers/`, which 0.3.14 untracked and gitignored. The contract
  passed only on a machine where the untracked file still sat on disk and raised
  `FileNotFoundError` on a fresh clone.

### Removed

- `tmwebdriver.py` and the `TMWebDriver` alias in `browser_bridge`. The shim
  existed so that code written before 0.3.6 could keep importing the original
  class name, and the rename retired the debt for free: no caller has ever been
  able to write `browsertap_mcp.tmwebdriver`, so the compatibility surface it
  preserved is one that never existed under this name. The packaging contracts and
  the stdout-discipline check no longer require the file.

## [0.3.14] - 2026-08-21

### Fixed

- `AGENT_BROWSER_STATE_DIR` now relocates every file ABM writes outside the
  package, not just one of them. The `.agent-browser-mcp` directory literal was
  spelled out at five call sites and only `bridge_state_dir` consulted the
  variable, so pointing it at a scratch directory moved `bridge.pid` there and
  silently left `bridge-token`, `bridge.log`, `spawn.lock` and
  `physical-input.lock` behind in the real home directory -- a state directory
  that was one-fifth redirected, which is worse than one that ignores the
  variable outright, because a test or a sandbox that sets it looks isolated and
  is not. All five now resolve through `agent_browser_mcp.paths.state_dir`, and
  `tests/test_paths.py` pins each one individually so a new writer that rebuilds
  the path by hand fails instead of quietly re-splitting the set. The narrower
  `AGENT_BROWSER_BRIDGE_TOKEN_FILE` override still wins for the token file; only
  the default it falls back to moved.
- The live workflow fails immediately, with the setup it needs, when no
  self-hosted browser runner is declared. It asked for `runs-on: [self-hosted,
  Windows, X64, abm]` while the repository had no runner registered, and a job
  whose labels match no online runner does not fail -- it queues for up to 24
  hours and then expires, because `timeout-minutes` governs execution time and
  not queue time. A dispatch therefore looked like an infrastructure hiccup
  rather than a workflow that was never configured. A GitHub-hosted `preflight`
  job now gates the run: it can report the missing prerequisite precisely because
  it is the one job guaranteed to start. It keys off the `ABM_LIVE_RUNNER`
  repository variable rather than the runner API, since listing self-hosted
  runners needs the Administration permission that `permissions:` cannot grant to
  `GITHUB_TOKEN`.

### Removed

- The three internal design notes under `docs/superpowers/specs/` are no longer
  part of the published tree. They shipped 686 lines of mid-implementation
  reasoning, named after the private framework that produced them, linked from
  nothing a reader would find. They stay on disk for whoever is working through
  them; the directory joins `docs/superpowers/plans/` in `.gitignore`. Anything
  in them that outlives its task belongs in `README.md`, `CONTRIBUTING.md` or
  `AGENTS.md` instead. Earlier tags still contain them, since removing them from
  history would rewrite published commits.

## [0.3.13] - 2026-08-20

### Added

- The two agent skills now ship as package data, so a plain
  `pip install agent-browser-mcp` carries them, and a new
  `agent-browser-mcp skill-path` prints the directory that holds them as
  `<name>/SKILL.md`. They previously lived only under `docs/` in a repository
  checkout and were excluded from both the wheel and the source archive, so
  anyone who installed the package could not reach them at all. Point a skill
  manager at that directory instead of copying the files; a copy stops receiving
  updates on the next upgrade without reporting anything.
- `AGENTS.md` is now part of the published tree: a machine-neutral contributor
  guide covering the three-process reload rules, tab-id volatility, the
  physical-input activation default, and the MV3 alarm floor. The release gates
  fail if an absolute local path reaches it.
- A publish workflow (`.github/workflows/release.yml`) that builds, gates and
  uploads the distributions through PyPI Trusted Publishing, defaulting to
  TestPyPI and reachable only from a manual run or a published GitHub Release.
  No API token is stored in the repository. The package is still not on PyPI;
  `CONTRIBUTING.md` lists the one-time account setup that only the maintainer can
  do, and the READMEs keep saying `pip install agent-browser-mcp` does not work
  yet.

### Fixed

- `page_click`, `page_type`, `page_press` and `page_drag` now prove the page holds
  focus before the first input goes out, which stops the silent miss these tools
  had on a tab that had never been focused. Chrome discards `Input.*` aimed at
  such a tab, and `Emulation.setFocusEmulationEnabled` acknowledges before the
  renderer has applied it, so an input dispatched right after the flag was
  dropped while every CDP command still reported success -- measured at roughly
  one miss in eight on a freshly opened tab. The batch now carries the flag, a
  `document.hasFocus()` probe and the input together: the probe is the renderer
  round trip the flag needs, and its answer is the proof. A `false` reading is
  reported as `page input may not have landed` and the input is **not** re-sent,
  because by then the events are already out and a repeat could double the
  click; check the page instead. Sampling focus after the dispatch cannot detect
  this, as it reads `true` by then either way.
- The CDP fallback used for CSP-restricted pages now retries once when the
  debugger attach itself never landed, which is the common one-off failure and
  cannot have run the caller's script. It deliberately does **not** retry after
  the command went out: `Detached while handling command` means the script may
  already have executed, and the tab has since committed a new document, so a
  retry would run it a second time somewhere else. That case now says so in the
  error message and reports `code` and `dispatched`.
- Failover no longer prefers the most recently registered tab. A session
  registers when its tab is *created*, so the freshest entry named the tab most
  likely to still be loading -- and running there lost the debugger to the next
  commit. Failover now prefers a tab that has been registered long enough to
  have settled, keeps the previous order within that set, and falls back to the
  old rule unchanged when nothing has settled yet. This only affects calls that
  named no tab or explicitly allowed any tab; a caller-named dead tab is still
  refused.
- The bridge now records a tab's disconnect once instead of on every extension
  snapshot. The snapshot sweep re-stamped the timestamp a few times a second, so
  the ten-minute reap in `clean_sessions` was never reached: a daemon kept one
  session per tab ever closed for as long as it ran, and re-logged every one of
  them. Measured on an hour-old daemon, 97% of the log was that repetition, and
  the tab whose disconnect was still being re-reported had been closed an hour
  earlier -- which matters because the log rotates at 5 MB, so a real failure
  scrolls out of the only place a detached daemon records one.

### Changed

- `scripts/check_distribution.py` now *requires* both skills in the wheel and the
  source archive and still refuses a `SKILL.md` anywhere else in either one, so a
  build that drops them cannot ship a `skill-path` command that resolves to an
  empty directory.
- `scripts/check_distribution.py` also checks the built core metadata for the
  fields a public index needs: a Markdown `Description-Content-Type` (without it
  the index renders the README as plain text), `Requires-Python`, a licence, a
  Homepage URL, and the maturity, audience, platform and per-interpreter
  classifiers an index search filters on. None of these can be caught by
  installing the wheel, and a PyPI filename can never be reused, so noticing
  after an upload costs the version number.
- Both READMEs link with absolute URLs. They are the package's long description
  on an index page, where a relative link resolves against the index host and
  404s, taking the usage guide, the security policy and the licence with it.
- The fork-divergence paragraph in both READMEs no longer quotes exact commit and
  line counts that went stale within a release; it names the command that prints
  the current figure instead.
- Every GitHub reference now points at `LinVireo/agent-browser-mcp`. The account
  was renamed from `0xlinn`; GitHub redirects the old paths, but the canonical
  URL in the metadata an index renders should be the current one.

## [0.3.12] - 2026-08-19

### Fixed

- Bounded the CDP lease a manual `beforeunload` dialog keeps after
  `Page.navigate` returns, and let `Page.javascriptDialogClosed` settle the same
  signal `handle_dialog` does. A dialog answered in the browser, or never
  answered at all, no longer leaves the lease attached so that every later
  `execute_js` on that tab fails with `debugger already attached`.
- Derived the dialog-suppression scope lifetime from the command's own budget
  instead of a fixed window, and injected the scope into sub-frames as well, so
  a long-running page command keeps its dialog policy for its whole run and a
  dialog raised inside an iframe is handled under the same policy.
- Released CDP attachments that Chrome kept alive across a service-worker
  eviction but that no surviving lease references. The boot sweep leaves live
  leases and their reference counts untouched and does not record a detach
  marker for a detach that failed.
- Minted the extension client id exactly once when several callers race at
  startup: concurrent callers now share one in-flight storage read/write, and
  when `chrome.storage` is unavailable they share one ephemeral id instead of
  each generating and storing their own.
- Prevented a cancelled MCP tool call from leaving the process-wide tool lock
  held, which would have blocked every serialized tool in that server process
  until it restarted.
- Answered an unauthenticated `POST /api/result` or `/api/longpoll` with the
  same clean `401` `/link` already returned. Both routes rejected the request
  without consuming its body, so on Windows the connection was reset instead —
  every time for a body larger than the socket buffer — and the caller could
  not tell a bad token from a dead bridge.
- Reported a held button in the `buttons` bitmask of a synthesized drag press.
  `page_drag` pressed with `buttons: 0` while every intermediate move reported
  the button, so a page that gates on `MouseEvent.buttons` — drag-and-drop
  widgets, canvases, editors — saw a drag that started with nothing held. The
  sequence now also ends with a zero-button move, because Chromium can keep the
  pressed state when the debugger detaches immediately after `mouseReleased`.
- Named the missing desktop when physical input or desktop capture cannot start.
  `pyautogui` binds a display while importing and `mss` binds inside `mss.mss()`,
  and neither failure is an `ImportError`, so on a headless, locked, or
  X11-less machine these tools surfaced a raw `KeyError('DISPLAY')` or
  `ScreenShotError` instead of saying the machine has no usable desktop and that
  the `page_*` tools do not need one.

- Gave `get_setup_status` a direction so its recovery advice can converge. It
  compared component versions with a bare `!=`, which cannot tell "the bridge is
  older" from "the bridge is newer and this process is older", so upgrading the
  package while an MCP session was live produced `stale_bridge` /
  `restart_bridge` — advice that never clears, because a restarted bridge
  re-reads the same new files and reports the same mismatch. The same held for
  `reload_extension` against a user who had just reloaded. A newer component now
  reports `status: stale_package` with `action: restart_mcp_session` and a new
  `restart_mcp_session_required` flag, and leaves `restart_bridge_required` and
  `reload_extension_required` false. Protocol skew is judged the same way, and a
  version that cannot be ordered keeps the old conservative verdict.
- Let the offline suite run on a non-Windows machine. Five physical-input tests
  installed a fake `ctypes.windll`, which the standard library defines only on
  Windows, so `monkeypatch.setattr` raised `AttributeError` and 11 cases failed
  on any POSIX runner — including the Linux CI this repository ships. They now
  create the attribute instead of requiring it, which keeps the Windows-only
  branches covered everywhere rather than skipping them off Windows.

### Added

- Regressions for the worker-restart debugger sweep, concurrent client-id
  minting, session-table reads under registration churn, tool-lock
  cancellation, the delivery verdict (`delivery_state` / `retry_safe` /
  `executed_tab_id`) an `execute_js` with no response must preserve across the
  remote HTTP hop, and a rejected request on every token-guarded HTTP route
  answering `401` rather than resetting the connection.
- Release guards that only a repository can answer: every file the distribution
  contract requires is tracked by Git, and every `__MSG_*` name the extension
  uses is defined in every bundled locale. Both gaps are invisible to archive
  checks, which inspect archives built from the same working tree — a clone
  missing `browser_bridge.py` cannot import the package, and a clone missing
  `_locales/` cannot load the extension at all, because Chrome refuses a
  `__MSG_extensionName__` it cannot resolve.

## [0.3.11] - 2026-08-17

### Fixed

- Removed content-script polling timers so an extension reload cannot re-enter
  an invalidated page context. The service worker now serializes WebSocket
  status delivery over Ports and top-frame messages, preserving automatic
  recovery after worker eviction without requiring page refreshes.

## [0.3.10] - 2026-08-17

### Fixed

- Replaced cross-context content-script teardown with passive instance takeover,
  so reloading the extension never invokes functions owned by an invalidated
  execution context.

## [0.3.9] - 2026-08-17

### Fixed

- Clarified that a missing bridge listener is auto-started while a stale bridge
  that still owns its port requires an explicit background restart.
- Added executable extension keepalive/connect recovery regressions and locked
  `get_setup_status` to the existing cached-bridge resurrection path.
- Required both `browser_bridge.py` and the legacy `tmwebdriver.py` shim in
  wheel and source-distribution release contracts.
- Bounded debugger target resolution, stale recovery, detach, and attach under
  one caller deadline; page-input batches retry only before dispatching input,
  shared waiters fail together, and late detach events preserve newer leases.
- Added reload-time content-script replacement and keepalive Port deduplication.

## [0.3.8] - 2026-08-17

### Fixed

- Recovered once from a timed-out or detached `Page.enable` before navigation
  by rebuilding the debugger lease within the original deadline, while still
  dispatching `Page.navigate` exactly once.

## [0.3.7] - 2026-08-17

### Added

- Localized the extension manifest, popup, and action badge in English and
  Simplified Chinese, with a popup setting that hides the badge without
  disabling keepalive or automatic bridge reconnection.
- Added structured bridge no-response metadata (`error_code`,
  `delivery_state`, and `retry_safe`) and a typed `BridgeNoResponseError` for
  callers that need to make exactly-once retry decisions.
- Added manifest-bound offline JUnit evidence and stricter release artifact
  validation, including locale presence and exact wheel/sdist counts.

### Changed

- Made URL-pattern tab selection reject zero or multiple matches instead of
  silently choosing a tab; ambiguous callers must pass the complete
  `session_id`.
- Changed the compatibility driver's `newtab()` default from a search-engine
  URL to `about:blank`.
- Archived prior release evidence before finalization so stale artifacts
  cannot be mistaken for results from the current source tree.

### Fixed

- Kept acknowledged storage writes non-retryable when the bridge loses their
  result, while retaining compatibility with older unstructured bridge errors.
- Replaced the remaining bare `except` and enabled Ruff's `E722` check.
- Documented complete extension removal in both READMEs.

## [0.3.6] - 2026-08-17

### Changed

- Renamed the bridge implementation to `browser_bridge.py` and its primary
  class to `BrowserBridge`; the old `tmwebdriver.py` module and `TMWebDriver`
  class name remain import-compatible without emitting protocol noise.

### Fixed

- Prevented randomly generated bridge instance IDs beginning with `-` from
  being misparsed as command-line options during daemon startup or restart.

## [0.3.5] - 2026-08-16

### Added

- Added structured `delivery_state` and `retry_safe` metadata to
  `execute_js` no-response results so callers can distinguish proven
  non-delivery from acknowledged work that must not be replayed.

### Changed

- Documented source and future PyPI installation paths, Windows virtualenv
  commands, status fields, diagnostics, extension permissions, and the local
  loopback threat model.
- Made OS-level input and desktop capture optional through the `desktop` extra.
- Added configurable extension bridge ports, all-display virtual-desktop
  screenshots, Ruff checks, explicit CI coverage enforcement, and release-only
  version-bump checks.

### Fixed

- Made background xterm/ttyd typing focus the sole helper textarea when the
  document body still owns focus, preserving the first input without raising
  the tab or moving the user's physical pointer.
- Directed delayed page-state checks to `wait_for`/`wait_for_url` and limited
  automatic JavaScript replay to requests proven not to have been delivered.

## [0.3.4] - 2026-08-15

### Added

- Unified package, bridge, extension, manifest, documentation, and changelog
  versioning.
- A 55-tool behavior-evidence manifest, documentation contract, 85% coverage
  gate, offline CI, and opt-in self-hosted live verification.
- Structured locators, screenshot clip/full-page/quality options, network
  result filtering, and user-context console filtering.
- Persistent local bridge-token authentication and setup diagnostics that
  distinguish bridge restart from manual unpacked-extension reload.
- Managed `bridge --stop`/`bridge --restart` lifecycle commands with PID,
  process-creation, and executable identity checks.
- A release evidence manifest that binds test, coverage, tool, and distribution
  artifacts to the exact Git/worktree source state.

### Changed

- Background `page_click`, `page_type`, `page_press`, and `page_drag` enable CDP
  focus emulation so Chrome delivers input to a named background tab.
- Tab lifecycle operations are generation-bound and owner-capability checked;
  agent-owned cleanup cannot silently include user tabs.
- All five direct physical-input tools accept `session_id` and default to target
  activation/on-screen verification.

### Fixed

- Normalized TMWebDriver timeouts and bounded new-tab registration queries.
- Preserved debugger leases, total deadlines, dialog cleanup, and directed-tab
  rejection across failure paths.
- Removed MCP stdio stdout noise, response-text control flow, the unused page
  privilege channel, and runtime writes into installed package directories.

## [0.3.0] - 2026-08-14

### Changed

- Introduced one dynamic package version and synchronized it with the unpacked
  extension and both READMEs.

## [0.2.2] - 2026-08-13

### Fixed

- Reconciled lost new-tab acknowledgements without duplicate creation.

## [0.2.1] - 2026-08-13

### Added

- Atomic native downloads, ownership-aware cleanup, network/console/bookmark
  tools, extension management, lab mode, and leave-dialog recovery.

## [0.2.0] - 2026-08-12

### Added

- Archived the initial real-browser MCP automation stack as a publishable
  Python package.

<!--
Only v0.3.12 exists as a tag: 0.2.0 through 0.3.11 were developed before this
history was published, so there is no commit for any of them and a comparison
link for those versions could never resolve. Their sections stay for the record,
without links. Releases from 0.3.13 on get the usual compare links.
-->

[Unreleased]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/LinVireo/browsertap-mcp/compare/v0.3.14...v0.4.0
[0.3.14]: https://github.com/LinVireo/browsertap-mcp/compare/v0.3.13...v0.3.14
[0.3.13]: https://github.com/LinVireo/browsertap-mcp/compare/v0.3.12...v0.3.13
[0.3.12]: https://github.com/LinVireo/browsertap-mcp/releases/tag/v0.3.12
