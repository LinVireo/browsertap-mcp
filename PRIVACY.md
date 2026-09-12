# BrowserTap Bridge Privacy Policy

**Effective Date:** September 12, 2026

## Scope

BrowserTap Bridge is an extension that connects an existing Chromium browser profile to a browsertap-mcp companion on the user's computer. The user separately chooses and configures an MCP client or AI assistant. The project does not operate a backend collection service and does not include analytics or telemetry.

## Data Access And Processing

While connected, BrowserTap keeps tab identifiers, URLs, titles and connection metadata so it can route commands. Fixed content scripts run on matching pages for connection indication and dialog handling. The manifest's `<all_urls>` pattern grants access to all sites covered by that pattern; that access is not a fresh per-command permission.

Commands may read page content, screenshots, cookies, site storage, console or network records, bookmarks, downloads, extension metadata and permission settings. The information depends on the selected website and requested task. It may include sensitive information already present in that page or profile.

The permissions support these data uses:

| Data or setting | Access and purpose |
| --- | --- |
| Open tabs | Read tab identifiers, URLs and titles to select and route browser operations. |
| Page content | Read page text, DOM state and screenshots, and run requested page scripts through scripting and debugger access. |
| Cookies and site storage | Read or change cookies and site storage for requested browser tasks. |
| Console and network activity | Capture console messages and network records through debugger access when requested. |
| Site permissions | Inspect settings and apply temporary permission leases through contentSettings. |
| Content-Security-Policy | Use declarativeNetRequest rules for requested temporary CSP changes. |
| Extension metadata | Read installed extensions and perform requested extension management operations. |
| Bookmarks | Read or change the profile's bookmarks when requested. |
| Download records | Inspect download metadata and start or manage requested downloads. |

## Data Flow

The extension's built-in bridge transport connects to `127.0.0.1` loopback on the same computer. Results are returned to the configured MCP client. If that client uses a remote model or service, it may send task data to that provider under its own terms. Requested page operations can also contact websites or download endpoints.

The maintainer does not receive task data through an analytics or collection endpoint. This does not mean that every task stays offline, nor that BrowserTap controls a third-party AI provider's retention or use of submitted data.

## Local Retention

Chrome extension storage contains configuration, indicator preference, generated client identity and permission-lease metadata. Session storage and process memory hold tab/create bookkeeping, operation results and active captures as required by the feature. Completed operation results have bounded retention and may be evicted under capacity pressure. These are local stores, not a maintainer database.

The companion stores its bridge token and local logs in the reported state directory. Logs are designed to redact sensitive URL parts and exclude the bridge token. Large results, requested screenshots and downloads may be written to local files. Local files remain until the user or relevant application removes them; removing the extension does not remove companion logs or downloaded files.

## Use And Controls

The project does not sell task data or use it for advertising, creditworthiness, or unrelated profiling. Use of data through this extension is limited to its declared browser-automation purpose and applicable Chrome Web Store Limited Use requirements. Separate client providers remain responsible for their own policies.

Users can disconnect the MCP client, stop the companion, remove the extension, or control its browser site access. Permission leases can be reset. Persistent local state and exported/downloaded files can be removed separately after active operations are stopped. BrowserTap is not a security boundary against software that already controls the user's account.

Site-permission leases have a configured duration of 60 to 600 seconds. BrowserTap records their expiry and attempts to restore the prior settings when it processes lease expiry, reset or startup recovery. Failed restoration retains recovery metadata, and unsupported restoration can require manual recovery. Stopping the extension can delay cleanup until it runs again.

## Contact

- **Support:** https://github.com/LinVireo/browsertap-mcp/issues
- **Security Reporting:** https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md
- **Project Repository:** https://github.com/LinVireo/browsertap-mcp
