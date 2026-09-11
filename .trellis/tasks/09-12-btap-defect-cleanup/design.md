# Defect cleanup design

Use a source-backed ledger rather than treating historical bug prose as current truth. Each entry records source, reproducibility, owner, affected paths, repair, focused verification and final disposition. The categories are confirmed defect, superseded record, environment/acceptance gap, and product decision.

Repair slices follow existing module boundaries: configuration and diagnostics; transport/client identity; page observation and timeout cleanup; physical-input lease recovery; any separately confirmed tool behavior. Avoid overlapping writers. The coordinator integrates reviewed patches and owns public contract documentation, final runtime checks, versioning and knowledge reconciliation.

Baseline and final evidence are separate. Loaded MCP, bridge and extension identity are independently checked; matching package versions alone are insufficient. Python changes can be tested in fresh processes. Extension source is verified offline until a permitted manual Reload makes that build available.
