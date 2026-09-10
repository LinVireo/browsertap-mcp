# Browser JavaScript Guidelines

BTAP's browser code is a vanilla JavaScript Manifest V3 extension plus injected
page scripts. The existing source uses neither React nor TypeScript. Start with
[AGENTS.md](../../../AGENTS.md) and the relevant implementation guide.

| Guide | Read when changing |
| --- | --- |
| [Directory structure](directory-structure.md) | Extension or page-script ownership |
| [Popup components](component-guidelines.md) | Popup DOM, controls, or localization |
| [Events and lifecycle](hook-guidelines.md) | Listeners, timers, or page injection |
| [State management](state-management.md) | Storage, tab generations, or reconnect state |
| [Boundary validation](type-safety.md) | Messages, parameters, or result shapes |
| [Quality](quality-guidelines.md) | JavaScript checks or browser verification |

For work shared with Codex, read the
[collaboration guide](../guides/collaboration.md).
