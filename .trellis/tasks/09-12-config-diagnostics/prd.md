# Repair bridge configuration and connection age diagnostics

## Goal

Implement allocated C01-C04, reproduce and repair C16 and M06 using the frozen shared baseline in an isolated worktree, then return a reviewed delta for sequential root integration.

## Requirements

- Continue the coordinator's allocation in
  `../09-12-btap-defect-cleanup/research/config-repair-request-from-root-20260912.md`.
- The shared task and main checkout stay in `D:/browsertap-mcp`; simultaneous
  implementation uses `D:/coding/btap-config-20260912`. Seed only the frozen
  setup/coverage baseline and return a delta relative to that seed.
- C01: preserve effective state/token paths across the daemon's changed cwd,
  including nonempty explicit overrides and legacy aliases. Keep default/env/
  legacy source labels truthful; diagnostics report comparable absolute paths.
- C02: doctor must emit JSON after DNS/socket probe errors. Use the correct
  address family and bracket IPv6 HTTP URLs. Keep the extension unchanged and
  distinguish tested Python transport support from browser configuration.
- C03: all bridge entry points reject invalid numeric configuration before
  socket/spawn side effects, accounting for the three consecutive bridge ports.
  Imports, help/version and path-only commands must still work with bad config.
- C04: distinguish missing/empty/unreadable/invalid-encoding token files without
  revealing token bytes, overwriting an existing file or dropping atomic-create
  and concurrent-writer behavior.
- C16: reproduce connection-age reset under ordinary HTTP polling/extension tab
  snapshots; separate activity from transport/lifetime age and retain real
  reconnect semantics. Exercise failover eligibility with a controlled clock.
- M06: reject a malformed or empty remote diagnosis as malformed/unavailable,
  preserving useful structured failures rather than claiming a healthy bridge.
- Preserve the runtime worker's separate identity-module/diagnose-response work;
  report overlapping regions to the coordinator for sequential integration.
- Root coordinator owns public documentation, version/tag decisions, shared
  runtime verification and knowledge-vault reconciliation.

## Acceptance Criteria

- [x] Seed hashes match the frozen shared setup and reviewed test files.
- [x] Each confirmed defect has a reproducing regression before repair and a
  passing regression afterward; synthetic states and owned loopback ports only.
- [x] Affected tests, Ruff, mypy and LF/diff checks pass in the isolated checkout.
- [x] An independent Trellis check reviews the complete own delta, including
  error/cleanup boundaries and public guidance required from the coordinator.
- [x] Return only the own repair patch, source hashes, commands/results and
  integration notes; preserve all seeded changes and main-tree source.
- [x] Record spec-sync judgment and send a new shared research receipt.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
