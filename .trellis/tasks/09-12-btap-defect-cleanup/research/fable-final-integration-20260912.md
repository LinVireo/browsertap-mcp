# Fable follow-up integration

Owner: codex-resume-01a0881c. Baseline: f63c4a7. Candidate version: 0.5.2.
This is a source-review record before committed-tree sealing. Later gate/tag
and original-checkout results belong in `out/fable-review-20260912/final-verification.json`.

## Findings and disposition

| Finding | Implementation and verification boundary |
| --- | --- |
| F1 | Exact expected extension Origin comes from an existing manifest key or Chromium's canonical unpacked path. No new key or installation-ID migration. Unrelated extension Origins are refused; a local non-browser process can still forge Origin. Chromium Base64/PEM compatibility and the CDP guard passed an independent re-review. |
| F2 | Native Windows handles protect token creation and readback with a current-user DACL and owner verification. POSIX publication now writes/fsyncs a private candidate before atomic no-replace publication; concurrent readers cannot adopt a short prefix. Real Windows TEMP/file-security checks and synthetic POSIX-branch tests pass; no native Linux/macOS claim. |
| F3 | All 51 tools advertise four explicit MCP effect hints, covering optional arbitrary code, clear/overwrite and native effects. Hints remain metadata. Private result-serialization files are transport bookkeeping. Independent registry/control-flow review passed. |
| F4 | Single and batch raw CDP validate the complete request before dispatch. High-impact methods, including Page.setDownloadBehavior, are refused by default; unsafe access needs lab plus explicit BROWSERTAP_ALLOW_UNSAFE_CDP. Allowed CDP can still modify profile state, so this is not a sandbox. |
| F5 | Physical and site-permission form approval return stable reason codes for unsupported hosts, decline, timeout, cancellation and errors. Refusal does not dispatch input or permission grants. Existing explicit lab/physical policy remains. |
| F6 | Default MAIN-world all-frame dialog-helper injection is removed. Extension execution prepares current injectable frames before caller code, uses a shared deadline, and never replays a caller merely because its error resembles CSP. Legacy Python CDP fallback installs/releases the same controller only in its current evaluation context. Both builders recheck the original deadline after scope installation and compilation, before calling eval or the async function. Old injected documents and future frame/navigation lifetimes remain explicit boundaries. |
| F7 | Bridge/server exception logs use fixed categories, exception types and hashed protocol IDs; they omit raw payloads, scripts and traceback text. Synthetic sensitive-payload regressions cover the changed boundaries. |
| F8 | JS/CDP descriptions lead with effects, targeting and uncertain-execution handling while preserving Unicode/result-file contracts. A review found scan_page's absolute read-only promise was false for extra_js; both READMEs, both Skills and tool text now distinguish that caller code. wait_for(js) explicitly describes repeated evaluation. |

Additional fixes found while reproducing/reviewing these findings:

- Known inventory/create-status read timeouts release only their own daemon
  reservation, retaining late-reply receipts and socket identity. Mutation,
  unknown and active MCP OS-scope locks remain conservative. A released probe
  does not prove a create was unexecuted or make it safe to replay.
- POSIX empty/partial token publication is closed by the complete-candidate
  publication rule above; Windows handle publication is unchanged.
- The dialog-preparation ordering and user-thrown CSP/EvalError replay defects
  have preserved failing probes and execution-once regressions. Separate Python
  fallback tests cover controller installation, sending deadline, result-envelope
  normalization, restoration and caller-start checks. Source identity now covers
  five import-cached JavaScript helpers, including the dialog controller.

## Evidence and coordination

Root reproductions are under `out/fable-review-20260912`: origin/CDP green union
121 passed; tools-v2 union 296 passed; bridge-v2 union 340 passed; bridge-v3 union
388 passed; extension-v2-r2 union 191 passed. These overlap and must not be added.
Failed earlier probes and the missing-test-filename invocation remain preserved.
The final affected union passed 632 tests with no failure, error or skip;
`root-final-regressions-r1.json` binds its inputs, JUnit and logs. Stamp, version,
tool documentation and diff checks also passed in that receipt. The extension
stamp is `1b751886b135aa05`; root verified that only the stamp literal differs
from the independently reviewed extension-only handoff.
`root-extension-deadline-review.json` binds root's unchanged original probe:
twelve scenarios, eight late callers on the frozen old source and zero on the
integrated successor, with every descriptor and marker restored. These local
checks precede the final clean-commit suite and do not replace it.
Both Skills passed portable/Codex/lint checks after the final F8 and legacy
fallback wording changes in the six `*-r5.json` reports; BODY_VERBOSE remains
an advisory.

Independent review receipts remain in the bounded worker trees:

- Extension `review-root-f1-f4-r2.md`: F1/F4 compatibility/policy re-review.
- Tools `review-f2-f6.md`: token-security/atomic-publication, reservation races,
  F6 v2 full-worker execution and original-tree content reconciliation.
- Bridge `out/f3-f5-f8-review-20260912/review-report.md`: registry, approval
  control flow and final effect descriptions, with frozen source/test inputs.
