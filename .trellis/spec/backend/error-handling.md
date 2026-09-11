# Error And Retry Contracts

Read [tool contracts](../../../docs/agent-guides/tool-contracts.md) for public
results and [tabs and input](../../../docs/agent-guides/tabs-and-input.md) for
target resolution, uncertain input delivery, and recovery.

- Preserve the structured result envelope in
  [server.py](../../../src/browsertap_mcp/server.py). `_result_envelope` and
  `_result_retryable` carry the existing classification rules; an explicit
  non-retryable result must remain non-retryable.
- Preserve distinctions between delivery failure, execution failure, and an
  unknown outcome. `BridgeNoResponseError` and `PageExecutionError` in
  [browser_bridge.py](../../../src/browsertap_mcp/browser_bridge.py) illustrate
  the transport boundary. Keep `delivery_state` and `retry_safe` evidence.
- Inspect state after an uncertain side effect. Replaying input can duplicate
  the action even when the first response was lost.
- Explicit tab targets keep their identity; fallback selection is governed by
  the existing policy for omitted defaults.

Use [result-envelope tests](../../../tests/test_result_envelope.py) and
[execution-failure tests](../../../tests/test_execution_failure_contract.py)
as regression examples, including failure and cleanup paths.

## Unicode at result and byte boundaries

Browser strings and Windows filenames can contain lone UTF-16 surrogate code
units. Preserve their values with explicit JSON escaping when a downstream
UTF-8 encoder cannot represent them. A successful dictionary conversion alone
does not establish that the final response can be sent.

- `_export_json_result` preserves the completed value and original receipt.
  A file-write `OSError` uses the inline `result_json` fallback; it does not
  make a completed operation retryable or dispatch it again.
- `result_file_scope` and `result_json_scope` identify a JS value, complete
  envelope, or complete adapted MCP call result. Interpret the declared scope
  before extracting the original data. The public tool descriptions and caller
  Skills define descriptor placement and decoding.
- If `result_file_encoding` is `json`, decode the path string once before
  opening the file, then decode its JSON content. Normal paths carry no marker.
  Replacing a descriptor clears obsolete path, encoding, size and hash fields.
- HTTP and CLI JSON use ASCII escapes so Bottle or a strict output stream
  cannot fail while encoding valid browser strings. Escaping must preserve
  decoded values and field names. Safe stderr diagnostics preserve exit codes.
- Command-lock and extension relative-path hashing preserve ordinary UTF-8
  bytes. Exceptional surrogate code units use `surrogatepass`; lossy replacement
  could merge distinct identities. This encoding is for hash input, not wire JSON.

Regression checks must exercise the final byte boundary: a complete
`JSONRPCMessage.model_dump_json()` and strict UTF-8 for MCP, real Bottle WSGI
output for HTTP, strict streams for CLI, and real temporary Windows directories
for filename cases. A mocked filename remains supplementary evidence. Verify
normal Chinese/emoji, literal backslash escapes, lone high and low surrogates,
file-write failure, descriptor replacement, and unchanged receipt/retry facts.

Examples: `tests/test_result_unicode.py`, `tests/test_http_response_unicode.py`,
`tests/test_unicode_boundary_regressions.py` and `tests/test_bookmark_backup.py`.
These checks are offline; they do not establish a browser Reload or live acceptance.

## Scenario: reserving the complete target set of a batch

### 1. Scope / Trigger

- Trigger: `BrowserBridge.ext_cmd` receives a `batch` with CDP child commands
  that inherit or override the top-level `tabId`.
- A single batch can operate on several tabs. Its outer target alone is not
  the set of tabs the extension will actually use.

### 2. Signatures

- `BrowserBridge.ext_cmd(cmd, client_id=None, timeout=15, *,
  requester_id=None, operation_id=None)` is the local and remote boundary.
- Batch payload: `{"cmd": "batch", "tabId": 1, "commands":
  [{"cmd": "cdp", "tabId": 2, "method": "Runtime.evaluate"}]}`.
- `guard_targets(namespace, targets)` and `PendingOperations.reserve(...,
  targets=targets)` must receive the same normalized target set.

### 3. Contracts

- Resolve every CDP child's actual target before dispatch. A valid explicit
  child target wins; an omitted or null target inherits the top-level target.
  Explicit child targets also work when the batch has no top-level `tabId`.
- Effective CDP targets must be positive int32 tab IDs. Normalize valid
  numeric-string IDs and integer-valued floats in both the outgoing payload
  and the `client:tab` reservation identity. Reject invalid or missing CDP
  targets before sending any part of the batch.
- Check the complete deduplicated target set before sending. If any target is
  busy, the entire batch stays undispatched. Keep the extension's single-batch
  execution and multi-tab support.
- The daemon reserves all targets as one operation. An uncertain timeout keeps
  every reserved target until that operation's bounded release or completion.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Child has a valid explicit target | Use and reserve that target |
