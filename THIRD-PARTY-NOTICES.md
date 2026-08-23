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
measured line-for-line against upstream `HEAD` on 2026-08-23 with
`difflib.SequenceMatcher`:

| In this distribution | Upstream file | Identical lines |
|---|---|---|
| `src/browsertap_mcp/simphtml.py` | `simphtml.py` | 780 of 873 (89%), longest run 192 |
| `src/browsertap_mcp/browser_bridge.py` | `TMWebDriver.py` | 130 of 289 (45%) |
| `src/browsertap_mcp/chrome_extension/background.js` | `assets/tmwd_cdp_bridge/background.js` | 248 of 422 (59%) |
| `src/browsertap_mcp/chrome_extension/manifest.json` | `assets/tmwd_cdp_bridge/manifest.json` | 29 of 40 |
| `src/browsertap_mcp/chrome_extension/content.js` | `assets/tmwd_cdp_bridge/content.js` | 9 of 19 |

Read that table rather than a summary of it. `simphtml.py` is substantially
upstream's file, extended here -- not a rewrite. `browser_bridge.py` and
`background.js` have each grown five to ten times over (1635 and 4591 lines
now), and most of what they still share with upstream is the wire protocol both
ends have to keep agreeing on.

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
