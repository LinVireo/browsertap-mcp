# Browser State Ownership

Read [tabs and input](../../../docs/agent-guides/tabs-and-input.md) before changing
tab targeting, cleanup, lifecycle generations, or debugger ownership. Read
[runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md) for worker
restart and persisted generation state.

- [background.js](../../../src/browsertap_mcp/chrome_extension/background.js)
  owns the extension's live connection and browser command state. Persisted
  settings and generations use the existing `chrome.storage` paths.
- Tab IDs are transient. Preserve ownership and generation evidence across
  reconnects. Treat unreadable generation storage as a failure, not empty state.
- Explicit targets cannot silently move to another tab. Follow the existing
  omitted-default policy for selecting a replacement.
- [popup.js](../../../src/browsertap_mcp/chrome_extension/popup.js) owns transient
  rendered-cookie state; reset it before fetching new data. Persist only the
  settings already intended to survive popup closure.
- Cleanup owns only tabs created by that operation or test. User tab activity
  is context, not ownership.