| Child omits target, outer target is valid | Inherit and reserve outer target |
| No outer target, all CDP children have targets | Support all explicit child targets |
| Numeric-string alias such as `"02"` | Normalize to the same identity as integer `2` |
| Any actual target is busy | `target_busy`; zero batch dispatches |
| Invalid target or missing effective CDP target | Fail before dispatch |
| Timeout after dispatch | Keep the whole target set reserved |

### 5. Good/Base/Bad Cases

- Good: outer tab 1 and child tab 2 reserve both before one send.
- Base: a normal same-tab batch reserves that tab once.
- Bad: reserve only outer tab 1 while a child modifies another caller's tab 2.

### 6. Tests Required

- `tests/test_command_scope.py`: exercise remote calls, inherited and explicit
  child targets, numeric aliases, invalid targets and zero dispatch on conflict.
- `tests/test_pending_bridge_operations.py`: exercise the local daemon with
  a fake transport; assert one operation retains every target after timeout
  and releases them together after a terminal reply.

### 7. Wrong vs Correct

#### Wrong

```python
targets = [f"{client_id}:{cmd['tabId']}"]
# Child commands may execute against other tabs.
```

#### Correct

```text
Normalize payload and collect every effective CDP target.
Guard the complete set, reserve it as one operation, then send the batch once.
```

## Scenario: bounded retention for an unknown execution outcome

### 1. Scope / Trigger

- Trigger: a dispatched page or extension command has an uncertain final
  outcome, including a `cdp_timeout` reply or a manual-dialog lease that
  disappeared before reporting the execution result.
- The operation registry must release its target after a bounded retention
  window; an uncertain result must never be silently treated as success.

### 2. Signatures

- `PendingOperations.reserve(..., caller_budget: float | None = None)` stores
  the resolved silence deadline on the operation.
- `PendingOperations.complete(operation_id, result, *,
  outcome_unknown: bool = False)` records an unknown outcome.
- `PendingOperations.observe_manual_execution(recovery_id)` records an unknown
  outcome when a manual recovery lease no longer proves that execution ended.
- `BrowserBridge.get_execute_js_result(...)` returns local and remote receipts;
  `_sync_pending_operations()` aligns transient bookkeeping with retained IDs.

### 3. Contracts

- `_Operation.started_at` anchors an unanswered `in_progress` operation.
- `_Operation.unknown_since` anchors the first transition to
  `outcome_unknown`; repeated observations do not extend it.
- `silent_deadline_seconds(budget)` returns `min(MAX_SILENT_SECONDS,
  budget + SILENT_GRACE_SECONDS)` for a finite positive budget, and the ceiling
  for an absent or unusable budget.
- Before the deadline, `read()` reports `status: "outcome_unknown"` and keeps
  the wire receipt and existing reservation. `reservation_held` is derived
  from the actual target/recovery owner maps, not from operation status.
- After the deadline, the record becomes `abandoned`, the target is released,
  and the receipt/diagnosis remains readable until the normal result TTL.
  Callers receive `retry_safe: false` for an uncertain execution.
- The first authenticated late terminal reply may add `late_result` and
  `late_reply_age` to an abandoned receipt. It does not settle capture/dialog
  state again, restore a reservation, change the original receipt or renew TTL.
- Generated selector/text/URL probes use the internal `wait_probe` kind. Their
  existing bridge timeout releases an ordinary `in_progress` lease while the
  receipt remains collectible. Caller JS, recovery leases and already-unknown
  or dialog-blocked operations do not qualify. A lost response leaves release
  unconfirmed until the caller queries; an older bridge remains conservative.
- A legitimate remote `status: unknown` receipt preserves the original error,
  `operation_status`, abandonment metadata and `reservation_held: false`.
  Its diagnostic `error` does not by itself make the receipt an RPC failure;
  real transport, missing-operation and access errors still raise.
- Once an operation leaves `retained_ids()`, remove its result, ACK and
  `_capture_commands` bookkeeping. Capture ownership is independent: an absent
  reply is not a successful stop, and bookkeeping expiry never releases it.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| No reply, silence deadline not reached | Keep `in_progress`; ordinary operations retain their target reservation |
| Generated read-only probe reaches its bridge wait deadline | Release its ordinary lease, retain receipt and original silence/retention bounds |
| No reply, deadline reached | `abandoned`, `js_return_lost: true`, release target |
| Wait collection ends without a successful condition snapshot | Return the original receipt and current reservation status; never dispatch again from an unknown, lost or failed result |
| Unknown outcome, `unknown_since` deadline not reached | Keep receipt and reservation |
| Unknown outcome, `unknown_since` deadline reached | `abandoned_reason: unknown_outcome_reservation_ttl`, release target |
| `blocked_by_dialog` | Keep reservation for explicit manual recovery |
| First authenticated late terminal reply after abandonment | Retain alongside the original receipt; never attach it to a new operation |
| Duplicate, uncertain, foreign or ended-lifecycle late reply | Do not replace retained terminal evidence or affect successor state |
| Remote query of a retained abandoned receipt | Preserve the local receipt and uncertainty fields |
| Capture operation leaves retained set without reply | Reclaim command bookkeeping; preserve real capture owner |

