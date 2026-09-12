# Fable findings: bounded remediation plan

Owner: codex-resume-01a0881c. Base: f63c4a7b9e4db1235b1f0177fe5122054fcab7ae.
Mode: Change under the existing user-authorized bug cleanup task.
Source: user findings F1-F8 and the vault inspect-2026-09-12.md.
Original checkout is frozen for another Codex's post-Reload live verification.

## Requirements and verification

- F1: reproduce acceptance of an unrelated extension Origin. Establish a
  precise expected-extension identity without inventing a Web Store key or
  breaking existing profile identity. Review migration/configuration first.
- F2: use real Windows file-security APIs to protect bridge-token creation and
  existing-token reads, with no token disclosure and no overwrite on failure.
  Verify a temporary file's effective security, error paths and POSIX behavior.
- F3: every exposed tool gets explicit MCP annotations reflecting its actual
  effects. Test the advertised tool list and representative classifications;
  hints remain metadata, never an authorization or execution lock.
- F4: reproduce raw-CDP bypass of the existing user-state safety boundary.
  Apply a bounded, documented production policy consistently to single/batch
  calls before dispatch; preserve safe diagnostics and explicit lab behavior.
- F5: retain opt-in physical approval, expose stable outcome reasons, and prove
  refusal/unsupported/timeout never sends input. No real keypress testing.
- F6: eliminate default page-visible dialog globals where possible without
  weakening scoped suppression, nested scopes, navigation or restore behavior.
  Validate the actual JS in the existing Node harness and document live limits.
- F7: reproduce exception-payload leakage using synthetic JS/token strings.
  Remove payload text and traceback leakage at logging boundaries while
  preserving safe diagnostic category/operation context; verify normal errors.
- F8: move critical effects and targeting semantics to the beginning of tool
  descriptions; retain async result retrieval instructions and exact contracts.

## Ownership and integration

All implementation worktrees are under D:/coding, based on f63c4a7. Each worker
owns only its own tree and handoff. Root owns integration and all public docs.

1. Bridge worker: F2/F7, browser_bridge.py and focused tests/private helpers.
2. Tool worker: F3/F5/F8, server.py and focused tests/private helpers.
3. Extension worker: F6, extension JS/manifest and focused Node/Python harnesses.
4. Root: F1 compatibility research, F4 policy, public docs, integration, review,
   final offline/build/install checks, version/local tag and vault reconciliation.

Workers report root-relative paths, base/after SHA-256, red/green evidence and
remaining constraints; no shared-source edits, live calls, global installs,
bridge restart, Reload, remote push or publication. Root reproduces the tests.

Independent semantic review covers the frozen integrated diff before candidate
verification. New evidence lives under out/fable-review-20260912. Prior RC,
failed tests and sealed artifacts are immutable. Shared integration waits for
the live owner to release the freeze. Any unresolved identity/product decision
is reported with a concrete reviewed candidate, not silently activated.

## Implementation decisions

- F1 pins the packaged extension's exact Origin without creating a manifest key.
  Chromium's public-key/path ID algorithm matches the current installed ID;
  manifest keys take precedence, otherwise the absolute native package path
  determines identity. Invalid identity fails closed. This preserves existing
  profiles; a future Web Store key remains a separate identity migration.
- F4 is guarded even in the default lab profile. Unsafe raw access requires
  both lab mode and the explicit BROWSERTAP_ALLOW_UNSAFE_CDP flag; safe mode
  always refuses. The entire batch is validated before dispatch. Policy refusal
  has raw_cdp_blocked, undelivered, retry_safe=false; this is not a CDP sandbox.
- F6 removes default MAIN-world helper injection. Old documents need normal
  navigation/refresh after an extension upgrade; Reload cannot remove wrappers
  already injected by an older version. No user tabs are refreshed for migration.
