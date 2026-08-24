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
measured line-for-line against upstream `HEAD` on 2026-08-24 with
`difflib.SequenceMatcher(None, upstream_lines, our_lines, autojunk=False)`.
"Identical lines" counts how much of the *upstream* file survives here, so the
denominator is upstream's line count and the percentage is that share:

| In this distribution | Upstream file | Identical lines |
|---|---|---|
| `src/browsertap_mcp/simphtml.py` | `simphtml.py` | 780 of 873 (89%), longest run 192 |
| `src/browsertap_mcp/browser_bridge.py` | `TMWebDriver.py` | 130 of 289 (45%) |
| `src/browsertap_mcp/chrome_extension/background.js` | `assets/tmwd_cdp_bridge/background.js` | 248 of 422 (59%) |
| `src/browsertap_mcp/chrome_extension/manifest.json` | `assets/tmwd_cdp_bridge/manifest.json` | 29 of 40 (73%) |
| `src/browsertap_mcp/chrome_extension/content.js` | `assets/tmwd_cdp_bridge/content.js` | 9 of 19 (47%) |
| `src/browsertap_mcp/chrome_extension/popup.html` | `assets/tmwd_cdp_bridge/popup.html` | 15 of 19 (79%) |
| `src/browsertap_mcp/chrome_extension/popup.js` | `assets/tmwd_cdp_bridge/popup.js` | 17 of 24 (71%) |
| `src/browsertap_mcp/chrome_extension/disable_dialogs.js` | `assets/tmwd_cdp_bridge/disable_dialogs.js` | 19 of 25 (76%) |

Read that table rather than a summary of it. `simphtml.py` is substantially
upstream's file, extended here -- not a rewrite. `browser_bridge.py` and
`background.js` have each grown several times over, and most of what they still
share with upstream is the wire protocol both ends have to keep agreeing on.

**Every file in `src/browsertap_mcp/chrome_extension/` is derived** -- the whole
directory was forked from upstream's `assets/tmwd_cdp_bridge/`, and the only
files there upstream never had are the `_locales/` message catalogues. Three of
the six spent their first three releases missing from this table
(`popup.html`, `popup.js`, `disable_dialogs.js`), and their upstream share is
*higher* than that of `background.js`, which was credited from the start. Size
is what made the difference, and size is not what the licence asks about.

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
