# Journal - codex-resume-01a0881c (Part 1)

> AI development session journal
> Started: 2026-09-11

---



## Session 1: R6 successor integration and retained wait receipts
<!-- trellis-session: v=2 fp=e4009b75cf73a95a -->

**Date**: 2026-09-11
**Task**: R6 successor integration and retained wait receipts
**Branch**: `codex/r6-integration-20260911`

### Summary

Integrated F1, LOW-5 and portability prerequisites onto sealed r6; fixed generated wait-probe lease release and long-wait replay. Independent r2 review passed. Local integration is complete; SPEC rev7 remains delivered_stub.

### Main Changes

- Preserved receipt ownership, late-result admission, retention and successor leases; returned unusable wait receipts without another dispatch.
- Updated packaged guidance and backend recovery conventions; archived the task with closure evidence and a native acceptance manual.
- Source review subject d686f961e276034c0858582ffecfb05ec931b659470bb6aee0543737814efffc excludes only task/journal bookkeeping; final handoff is out/integration-20260911/verification.json.

### Git Commits

| Hash | Message |
|------|---------|
| `7d7d5d2c2e44d4fc7675559648fdf87167985fc4` | fix: integrate retained receipts and wait recovery on r6 |

### Testing

- [OK] 2809 offline tests passed with 95.52% combined coverage; 93 focused contracts passed; lint/type/docs, distribution, strict twine and full fresh installation passed.
- [OK] 61 full Windows live cases passed before the final collector repair; 10 affected wait live cases passed after it, with enforced matching extension and no owned tabs outstanding.
- [OK] Independent r2 PASS with zero findings, current bound gate passed and checkpoint satisfied. Source/task/journal secret scans recorded separately.
- [OK] The older ad hoc full-live preflight was not separately snapshotted; its JUnit/log and reviewer observation remain. The r2 preflight covers only its actual 10-case run.

### Status

[OK] **Completed**

### Next Steps

- Use the local native-validation bundle on available macOS/Linux desktops. A7 needs a product decision and a registered capability or a revised approved contract.
- Historical wait delay remains unproven; add correlated extension and WebSocket timestamps if it recurs. Persistent host MCP activation is a separate step. No push, tag, publication or release finalizer.


## Session 2: Fable remediation and 0.5.2 offline candidate preparation
<!-- trellis-session: v=2 fp=cb0cc4fc63fdafe9 -->

**Date**: 2026-09-12
**Task**: Fable remediation and 0.5.2 offline candidate preparation
**Branch**: `codex/fable-fixes-20260912`

### Summary

Integrate F1-F8 and additional execution fixes, preserve canonical history, and prepare exact-tree offline verification; live and compatibility boundaries remain open.

### Main Changes

The Fable F1-F8 follow-up is integrated with exact extension Origin trust,
Windows token security, complete POSIX token publication, all-tool effect hints,
bounded raw-CDP restrictions, stable approval reasons, scoped dialog handling,
payload-free exception logging and aligned caller documentation.

Review also repaired read-only inventory/status reservation cleanup and both
JavaScript execution routes. Scope preparation and compilation share the
original deadline; caller-thrown CSP-like errors do not replay caller code.
The legacy Python CDP fallback applies the controller in its current context.

The implementation commit is 3b57c09. The integration preserves canonical
564e2509 and 6b391d88 in history. Conflict hunks were reviewed separately by
root and two existing collaborators; the selected production/test bytes remain
the reviewed successor. Existing 0.5.0 and 0.5.1 RC tags remain unchanged.
The successor source version is 0.5.2 because the existing 0.5.1 RC is the local
finalizer's increment baseline.

The final affected union passed 632 tests before the version-only integration;
the original twelve-scenario extension probe reproduced eight late callers on
the old source and none after the fix, with all descriptors/markers restored.
The authoritative final complete-suite, lint, build, fresh full-install and
canonical transfer outcomes are recorded after this journal commit in
out/fable-review-20260912/final-verification.json. This journal does not assert
future gate success. Full installation binds before/after archive hashes.

The task remains in_progress for complete M01 persistent-file retention, real
A7 and native macOS/Linux acceptance, historical wait correlation and current
build activation/live verification. SPEC rev7 remains delivered_stub. No live
run, automatic Reload, user-page refresh, shared-venv reinstall, push or public
release is part of this successor preparation.


### Git Commits

| Hash | Message |
|------|---------|
| `3b57c09` | fix: harden BTAP transport and scoped execution for 0.5.1 |
| `d42a6fc` | merge: preserve canonical BTAP candidate history |
| `a2a5927eb54e6c734fa7c11233189e6ec497cf31` | chore: prepare BTAP 0.5.2 offline candidate |

### Status

[OK] **Completed**
