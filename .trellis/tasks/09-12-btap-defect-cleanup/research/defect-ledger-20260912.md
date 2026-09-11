# Remaining-defect ledger

Coordinator: `codex-resume-01a0881c`. Inventory: `/root/btap_bug_inventory`; implementation/review reports are kept separately. Integration baseline: `e631522` (includes the setup and coverage baseline). Current implementation and measured regressions override historical prose.

Canonical follow-up: the first full attempt at `147c781` had 3965 passes and
one failure because `tests/test_offline.py` retained the old 49-name expected
set. A standalone invocation reproduced the exact missing-two-tools mismatch.
The list now includes the two explicit native-dialog tools; its exact-set
assertion and product code are unchanged. Original failed evidence is retained
under `out/bug-cleanup-20260912/canonical-r1-evidence/` and `registry-red.*`.
Root's 28-test registry follow-up and the independent four-test supplement
passed; `root-registry-review-verification.json` binds the final two-line delta
to the previous review of the other 350 public files.
Use the final verification receipt for the later committed-tree gate result.

Sources: knowledge-vault `projects/browsertap-mcp/task-queue.md` (Q), `source-review-2026-09-10.md` (R), `source-audit-2026-09-07.md`, `traps.md`; archived Trellis `browser_bridge-review.md` (BR); repository `BUGREPORT.md`, issue history, `DELIVER.md`, `SPEC.md`, public docs and current tests. Line references below identify the inventory snapshot.

## Source and documentation repairs

Statuses below reflect shared-tree integration and scoped checks. They do not
claim a complete final suite or live acceptance; overlapping test counts are
never added. This tracked ledger captures the reviewed source before the seal.
For the later committed-tree identity, canonical gates, installation and local
tag, read `out/bug-cleanup-20260912/final-verification.json`. Recording that
generated result outside the tracked tree keeps the seal valid.

| ID | Candidate and source | Owner / verification direction | State |
| --- | --- | --- | --- |
| C01 | Relative state/token paths split across spawn cwd; Q 1124/1290/1869 | External r2, root configuration union | integrated; 690 scoped passes |
| C02 | CLI DNS/IPv6 port-probe exceptions escape diagnostics; Q 1162 | Matching Python address families and preserved probe errors; no extension IPv6 claim | integrated; same union |
| C03 | Bridge port env parsed with unguarded int; Q 1198 | Lazy 1..65533 validation before network/spawn/reset effects | integrated; same union |
| C04 | Unreadable/invalid-UTF8 token reported as empty; Q 1894 | Distinct redacted states; unknown directory stays canonical; two independent-review fixes included | integrated; same union |
| C05 | Empty or damaged physical lease remains permanently busy; Q 1244 | Real lifetime guard, crash and competing-owner regressions | integrated; 314 scoped passes |
| C06 | SVG accessible name/title stripped; Q 1609 | Preserve title/desc/ARIA while removing geometry | integrated; 426 scoped passes |
| C07 | Deep DOM truncation recurses without a bound; Q 1648 | Iterative deep-wrapper truncation and hints | integrated; same union |
| C08 | Empty/unknown execution reply falls through to success; Q 1539 | Absent result refuses settlement; explicit null is legal; retry_safe=false dominates | integrated; 343 and later 690 scoped checks |
| C09 | Failed transient read reported as no changes; Q 1569 | transients_available distinguishes read failure from empty success | integrated; 426 scoped passes |
| C10 | Main, CDP fallback and manual serialization differ; Q 2002 | Shared serializer, execution-once/async builder, and late debugger cleanup integrated | integrated; 325 and later 401 scoped passes |
| C11 | Modifier-only error branch unreachable; Q 1395 | ctrl / ctrl,shift / ordinary-key checks | integrated; 426 scoped passes |
| C12 | Outside-viewport refusal gives overlay advice; Q 1411 | Distinct viewport/scroll/frame-offset guidance | integrated; same union |
| C13 | Initial navigation 30-second cap and uncleared race timers; R 383 | Shared deadline and timer cleanup | integrated; same union |
| C14 | Bookmark deletion lacks previously chosen backup; Q 801/840/854 | Atomic bounded subtree backup before a pinned-browser delete; reject links/reparse points in the managed child directory | integrated; root directory-boundary 24 passes and later Unicode/backup 38 passes; same-user malicious TOCTOU is outside this guarantee |
| C15 | Unsupported permission restore remains in retry loop; Q 2142 | Persist manual_recovery with prior setting; explicit reset can retry | integrated; 214 scoped passes |
| C16 | Normal polling/snapshots reset connection age; BR 7, R 379/577 | Stable connection age, separate activity timestamp | integrated; 690 scoped passes |
| C17 | Extension stamp decodes binary data lossily; Q 1147 | Raw binary bytes; text normalizes only CR/LF | integrated; 426 scoped passes; regenerated stamp `2752911b822fab7e` |
| C18 / FS-R1 | Same-version source drift is invisible, including import-cached JavaScript | Frozen package-source v2 identity covers all Python and four required JS assets; old/unknown identities are unverifiable | integrated; root identity/preflight/diagnostics run had 165 passes and three outdated diagnostic-text assertions; after synchronizing the text, the 43-test diagnostics file passed. Final canonical union remains required. |
| C19 | Privacy text omits event/keepalive snapshots; Q 2054 | Privacy, persistence/backup and native-marker disclosures synchronized | integrated; root documentation regressions and check_tool_docs passed |
| C20 | Allowed-origins docs omit HTTP enforcement; Q 1781 | Both README translations, SECURITY and transport guide synchronized | integrated; same documentation checks passed |
| C21 | Unpaired UTF-16 surrogate crashes result sizing, MCP wire output, bookmark backup and Bottle HTTP responses | Separate bounded server, bookmark and bridge patches preserve JSON values/keys and existing verdicts | bookmark integrated, root 38 passes; HTTP integrated with 437 root HTTP/auth/ingress/recovery passes. Server r3 integrated after independent PASS; root 291-test Unicode/result/docs/version union passed, including actual NTFS result paths and complete MCP wire serialization. r1/r2 holds remain historical evidence. |
| C22 | A completed large result becomes retryable transport failure when result-file writing fails | External result-unicode r3 preserves the original receipt, complete value and retry classification | integrated; same root 291-test union verifies complete result_json fallback and no replay |
| C23 | CLI JSON output fails on browser titles containing lone UTF-16 surrogates | Isolated Unicode-boundary r2; preserve decoded JSON values and exit codes, with safe diagnostic output | integrated; root 184-test adjacent-boundary/CLI/backup/geometry/stamp union passed, including strict UTF-8 stdout |
| C24 | An accepted longpoll session containing a surrogate fails when its command lock key is encoded | Same r2; byte-compatible hashing for ordinary identities and surrogate-preserving encoding for exceptional identities | integrated; same root union includes real in-process WSGI registration, command dispatch and receipt |
| C25 | Extension stamping fails on real Windows resource filenames containing lone surrogates | Same r2; preserve ordinary path bytes and the current extension stamp | integrated; same root union includes actual temporary NTFS files |
| C26 | Required Ubuntu CI rejects expected Windows-only fixture skips and a headless geometry skip under the complete-run gate | Peer supplied Unicode parameter selection; root integrated bookmark parameters, real unknown-geometry assertions and explicit Node setup for every Python job | all bounded slices integrated; root adjacent 184 and Unicode 291 scoped unions passed. Final canonical validation remains pending. Strict collection and zero-skip checks retained; no native Linux/macOS claim. |
| ND-R1/R2 | Native cancellation mixes DPI coordinate models; cancelled/concurrent approval leaves reusable tickets | One thread DPI context and claim before the first approval await, preserving bounded lifetime and cleanup | integrated; root 291 scoped passes, `out/bug-cleanup-20260912/root-native-p2.xml` |

