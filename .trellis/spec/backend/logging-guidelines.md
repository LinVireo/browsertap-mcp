# Runtime Logging

Read [transport authentication](../../../docs/agent-guides/transport-auth.md)
before logging connection or browser data. Read
[runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md) before
changing log rotation or process ownership.

- MCP stdio logging goes to stderr so stdout remains the protocol channel.
  See `configure_stdio_logging` in [server.py](../../../src/browsertap_mcp/server.py).
- Redact at the logging site with the existing `redact_url` and `redact_pattern`
  helpers in [browser_bridge.py](../../../src/browsertap_mcp/browser_bridge.py).
  Tokens, cookies, page content, and sensitive URL values must not enter logs.
- Use the module's existing logger and levels. Preserve useful operation and
  process identity without serializing entire request payloads.
- Each process owns its log rotation. Follow `rotate_own_log` in
  [bridge.py](../../../src/browsertap_mcp/bridge.py), including startup failures.

Regression examples are in [test_log_redaction.py](../../../tests/test_log_redaction.py).
