# JavaScript Boundary Validation

This source uses JavaScript with runtime checks. Keep validation at message and
tool boundaries and follow the existing result schema.

Read [tool contracts](../../../docs/agent-guides/tool-contracts.md) before changing
public arguments, defaults, or return shapes, and
[transport authentication](../../../docs/agent-guides/transport-auth.md) before
changing bridge messages or authentication.

- Trace values across [server.py](../../../src/browsertap_mcp/server.py),
  [browser_bridge.py](../../../src/browsertap_mcp/browser_bridge.py), and
  [background.js](../../../src/browsertap_mcp/chrome_extension/background.js).
  Keep missing, null, false, and unknown outcomes distinct where the protocol does.
- Preserve result identity, error classification, and retry evidence when
  forwarding an extension response; a timeout does not establish non-execution.
- Update both caller documentation languages and regression tests with a public
  contract change. Avoid adding a second local interpretation of shared fields.

[test_extension_command_guards.py](../../../tests/test_extension_command_guards.py)
and [test_result_envelope.py](../../../tests/test_result_envelope.py) provide
examples of boundary regression coverage.
