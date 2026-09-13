# Changelog

Notable user-facing changes to `browsertap-mcp` are recorded here. This file
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- Record four unresolved limitations from live verification: native file-dialog
  cancellation, JavaScript continuing after a timeout releases its reservation,
  sandboxed child frames blocking main-page dialog-policy setup, and Chrome's
  user-gesture requirement for extension removal. Fixes remain deferred.
- Update both READMEs, usage guides, and troubleshooting guides with the observed
  failure modes and current recovery guidance. Offline CI passing does not
  establish live success; manual dialog closure is not automated recovery.

## [0.5.2] - 2026-09-12

### Security

- Publish POSIX bridge tokens only after a private candidate is fully written,
  preventing concurrent clients from adopting an incomplete token.
- Match Chromium's strict Base64/PEM manifest-key parsing, including its input
  limit and padding rules. Preserve existing unpacked installation identities;
  document Windows path spelling and junction aliases that need an exact Origin.

### Fixed

- Prepare all current injectable frames before caller execution under the same
  deadline, including CSP fallback. A caller-thrown CSP-like error no longer
  causes the script to run again; uncertain delivery remains non-replayable.
- Keep the legacy Python CDP fallback's dialog lease inside its cleanup scope,
  retain the original sending deadline and normalize dialog records separately
  from caller results. That fallback covers only its current evaluation context.
- Recheck the original deadline after scope installation or script compilation,
  before either JavaScript execution route starts the caller.
- Return stable approval failure reasons for temporary site permissions as well
  as physical input: unsupported elicitation, decline, timeout, cancellation or
  error. A refused action does not grant permission or dispatch input.

### Changed

- Release a timed-out inventory or creation-status probe's bridge reservation
  while preserving its operation receipt and late-result ownership. Keep
  pending mutation reservations and the caller's OS scope lock. Failed tab
  creation probes expose a separate bridge receipt for result lookup.
- Classify setup diagnostics as state-changing because Windows token inspection
  can harden an existing ACL. Keep all 51 tools' effect hints explicit.
- Clarify the raw-CDP guard's finite high-risk list, including the Page
  download-behavior alias. Allowed CDP and JavaScript can still modify page or
  profile state; the guard is not a sandbox. Descriptions lead with effects,
  target choice and uncertain delivery while retaining the result-file contract.
- Document optional `scan_page` scripts and repeated `wait_for(js=...)`
  evaluation, current-frame scope limits, and old document helpers that survive
  an extension Reload until normal navigation or refresh.

## [0.5.1] - 2026-09-12

### Added

- Explicit MCP tool effect annotations for all 51 tools, including read-only,
  destructive and idempotent hints derived from the public capability inventory.
- Source identity now covers `chrome_extension/disable_dialogs.js` in addition
  to the four JavaScript assets already frozen by 0.5.0.

### Fixed

- JavaScript dialog interception is installed only for explicit accept/dismiss
  scopes, is shared by the extension and Python CDP fallback, records bounded
  alert/confirm/prompt results, and restores native page descriptors after the
  last lease or deadline.
- Physical approval failures expose stable reasons without dispatching input;
  cancellation still propagates while native-dialog tickets are cleaned up.
- Bridge and MCP logging boundaries retain useful categories while preventing
  browser payloads, protocol identifiers, Origin values, and configuration
  secrets from reaching logs or WSGI tracebacks.
- Bridge token creation and reads now preserve exclusive per-user ownership,
  reject unsafe existing files, and keep secret bytes out of exception chains.

### Security

- WebSocket and HTTP Origin checks trust only the exact packaged extension
  identity, derived from Chromium-compatible manifest keys or unpacked paths;
  explicit allowlist entries are exact matches and HTTP bodies are drained on
  rejection.
- Raw CDP browser-wide writes and common ownership/recovery bypasses are blocked
  before dispatch. They require both lab mode and the explicit
  `BROWSERTAP_ALLOW_UNSAFE_CDP=1` operator opt-in; safe mode always refuses them.

## [0.5.0] - 2026-09-12

### Added

- Task-scoped implementation guides under `docs/agent-guides/`, with a shorter
  root `AGENTS.md` that keeps the maintainer entry points within instruction
  loading budgets. The detailed guides are included in source distributions.
- `PRIVACY.md`, the privacy policy the Chrome Web Store listing serves. It
  states what the extension can reach, that its only network destination is
  `127.0.0.1`, and what persists on disk. Two disclosures go beyond the
  permission names: the extension holds all-sites access (`<all_urls>`) rather
  than a per-site grant, and it strips a tab's Content-Security-Policy header
  while a script runs in that tab. A contract test derives the required
  disclosures from `manifest.json`, so adding a permission that reads user data
  now fails offline until the policy covers it.
- `get_execute_js_result` tool for retrieving results from
  `execute_js(wait=false)` operations. Acknowledged operations can be polled or
  claimed without replaying side effects; completed results can be read
  repeatedly within the retention limits.
- Explicit Windows native-file-dialog inspection and cancellation through
  `inspect_native_file_dialog` and `cancel_native_file_dialog`. Both require
  `[desktop]` and `desktop_opt_in=true`; cancellation consumes a short-lived
  ticket and checks browser ownership, foreground, input quiet and the Cancel
  control before one bounded dispatch. They do not expose general desktop input.
- Atomic, bounded bookmark-subtree backups before `remove_bookmark`; a failed
  backup prevents deletion and an uncertain deletion retains backup metadata.
- Import-time package source identity in setup diagnostics, including Python
  and the four imported JavaScript assets, so same-version source changes
  identify the MCP or bridge process that needs restarting.
- Complete collection and execution receipts for offline/live evidence, plus
  deterministic validation of acceptance-report content against sealed inputs.

### Removed

