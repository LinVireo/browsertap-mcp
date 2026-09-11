# Preserve JavaScript Unicode results across Python and MCP serialization

## Goal

Reproduce unpaired UTF-16 surrogate results and completed-result file-write failures through the Python externalizer and MCP serializer, implement a lossless bounded repair in an isolated worktree, and hand an independently reviewed delta to the cleanup coordinator.

## Requirements

- Work in `D:/coding/btap-result-unicode-20260912`; shared product files stay
  coordinator-owned. Keep the configuration r2 worktrees and evidence sealed.
- JavaScript strings may contain unpaired UTF-16 surrogates. Preserve their
  code units in valid UTF-8 JSON rather than replacing or discarding them.
- Reproduce both the Python externalizer and actual MCP/Pydantic wire boundary.
  A successful script must not become a serialization error that suggests replay.
- Cover small/large strings, nested values and keys, lone high/low surrogates,
  real non-BMP Unicode, and repeated asynchronous result retrieval as applicable.
- Preserve the result envelope, truthful success/error and retry semantics,
  ordinary readable Unicode, size/hash metadata, and private atomic file output.
- A result-file write failure after execution must preserve the completed value
  and original receipt, including for ordinary large ASCII/Unicode results.
  Return an explicit recoverable representation instead of a retryable transport
  error; cover synchronous, completed-poll and late-result paths.
- Result-file paths can themselves contain unpaired surrogates on Windows.
  Preserve the exact readable filesystem path through an explicitly encoded
  descriptor, verify a real NTFS file round trip through the complete JSONRPC
  wire serializer, and leave ordinary Unicode/backslash paths unchanged.
- Make the minimum necessary Python change. If transport limitations require
  file externalization or a documented representation fallback, keep the value
  recoverable and report the behavior to the public-documentation owner.
- This task does not choose a result-file retention policy or delete existing
  artifacts. No shared bridge, Reload, live test, push, or release operation.
- Return a seed-relative patch and independent review; the coordinator owns
  public docs, knowledge, final integrated checks, version and local tag.

## Acceptance Criteria

- [x] Freeze a reproducible isolated seed and record main source identity.
- [x] Regression fails before repair at the real result/transport boundaries.
- [x] Repaired values and result-file paths retain exact data through MCP UTF-8.
- [x] Final affected checks, Ruff, mypy, diff and LF pass with a stable candidate.
- [x] Independent Trellis review passes and the bounded patch is handed off.
- [x] Record public guidance and reconcile the new defect with the coordinator.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
