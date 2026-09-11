# Configuration repair in progress

Sender: `codex-coverage-01a09080`, native thread
`01a09080-c3ed-7560-b872-8950f5972a6a`. Date: 2026-09-12.

The implement agent is now working on allocated C01-C04, C16 and M06 in
`D:/coding/btap-config-20260912`. Seed tree:
`e44b92a9f5fff955d1c86939e162d3f2db1ee604`; seed hashes were verified. An
independent check will follow before the seed-relative repair patch is handed
back. Shared source/tests and the live runtime remain untouched by this slice.
The shared claim includes the exact paths and overlap boundary.
The local implementation is now split: the implement agent owns C01-C04 in
the original isolated config worktree; this thread owns C16/M06 in
`D:/coding/btap-config-age-20260912` from `e631522`. Its 122 source/test files
were verified against the identical frozen seed before creation. These two
slices will be combined and independently checked before returning one patch;
the coordinator's integration boundary is unchanged.

Root independently reproduced C16 against the frozen `browser_bridge.py` bytes
(`cd4d9133597e0b1630d544b3f2d07a8aa3ec56398b4ed36a74d0ee3a2ba6a9e9`).
Evidence: `out/coverage-handoff-20260912/residual-inventory/age_diagnosis_probe.py`
and `age-diagnosis-results.json`. The fake-clock observations are:

- A repeat poll on the same HTTP queue resets `connect_at` from 1000 to 1010,
  removing that stable session from the settled failover pool.
- A repeat extension snapshot on the same socket resets `info.connected_at`
  from 2000 to 2010 while `connect_at` remains 2000.
- Replacing the socket while keeping the same generation leaves `connect_at`
  at 2000; actual transport age should reset. A changed generation already
  creates a new session with a fresh age.

M06 probe confirms `{}`, an unrelated object and `ok: "yes"` are returned
unchanged by remote `diagnose`. A wrong container is already classified as
unreachable. The `/link` producer can also return a legitimate structured
`{error, error_code}` failure; repair must retain that information rather than
require version/capability fields on every response. This does not revise the
ledger into a claim that empty diagnosis currently means healthy: the setup
consumer currently treats missing version as stale.

The evidence uses synthetic clocks/queues/payloads only, loads the immutable
seed under a separate module name, and verifies the source hash after execution.
It has been sent to the implementer. Public documentation, version/tag, shared
runtime verification and canonical knowledge reconciliation remain with the
coordinator as allocated.

## Additional historical-note reconciliation

The knowledge-vault `traps.md` around lines 319-322 still describes duplicated
manifest-version normalization in `scripts/check_derived_notices.py`. That
script was removed by `f3edc7e` (`git log -1 --diff-filter=D --
scripts/check_derived_notices.py`), so the two-reader defect is superseded.
`src/browsertap_mcp/extension_build.py` around lines 67-71 still mentions the
removed reader in a comment. Since the coordinator owns C17 in that file, it
can clean this stale comment during the byte-safe stamp change and reconcile
the historical vault note in its final update. This is not an additional
runtime defect or a request to restore the removed attribution checker.
