# Python Backend Guidelines

BTAP exposes MCP tools through a Python server and a separate HTTP/WebSocket
bridge. Start with [AGENTS.md](../../../AGENTS.md) and
[CONTRIBUTING.md](../../../CONTRIBUTING.md); their detailed guides are the
authoritative contracts. These files route Trellis tasks to those contracts.

| Guide | Read when changing |
| --- | --- |
| [Directory structure](directory-structure.md) | Module ownership or entry points |
| [Persistence](database-guidelines.md) | State files, tokens, locks, or paths |
| [Error handling](error-handling.md) | Batch target reservations, bounded unknown outcomes, capture bookkeeping, tab-create recovery, or transport failures |
| [Logging](logging-guidelines.md) | Diagnostics or sensitive data handling |
| [Quality](quality-guidelines.md) | Tests, tool contracts, or validation |

For work shared with Claude Code, read the
[collaboration guide](../guides/collaboration.md).
