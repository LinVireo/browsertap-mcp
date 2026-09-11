# Combined configuration repair candidate

Sender: `codex-coverage-01a09080`. Time: 2026-09-12 03:33 +08:00.

The two slices are merged in `D:/coding/btap-config-20260912`. Root verified all
10 implementer file hashes, applied the independently reviewed age patch only
after `git apply --check`, and checked the reverse patch and unchanged real
index. C01-C04, C16 and M06 now form one 12-file delta from seed tree
`e44b92a9f5fff955d1c86939e162d3f2db1ee604`.

This thread ran the combined 15 affected test files: **785 passed in 66.35s**.
Ruff for `src` and `tests`, mypy for all 15 source files, and 17 fresh-process
CLI probes passed. The latter confirm invalid-port JSON errors, lazy non-network
commands, corrected port recovery and the empty-diagnosis path through doctor
JSON and stderr; no state directory was created. Evidence:
`out/config-repair/combined-verification/` in the isolated config worktree.

The seed-relative patch and 12-file manifest are prepared under
`D:/coding/btap-config-20260912/out/config-repair/final-handoff/`:

- `config-diagnostics.patch` SHA256:
  `0406372925082ad13ce08334e646051e504e1eac3a1508e725518d4828fce74b`.
- `manifest.json` binds each before/after hash, the seed tree, patch and real
  index. Diff/LF checks include all three newly added test files. Packaging
  verified product/test and real-index stability.

**Complete independent review is still running.** The prior four-file C16/M06
review passed; the checker is reviewing C01-C04 and the merged CLI change now.
This is a reviewable candidate, not a final review clearance. A final receipt
will follow. The candidate may be inspected while other root work continues.

One extra M06 defect is included: `doctor` previously printed `stale_bridge` on
stderr for every restart action even when JSON said `bridge_unreachable`.
Root reproduced it, and the implementer fixed the label with separate regression
coverage. Existing actual stale-bridge advice is retained.

Public-documentation and spec-sync requirements are in
`../../09-12-config-diagnostics/research/public-guidance-needed.md`. They include
the valid three-port range, anchored explicit paths, token-state fields,
malformed diagnosis and connection age. The extension's IPv4 connection URL is
unchanged; Python IPv6 loopback evidence does not establish browser IPv6 support.
Shared product/live ownership, canonical vault, version and local tags remain
with the coordinator as confirmed in its `root-progress-to-config` receipt.
