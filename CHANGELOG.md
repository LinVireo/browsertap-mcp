# Changelog

Notable user-facing changes to `agent-browser-mcp` are recorded here. This file
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Changed

- `scripts/check_distribution.py` now *requires* both skills in the wheel and the
  source archive and still refuses a `SKILL.md` anywhere else in either one, so a
  build that drops them cannot ship a `skill-path` command that resolves to an
  empty directory.

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

[Unreleased]: https://github.com/0xlinn/agent-browser-mcp/compare/v0.3.12...HEAD
[0.3.12]: https://github.com/0xlinn/agent-browser-mcp/releases/tag/v0.3.12