## Remaining reliability and acceptance questions

| ID | Current fact | Required disposition |
| --- | --- | --- |
| M01 | Requester and per-target lock files accumulate; BR 6 / R 578 | The real `_Scope.close` fd leak is fixed and included in root's 401-test union. Full retention remains open: v1 death evidence and stable lock inodes cannot be deleted safely while old readers/waiters may exist. See `out/bug-cleanup-20260912/leases/m01-final-disposition.md`; no full-growth-closure claim. |
| M02 | Orphan tab-create pending records bypass retention/capacity; Q 2074 | Integrated bounded orphan terminal-unknown and retired-ID replay guard; root 214 lifecycle tests passed. Creation retains at-most-once semantics. |
| M03 | After interrupted storage, durable generation can overwrite a newly observed lifetime using the same native id; Q 2103 | Integrated lifecycle tombstones, preserving durable-wins recovery; included in the same 214-test union. Synthetic interleavings do not claim a native browser reproduction. |
| M04 | Acceptance report content itself has no consistency check; DELIVER 363 | Integrated deterministic comparison against sealed inputs without self-hash cycles; root 162 evidence tests passed. Final canonical seal pending. |
| M05 | JUnit gate does not establish complete collection; DELIVER 365 | Integrated expected collection and run receipts, including rejection of incomplete successful XML; same 162-test union. Final canonical seal pending. |
| M06 | Empty diagnosis is mislabeled stale bridge, rather than malformed; Q 1179 | Integrated final configuration r2 structured malformed-diagnosis handling; root 690 scoped tests passed. |
| M07 | Local version/tag history trails current source | Source version is now 0.5.0; local `v0.5.0-rc.1` awaits the final committed-tree offline seal. Existing tags stay intact; no remote publication. |
| M08 | Coverage intentionally measures the package, not scripts | Existing coverage contract; no denominator expansion under the guise of fixing a bug. |
| G01 | SPEC rev7 A7 requires genuine native verification | Narrow Windows file-dialog inspect/cancel tools and desktop registry are implemented, including ND-R1/R2; root 291 native/input/envelope tests passed. Real A7 remains unrun; the offline implementation does not establish R6 or formal delivery. |
| G02 | macOS/Linux native browser verification absent | Record unavailable platforms accurately; offline tests do not substitute. |
| G03 | Historical wait reply latency lacks send/receive correlation | Add bounded diagnostic evidence if needed; do not invent a root cause. |
| G04 | Extension changes require manual Reload | Preserve reload guards and record the actual loaded build separately from offline source verification. |

## Superseded records

F0-A, late wait-result replay, F1 late payload loss and LOW-5 send-failure cleanup were integrated with `7d7d5d2` and its follow-up. Navigation cancellation/timeout classification and the post-accept three-second cap were repaired in r5/r6; C13 is the separate initial wait. Bounded unknown-outcome ownership, capture bookkeeping, waiter lifecycle and ACK races have later implementations. Earlier HTML budget bypasses, non-ASCII HTTP header failures, global tool locking, legacy BUGREPORT items and old transform handling likewise have replacements. Their historic evidence stays intact and final knowledge reconciliation will point to the applicable fixes.

Pending source inspection and worker reports are evidence inputs only. This ledger will be updated after reproduced fixes and integration checks; it is not a declaration that all defects are fixed.