- **BREAKING: the seven OS-level tools are gone.** A caller that invokes one now
  gets "no such tool" rather than a deprecation warning. `mcp.list_tools()`
  reports **51** tools, including the two explicit native-file-dialog tools.
  - `mouse_click` → use `page_click` instead (browser-safe element clicking)
  - `mouse_move` → use `page_click` (no separate move needed)
  - `mouse_drag` → use `page_drag` (drag within the page)
  - `type_text` → use `page_type` (type in browser inputs)
  - `hotkey` → use `page_press` (send keyboard events to page)
  - `pointer_info` → use `execute_js` to query element positions
  - `capture_desktop_screenshot` → use `capture_page_screenshot` (safer, browser-only)

  **Rationale:** they controlled the OS desktop rather than one tab, so they acted
  on whatever happened to be on screen -- a security boundary problem and a
  maintenance burden both. The page-level tools cover the same ground under
  tighter constraints. They were announced as deprecated earlier in this same
  unreleased cycle, so nothing shipped with the warning stage.

  **`resolve_leave_dialog` is unaffected** and keeps its lab-only Enter fallback,
  which is why the approval gate, the cross-process lock, the quiet-input window,
  target activation, and the `on_screen` check all remain. The `desktop` extra
  also gates the new explicit native-file-dialog tools; none accepts caller
  supplied screen coordinates or arbitrary key sequences.

### Changed

