# Browser Events And Lifecycle

Here, hooks are browser event listeners and injection entry points. Follow the
native APIs already used in the extension.

- See the `DOMContentLoaded` handler in
  [popup.js](../../../src/browsertap_mcp/chrome_extension/popup.js) for localization,
  control binding, and settings initialization.
- Before changing worker listeners, timers, alarms, or reconnect logic, read
  [runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md). The live
  worker timer and post-collection alarm recovery serve different purposes.
- Preserve extension self-disable and manual-reload guards. A browser event or
  retry must not revive a stale extension generation.
- Keep bare page-function references at the end of
  [page_outline.js](../../../src/browsertap_mcp/page_scripts/page_outline.js) and
  [list_groups.js](../../../src/browsertap_mcp/page_scripts/list_groups.js).
  Calling these in the file changes the Python loader's execution contract.

For input listeners or debugger attachment, also read
[tabs and input](../../../docs/agent-guides/tabs-and-input.md).
