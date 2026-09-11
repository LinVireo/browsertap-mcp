# Setup diagnostics contract

## Scope and trigger

Read when changing extension-handshake diagnostics or their callers. After a
bridge restart, absent runtime data once requested an extension Reload even
though reconnection alone recovered it. Process loading and build-stamp rules
remain in [runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md).

## Signatures

`server.get_setup_status() -> dict[str, Any]` takes no parameters. The MCP tool
and `browsertap doctor` expose its diagnostics; live preflight consumes them.

## Contracts

- `extension_status_available` reports whether runtime status was obtained from
  bridge-forwarded fields or a successful extension status response.
- When false, `reload_extension_required` is false and
  `missing_extension_capabilities` is empty. Compatibility remains unknown.
- A valid legacy reply with absent required fields still runs compatibility
  checks. Missing status and a confirmed capability gap are different inputs.
- These are successful diagnostic results in the MCP envelope. An unavailable
  extension still prevents live preflight from starting.
- No request fields, environment variables or process-reload mechanism are added.

## Validation and action matrix

| Observed state | Setup status | Action |
| --- | --- | --- |
| No runtime status; bridge diagnosis is `starting` | `starting` | `wait_for_extension` |
| No runtime status after startup | `extension_unavailable` | `check_extension_connection` |
| Confirmed old bridge, including during startup | `stale_bridge` | `restart_bridge` |
| Confirmed newer component than the MCP process | `stale_package` | `restart_mcp_session` |
| Runtime reply proves incompatible extension | `stale_extension` | `reload_extension` |
| Fallback reply arrives after a starting snapshot and passes compatibility | `healthy` | `none` |

## Good, base and bad cases

- Good: matching versions, capabilities and worker stamp report healthy.
- Base: no extension reply reports waiting or connection inspection, without Reload.
- Bad: a real legacy reply missing required capabilities keeps the Reload verdict.

## Required tests

`tests/test_setup_diagnostics.py` covers unavailable status, startup recovery,
legacy replies, stale-component priority and build stamps.
`tests/test_live_preflight.py` checks refusal without a false stale-build label.
`tests/test_result_envelope.py` preserves diagnostic success for waiting and unavailable states.
Run these and the checks in [quality guidelines](quality-guidelines.md).

## Wrong and correct inference

Wrong: compare an absent extension version with the package version and infer
that the worker is outdated. Correct: first establish that runtime status was
received, then apply the existing version, protocol, capability and stamp rules.
