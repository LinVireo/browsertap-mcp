# BrowserTap Bridge Privacy Policy

**Effective Date:** September 13, 2026

## Scope

BrowserTap Bridge is an extension that connects an existing Chromium browser profile to a browsertap-mcp companion on the user's computer. The user separately chooses and configures an MCP client or AI assistant. The project does not operate a backend collection service and does not include analytics or telemetry.

## Data Access And Processing

While connected, BrowserTap keeps tab identifiers, URLs, titles and connection metadata so it can route commands. Fixed content scripts run on matching pages for connection indication and dialog handling. The manifest's `<all_urls>` pattern grants access to all sites covered by that pattern; that access is not a fresh per-command permission.

Commands may read page content, screenshots, cookies, site storage, console or network records, bookmarks, downloads, extension metadata and permission settings. The information depends on the selected website and requested task. It may include sensitive information already present in that page or profile.

Depending on the requested page and tools, this information may include personal
identifiers, authentication cookies or headers, personal communications, health
information, financial and payment information, location information, browsing
activity and other website content. BrowserTap does not separately profile these
categories; they may be present in a page, screenshot, cookie or network record
the user chooses to expose to their client.

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
| Native Messaging | Start the installed local companion and exchange browser commands and results with it. |

For requested page execution, the extension may temporarily remove
Content-Security-Policy response headers for the selected tab. These rules are
tab-scoped session rules and are reference-counted during concurrent work.
BrowserTap attempts to remove them during cleanup and sweeps stale rules on
worker startup. Session rules do not persist across browser restarts.

## Data Flow

The extension connects to the installed Native Messaging host on the same computer. The host starts or connects to the local BrowserTap bridge at `127.0.0.1`; the extension uses a WebSocket connection to that loopback address as a fallback. Results are returned to the configured MCP client. If that client uses a remote model or service, it may send task data to that provider under its own terms. Requested page operations can also contact websites or download endpoints.

Native Messaging uses Chrome's local process input/output channel. The host talks to the bridge over authenticated local HTTP. The loopback transport uses unencrypted local HTTP and WebSocket connections.

The maintainer does not receive task data through an analytics or collection endpoint. This does not mean that every task stays offline, nor that BrowserTap controls a third-party AI provider's retention or use of submitted data.

If you choose to send a support report, the information you include reaches the
support channel you selected. Public GitHub issues are visible to others; do not
include credentials or private browsing content in them.

## Local Retention

Chrome extension storage contains configuration, indicator preference, generated client identity and permission-lease metadata. Session storage and process memory hold tab/create bookkeeping, operation results and active captures as required by the feature. Completed operation results have bounded retention and may be evicted under capacity pressure. These are local stores, not a maintainer database.

The generated client identifier distinguishes connected browser/profile
instances on the local bridge; it is not derived from the user's identity or
hardware.

The companion stores its bridge token and local logs in the reported state directory. Logs are designed to redact sensitive URL parts and exclude the bridge token. Large results, requested screenshots and downloads may be written to local files. Local files remain until the user or relevant application removes them; removing the extension does not remove companion logs or downloaded files.

See [SECURITY.md](SECURITY.md) for token access protections and their limits, and
for the URL information retained in local logs. Review logs before sharing them.

## Use And Controls

The project does not sell task data or use it for advertising, creditworthiness, or unrelated profiling. Use of data through this extension is limited to its declared browser-automation purpose and applicable Chrome Web Store Limited Use requirements. Separate client providers remain responsible for their own policies.

Users can disconnect the MCP client, stop the companion, remove the extension, or control its browser site access. Permission leases can be reset. Persistent local state and exported/downloaded files can be removed separately after active operations are stopped. BrowserTap is not a security boundary against software that already controls the user's account.

Site-permission leases have a configured duration of 60 to 600 seconds. BrowserTap records their expiry and attempts to restore the prior settings when it processes lease expiry, reset or startup recovery. Failed restoration retains recovery metadata, and unsupported restoration can require manual recovery. Stopping the extension can delay cleanup until it runs again.

## Children

This is a developer tool and is not directed at children.

## Changes

Material changes will be published in this file and in the project's
`CHANGELOG.md`. The version of this policy that applies is the one published
alongside the extension version you have installed.

## Contact

- **Support:** https://github.com/LinVireo/browsertap-mcp/issues
- **Security Reporting:** https://github.com/LinVireo/browsertap-mcp/blob/main/SECURITY.md
- **Project Repository:** https://github.com/LinVireo/browsertap-mcp
