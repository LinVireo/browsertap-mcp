# F2/F7 bridge implementation handoff

Base: `f63c4a7b9e4db1235b1f0177fe5122054fcab7ae`.
Branch: `codex/fable-bridge-20260912`.
Role: bounded `trellis-implement`; no further delegation, commit, installation,
daemon/browser operation or public-document change.

## Integration files

Only these six source/test files form the implementation. The accompanying
delivery manifest records each base/after SHA-256 and includes exact byte copies.

| File | Change |
| --- | --- |
| `src/browsertap_mcp/_token_file.py` | New private standard-library Win32 security adapter and token I/O contexts. |
| `src/browsertap_mcp/browser_bridge.py` | Secure token I/O, safe storage exceptions, safe bridge log arguments, HTTP callback error boundary. |
| `tests/test_token_file_security.py` | Native DACL and access checks; failure paths; ownership and creation races; filename cases; portable I/O contracts; handle lifecycle tests. |
| `tests/test_log_payload_boundary.py` | Synthetic JS, short/long secrets, traceback/source-line and WSGI error propagation regressions. |
| `tests/test_browser_bridge_coverage.py` | Token mocks follow the new file-I/O boundary; POSIX chmod behavior remains explicitly exercised; storage cause is suppressed. |
| `tests/test_config_diagnostics_regressions.py` | Token read-denial injection follows the new file-I/O boundary; all prior diagnostic assertions remain. |

The patch also includes this private handoff. Root owns public documentation,
the F1 crossing hunks and the separate server.py log fixes.

## F2 behavior to document

- Windows new tokens use `CreateFileW(CREATE_NEW)` with an explicit current-user
  owner and protected, non-inheriting DACL containing one full-control allow ACE
  for that user. Token bytes are written only after native descriptor readback.
- Existing token files are opened with a single handle, checked for ordinary-file
  status and current-user ownership, hardened with `SetSecurityInfo`, verified
  again, then read through that same handle. Their contents and identity remain.
  A different owner or a security operation that cannot be verified is an error;
  BTAP does not take ownership, replace the file or choose a new in-memory token.
- The creating handle is exclusive through the final write. Windows reads retry
  a sharing violation for up to 200 ms, so concurrent starters cannot consume a
  partially written token. A persistent sharing violation fails closed.
- A failed new write marks that exact handle for deletion. If the filesystem
  refuses delete disposition, the fallback empties the file before closing; it
  retains the private DACL. Existing/racing files are never deleted or truncated.
- POSIX retains `O_CREAT | O_EXCL`, mode `0600`, and best-effort post-create chmod.
  The Windows DLLs are lazy. There is no new package dependency.
- File-state diagnostics retain missing/empty/ready/unreadable/invalid-encoding
  distinctions. Security failures report unreadable/permission_denied; I/O and
  unencodable-bootstrap errors expose fixed reasons and suppress the raw cause.
  Reading diagnostics can harden an existing Windows ACL, but creates no token.

The ACL is an OS-user boundary, not isolation from processes running as that
same user or privileged administrators. Hardening cannot retract a token already
read from an older permissive file. Native validation here used temporary files
on this Windows host; native Linux/macOS execution and live startup are not claimed.

## F7 behavior to document

- The four previous `logger.exception` sites now log fixed operation/category
  messages and exception type only. No exception object, text, traceback, source
  line or raw request/result is attached to the LogRecord.
- Arbitrary session/client/browser/identity/generation strings and rejected
  Origin values use a stable 12-hex SHA-256 reference for log correlation. Tab
  snapshot logging reports the count instead of the raw identifier collection.
  Existing URL/pattern redaction policy and useful socket/category context remain.
- Unexpected HTTP callback and before-request-hook errors are caught before
  Bottle can write a payload-bearing traceback into `wsgi.errors`. They retain
  HTTP 500, drain unread request bytes and emit a safe operation/type diagnostic.
  Normal HTTPResponse authentication errors and fatal process exceptions retain
  their prior behavior. Normal execute_js error envelopes/dispatch/retry receipts
  are unchanged.

Server.py findings sent to root/tools worker: dialog cleanup exception at the
former line 5603, invalid approval-timeout raw value at 6997, permission/origin
at 7027 and physical-input summary at 7942. This worker did not edit server.py.

## Evidence

All evidence is under `out/fable-bridge-20260912`; the runner records exact
commands, cwd, PYTHONPATH, input hashes, exit code and log hash. The interpreter's
editable install was not changed; baseline and final native observations confirm
imports resolve to this worktree's `src`.

| Record | Observed result |
| --- | --- |
| `red-f2-f7` | 10 failures, including seven genuine log leaks and two native ACL failures. The third ACL test initially stopped at a CRLF fixture mismatch; this original output is preserved. |
| `red-f2-fixture-corrected` | All three native ACL assertions fail against unchanged baseline source after correcting only the fixture bytes. |
| `red-f7-wsgi` | Two failures: unexpected callback errors propagate when catchall is false and reach wsgi.errors when true. |
| `red-f2-unencodable-bootstrap` | Raw UnicodeEncodeError escaped instead of the safe storage error. |
| `final-focused` | 201 passed: native security, log payloads, prior URL redaction, ingress, real Bottle WSGI encoding and state-path diagnostics. |
| `final-token-regressions` | 33 passed, 225 deselected: existing token/file diagnostic regressions. |
| `final-lint` | Ruff over src/tests/scripts clean. |
| `final-types` | Mypy clean across all 19 shipped Python files. |
| `shared-lint-report` | Python lint/type halves clean; JavaScript unavailable because this isolated tree has no node_modules. This is not a full release-gate pass. |
| `windows-acl-readback-r2` | Native created/existing descriptor snapshots: protected=true, one explicit current-owner full-control ACE, existing inode/content unchanged. |

The first `windows-acl-readback` helper run failed to import the tests package;
the helper's sys.path was corrected and its original failed record remains.
Early passing runs and the initial unused-import Ruff failure are retained too.
No recorded failure was overwritten or relabeled as a pass.

The 35 focused security cases include real Windows AccessCheck (owner allowed,
restricted token denied), protection before first secret byte, ineffective ACL
set readback, ownership rejection, no-read/no-overwrite failure paths, deletion
and empty-file fallback, two actual concurrent-start paths, and native Unicode
filenames. Native-only cases are defined only on Windows, so POSIX collection
does not gain skips. The portable tests cover POSIX flags and mocked handle
ownership/cleanup paths without Windows APIs.

Root should merge the six files by hunk, retain the F1 Origin policy, integrate
the separately owned server log changes, update public guidance and run the
complete integrated offline/build/install gates. Live verification remains with
its existing owner; this delivery neither restarts the bridge nor Reloads Chrome.