- **Documentation no longer presents OS-level input as a capability.** Warning at
  call time was the weakest layer available: every piece of prose a reader or an
  agent sees *first* still opened with "five tools send real OS-level mouse and
  keyboard input", which is the opposite of the migration advice logged one layer
  down, and an agent acts on the handshake text rather than on a log line emitted
  after the call. Rewritten in both READMEs (lede, key features, "what this
  project is actually for", the folded tool sections), the FastMCP `instructions`
  the agent reads at handshake, `docs/USAGE.md` §4/§5/§8 and its Chinese
  counterpart, `AGENTS.md` §4, `SECURITY.md`, both bundled skills, the
  `browsertap` CLI `--help` description, and the PyPI summary in `pyproject.toml`.
  The `page_*` tools are named as *the* input path, and the cases the desktop
  tools were once kept for (browser chrome, extension popups and general OS
  dialogs) remain outside page input. Supported Windows file dialogs now have
  their own explicit inspection/cancel workflow. A failed `page_click` does not
  escalate to it. Each README keeps one folded section listing the removed
  names against their replacements, so a caller arriving from an older version
  finds the migration rather than silence.
- The tool table in the `browsertap-default` skill said "工具全表（55 个）" while
  56 were registered, so the one line a caller reads to decide whether the table
  is complete was the line that was wrong. The public surface now has 51 and is verified
  mechanically against `list_tools()` rather than by eye.
- The physical-input gate no longer has a caller that passes coordinates.
  `check_screen_bounds`, `screen_bounds`, and the `points=` argument to the
  internal dispatch helper are kept -- they are the gate, not a removed tool --
  but they are now exercised by tests against the helper directly instead of
  through a shipped tool.

### Fixed

- Unpaired UTF-16 code units survive JSON result export, MCP response adaptation,
  bookmark backups and bridge HTTP responses without lossy replacement. If a
  completed result cannot be written to a file, an explicit inline JSON backup
  preserves its full value, operation receipt and original retry verdict.
  Result-file paths containing these code units carry an explicit JSON encoding
  marker, allowing callers to decode the path before reading its JSON content.
- CLI JSON output also preserves these strings on strict output streams.
  Command-lock keys and extension resource filenames can contain surrogate code
  units without crashing their hashes; ordinary identities and stamps retain
  their existing byte representation.
- Relative state/token paths remain anchored to the launching directory across
  detached startup. Port validation happens before network or process effects;
  Python address-family selection and IPv6 URLs agree. Token diagnostics
  distinguish missing, empty, unreadable and invalid UTF-8 files without
  overwriting them; unknown default-directory metadata preserves its path.
- Connection age no longer resets on ordinary polling. Malformed remote
  diagnostics remain unreachable evidence instead of being mislabeled stale.
- A crashed or damaged physical lease can recover while a live OS lock remains
  authoritative. Command-scope cleanup attempts every held descriptor after a
  close error and never retries an uncertain descriptor number.
- Native dialog cancellation samples input under one thread DPI context and
  claims its ticket before approval; cancelled or concurrent approval cannot
  leave a reusable ticket. Bookmark backup refuses redirected managed directories
  before retention cleanup, file writes or browser deletion.
- SVG accessible names survive simplification, deep DOM truncation is iterative,
  and failed transient reads are distinguished from an empty successful read.
  Modifier-only input and outside-viewport refusals give the appropriate cause.
- JavaScript execution routes share bounded result conversion. Runtime script
  errors do not trigger re-execution; async-body construction preserves multiple
  statements and identifiers beginning with `return`. Complex async results use
  explicit returns. Late debugger attachment/dispatch/cleanup cannot steal a
  successor's lease or delete its pending operation.
- Unknown or malformed execution receipts do not become successful null values;
  an explicit `retry_safe=false` prevents replay even alongside `undelivered`.
  Initial navigation waits use the caller's deadline and clear race timers.
- Orphaned tab-create records become terminal unknown after worker restart;
  bounded retirement keeps replay guards. Lifecycle tombstones prevent older
  persisted generations from overwriting a newly observed tab lifetime.
- Permission restoration that becomes unsupported retains `manual_recovery`
  with the prior setting and stops automatic retries; explicit reset can retry.
- Extension build stamps hash binary assets without lossy decoding. Public
  privacy and origin-policy guidance now includes event snapshots, backups,
  native inspection markers and HTTP origin enforcement.
- Setup diagnostics no longer request an extension Reload when no runtime
  status has arrived after a bridge restart. Startup asks callers to wait;
  later `extension_unavailable` asks them to check the extension connection.
  Confirmed version, protocol, capability and worker-build mismatches retain
  their recovery actions. Live preflight refuses unavailable status without
  labeling it as a stale build.
- Timed-out server-generated selector/text/URL wait probes can release their
  tab reservation while keeping the original result receipt. The bridge does
  this at its existing probe deadline, without an extra cleanup request.
  Caller JavaScript, uncertain outcomes and dialog recovery retain their
  conservative reservation policy; late probe replies cannot release a
  successor's tab. `reservation_held` now reflects actual target ownership.
- Long waits preserve the original operation receipt after silence expiry or
  an unusable result instead of dispatching the condition again. This also
  prevents automatic replay of caller JavaScript whose result is still unknown.
- Retire the failed extension socket and all tabs it still owns when a command
  cannot be sent. Reconnect publication and disconnect cleanup are serialized,
  so an old send failure or delayed close cannot remove a replacement channel.
  Uncertain operation receipts, reservations and capture ownership remain intact.
- Retain the first valid late terminal reply after an operation's reservation
  expires. `get_execute_js_result` exposes it as `late_result` with
  `late_reply_age`, preserving the original unknown receipt and `retry_safe=false`.
  Late replies cannot restore reservations, change successor operations or
  extend retention; successful large values retain lossless file metadata.
- Reserve every CDP target in a cross-tab batch before dispatch. Child target
  overrides and numeric-string IDs now use the same identities as ordinary
  commands, so another call's busy tab cannot be reached through a batch.
- Preserve expired unknown-operation receipts through the remote bridge,
  including their timeout diagnosis, abandonment reason and `retry_safe=false`.
- Reclaim capture-command bookkeeping after its operation expires even when no
  reply arrives. Capture ownership remains protected until a confirmed stop or
  the tab lifecycle ends.
- Release tab reservations after a bounded unknown-outcome window while keeping
  the original timeout diagnosis and `retry_safe=false`. Manual-dialog recovery
  starts that window at the first unknown observation, so a late observation
  still has time to be inspected; repeated observations do not extend it.
- Give `open_new_tab` callers an exit when the first recovery probe finds no
  operation record: inspect the browser instead of polling the same missing
  record indefinitely. Pre-dispatch failures now explicitly direct a fresh call
  without `operation_id`; uncertain creates retain their owner capability and
  remain unsafe to replay.
- **`set_cookies`, `delete_cookies`, `cdp_batch` and `upload_files` work again
  on the current extension.** The cmd/code protocol split below made the
  extension evaluate whatever arrives in `code` as page JavaScript, but these
  four still sent their `{"cmd": ...}` envelope as a text script. Measured live:
  `cdp_batch` and `upload_files` failed with `SyntaxError: Unexpected token
  ':'`, and both cookie tools silently took the `document.cookie` fallback --
  HttpOnly dropped, `status: ok` reported, and the live suite green because it
  read `status` rather than `method`. The envelopes now travel on `ext_cmd`'s
  `cmd` field like every other command; the text route remains only for a
  router that explicitly answers `unknown cmd`. The live cookie tests assert
  `method == "cdp"`, and `cdp_batch` / `upload_files` have live coverage.
- **`page_type`'s focus guard now guards.** The re-resolution command that
  should stop the batch when the target loses focus or editability was sent
  without `"cmd": "cdp"`, so `handleBatch` recorded `unknown cmd: undefined`
  and typed anyway; `batch_guard_failed` could never fire. The offline shape
  test now checks `cmd` on every batch command.
- **Enter submits, printable keys type.** `page_press` and the `submit_key` of
  `page_type` dispatched `keyDown` without `text`, which Chrome treats as a raw
  key event: `keydown`/`keyup` reached the page, but no `keypress`, so
  `submit_key="Enter"` typed into a form and never submitted it, Enter inserted
  no newline in a textarea, and `page_press("a")` inserted nothing. Presses
  without Ctrl/Alt/Meta now carry `text` (`"\r"` for Enter, the character for a
  printable key); modifier chords stay text-less so Ctrl+A still selects
  instead of inserting an "a".
- The challenge-stall window now measures idleness in both places that count
  attempts. `ChallengeAttemptTracker` expired on age since the first attempt
  while the server-side counter expired on idleness, so a third click just
  past the window reported `attempts=3` without a stall.
- `cdp_batch` rejects a non-object document with `invalid_request` instead of
  an `AttributeError` reported as `internal_error`.
- Two "no connected tabs" messages told every client to keep the server
  running "via Hermes"; they now name the bridge daemon.
- **Documented the write sandbox for the file-writing tools.** `save_pdf` and
  `capture_page_screenshot` route `save_path` through `_validate_safe_path`, which takes a *relative* path under
  `~/Downloads/browsertap` and rejects absolute paths and `..` escapes — but the
  docs described `save_path` as a path the caller chooses ("atomically writes
  `save_path`", "only adds a disk copy"). A caller following them passed an
  absolute path and got a `ValueError` that read like a bug. The tool
  descriptions, both READMEs, and `docs/USAGE.md` §4 plus its Chinese
  counterpart now state the sandbox and that it is not configurable by
  environment variable.

### Security

- **Path traversal protection** for file-writing tools. `save_pdf` and
  `capture_page_screenshot` validate that user-supplied `save_path` parameters
  stay within `~/Downloads/browsertap` by default. Absolute paths (`/etc/passwd`, `C:\Windows\...`), parent directory
  traversal (`../../outside`), and symlink escape attempts are rejected with a
  `ValueError`. This prevents arbitrary filesystem writes through malicious path
  manipulation. The validation is implemented by the internal `_validate_safe_path`
  function and is covered by comprehensive unit and integration tests in
  `tests/test_path_traversal_protection.py`.
- **Atomic file writes** for `capture_page_screenshot`, which now uses the
  temporary file + fsync + atomic rename pattern (via `_atomic_write_bytes`),
  ensuring saved files are never left in a partially-written state. Write failures clean up temporary files and provide
  user-friendly error messages for common issues (disk full, permission denied).
- **File size limit** for `capture_page_screenshot`. A screenshot exceeding 50MB
  is rejected before any write occurs, preventing resource exhaustion through an
  oversized image payload.
- `execute_js` no longer reaches the extension's internal command router. A
  string script that happened to parse as JSON was coerced into a command
  envelope, so passing `{"cmd":"site_permission",...}` as a *script* called
  `setSitePermission` directly and skipped the `ctx.elicit` approval that
  `set_site_permission` requires in `safe` mode -- the approval `SECURITY.md`
  promises for every site-allow action. Caller scripts are now marked on the
  wire so they cannot parse as JSON, and the extension coerces only the three
  envelopes the server still sends as text (`cookies`, `cdp`, `batch`). As a
  side effect, `execute_js('[1,2,3]')` returns its array instead of failing as
  an unknown command.

### Changed

- `execute_js(wait=false)` now returns an acknowledged `operation_id` for
  genuinely long-running scripts. `get_execute_js_result` can wait briefly for
  or claim the late result without replaying side effects; completed results are
  consumed once and retained for 10 minutes.
- `execute_js` now preserves the final expression in multi-statement scripts
  that use top-level `await` (for example, `const value = await read(); value`).
  The extension previously evaluated the declaration successfully but returned
  `undefined`, which crossed the bridge as a silent missing `js_return`.
- Large `execute_js` return values are now lossless across bounded MCP text
  channels. When the JSON-encoded `js_return` exceeds 24 KiB UTF-8, the
  complete value is written to a private temporary JSON file and the response
  carries `result_file`, `result_bytes`, `result_sha256`, and `result_format`
  instead of a silently truncated inline payload.
- The toast `disable_dialogs.js` draws on the user's own page follows the page's
  colour scheme instead of being a fixed dark panel. It hardcoded `#222` on
  `#fff`, which is wrong on a light theme and cannot follow a theme switch; it
  now sets `color-scheme: light dark` and paints with the `Canvas` /
  `CanvasText` system colours, the same mechanism `popup.html` already uses. It
  also resets inherited styling (`all: initial`) so a host page's CSS cannot
  restyle it, and uses `inset-inline-end` so it stays out of the way on
  right-to-left pages.
- `test_no_published_document_repeats_a_section` asserts that every document on
  its roster was reachable, instead of a literal floor of nine against a list of
  eleven. The literal tolerated two unreadable documents, and removing an entry
  from the list widened that gap without failing anything.

## [0.4.20] - 2026-08-29

### Added

- The packaged extension now ships its own icons (16, 32, 48 and 128 px) and
  declares them in `manifest.json` for both the extension list and the toolbar
  action. Chrome previously drew a generated placeholder, and the Chrome Web
  Store requires a 128x128 icon inside the package, so this was the one code
  prerequisite left for a store listing. The artwork is original; upstream
  ships no image of any kind.

## [0.4.19] - 2026-08-29

### Fixed

- Bumping the release version no longer asks you to reload the Chrome
  extension. `get_setup_status` compares the extension's compiled build stamp
  against the source tree, and when that comparison says `matches_tree` the
  worker is running this code -- the only difference left is the version string
  Chrome parsed when the extension was loaded, which Chrome does not re-parse
  without a reload. That number was still being OR-ed into
  `reload_extension_required` beside the stamp verdict, so every release left
  the extension permanently one version behind and demanded a manual reload
  whose only effect was on the number the check was complaining about. The live
  suite reads that flag and has no override, so the click stood between a
  version bump and any live evidence at all. The version number is now the
  fallback used when the stamp cannot judge, and a mismatch the stamp overrules
  is still reported as a note rather than hidden. A protocol-version or missing
  capability gap still requires a reload, matching stamp or not.

## [0.4.18] - 2026-08-29

### Added

- The release seal now proves that the live suite was answered by the extension
  build in the sealed source tree. `live-preflight.json` records
  `extension_build_verdict`, `extension_build_enforced` and both build stamps,
  and the acceptance report reads them: a live run answered by a stale service
  worker, or one whose verdict could not be established, no longer counts as
  live evidence. A stamp that was never regenerated names its own fix.

### Changed

- Both READMEs now state plainly that reusing an already-logged-in browser is
  not unique to this project, and name the cases where
  [playwright-mcp](https://github.com/microsoft/playwright-mcp) is the better
  choice: one-step Chrome Web Store install, headless, Docker, CI, Firefox or
  WebKit, accessibility-tree snapshots with stable handles, and a smaller
  default tool surface. What remains specific to this server — guarded
  OS-level input, retargeting without raising a window, the whole `chrome.*`
  surface, and working with zero tabs open — is stated in the same place.

## [0.4.17] - 2026-08-26

### Changed

- The live suite now records user browser activity as `tab_activity` context
  instead of skipping because a user opened, closed, navigated, or focused a
  tab. It fails only when a tab owned by the suite remains open, and reports
  whether the cause was a missing cleanup call or a lifecycle-generation
  mismatch.
- The release workflow now runs the shared Python and extension JavaScript lint
  gate before tests and distribution builds, so a publish cannot pass on Python
  checks while shipping invalid extension code.
- Distribution validation now compares the installable package-file sets in the
  wheel and source archive, rejecting stale build output that would make the two
  release artifacts disagree.

### Fixed

- A failed or unsupported popup cookie refresh now clears the previous result,
  so `Copy` cannot reuse credentials read from an earlier tab or page.
- `mouse_click` requires `x` and `y` together, and `type_text` requires
  `click_x` and `click_y` together. Half-specified pairs are rejected before
  approval, activation, or input dispatch.
- Desktop capture and virtual-screen probing use the supported `mss.mss()` factory,
  keeping the desktop path compatible with current `mss` releases.

## [0.4.16] - 2026-08-26

### Fixed

- **`execute_js` no longer loses a collection it was not told about by name.** The
  converter that turns a script's return value into something the extension
  boundary can carry had one branch each for jQuery, `NodeList` and
  `HTMLCollection`, so a `Set` of elements, a `Map`, a generator's output or any
  other wrapper fell through to `JSON.stringify` and arrived as `{}` with nothing
  reporting the loss. It asks `Object.prototype.toString` and the iteration
  protocol instead -- one question the engine already answers for every
  collection. Five more silent losses go with it: the 100-item cap sat only on
  the branch least likely to be large, so `querySelectorAll('div')` on a big page
  serialised every element (the cap is now 200 across every collection, and says
  how many it dropped rather than truncating quietly); a collection whose first
  slot was empty was not recognised as one; a text node, comment or document
  became `{}` and a nested `window` became the string `[Object]`, which a real
  object can also produce; one cycle anywhere in the value discarded the *entire*
  result; and a `BigInt`, a function, an `Error` or a throwing getter each took
  more with it than itself.
- **The extension popup follows the browser's theme.** It had a dark palette
  written into it, which rendered a dark popup for anyone reading everything else
  in light and could not follow a theme switch at all. It now opts into
  `color-scheme: light dark` and takes its colours from CSS system keywords,
  which is the browser's own answer to the question.

## [0.4.15] - 2026-08-25

### Changed

- **`scan_page` no longer modifies the page it reads.** With `cutlist` on -- the
  default -- reading a page used to write a `data-btap-list` attribute onto every
  container it collapsed and leave two `window.__btap*` counters behind, because
  the CSS selector it reports had to survive into a second roundtrip. That
  selector is now derived from the container's own structure, so there is nothing
  to mark: a scan is invisible to the page's own scripts, to `#id` and
  attribute rules in its stylesheet, and to anything that serialises the
  document. The tool description and both README tool tables used to disclose
  what it wrote; they now state that it writes nothing, and the offline suite
  fails if either claim outlives the code -- in both directions, so a stale
  disclosure is a failure too.
- The two injected page scripts were replaced rather than edited.
  `page_scripts/opt_html.js` and `find_main_list.js` are now `page_outline.js`
  and `list_groups.js`, and they decide what the model sees of a page by asking
  the browser instead of restating the answer: `Element.checkVisibility()` for
  whether an element renders, and a structural signature (tag plus sorted class
  list) for which block is a repeated list, in place of a hand-rolled
  `display`/`visibility`/`opacity` walk and a scored search over tuned constants.
  Measured against one real page, the simplified copy came out 18% smaller in 27%
  less time, carrying the same extracted text.
- The extension declares `minimum_chrome_version: 121`, up from 111, because that
  is what the visibility call above needs. `Element.checkVisibility()` has existed
  since Chrome 105, but the three option names passed to it were renamed in 121,
  and unknown members of a WebIDL dictionary are dropped without an error -- so an
  older engine would have returned a verdict computed without the opacity and
  visibility checks, putting hidden text back in front of the model with nothing
  reporting a problem. An install-time refusal naming the browser is the audible
  version of that. The gate that derives this floor from the code now reads
  `page_scripts/` as well as the extension directory, which is what caught the
  stale 111.

### Fixed

- Hidden text no longer reaches the model. Text under a `display:none` block
  clones fine, so counting *any* surviving child made the hidden parent look as
  though it still held content and the whole block was kept. Only a surviving
  child *element* counts now.
- The selector `cutlist` reports resolves against the copy it is meant for. The
  attribute the old marker wrote was itself pruned out of the simplifying clone
  it had to cross into, so on a real page the reported selector could match
  nothing at all: measured on one news front page,
  `[data-btap-list="1"] > tr.athing` selected **zero** rows where the structural
  selector selects all 30.
- Five kinds of state the previous pass dropped now survive into the simplified
  copy: a checked checkbox, a `<select>`'s current value, the links inside a
  `<nav>`, a form's submit button, and a button in an overlay.
## [0.4.14] - 2026-08-24

### Added

- `server._TAB_OWNERSHIP.outstanding()` and `.counters()` expose, read-only, which
  tabs this process opened and has not closed. `release` runs only after a close
  actually succeeded, so what is left is exactly "opened by this task, never
  cleaned up" -- the one claim about a browser that can be made without inspecting
  anybody else's tabs. `outstanding()` deliberately withholds the `owner_id`: it
  feeds a report, and a capability that is never handed out cannot leak into a
  published artifact.
- The counters are lifetime totals that are never decremented, and
  `artifacts/live-preflight.json` publishes them next to `own_tabs.enforced`.
  `outstanding()` alone cannot tell "nothing was leaked" from "nothing was ever
  opened", and those two readings are not interchangeable for anybody auditing a
  run -- the same trade already made by `input_quiet.enforced` and `on_screen`.

### Changed

- The live preflight now fails a run over one thing only: a tab the suite opened
  and did not close, read from that registry. The previous rule required the tab
  inventory to come out the way it went in, which made a claim about tabs the
  suite never touched. A user opening a tab, a user closing one, and the suite
  leaking its own scratch tab all produced the *same* verdict, so the verdict
  attributed nothing -- and the two people it accused were mostly the browser's
  owner. What the browser did during a run is still recorded, as `tab_activity`,
  as context.
- A leak message names both causes, because they need different fixes: a missing
  `close_tabs(..., owner_id=...)`, or Chrome discarding and restoring the suite's
  own tab so that the close was refused with `lifecycle generation changed`.
- The busy-browser sample is a note rather than a gate. It used to skip the whole
  live layer, which is wrong twice over: it let the user's tabs decide whether the
  suite may run at all, and on a machine whose browser is never idle it meant the
  live layer stopped running. It is kept because a browser in use makes
  timing-sensitive cases flakier, so it is the first thing to read when one fails
  oddly.

### Removed

- `BTAP_LIVE_ALLOW_BUSY_BROWSER`. It existed to bypass the busy-browser skip and
  the inventory check, and both of those are gone -- leaving the variable would
  have left a knob that no longer does anything, which is worse than no knob.

### Fixed

- `scan_page` no longer injects an `id` into the caller's page. With `cutlist`
  (the default) it marked the container it had picked so the second roundtrip
  could find it again, and an injected `id` is visible to
  `document.getElementById`, to the page's own `#id` CSS rules, to `:target` and
  to anything that serialises the document. It now reuses the element's existing
  `id` when it has one -- writing nothing at all -- and otherwise sets a
  `data-btap-list` counter, which collides with nothing and is idempotent per
  container, so repeated scans do not accumulate marks. The write that remains is
  now stated in the tool's own description and in both README tool tables: a tool
  documented as reading a page had been modifying it.
- `execute_js` no longer leaves a timer running on the page after it returns. To
  report `transients` it starts a 450ms interval that walks every text node in
  the document, and three of its return paths never stopped it: an early return
  when the tab gave no response, an exhausted deadline, and a failed read. On
  those paths a full-document `TreeWalker` kept running for as long as the
  document lived. The script now carries its own expiry and stops itself without
  needing another roundtrip -- which is the only cleanup that can work, because
  those are exactly the states in which the page can no longer be reached -- and
  the no-response path sends an explicit stop while a channel may still exist.
  The global it parks on is namespaced (`window.__btap_tm`) rather than
  `window._tm`, and a superseded interval now clears itself instead of the
  monitor that replaced it.

### Security

- The extension popup no longer copies cookies to the clipboard when it opens.
  Opening it ran `fetchCookies()` unconditionally, and the tail of that function
  wrote every cookie of the active tab -- including the `HttpOnly` ones that page
  JavaScript cannot read -- into the system clipboard as `name=value; ...`. So an
  access whose visible purpose was to check the page indicator silently replaced
  the clipboard contents with session credentials. Both halves are now gestures:
  `Refresh` renders the list, a new `Copy` button copies it, and a clipboard
  failure is reported on the button instead of overwriting the rendered list.
  `SECURITY.md` said "refreshed" where the code copied, and now describes what
  the code does.

## [0.4.13] - 2026-08-24

### Added

- `scripts/lint_report.py`: lint is now a scored acceptance gate with sealed
  evidence behind it. `.github/workflows/test.yml` ran `ruff check src tests
  scripts` and the release finalizer did not run ruff at all, so a sealed
  `release_ready: true` and a red CI run on the very same commit could both be
  correct, and the sealed one was what the release notes quoted. CI and
### Fixed

- Removed a duplicated `## Listing on the MCP Registry` section from
  `CONTRIBUTING.md`. The two copies were identical, so a later edit to one of them
  would have left the file contradicting itself with no diff to show why.

## [0.4.12] - 2026-08-23

### Added

- Physical-input coordinates are checked against the real display geometry, and a
  point on no display at all is refused with `coordinates_off_screen` before the
  tab is raised and before anything is dispatched. Windows `SetCursorPos`
  *clamps* an out-of-range point and reports success: measured on a 1920x1080
  panel, `mouse_click(2400, 1300)` moved the cursor to `(1919, 1079)` and clicked
  the bottom-right hot corner -- the one that can minimise every window -- while
  the result said `status: "success"` and named the coordinates it had not used.
  `mouse_move`, `mouse_click`, `mouse_drag`, and `type_text`'s focusing click all
  go through the check; `hotkey` takes no coordinates and is unaffected.
- `screen_bounds` on every physical-input result and on `pointer_info`: the
  virtual-desktop rectangle across all displays, with `source` naming the probe
  that answered. `pointer_info`'s existing `screen_width`/`screen_height` are the
  *primary* display, which is the wrong bound on a multi-monitor desk and was the
  only geometry the tool had ever reported.
- `image_width`, `image_height`, and `pixel_space` on `capture_page_screenshot`,
  parsed from the returned bytes (PNG, JPEG, WebP). The result previously carried
  `size`, a byte count, as its only number, and a caller reading a point off the
  picture had nothing telling it that CDP returns **device** pixels while
  `page_click` takes **CSS** pixels. Measured at 125% display scaling: viewport
  1482x780 CSS, screenshot 1852x975, and a link needing `page_click(340, 209)`
  sat at screenshot pixel `(425, 261)` -- feeding that back made
  `document.elementFromPoint` answer `HTML`, the page background. Coordinate mode
  is the one input path with no hit test, so nothing reported the miss. A header
  that cannot be parsed reports `null` dimensions plus a `dimensions_note` rather
  than a fabricated `0x0`.
- `pixel_space: "physical"` on `capture_desktop_screenshot`, with the model note
  stating the image is not resized -- so unlike the page screenshot, its pixels
  *are* `mouse_click`'s coordinates.

### Changed

- The `mouse_*`, `type_text`, `page_click`, `page_drag`, and both screenshot tool
  descriptions now name their units instead of saying "coordinates". Three spaces
  are in play -- physical screen pixels, viewport CSS pixels, and device pixels
  -- and the two READMEs, both skills, and the tool descriptions each documented
  only the page/desktop split, which does not distinguish the two that differ by
  `devicePixelRatio`.
- Where the display geometry cannot be read, the call proceeds with
  `screen_bounds.enforced: false` and a note naming what was unavailable, rather
  than refusing. This follows `input_quiet` and `on_screen`: refusing would take
  physical input away from machines where pyautogui works fine. Read a pass on
  such a machine as unverified, not as coordinates confirmed valid.
- The Windows `GetSystemMetrics` probe declines outright unless the process is
  already DPI-aware, because a DPI-unaware read is virtualized: measured cold on
  a 1920x1080 panel at 125% scaling it returns 1536x864, which would have refused
  every legitimate x between 1537 and 1919. `mss.monitors[0]` answers with the
  true extent regardless of the caller's awareness -- it makes itself aware
  internally -- so it is tried first, and neither probe loads the input backend
  or changes this process's awareness level.

## [0.4.11] - 2026-08-23

### Fixed

- An abandoned CDP attach no longer surfaces as an uncaught error on
  `chrome://extensions` for an install that is working. The shared attach promise
  had exactly one reader, and the deadline is re-checked after the promise
  exists, so a caller whose budget ran out resolving the target threw and took
  that reader with it; the late-completion branch then rejected on purpose to
  mark the abandoned lease, with nobody listening. A terminal `.catch` keeps one
  reader that cannot walk away, and every real waiter still races the promise
  itself and still sees the rejection. Two node harnesses that raced real
  milliseconds against a 10ms and a 20ms budget now freeze the clock the
  extension reads, which is what made them fail on the slowest CI runner for a
  reason unrelated to what they test, and the leak gets a test of its own.

## [0.4.9] - 2026-08-23

### Added

- `server.json`, the MCP Registry manifest, so the package can actually be
  listed. README.md has carried the `mcp-name` ownership marker since 0.3.12
  with nothing beside it to claim, which is half a submission. The manifest is
  wired into the single-version mechanism rather than left standing alone: it
  states the version twice -- the label the registry displays and the version a
  client installs -- and `scripts/versioning.py` rewrites both, requires them to
  agree with the other six surfaces, and refuses a listing whose `identifier`,
  `registryType` or `repository.url` stops describing this package. A stale or
  redirected listing is invisible from the repository once accepted, so it is
  gated here instead, down to the environment-variable block: every name has to
  be one a module really reads, and `BROWSERTAP_MODE` has to name the profiles
  `_AUTOMATION_MODES` accepts and the default `_automation_mode` resolves to. The
  first draft of the listing offered `strict, standard or lab` and claimed a
  `standard` default, none of which exists -- a stranger setting `standard` is
  folded straight back to `lab`, so the listing would have told them they had
  asked for approval prompts on the profile that skips them and drives real mouse
  and keyboard input. Every other check passed it, because none of them reads
  that block.
- `.github/dependabot.yml` for the GitHub Actions ecosystem, monthly and
  grouped. Every action is pinned by commit SHA, which has no update channel of
  its own; this is that channel. The Python side is deliberately absent --
  `supply-chain.yml` already runs pip-audit and builds an SBOM, and the upper
  bounds on the `dev` extra are load-bearing, so a bot raising them would undo
  the thing they exist for.

### Changed

- Both READMEs say under Requirements that there is no Docker image and why: the
  server attaches to the browser you are signed into, through an extension a
  human loads once, so a container has nothing to drive. It was the one
  install-shaped question the docs left a reader to guess at.

## [0.4.8] - 2026-08-23

### Fixed

- Two MCP instances starting at the same moment can no longer both spawn a
  bridge daemon -- the duplicate the spawn lock exists to prevent. `O_EXCL`
  publishes the lock file before the owner's pid is written into it, and an
  instance arriving in that window read it empty, took "no pid" to mean "the
  owner is gone", deleted the winner's lock and spawned. An unreadable pid is
  now a different fact from a dead one: only a pid that parsed and whose
  process is really gone recycles the lock, and a genuinely corrupt lock is
  still recovered by the 30-second staleness window that exists for it.
  Measured off Windows only, at 12 concurrent callers and 2 daemons; the
  window is real everywhere but its width is scheduler-specific.
- `download_file` validates the name it actually sends. The payload rewrites
  backslashes to `/`, and the check ran before that rewrite against the
  server's *native* path flavour -- so on Linux and macOS `filename`
  `"\escape.bin"` looked like one ordinary path component, passed, and
  reached Chrome as the absolute path `/escape.bin`. The check now runs on the
  rewritten value against both flavours, so `"C:escape.bin"` (drive-relative
  to Chrome on Windows) and `"//host/share/x"` are refused everywhere and the
  answer no longer depends on which OS the server happens to run on.
- The offline suite can complete on Python 3.10 and 3.11 again. A test asked
  for the other platform's virtualenv layout by patching `os.name`
  process-wide; on those versions `pathlib.Path` reads that global to pick its
  concrete class, so every `Path(...)` in the process raised
  `NotImplementedError` -- including the ones pytest runs while formatting a
  report, which turned one assertion into an `INTERNALERROR` that abandoned
  the run after 411 of 1331 tests. `check_install._venv_paths` now takes the
  platform as an argument.
- The offline suite runs on Linux and macOS. Its free-port probe set
  `SO_EXCLUSIVEADDRUSE`, which does not exist off Windows, and guarded the
  call against `OSError` while reading the missing constant raises
  `AttributeError` -- so every test that needs a real port (the whole
  `/link` token-auth group, 26 of them) errored out before it started.
- Four deadline tests no longer depend on how loaded the machine is. They let a
  fake driver really `sleep()` through a budget measured in tens of
  milliseconds, so a shared CI runner spent the remainder before the next
  dispatch and the tests failed on Linux and macOS while passing here. They now
  advance a fake monotonic clock, which is what they were always asserting
  about. One of them compared two budgets with a strict `<` that needed
  measurable clock movement inside 10 ms -- below Windows' 15.6 ms timer
  granularity, so both sides read the same float.


## [0.4.7] - 2026-08-23

### Fixed

- The extension refuses to disable *itself*. `chrome.management.setEnabled`
  is the one documented way to make Chrome re-read `background.js`, and
  aiming it at the bridge is unrecoverable: nothing is left that could
  re-enable it, so the transport goes down until a human intervenes. The
  sibling operations already refused a self-target -- `uninstall` and
  `call_extension` both do -- and the one with the worst outcome was the one
  left open, reachable through the `set_extension_enabled` tool whose
  description never mentioned it.
- `set_extension_enabled` reports a refusal as `status: error` with the
  extension's code. It used to return `status: ok` no matter what came back
  and put the real answer in a nested `result`, so any failed toggle -- not
  just this one -- read as a completed one in the field a caller checks.

## [0.4.6] - 2026-08-23

### Added

- A macOS job in `test.yml`, so all three platforms this product runs on are
  exercised by CI. Two code paths exist solely for that one --
  `_macos_pointer_position` reads the cursor through Quartz, and
  `_darwin_process_identity` is how the bridge decides whether the PID in its
  lock file is still its own daemon -- and neither had ever been executed
  anywhere but in a reviewer's head. It is also where the quiet-input gate
  really degrades, because the pointer read needs an accessibility permission
  a hosted runner never grants.
- A per-file coverage floor in the acceptance report. The 85% gate is an
  average, and an average hides a module that has stopped being tested:
  `bridge.py` holds most of the platform-specific daemon code and sits around
  63% while the total stays comfortably above the line. coverage.py has no
  per-file threshold, so the floor is read out of the sealed `coverage.json`
  and folded into the existing `code_coverage` gate. A coverage payload with
  no per-file section fails rather than passing for want of data.

### Changed

- `artifacts/live-preflight.json` is now bound by the evidence manifest. It is
  the record of which build each of the three processes was running and whether
  the browser was idle -- the difference between a live run worth believing and
  a junit full of passes -- and it was written and uploaded but never hashed,
  so a seal could pair a passing suite with a preflight record from an older
  run, or with none at all.
- `bridge_status.version` now reports the extension's manifest version instead
  of a string typed on the day it was written. That literal read the same
  through every release while `extension_version` beside it moved, so the one
  field a reader reaches for when asking whether an extension is stale was a
  constant. The `unknown_cmd` reply, which is what a version skew actually
  produces, carried the same frozen value.
- The development extra now carries upper bounds. Every number in the
  acceptance report is produced by `pytest`, `pytest-cov` and `ruff`, and they
  were the last third party in the gate path resolved fresh on each run --
  actions are pinned by commit, the secret scanner by version and digest, the
  audit and SBOM tools by exact version. A ruff minor that adds a rule turns
  the lint gate red with no commit behind it, and one that drops a rule stops
  enforcing it just as quietly. These are bounds and not a hash-pinned
  lockfile; the runtime dependencies a user installs are deliberately left
  unbounded.

## [0.4.5] - 2026-08-23

### Fixed

- The quiet-input gate now reports what it was able to observe instead of
  passing silently when it could observe nothing. It raises only when a marker
  present in *both* samples changed, and the markers are a Windows-only
  last-input timestamp plus the pointer position -- which is unavailable under
  Wayland, in a headless container, and on macOS without the accessibility
  permission. With all three missing the gate degraded to a bare `sleep()`
  that passed no matter what the person at the keyboard was doing, and said
  nothing about it, while four places in this documentation promised the gate
  is always enforced. Refusing on such a machine would take physical input
  away from systems where it otherwise works, so the gate now does what
  `_activate()` already does for `on_screen`: it acts and reports honestly.
  Every physical-input result carries `input_quiet` with `observed` (the
  markers actually sampled), `quiet_seconds`, and `enforced: false` when
  nothing could be sampled at all.

## [0.4.4] - 2026-08-23

### Fixed

- A tab's lifecycle generation now survives a service-worker eviction, which is
  the one thing it exists to do. The snapshot in `chrome.storage.session` is
  written whole -- that write is also what prunes generations for closed tabs --
  so a worker that started, failed to read the store and then wrote its own map
  deleted every generation it did not happen to contain, and a fresh worker's map
  contains none of them. Measured once on a real browser: an eviction mid-run
  re-minted all 15 tabs inside a single millisecond, `close_tabs` then refused
  two tabs its caller owned as `lifecycle generation changed`, and they leaked
  until a human closed them. An unreadable store is now a different fact from an
  empty one -- nothing is published until the store has actually been read, the
  failed read is retried instead of being memoised for the worker's lifetime, the
  durable value wins over anything minted during the outage, and a generation
  minted for a tab the store never knew is kept and published once the read
  succeeds. `bridge_status` also reports `tab_generation_load_failures`, because
  the original failure left no trace at all: the only symptom was a refused close
  minutes later.

## [0.4.3] - 2026-08-23

### Added

- The live suite refuses to run when the browser is not running the checkout
  under test. It travels through three programs and only one of them is the code
  pytest imported: the bridge daemon and the extension are long-lived and keep
  whatever build they started with until each is restarted or reloaded by hand.
  So a live round could pass, hand over 55/55 tool evidence and be sealed as a
  release while the build that actually answered was the previous one -- and
  nothing noticed, because no gate and no sealed artifact recorded which build
  had answered. The session fixture now asks `get_setup_status()` first, fails
  the run naming every stale component with the one step that fixes it, and
  records what each of the three processes was running in
  `artifacts/live-preflight.json`.

### Fixed

- A working install no longer collects red `tabs_update failed` entries on
  `chrome://extensions`. Chromium refuses to dispatch a `chrome.*` call whose
  service worker has already stopped, answering with the bare message `No SW`,
  and it rejects that call *before* the WebSocket close is processed -- so the
  socket still read `OPEN` and the extension filed an ordinary MV3 eviction as a
  real failure. Nothing behaved differently; the report did, which matters more
  than it sounds for a project whose first troubleshooting instruction is to
  look at that page. The log sites in the WebSocket client now ask
  `isWorkerGoneError` first and each names which side went away: the bridge, the
  worker, or neither. The error reply that saves the bridge a full request
  timeout stays keyed on the socket rather than on this check, because an
  evicted worker still has an open socket to answer through.

## [0.4.2] - 2026-08-23

### Security

- A local process can no longer displace the connected extension on the
  WebSocket port. That port carries no token -- it is guarded by the handshake
  `Origin`, which is a prefix the extension cannot keep secret and any local
  program can put in a header -- and the client id identifying a browser arrived
  in the message body, chosen by the sender, and was written straight into the
  routing table. One forged `ext_ready` therefore re-pointed every subsequent
  command, `execute_js` included, at the sender's socket. A client id now stays
  bound to the socket holding it: a second socket claiming the same id is refused
  and closed while the incumbent is still being heard from. Takeover is allowed
  only after the incumbent has been silent for a minute, because a socket can be
  dead while the operating system still reports it as connected and refusing
  forever would leave the bridge unusable until someone restarted it by hand; the
  keepalive ping the extension already sends every 20 seconds is what keeps a
  live socket's claim fresh. Refusals are counted rather than swallowed --
  `browsertap doctor` reports `rejected_client_takeovers` and describes the last
  one under `last_rejected_takeover` -- because the failure this prevents is
  silent by construction.

### Changed

- Every GitHub Action used in CI is pinned to a commit SHA instead of a moving
  major tag, so a green run stays reproducible and no third party can change what
  executes on a runner holding this repository's checkout. The PyPI publish step
  is the one documented exception: PyPA ships Trusted Publishing fixes onto
  `release/v1` and asks callers to track it, and a stale pin on the only step
  holding `id-token: write` fails closed on a release that cannot be retried by
  hand. The offline suite now fails on any other floating action, and on a pin
  with no version comment next to it.

### Fixed

- The documentation no longer leaves the impression that one lock does both jobs.
  `PORT+2` is a lock socket deciding which bridge *hosts* the other two ports --
  a bridge that loses it keeps running and works through the winner -- while a
  separate `spawn.lock` file is what stops several MCP sessions starting at the
  same moment from each launching a daemon. Only the second one has anything to
  do with duplicate daemons, so a single listener on `PORT+2` was being read as
  proof of something it does not show.

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

v0.4.13 and v0.4.14 are tags with no published artifact. Both rounds were sealed
with full evidence, and both times the next round of work landed before the
upload, so their changes reach PyPI inside a later release instead. The tags
exist so that every compare link spans one version rather than several; there is
no 0.4.13 and no 0.4.14 on PyPI, and no GitHub Release for either.
-->

[Unreleased]: https://github.com/LinVireo/browsertap-mcp/compare/v0.5.2...HEAD
[0.5.2]: https://github.com/LinVireo/browsertap-mcp/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/LinVireo/browsertap-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.20...v0.5.0
[0.4.20]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.19...v0.4.20
[0.4.19]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.18...v0.4.19
[0.4.18]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.17...v0.4.18
[0.4.17]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.16...v0.4.17
[0.4.16]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.15...v0.4.16
[0.4.15]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.14...v0.4.15
[0.4.14]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.13...v0.4.14
[0.4.13]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.12...v0.4.13
[0.4.12]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.11...v0.4.12
[0.4.11]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.9...v0.4.11
[0.4.9]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.8...v0.4.9
[0.4.8]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.7...v0.4.8
[0.4.7]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/LinVireo/browsertap-mcp/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/LinVireo/browsertap-mcp/compare/v0.3.14...v0.4.0
[0.3.14]: https://github.com/LinVireo/browsertap-mcp/compare/v0.3.13...v0.3.14
[0.3.13]: https://github.com/LinVireo/browsertap-mcp/compare/v0.3.12...v0.3.13
[0.3.12]: https://github.com/LinVireo/browsertap-mcp/releases/tag/v0.3.12
