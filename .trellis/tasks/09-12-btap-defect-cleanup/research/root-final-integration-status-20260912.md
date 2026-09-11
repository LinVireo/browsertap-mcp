# Final integration status from root

Coordinator `codex-resume-01a0881c`, native thread
`01a09113-7fb4-71d1-b6ef-34cc2d6c9712`, 2026-09-12.

HTTP Unicode is already integrated. Root independently ran the seven-file
HTTP/auth/ingress/recovery union: 437 passed. Receipt:
`out/bug-cleanup-20260912/http-unicode-root-integration/integration.json`.
The old combined integration script must not be rerun.

Server r1 has not been applied. Root has read the frozen r2 manifest and is
waiting for the renewed independent full wire/path review and parent receipt
before a separate bounded server integration. Prepared public guidance now
includes `result_file_encoding="json"` and the two-step path/content decode.
These docs will be validated against the actual integrated revision.

A finite adjacent-boundary audit found three additional reproduced failures:
CLI strict Unicode output, lock-key encoding after accepted longpoll
registration, and extension-stamp encoding of real NTFS relative filenames.
The existing root worker is implementing only cli.py, command_scope.py,
extension_build.py and a new test in an isolated copy. Shared source remains
root-owned. C23-C25 and the new test claim are now recorded. The raw audit is
frozen in `out/bug-cleanup-20260912/unicode-boundary-audit/`.

After accepting r2, root will acknowledge it here so the peer can commit its
own exact Trellis records. The final full-scope checker will inspect only the
remaining server/boundary/public-doc delta; prior immutable reviews stay intact.
The final source commit, canonical offline seal, full isolated install, local
RC tag and knowledge reconciliation remain root's responsibility.
