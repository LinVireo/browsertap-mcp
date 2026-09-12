# Canonical reconciliation update

Owner: codex-resume-01a0881c. Base remains f63c4a7.

The previous 34-file reconciliation is retained as a historical checkpoint.
At 02:40 UTC, independent readback found new changes in runtime_identity.py,
test_runtime_identity.py and test_serialization_routes.py, plus moved bytes in
server.py and background.js. Their author is still unknown; no attribution is
inferred from matching code.

The new tests expose a Python CDP fallback dialog-controller gap that the
extension-only F6 v2 work does not cover. The runtime-identity worker is
reproducing and fixing this in an isolated candidate. The observation worker
is freezing and independently reviewing all five new deltas. Root is keeping
the canonical checkout unchanged until their contents are reconciled.

Active canonical writers should retain current bytes and place subsequent
handoffs in a separate research record rather than changing the source during
final integration. Root will recheck every reviewed path, HEAD and index, keep
a scoped recovery stash and byte snapshots, and fast-forward only to the
candidate that passed committed-tree gates. New unmatched edits trigger another
bounded reconciliation; nothing is silently overwritten.

The candidate version is 0.5.1. Finalization and v0.5.1-rc.1 remain pending.
The former live owner released the browser claim, but its failed 0.5.0 attempts
remain failed evidence. No reload, page refresh, shared-venv reinstall, remote
push or publication is part of this integration.
