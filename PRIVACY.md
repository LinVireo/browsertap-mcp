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

To do its job the extension may access the following while carrying out a command
you or your assistant issued. The data depends on the selected tab and requested
operation and may include sensitive information already present in that page or
profile.

- **Open tabs** — titles, URLs and connection metadata.
- **Page content** — text, structure and screenshots when requested.
- **Cookies and site storage** — for sites named by a command.
- **Console and network activity** — for a tab while capture is running.
- **Bookmarks, downloads and installed extensions metadata** — for management tools.
- **Site permissions** — granted to one origin for 60 to 600 seconds, then restored.

Two things about the *breadth* of that access, stated plainly because the
permission list is broader than any single command needs:

- **The extension holds access to all sites** (`<all_urls>`), not a list you
  approve per site. It has to: the whole point is to work in whichever tab you
  are already using, and that tab is not known ahead of time. Access is held
  continuously; it is *used* only for the tab a command names.
- **It can modify page requests, and does one thing with that.** While a script
  is running in a tab, the extension strips that tab's Content-Security-Policy
  header, because CSP would otherwise block the script from running at all. The
  rule is scoped to the one tab, is removed when the script finishes, and does
  not survive a browser restart. No other request modification or blocking is
  performed.

## Where that data goes

The extension sends results to your own MCP server over loopback on
`127.0.0.1`, and from there to the AI assistant you configured. Requested page
operations can also contact websites or download endpoints.

**It does not go to the developer.** The extension has no developer-operated
collection endpoint, cloud component, crash reporting or usage statistics.

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
- **Kept across restarts, in `chrome.storage.local`.** Four items, none of them
  browsing history: the bridge port number; whether the on-page indicator is
  shown; any site-permission leases still counting down, so that a browser
  restart mid-lease does not leave a permission granted; and a **client id** the
  extension generates for itself, of the form `chrome_a1b2c3` — a browser label
  plus six random characters, used to tell one connected browser from another on
  the same bridge. It is not derived from you, your profile, or your hardware,
  and it is not sent anywhere except to your own local bridge.
- **Settings and the bridge token** live in a state directory under your home
  directory. The token file is created with owner-only permissions (`0600`).
- **Logs.** The bridge writes a local log file, `bridge.log`, so that you have
  something to attach to a bug report. URLs in it are cut down at the point they
  are written: the origin and a short path are kept, and the query string and
  fragment are replaced with `?...` and `#...`, because that is where OAuth
  codes, signed links and search terms live. The origin is deliberately kept —
  the log would not answer "which tab was this?" without it. The bridge token is
  never written to the log in any form.
- **No maintainer retention schedule.** The developer holds no task database and
  therefore has no task data to retain, disclose or delete.

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
