# Third-party notices

`LICENSE` covers this work as a whole. This file covers the parts of it that
are not original to it.

One upstream project is redistributed here. Its licence requires its copyright
notice to travel with every copy, so that notice is reproduced below in full
rather than summarised or linked -- a link is not a copy, and the obligation is
on the copy.

## GenericAgent

- Upstream: <https://github.com/lsdefine/GenericAgent>
- Licence: MIT
- Copyright: `lsdefine`

The browser layer here began as GenericAgent's. What is still derived from it,
measured line-for-line against upstream `0c235a8` on 2026-08-25 with
`difflib.SequenceMatcher(None, upstream_lines, our_lines, autojunk=False)`.
"Identical lines" counts how much of the *upstream* file survives here, so the
denominator is upstream's line count and the percentage is that share:

| In this distribution | Upstream file | Identical lines |
|---|---|---|
| `src/browsertap_mcp/simphtml.py` | `simphtml.py` | 207 of 873 (24%) |
| `src/browsertap_mcp/browser_bridge.py` | `TMWebDriver.py` | 132 of 289 (46%) |
| `src/browsertap_mcp/chrome_extension/background.js` | `assets/tmwd_cdp_bridge/background.js` | 248 of 422 (59%) |
| `src/browsertap_mcp/chrome_extension/manifest.json` | `assets/tmwd_cdp_bridge/manifest.json` | 29 of 40 (72%) |
| `src/browsertap_mcp/chrome_extension/content.js` | `assets/tmwd_cdp_bridge/content.js` | 7 of 19 (37%) |
| `src/browsertap_mcp/chrome_extension/popup.html` | `assets/tmwd_cdp_bridge/popup.html` | 15 of 19 (79%) |
| `src/browsertap_mcp/chrome_extension/popup.js` | `assets/tmwd_cdp_bridge/popup.js` | 12 of 24 (50%) |
| `src/browsertap_mcp/chrome_extension/disable_dialogs.js` | `assets/tmwd_cdp_bridge/disable_dialogs.js` | 18 of 25 (72%) |

Read that table rather than a summary of it. Each upstream file now has exactly
one heir here, so the rows are independent and can be read on their own; that was
not true in 0.4.14, where three of them shared `simphtml.py` as an ancestor and
adding them up counted the same upstream lines more than once.

The `simphtml.py` row fell from 84% to 24% in 0.4.15, and the distinction that
makes that figure meaningful is that the code was **replaced, not moved**.
Upstream's browser-side page analysis had been carved out of a Python string
literal into two files under `page_scripts/`, which relocated it without reducing
it. What replaced it asks the engine for what it already computes --
`Element.checkVisibility()` for whether an element renders, and structural
signatures for which block is a repeated list -- in place of upstream's hand-rolled
`display`/`visibility`/`opacity` walk and its scored search over tuned constants.
Different mechanism, different code, and the 528 upstream lines those two files
held are no longer in this distribution. Splitting a file, by contrast, cannot
reduce what is borrowed, and this notice would have been misleading if the 24%
had been reached that way.

`browser_bridge.py` and `background.js` have each grown several times over, and
most of what they still share with upstream is the wire protocol both ends have
to keep agreeing on.

**Every file in `src/browsertap_mcp/chrome_extension/` is derived** -- the whole
directory was forked from upstream's `assets/tmwd_cdp_bridge/`, and the only
files there upstream never had are the `_locales/` message catalogues. Three of
the six spent their first three releases missing from this table
(`popup.html`, `popup.js`, `disable_dialogs.js`), and their upstream share is
*higher* than that of `background.js`, which was credited from the start. Size
is what made the difference, and size is not what the licence asks about.

`src/browsertap_mcp/page_scripts/` is where the same trap caught this notice a
second time, and the shape is worth stating because a directory is not evidence
either way. The two files that used to live there were derived, held a higher
share of upstream's lines than any credited file except `popup.html`, and were
credited by nothing for three releases -- because they arrived by a refactor
rather than by a fork, and the check built to catch exactly this walked the
extension directory. The files there now are not derived, and the check no longer
takes the directory's word for it: a new file under `page_scripts/` is treated as
derived until it is named in `ORIGINAL_PAGE_SCRIPTS`, so the next extraction
cannot be invisible the way the first one was.

`scripts/check_derived_notices.py` is what keeps this table honest -- it
re-measures every row against an upstream checkout, and it records what each
derived file hashed to at that measurement so that editing one without
re-measuring fails offline. The figures above are its output, not typed by hand.

Everything else in this distribution is original: the MCP tool surface, the
bridge daemon and its token authentication, the CDP focus and hit-test paths,
the packaged agent skills, the release evidence pipeline, the test suite, and
the documentation.

Reproduced in full, as its terms require:

```
MIT License

Copyright (c) 2025 lsdefine

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
