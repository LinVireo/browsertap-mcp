# File-Based Persistence

The runtime has no ORM, database schema, or migration framework. Its persistent
state consists of files and browser extension storage.

Read [runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md) before
changing PID files, locks, generations, or state locations. Read
[transport authentication](../../../docs/agent-guides/transport-auth.md) before
changing token persistence or socket ownership.

- Resolve runtime locations through [paths.py](../../../src/browsertap_mcp/paths.py).
  `state_dir(create=False)` preserves read-only lookups; creation is explicit.
- Follow the existing atomic-write pattern for user output and state updates.
  See `_atomic_write_bytes` in [server.py](../../../src/browsertap_mcp/server.py)
  and [file-write tests](../../../tests/test_file_write_safety.py).
- Preserve lifecycle generation evidence. An unreadable store is an error state,
  not proof that no prior generation exists.
- Keep runtime data out of the repository and distribution archives; follow
  [CONTRIBUTING.md](../../../CONTRIBUTING.md) for packaging exclusions.
