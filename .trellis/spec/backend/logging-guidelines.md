# Runtime Logging

Read [transport authentication](../../../docs/agent-guides/transport-auth.md)
before logging connection or browser data. Read
[runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md) before
changing log rotation or process ownership.

- MCP stdio logging goes to stderr so stdout remains the protocol channel.
  See `configure_stdio_logging` in [server.py](../../../src/browsertap_mcp/server.py).
- Follow the payload boundary in
  [transport authentication](../../../docs/agent-guides/transport-auth.md#8-odds-and-ends):
  redact URLs/patterns at their call sites, hash arbitrary protocol identifiers,
  and log fixed operation names plus exception types at failure boundaries.
  Exception objects, tracebacks and source lines can contain caller scripts;
  exclude them along with request/result bodies.
- Use the module's existing logger and levels. Preserve useful operation and
  process identity without serializing entire request payloads.
- Each process owns its log rotation. Follow `rotate_own_log` in
  [bridge.py](../../../src/browsertap_mcp/bridge.py), including startup failures.

Behavioral regressions are in
[test_log_payload_boundary.py](../../../tests/test_log_payload_boundary.py) and
[test_server_log_boundaries.py](../../../tests/test_server_log_boundaries.py).
