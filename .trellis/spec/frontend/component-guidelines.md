# Popup DOM And Controls

Follow [popup.html](../../../src/browsertap_mcp/chrome_extension/popup.html) and
[popup.js](../../../src/browsertap_mcp/chrome_extension/popup.js). Components are
native HTML controls with JavaScript event listeners and the existing CSS.

- Keep the popup's compact layout, labels, focus behavior, and native controls.
- Use `chrome.i18n` and the existing `localizeDocument` mapping. Update both
  locale message files when visible strings change.
- Follow the existing `textContent` assignments for dynamic text.
- Keep cookie refresh and clipboard copy as separate explicit actions.
  Opening the popup does not authorize either action. Clear stale rendered
  cookie state before a refresh so failed reads cannot copy old data.
- The popup does not configure the bridge port. Preserve that boundary and the
  extension's manual-reload and self-disable guards.

Read [runtime lifecycle](../../../docs/agent-guides/runtime-lifecycle.md) before
changing extension settings or identity, then use the
[browser quality guide](quality-guidelines.md) for verification.