### 5. Good/Base/Bad Cases

- Good: a manual lease is observed after the original budget elapsed; the
  operation gets a fresh bounded window anchored at `unknown_since`.
- Base: a normal timeout reply is retained long enough for polling, then
  expires and frees the tab.
- Bad: age `outcome_unknown` from `started_at` only; a late observation is
  immediately abandoned and the caller cannot inspect its diagnostic receipt.

### 6. Tests Required

- `tests/test_silent_operation_expiry.py` and `tests/test_late_operation_results.py`:
  assert the fresh manual grace window, expiry/release, receipt retention,
  per-budget deadlines, authenticated late evidence and rejection boundaries.
- `tests/test_read_only_wait_release.py` and `tests/test_wait_reservation_contract.py`:
  assert release/receipt separation, requester and kind guards, successor
  isolation, conservative caller JS, and local/HTTP/old-bridge/lost-response paths.
- `tests/test_pending_bridge_operations.py` and `tests/test_pending_execution_results.py`:
  assert bridge/MCP envelopes preserve `operation_status`, `reservation_held`,
  `retry_safe`, and the unknown diagnostic.
- `tests/test_capture_ownership.py`: advance a fake clock through no-reply
  abandonment and result expiry; assert bookkeeping is removed and another
  requester still cannot take over or stop the capture.
- `tests/test_live_agent_concurrency.py -m live`: exercise the real timeout and
  manual recovery path against the running bridge.

### 7. Wrong vs Correct

#### Wrong

```python
operation.status = "outcome_unknown"
# The expiry sweep still measures from operation.started_at.
```

#### Correct

```python
if operation.status != "outcome_unknown":
    operation.unknown_since = time.monotonic()
operation.status = "outcome_unknown"
```

## Scenario: recovering a tab create with a missing operation record

### 1. Scope / Trigger

- Trigger: `open_new_tab` is called with an existing `operation_id`, and its
  first status probe returns `not_found`.
- Missing records can follow browser restart, record expiry, or a different
  browser selection. They do not establish whether a tab was created.

### 2. Signatures

- `open_new_tab(url, timeout=15.0, active=False, session_id=None,
  owner_id=None, operation_id=None, client_id=None)`.
- `list_tabs()` takes no `client_id`; inspect the returned browser identities.
- `close_tabs(tab_id=<exact session_id>, owner_id=<returned owner_id>)` remains
  subject to this MCP task's ownership registration and generation check.

### 3. Contracts

- First-probe `not_found` returns `status: unknown`, `may_have_created: true`,
  `retry_safe: false`, the original owner capability, and
  `reconciliation.resume_required: false`.
- `recovery.instruction` directs inspection of the actual browser instead of
  another identical recovery read. It must not declare a fresh create safe.
- A matching URL, unchanged tab count, or absence of matching tabs proves
  neither ownership nor that the original create did not happen. An owner
  capability alone does not register a tab or permit cleanup.
- A pre-dispatch response with `may_have_created: false` and `retry_safe: true`
  directs a fresh call without `operation_id`, after resolving the failure.
- An unreadable probe, or `pending` followed by `not_found`/`unknown`, retains
  conservative read-only recovery. Recovery never dispatches another create.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Initial create cannot dispatch | `may_have_created: false`, `retry_safe: true`; omit operation ID on retry |
| Recovery first probe is `not_found` | Preserve uncertainty and owner ID; stop repeated reads; inspect browser |
| Recovery probe raises or returns `unknown` | Preserve uncertainty and read-only recovery instruction |
| Recovery sees `pending`, then record disappears | Preserve uncertainty; never recreate the operation |
| Completed record has exact client/tab/generation | Register only that identity for this MCP task |
| Ownership or current generation cannot be established | Leave tabs untouched and report unresolved outcome |

### 5. Good/Base/Bad Cases

- Good: a missing recovery record gives the caller an observation step without
  promising that a new tab would be a non-duplicate.
- Base: a completed durable record resolves to its exact tab identity.
- Bad: infer `may_have_created: false` or `new_operation_safe: true` from
  `not_found`, or close a same-URL tab using an unregistered owner capability.

### 6. Tests Required

- `tests/test_tab_create_recovery_contract.py`: assert first-probe `not_found`
  preserves uncertainty/owner, sets `resume_required: false`, names
  `list_tabs()`, makes only status calls, and does not register ownership.
- Preserve cases for unknown probes, pending-to-missing transitions, fresh
  pre-dispatch retries, lost ACKs, and completed identity reconciliation.
- `python -m scripts.check_tool_docs`: verify both README tool tables and both
  packaged caller Skills agree with the public description.

### 7. Wrong vs Correct

#### Wrong

```python
if probe_state == "not_found":
    return {"may_have_created": False, "retry_safe": True}
```

#### Correct

```python
if resuming and first_probe_state == "not_found":
    return unknown_result(
        {**probe_info, "resume_required": False},
        may_have_created=True, retry_safe=False, terminal=True,
    )
```
