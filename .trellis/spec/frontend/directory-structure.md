# Browser Source Layout

The [chrome_extension](../../../src/browsertap_mcp/chrome_extension) directory
contains the shipped Manifest V3 extension:

- `background.js`: service worker, browser command dispatch, and bridge state.
- `content.js` and `disable_dialogs.js`: page-side extension behavior.
- `popup.html` and `popup.js`: popup controls and their DOM behavior.
- `_locales/en/messages.json` and `_locales/zh_CN/messages.json`: UI strings.
- `manifest.json` and bitmap icons: extension identity and packaged resources.

[page_scripts](../../../src/browsertap_mcp/page_scripts) holds separately loaded
page functions, including `page_outline.js` and `list_groups.js`.

Read [runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md) before
moving or changing injection entry points. Page-script files end in bare
function references such as `pageOutline;`; the Python loader supplies invocation.
Extension source needs manual browser reload; page scripts have a different
loading path. A matching version alone does not prove matching code.
