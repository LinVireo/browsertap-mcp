# BrowserTap Bridge Privacy Policy

**browsertap-mcp / BrowserTap Bridge**
Last updated: 2026-09-09

## Summary

BrowserTap Bridge connects an existing Chromium browser profile to a
`browsertap-mcp` companion running on the user's computer. The project does not
operate a backend collection service and does not include analytics or telemetry.

## What this software is

BrowserTap Bridge is a Chrome extension that connects the Chrome you are already
signed in to with a Model Context Protocol (MCP) server running on the same
computer. That server is started by you and is used by an AI assistant of your
choosing (for example, a coding agent in your editor). The extension is one half
of a local bridge; it is not useful on its own.

## What the software can access

While connected, BrowserTap automatically maintains tab identifiers, URLs, titles
and connection metadata to route commands. Fixed content scripts load on matching
pages for connection indication and dialog handling. This bookkeeping does not
wait for a separate command to read a page.

Commands may access the following. The data depends on the selected tab and
requested operation and may include sensitive information already present in that
page or profile.

- **Open tabs** — titles, URLs and connection metadata.
- **Page content** — text, structure and screenshots when requested.
- **Cookies and site storage** — for sites named by a command.
- **Console and network activity** — for a tab while capture is running.
- **Bookmarks, downloads and metadata about installed extensions** — for management tools.
- **Site permissions** — granted to one origin for 60 to 600 seconds, then restored.

Depending on the requested page and tools, this information may include personal
identifiers, authentication cookies or headers, personal communications, health
information, financial and payment information, location information, browsing
activity and other website content. BrowserTap does not separately profile these
categories; they may be present in a page, screenshot, cookie or network record
the user chooses to expose to their client.

Two things about the *breadth* of that access, stated plainly because the
permission list is broader than any single command needs:

- **The extension holds access to all sites** (`<all_urls>`), not a list you
  approve per site. It has to: the whole point is to work in whichever tab you
  are already using, and that tab is not known ahead of time. Access is held
  continuously. Fixed content scripts and tab bookkeeping run automatically;
  command execution targets the selected tab.
- **It can modify page requests.** When the page-execution path requires it,
  the extension temporarily removes Content-Security-Policy response headers
  for the executing tab. Rules are scoped to that tab, reference-counted during
  concurrent work, removed during cleanup and not retained across browser restarts.

## Where that data goes

The extension sends results to your own MCP server over loopback on
`127.0.0.1`, and from there to the AI assistant you configured. Requested page
operations can also contact websites or download endpoints.

**Task data is not automatically sent to the developer.** The extension has no
developer-operated collection endpoint, cloud component, crash reporting or usage
statistics. If you choose to send a support report, the information you include
reaches the support channel you selected. Public GitHub issues are visible to others;
do not include credentials or private browsing content in them.

Two consequences worth stating plainly:

- **The AI assistant you choose is a third party, and this policy does not
  cover it.** If your assistant runs on a remote model provider, page content
  sent to it leaves your machine *through that provider*, under their terms.
  Choosing what to expose to your assistant is your decision, not this
  software's.
- **This software is not a security boundary.** The local bridge port requires a
  bearer token so that other processes on your machine cannot drive your browser
  through it, but the token is a barrier against accidents and casual local
  access, not a guarantee against a determined attacker who already controls
  your user account.

## What is stored, and where

The extension's own stores and the companion's state are on your computer. The
maintainer does not receive them.

- **Kept until the browser closes.** Bookkeeping for tabs the assistant opened —
  including their URLs and titles — lives in Chrome's own
  `chrome.storage.session` and is discarded with the browsing session.
- **Kept across restarts, in `chrome.storage.local`.** Bridge configuration,
  indicator preference, site-permission lease metadata and a generated client id.
  The client id distinguishes connected browser instances on the local bridge;
  it is not derived from the user's identity or hardware.
- **Settings and the bridge token** live in a state directory under your home
  directory or a directory you configure. The token file requests owner-only
  permissions (`0600`) where the platform supports them; Windows access is also
  subject to the directory's ACLs. The loopback transport is local HTTP/WebSocket,
  not an encrypted remote connection.
- **Logs.** The bridge writes a local log file, `bridge.log`, so that you have
  something to attach to a bug report. URLs in it are cut down at the point they
  are written: the origin and a short path are kept, and the query string and
  fragment are replaced with `?...` and `#...`, because that is where OAuth
  codes, signed links and search terms live. The origin is deliberately kept —
  the log would not answer "which tab was this?" without it. The bridge token is
  never written to the log in any form.
- **Results and exported files.** Operation results and active captures are held
  in process memory with bounded retention. Large results, requested screenshots
  and downloads may be written to local files and remain until you or the relevant
  application removes them. Removing the extension does not remove companion logs
  or exported files.
- **No maintainer retention schedule.** The developer holds no task database and
  therefore has no task data to retain, disclose or delete.

## What is never done

- No selling task data or transferring it outside the uses described above and
  permitted by the Chrome Web Store User Data Policy, including its Limited Use
  requirements.
- No use of your data for advertising, ad personalization, or creditworthiness.
- No use of your data for any purpose unrelated to the extension's single
  purpose described on its Chrome Web Store listing.

You can disconnect your MCP client, stop the companion, remove the extension or
control its browser site access. Permission leases can be reset. Persistent local
state and exported files can be removed separately after active operations stop.
Third-party clients and model providers remain responsible for their own policies.

## Children

This is a developer tool and is not directed at children.

## Changes

Material changes will be published in this file and in the project's
`CHANGELOG.md`. The version of this policy that applies is the one published
alongside the extension version you have installed.

## Contact

Report a problem or ask a question by opening an issue at
<https://github.com/LinVireo/browsertap-mcp/issues>, or by email to
`Linvireo@gmail.com`. Security reports have their own channel, described in
`SECURITY.md`.
