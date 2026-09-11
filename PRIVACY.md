# Privacy Policy

**browsertap-mcp / BrowserTap Bridge**
Last updated: 2026-09-12

## Summary

browsertap-mcp runs entirely on your own computer. The extension's only network
destination is a loopback address (`127.0.0.1`) on your own machine. There is no
developer-operated server, no account, no analytics, and no telemetry. Browser
data goes to your local MCP server and may then reach your chosen AI provider.

## What this software is

BrowserTap Bridge is a Chrome extension that connects the Chrome you are already
signed in to with a Model Context Protocol (MCP) server running on the same
computer. That server is started by you and is used by an AI assistant of your
choosing (for example, a coding agent in your editor). The extension is one half
of a local bridge; it is not useful on its own.

## What the software can access

To do its job the extension is granted access to the following. Tab metadata is
read automatically to keep the local bridge's routing state current; other
data is read when a command or an active capture requests it.

- **Open tabs** — their titles, URLs, and which one is frontmost. The extension
  sends snapshots to the local bridge when it connects, on tab events, and
  during keepalive. This happens even when no tool call is running; hiding the
  page indicator does not stop it.
- **Page content** — the text and structure of a page, and screenshots of it,
  when a command asks for them.
- **Cookies and site storage** — for sites named by a command.
- **Console and network activity** — for a tab, while a capture is running.
- **Bookmarks, downloads, and your list of installed extensions** — for the
  tools that manage them.
- **Site permissions** — granted to one origin for 60 to 600 seconds at your
  request. BTAP attempts to restore the previous setting afterwards; a restore
  that loses browser support is retained with manual-recovery instructions.

The optional Python desktop tools inspect the current foreground Windows file
dialog only after explicit desktop opt-in. They check executable/process and
window identities, owner relationships, control geometry, cursor position,
held-input state and the last-input timestamp. They do not read dialog titles,
file names or file contents, or record a history of keystrokes.

Two things about the *breadth* of that access, stated plainly because the
permission list is broader than any single command needs:

- **The extension holds access to all sites** (`<all_urls>`), not a list you
  approve per site. It has to: the whole point is to work in whichever tab you
  are already using, and that tab is not known ahead of time. Access is held
  continuously. Page inspection and input are scoped to the requested tab;
  automatic tab metadata snapshots cover the open tabs used for routing.
- **It can modify page requests, and does one thing with that.** While a script
  is running in a tab, the extension strips that tab's Content-Security-Policy
  header, because CSP would otherwise block the script from running at all. The
  rule is scoped to the one tab, is removed when the script finishes, and does
  not survive a browser restart. No other request modification or blocking is
  performed.

## Where that data goes

To your own MCP server, over a loopback connection on `127.0.0.1`, and from
there to the AI assistant you pointed at it.

**It does not go to the developer.** The extension contacts no external host.
There is no cloud component, no crash reporting, and no usage statistics.

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

Everything below is on your own computer. Nothing is uploaded.

- **Kept until the browser closes.** Tab ownership, lifecycle generations and
  tab-create recovery records live in Chrome's `chrome.storage.session`.
  Recovery records can include the requested URL and returned tab metadata.
  Terminal records expire after 24 hours or under the 256-record capacity limit;
  a fixed-size replay filter retains uncertain operation identities for the
  browsing session. Current tab snapshots also live in the local bridge's memory.
- **Kept across restarts, in `chrome.storage.local`.** The bridge port number;
  whether the on-page indicator is shown; site-permission leases with their
  origin and prior setting, including failed restores requiring manual recovery;
  and a **client id** the
  extension generates for itself, of the form `chrome_a1b2c3` — a browser label
  plus six random characters, used to tell one connected browser from another on
  the same bridge. It is not derived from you, your profile, or your hardware,
  and it is not sent anywhere except to your own local bridge.
- **Settings and the bridge token** live in a state directory under your home
  directory. The token file is created with owner-only permissions (`0600`).
- **Native-dialog tickets.** Up to eight short-lived tickets and their verified
  window/process identities are held in the MCP process's memory. Inspection
  also installs a random property on the dialog window to identify that window
  lifetime. Tickets expire after 15 seconds or are consumed by an opted-in
  cancellation attempt. BTAP removes its property on cleanup; an OS API failure
  or abrupt process exit can leave it until the window is destroyed. No dialog
  title or file name is stored in a ticket.
- **Bookmark recovery files.** Before deleting a bookmark or folder, the MCP
  server saves that subtree, including titles and URLs, under
  `bookmark-backups` in its state directory. The result includes the file path
  and SHA-256. A failed backup prevents deletion. Each file is limited to 16 MiB;
  the store keeps at most 100 files and 64 MiB. Files older than 30 days and the
  oldest files exceeding those limits are removed when a new backup is made.
  These local files are not encrypted by BTAP.
- **Large script results and requested exports.** Results larger than the inline
  limit are written to a private temporary JSON file; the tool returns its path
  and hash. Screenshots and PDFs are saved when a command supplies a save path.
  These files can contain page data and remain under your control.
- **Logs.** The bridge writes a local log file, `bridge.log`, so that you have
  something to attach to a bug report. URLs in it are cut down at the point they
  are written: the origin and a short path are kept, and the query string and
  fragment are replaced with `?...` and `#...`, because that is where OAuth
  codes, signed links and search terms live. The origin is deliberately kept —
  the log would not answer "which tab was this?" without it. The bridge token is
  never written to the log in any form.
- **Developer retention.** The developer receives none of these records and
  holds no copy to retain, disclose, or delete. Local retention is described
  above; your AI provider has its own policy.

## What is never done

- No selling or transferring your data to third parties.
- No use of your data for advertising, ad personalization, or creditworthiness.
- No use of your data for any purpose unrelated to the extension's single
  purpose described on its Chrome Web Store listing.

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
