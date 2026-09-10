# Shared Development Guides

[AGENTS.md](../../../AGENTS.md) and its linked implementation guides define
BTAP's contracts. Load the relevant backend or browser index before editing.

| Guide | Read when |
| --- | --- |
| [Codex and Claude Code collaboration](collaboration.md) | Assigning work, handing off a change, or requesting independent review |
| [Code reuse](code-reuse-thinking-guide.md) | Adding a helper or repeating existing logic |
| [Cross-layer changes](cross-layer-thinking-guide.md) | Changing an MCP, bridge, extension, or page-script boundary |

Use `rg` to find existing definitions and callers before changing a shared
value. Review findings need file references and evidence from the current tree;
trace the actual data source and intended contract before implementing a fix.