- Tools `review-python-fallback.md`: separate Python v1/v2 review, including
  the install/compile deadline defect found after the first passing candidate.
- Tools `review-f6-deadline-window-v2.md`: twelve original extension deadline
  scenarios; eight late callers remained after v2. The extension-only successor
  and root's later regression receipt close this additional finding; the earlier
  v2 PASS is not evidence for it. The final observation-agent channel failed
  twice before executing its extension successor tests; no independent green
  result is attributed to that agent for this delta.

The original 34 dirty canonical paths were snapshotted and content-reconciled.
Three later runtime/serialization paths plus moved server/background bytes were
frozen for a second bounded review. Attribution remains unknown. Root retains
both byte histories and integrates only reviewed functionality on top of its
current candidate; matching content does not identify its author.

The canonical checkout subsequently committed these intermediate bytes as
`564e2509e1c375013e6cb4b0e4e4221aa27c5e6f` and version/record updates as
`6b391d880eda60cc0a1b55c3e2e3350f3e198f1d`. The bridge reviewer compared Git
objects: 40 of the first commit's 43 paths match the previous freeze catalogs;
the remaining three reconciliation records match the frozen root candidate.
The second commit changes nine version/document/metadata paths and introduces
no unique runtime implementation. Its unbound narrative of test completion is
not a substitute for the candidate's final gate receipts. Both commits remain
in the integration history; root retains the later reviewed fixes and task
metadata pointing to this follow-up rather than the historical 0.5.0 receipt.

The reviewed implementation is committed as `3b57c09`, and merge `d42a6fc`
retains both canonical commits. All 26 conflicted files were resolved by 79
explicit hunk decisions: root handled 21 documentation and 43 runtime/test
hunks; the bridge reviewer handled 15 security/test hunks. The runtime review
worker hit a service rate limit before delivery, so root completed that review
from the frozen inputs. Every resolved file matches its root stage-2 bytes;
all production source and tests remain byte-identical to `3b57c09`. The merge
adds only the independently reviewed canonical research record and historical
handoff/ledger updates. `out/fable-review-20260912/merge-integration.json` binds
the decisions, source comparison and clean two-parent commit.

## Finalization and remaining work

Complete the 0.5.2 version and journal commits, then run the finalizer with
`--bump none --skip-live`, a full dependency-install check in a fresh
environment, and the evidence-manifest check. Preserve prior build/evidence
outputs. The install receipt binds both archive inputs to the seal before the
command and rechecks their SHA256 and byte lengths afterwards. Only a passing
exact-tree seal permits the next unused local `v0.5.2-rc.*` candidate tag. Keep
pre-existing candidate tags unchanged. The local receipt authorizes no push
or public release.

During final preparation another session created `v0.5.1-rc.1` at `6b391d88`
and started its canonical r3 finalizer in the original checkout. Its resumed
peer reports that chain stopped after offline testing and retains the older
complete seal in the archive; root observed no remaining finalizer process.
The repository's increment gate uses the reachable RC as its baseline, so the
successor numeric version is 0.5.2. The extension stamp remains
`1b751886b135aa05` after regeneration because version metadata is normalized by
the stamp algorithm. Root preserves the existing tag and all older evidence;
their results do not certify the later deadline and other successor changes.

The resumed peer `codex-resume-01a0932f` owns five vault current-state pages and
will reconcile them to the final successor receipt. Root owns the Fable
inspection page; the separate coordination record is
`out/fable-review-20260912/coordination-to-01a0932f-20260912.md` in canonical.

Before canonical fast-forward, require a clean checkout at the reviewed HEAD,
verify that HEAD is an ancestor of the sealed candidate, and recheck its index
and source state. Compare every tracked file byte after integration. The former
43-dirty-path stash plan was stopped when canonical became clean; no stash was
created. A new unreviewed delta pauses transfer until reconciled. Preserve the
old canonical artifacts in a same-volume archive, copy the sealed artifacts
without overwriting newly appearing files, and check the manifest there too.

Open boundaries remain M01 persistent v1 death/lock evidence, real A7/native
platforms, historical wait reply correlation, and new-build Reload/live. The
prior 0.5.0 live Attempt 2 had 58 passed / 3 failed; it does not certify these
changes or user-tab preservation. SPEC rev7 is still delivered_stub. Bridge
read-probe release does not establish the cause of the initial missing replies.

## Canonical r1 follow-up

The complete offline gate at `5b39e29` reported 4393 passed and two failures in
the parameterized `test_predispatch_unknown_directs_a_fresh_create`. Both stopped
at the description's old retry wording, after confirming that the first call
had not dispatched a create and was safe to retry. The compact F8 description
still conveyed the same condition; restore its explicit `When retry_safe=true`
and `retry with no operation_id` wording without changing runtime behavior or
weakening the regression. Preserve `canonical-r1.log` and its runner receipt.
The successor must rerun the affected contract and full finalizer on its own
clean commit; this failed run is not acceptance evidence. Final results remain
in `out/fable-review-20260912/final-verification.json`.
