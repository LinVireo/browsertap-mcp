# Integration design

Use an isolated worktree and cherry-pick only selected committed patches, without copying whole source directories. The candidate omits local Trellis history and the r6 navigation fix, so a tree replacement would lose valid work.

Selected prerequisite commits: 3511448 (portable paths/Windows API absence), dbbc16d (timeout fixture scheduling), 215e34e (HTTP fixture failure evidence), 941d1b3 (HTTP listener reverse DNS), 2f585c5 (clean audit report). Then F1 51ff97b and LOW-5 6b74d07.

F1 adds evidence alongside expired receipts. LOW-5 serializes connection publication and cleanup under the existing driver lock, leaving blocking sends/polls outside it. Neither permits automatic replay.

Wait release must be an internal server-to-bridge operation: preserve requester ownership, active state, authenticated reply admission, result retention and target identity. Reservation status must reflect the actual target mapping. Prove the boundary against late replies and successor operations before adopting the research proposal.

The adopted implementation uses the existing probe timeout branch in the bridge instead of the proposal's extra release RPC. The MCP server marks only its generated selector/text/URL probes with the private `readOnlyProbe` flag; the bridge stores `kind=wait_probe` and releases only an in-progress ordinary lease owned by that requester. Recovery leases and uncertain/dialog states do not qualify. The authenticated local /link client remains a trusted code dispatcher, not a new untrusted caller boundary. Public execute_js and wait_for(js=...) cannot opt into this behavior. An older bridge ignores the flag and remains conservative. A lost response leaves reservation status unknown until queried. The overall wait keeps collecting the same receipt without replay, even when the probe's shorter bridge deadline has already released the tab. No new public tool, cancellation claim, retention renewal, or post-deadline release request is introduced.

The original delayed-reply cause is an independent diagnosis. Use bounded local transport instrumentation and owned browser fixtures; distinguish extension evaluation, wire reception, HTTP completion and MCP completion. Record inability to reproduce honestly.

Review r1 exposed a pre-existing long-wait replay path: after the short probe's silence deadline, collection becomes unknown and the old loop discarded its handle. The collector now continues reading only in-progress receipts. A receipt without a successful condition snapshot (unknown, navigated, completed-without-result or failed) ends the wait with its original operation ID and current reservation state. The caller can still query F1 late evidence; no new probe is dispatched. Released-operation guidance also applies to caller JS released by the ordinary silence cleanup and does not mislabel that JS as read-only.

Public behavior changes update descriptions, bilingual READMEs, and both packaged caller Skills. Keep self-use manager Skills separate. Native acceptance remains evidence-bound and cannot be inferred from offline CI.
