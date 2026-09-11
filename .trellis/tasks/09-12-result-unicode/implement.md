# Bounded implementation

1. Create a fresh isolated worktree from the current shared HEAD. Snapshot the
   source/test dependencies needed from the dirty shared checkout, verify
   before/after hashes, and freeze a seed tree without touching the main index.
2. Add a new focused regression file and capture red results for both the
   successful JS result and MCP wire encoding; all inputs are synthetic.
3. Implement the smallest lossless correction in server result serialization
   and its envelope adapter only if the transport proof requires it.
4. Run relevant existing and new tests, Ruff and mypy using explicit isolated
   PYTHONPATH. Keep verification artifacts in a new ignored output directory.
5. Freeze source and submit for independent check. Address findings, package
   only the own delta from the seed, and notify the coordinator through a new
   shared research receipt. Main source, docs and release remain coordinator-owned.
