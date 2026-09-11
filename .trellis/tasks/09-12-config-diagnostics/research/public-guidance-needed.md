# Public guidance needed after configuration integration

Owner: `codex-coverage-01a09080`. The coordinator retains public-documentation,
version/tag and knowledge-vault ownership. These notes identify the contract to
describe after the combined repair passes independent review; they are not a
claim that final shared-tree verification has completed.

## Operator-facing behavior

- `BROWSERTAP_BRIDGE_PORT` is an integer from `1` through `65533`: WebSocket,
  HTTP and host lock use the base, base + 1 and base + 2. Invalid configuration
  is rejected before network/spawn effects, without preventing imports,
  help/version or package-path commands. Update the configuration tables in
  both READMEs and the custom-port sections in both troubleshooting guides.
- Explicit nonempty `BROWSERTAP_STATE_DIR` and
  `BROWSERTAP_BRIDGE_TOKEN_FILE` paths are resolved against the launching
  process's working directory before daemon spawn changes cwd. The child gets
  absolute overrides; default and legacy-directory selection keep their
  existing source labels. `state_paths` contains comparable absolute paths.
  The README environment tables currently omit `BROWSERTAP_STATE_DIR`; add it
  when documenting this behavior. Legacy aliases remain supported.
- `doctor` keeps JSON stdout if its later DNS/socket port probe fails after
  setup status has returned. Configuration and probe failures must remain
  distinct from confirmed old component versions. Python probes, remote HTTP
  URLs and host listeners support IPv6 with a matching address family; this
  slice does not change the extension's `127.0.0.1` connection URL and makes no
  browser IPv6 claim.
- `state_paths.token_file_status` separates `missing`, `empty`, `ready`,
  `unreadable` and `invalid_encoding`. `token_file_error` supplies a safe reason;
  `token_file_exists` and `state_dir_exists` are null when their metadata is
  unreadable. Unknown default-directory metadata does not trigger selection of
  the legacy directory. Only ready
  content has a fingerprint. Existing files are not overwritten; permission
  and encoding failures are not reported as an empty file. No token content or
  invalid UTF-8 byte repr belongs in an error or example.
- A malformed remote diagnosis returns `cause: bridge_unreachable`, `ok: false`
  and `error_code: malformed_diagnosis`, retaining the existing restart action.
  It does not prove that the bridge is stale. Legitimate structured failures
  retain their error code and details; minimal valid diagnoses are not required
  to carry package-version/capability fields.

Useful locations: both README CLI/configuration sections, both troubleshooting
guides' token and port sections, `docs/agent-guides/transport-auth.md`, and the
two packaged caller skills. The coordinator should synchronize the tool
description if it names these diagnostic fields and run the tool-doc gate.

## Maintainer contract / spec-sync judgment

`docs/agent-guides/runtime-lifecycle.md` should describe the repaired age rule:
`Session.connect_at` and `info.connected_at` represent the current transport's
connection age; `last_activity_at` tracks HTTP activity. Polling and tab
snapshots preserve age. New transport/socket, disconnect recovery, a new tab
generation or HTTP recovery after the idle window starts a new age. Settled
failover eligibility uses connection age, and HTTP expiry uses activity.

`.trellis/spec/backend/setup-diagnostics.md` should add malformed diagnosis to
the unavailable/restart row and reference the new regression tests. The same
spec or its linked runtime guide should retain the configuration-validation
boundaries above. No MCP input signature changes or automatic Reload mechanism
are introduced. Public docs and the canonical vault should reference the
coordinator's final commit/version, not an isolated worktree or stale counts.

Historical `traps.md` content about a duplicate reader in the removed
`scripts/check_derived_notices.py` is superseded by deletion commit `f3edc7e`.
The coordinator's C17 ownership already includes removing the obsolete comment
in `extension_build.py`; preserve the historical note and link its disposition.
