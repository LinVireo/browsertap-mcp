# Trellis spec assessment

Author: `codex-coverage-01a09080`. Date: 2026-09-12.

Applied the project's `trellis-update-spec` review step. This integration changes
tests only; it adds no API, response contract, persistence mechanism or dependency.

The only candidate-to-original adaptation is the failed wait collector case in
`tests/test_server_coverage.py`. The original checkout's
`server._poll_wait_condition()` retains the operation receipt on a failed/lost
result, sets `operation_status`, and stops without dispatching the script again.
The previous candidate assertion expected an empty receipt. The integrated test
now checks the full retained receipt as well as the existing single-dispatch and
poll-count assertions.

This behavior is already explicit in
`.trellis/spec/backend/error-handling.md`, under the bounded-unknown-outcomes
validation matrix: a wait collection without a successful condition snapshot
returns the original receipt and current reservation status, without redispatch.
The listed `tests/test_wait_reservation_contract.py` regressions cover the same
contract. A second spec would duplicate the current one, so no spec edit is needed.

Shared task ownership, per-session pointers, file hashes and reply records are
already covered by `AGENTS.local.md` section 7 and the collaboration guide.
The setup owner has separately added `backend/setup-diagnostics.md`; that change
remains attributable to the setup task. This coverage task does not edit it.
