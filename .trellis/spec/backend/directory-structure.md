# Python Module Ownership

Implementation lives in [src/browsertap_mcp](../../../src/browsertap_mcp).
Follow existing module boundaries:

| Module | Responsibility |
| --- | --- |
| [server.py](../../../src/browsertap_mcp/server.py) | MCP tools, result envelopes, and tool-facing validation |
| [browser_bridge.py](../../../src/browsertap_mcp/browser_bridge.py) | HTTP/WebSocket transport, pending requests, and connection state |
| [bridge.py](../../../src/browsertap_mcp/bridge.py) | Bridge process startup, identity, and lifecycle |
| [paths.py](../../../src/browsertap_mcp/paths.py) | Runtime paths and legacy location resolution |
| [cli.py](../../../src/browsertap_mcp/cli.py) | User-facing command entry points |

Before changing startup or process state, read
[runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md).
MCP, bridge, and extension changes require different reload actions.

Keep `paths.py` a leaf module: its `state_dir(create=False)` is an example of a
read-only path lookup. Put regression tests in [tests](../../../tests), following
the affected module's existing fixtures. Release helpers live in
[scripts](../../../scripts), outside the shipped runtime package.
