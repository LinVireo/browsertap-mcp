import difflib
import json
import logging
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger(__name__)

_PAGE_SCRIPTS = Path(__file__).resolve().parent / "page_scripts"


def _load_page_script(name: str) -> str:
    """Read one injected page script from the packaged ``page_scripts`` dir.

    These scripts are the scan_page payload -- browser JavaScript that used
    to live as string literals in this module, unreachable by eslint or
    `node --check` in the ordinary editor. They ship as package data (see
    `pyproject.toml`) and are read back here.

    The trailing newline every text file carries is stripped: the call sites
    below append `return pageOutline(...)` / `return listGroups(...)` to the
    fragment, so a stray newline would change the payload.
    """
    return (_PAGE_SCRIPTS / name).read_text(encoding="utf-8").rstrip("\n")


# The page analysis. Both ask the engine for what it already knows --
# element visibility via `Element.checkVisibility()`, repetition via
# structural signatures -- rather than re-deriving either from tuned
# constants.
js_page_outline = _load_page_script("page_outline.js")
js_list_groups = _load_page_script("list_groups.js")


# Which attributes survive the strip below, grouped by the reason each group
# is worth its tokens. Four unrelated questions decide an attribute's fate --
# is it a script hook, is it framework bookkeeping, can a caller act on it, and
# is its value long enough to be worth shortening -- and they are asked
# separately, because two of them are namespace rules that need no list at all.
#
# Keeping the whole `aria-*` namespace is the one deliberate widening: naming
# individual members means `aria-checked`, `aria-disabled` and `aria-selected`
# are dropped, and those are exactly the state a caller has to read before
# deciding whether to click. A namespace covers them without growing.
_SELECTOR_ATTRS = frozenset({'id', 'class', 'name', 'type', 'for'})
_CONTENT_ATTRS = frozenset({'src', 'href', 'alt', 'value', 'title', 'placeholder'})
_STATE_ATTRS = frozenset({
    'disabled', 'checked', 'selected', 'readonly', 'required', 'multiple',
    'contenteditable',
})
_SUBMIT_ATTRS = frozenset({'action', 'method', 'target'})
_TABLE_ATTRS = frozenset({'colspan', 'rowspan'})
_KEPT_ATTRS = (
    _SELECTOR_ATTRS | _CONTENT_ATTRS | _STATE_ATTRS | _SUBMIT_ATTRS | _TABLE_ATTRS
)

# Two caps, because the right way to shorten a value depends on whether a
# prefix of it is usable. No prefix of a URL is -- a caller cannot navigate to
# half of one -- so a long URL is replaced outright and the tokens go to zero.
# Human-readable text is the opposite: the opening is the informative part, so
# it is kept and the cut is marked.
_URL_CAP = 30
_URL_ATTRS = frozenset({'src', 'action'})
_TEXT_CAP = 100
_TEXT_KEEP = 50
_ELIDED = ' ...'
# `data-*` is kept because a page's own hooks are often the only stable
# selector a caller has, but only while the value stays short enough to read.
_DATA_CAP = 20


def _keeps_attribute(name):
    """Whether an attribute survives, decided by rule rather than by a list."""
    if name.startswith('on'):
        return False  # a script hook; there is no reader for it here
    if name.startswith('data-v'):
        return False  # scoped-style bookkeeping, one per element, no meaning
    if name.startswith('data-'):
        return True  # capped by length in `_shortened`
    return name == 'role' or name.startswith('aria-') or name in _KEPT_ATTRS


def _shortened(name, value):
    """The value trimmed to its cap, or unchanged when it is already small."""
    if name in _URL_ATTRS and len(value) > _URL_CAP:
        return '__url__'
    if name in _CONTENT_ATTRS and len(value) > _TEXT_CAP:
        return value[:_TEXT_KEEP] + _ELIDED
    if name.startswith('data-') and len(value) > _DATA_CAP:
        return '__data__'
    return value


def _href_ref(url, link_refs, base_url):
    """A long href as a short '#r7' ref, recording the real URL out of band."""
    if link_refs is None:
        return '__link__'
    if base_url:
        # Store absolute URLs: a ref of '/en-US/docs/x' is not something
        # open_url can navigate to.
        try:
            url = urljoin(base_url, url)
        except Exception:
            pass
    ref = link_refs.get(url)
    if ref is None:
        ref = f"r{len(link_refs) + 1}"
        link_refs[url] = ref
    return f"#{ref}"


def optimize_html_for_tokens(html, link_refs=None, base_url=None):
    """Shrink HTML for token budget.

    link_refs: optional dict that collects {ref_id: real_url}. Long hrefs used
    to be replaced by a bare '__link__', which threw the URL away entirely —
    on a search results page the agent could read 30 titles and reach none of
    them. Now each long href becomes a short '#r7'-style ref and the real URL
    is handed back out of band, so the text stays small AND addressable.
    """
    soup = BeautifulSoup(html, 'html.parser') if isinstance(html, str) else html
    for svg in soup.find_all('svg'):
        # An inline icon's path data is pure noise to a reader, and there can be
        # hundreds of them. The tag stays so the layout still reads.
        svg.clear()
        svg.attrs = {}
    for tag in soup.find_all(True):
        for name in [name for name in tag.attrs if not _keeps_attribute(name)]:
            del tag.attrs[name]
        src = tag.get('src')
        if isinstance(src, str) and src.startswith('data:'):
            # Length is the wrong question for these: a short data: URL is
            # still an image nobody can read, and a long one is megabytes.
            tag['src'] = '__img__'
        href = tag.get('href')
        if isinstance(href, str) and len(href) > _URL_CAP:
            # Handled before the loop below and then skipped by it: an href is
            # the one long value a caller still has to be able to reach, so it
            # becomes a ref rather than being trimmed or blanked.
            tag['href'] = _href_ref(href, link_refs, base_url)
        for name, value in list(tag.attrs.items()):
            # `class` arrives as a list, and no cap here applies to one.
            if name != 'href' and isinstance(value, str):
                tag[name] = _shortened(name, value)
    return soup


# Upper bound on how long the transient-text monitor may keep ticking after
# the call that started it has given up. The three paths that skip the Python
# cleanup below -- an early return on a no_response ``kind``, an exhausted
# deadline, and a failed roundtrip -- are precisely the ones where the page
# can no longer be reached, so the stop has to be inside the injected script.
# Without it a 450ms full-document TreeWalker runs on the user's real page for
# as long as the document lives.
TEMP_MONITOR_TTL_GRACE = 5.0


def build_temp_monitor_js(ttl_seconds, interval_ms=450):
    """Return the monitor script, wired to stop itself after ``ttl_seconds``.

    The interval captures its own handle rather than reading ``.id`` back off
    the global: a monitor that has already been replaced by a newer one must
    clear *itself* and leave the newcomer alone.
    """
    ttl_ms = max(0, int(float(ttl_seconds) * 1000))
    return """function startStrMonitor(interval, lifetimeMs) {
        if (window.__btap_tm && window.__btap_tm.id) clearInterval(window.__btap_tm.id);
        const expires = Date.now() + lifetimeMs;
        window.__btap_tm = {extract: () => {
            const texts = new Set(), walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node, t, s; while (node = walker.nextNode())
                ((t = node.textContent.trim()) && t.length > 10 && !(s = t.substring(0, 20)).includes('_')) && texts.add(s);
            return texts;
        }};
        window.__btap_tm.init = window.__btap_tm.extract();
        window.__btap_tm.all = new Set();
        const id = setInterval(() => {
            const m = window.__btap_tm;
            if (!m || m.id !== id) { clearInterval(id); return; }
            if (Date.now() > expires) { clearInterval(id); delete window.__btap_tm; return; }
            m.extract().forEach(t => m.all.add(t));
        }, interval);
        window.__btap_tm.id = id;
    }
    startStrMonitor(%d, %d);
""" % (int(interval_ms), ttl_ms)


def _execute_in_session(driver, script, timeout, session_id=None, **kwargs):
    call_kwargs = {"timeout": timeout, **kwargs}
    if session_id is not None:
        call_kwargs["session_id"] = session_id
    return driver.execute_js(script, **call_kwargs)


def start_temp_monitor(driver, timeout=15, session_id=None, ttl=None):
    """Start the transient-text monitor, bounded by its own lifetime.

    ``ttl`` defaults to the roundtrip budget plus a grace window, so a monitor
    whose reader never comes back stops on its own instead of ticking for the
    life of the page.
    """
    if ttl is None: ttl = float(timeout) + TEMP_MONITOR_TTL_GRACE
    try: _execute_in_session(driver, build_temp_monitor_js(ttl), timeout, session_id=session_id)
    except Exception: pass


# Stopping the monitor and reading what it collected are two different needs: the
# paths with no reader still have to stop the ticking, and the path that reads it
# must not leave the monitor behind. Both drop the global *before* touching the
# DOM, so a throw inside the walk cannot leave a page whose global says a monitor
# is running while its interval is already gone. A tick that fires in between
# clears itself, because the interval callback compares its own captured handle
# against the global (`build_temp_monitor_js`).
_MONITOR_DISCARD_JS = """function discardStrMonitor() {
    const monitor = window.__btap_tm;
    if (!monitor) return false;
    delete window.__btap_tm;
    clearInterval(monitor.id);
    return true;
}
discardStrMonitor();
"""

# What a transient *is*: text the page showed at some point inside the window and
# is no longer showing. Both halves of the filter follow from that -- text in the
# opening snapshot was never new, and text still on the page is not transient and
# not lost either, since it is in the HTML and in the diff the caller gets beside
# this list. Reporting it here instead would be actively misleading: an agent
# reading `transients: ["Email is required"]` cannot tell whether the message is
# still on screen, which is the one thing it needs to know.
_MONITOR_READ_JS = """function readStrMonitor() {
    const monitor = window.__btap_tm;
    if (!monitor) return [];
    delete window.__btap_tm;
    clearInterval(monitor.id);
    const present = monitor.extract();
    return [...monitor.all].filter(t => !monitor.init.has(t) && !present.has(t));
}
readStrMonitor();
"""


def stop_temp_monitor(driver, timeout=15, session_id=None):
    """Clear the monitor without paying for its payload.

    Used by the return paths that have no reader for the transients but did
    start the monitor; the collected text is discarded, the interval is not.
    """
    try: _execute_in_session(driver, _MONITOR_DISCARD_JS, timeout, session_id=session_id)
    except Exception as e: logger.debug("Temporary monitor stop failed: %s", e)

def get_temp_texts(driver, timeout=15, session_id=None):
    """Stop the monitor and return the text that came and went while it ran."""
    try:
        # `dict.fromkeys` rather than `set`: the page walks its text nodes in
        # document order and that order is worth keeping, where a set would hand
        # the agent the same toasts in an order that changes between runs.
        return list(dict.fromkeys(_execute_in_session(
            driver, _MONITOR_READ_JS, timeout, session_id=session_id).get('data', [])))
    except Exception as e:
        logger.debug("Temporary monitor read failed: %s", e)
        return []

from urllib.parse import urljoin


class PageUnavailable(RuntimeError):
    """The page never answered, so there is no HTML to process.

    Raised instead of quietly returning '' — an empty string used to flow
    downstream and either divide by zero in the cutlist ratio or make the
    agent believe the page was blank, throwing away the bridge's own
    diagnosis of WHY it didn't answer.
    """

def _collapsed_text(page):
    """One space between words, at most one blank line between blocks.

    Done line by line rather than by pattern, because ``str.split`` already
    answers "what are the words on this line" for every kind of whitespace. A
    pass tuned to runs of the space character leaves a tab between two table
    cells and a trailing space after a heading exactly where they were, and text
    lifted off a real page is full of both.
    """
    kept = []
    for line in page.split('\n'):
        text = ' '.join(line.split())
        # A blank line survives only if the last kept line was not blank: the
        # break between two blocks is information, the size of the gap is not.
        if text or (kept and kept[-1]):
            kept.append(text)
    return '\n'.join(kept).strip()


def get_main_block(driver, extra_js="", text_only=False, timeout=15,
                   allow_failover=False, session_id=None):
    # Default is no failover. Reading has no side effects, but silently reading
    # a DIFFERENT tab than the caller asked for is its own wrong answer: the
    # agent gets a page it never requested and no indication of the swap.
    raw = _execute_in_session(
        driver,
        f"{extra_js}\n{js_page_outline}\nreturn pageOutline({str(text_only).lower()});",
        timeout,
        session_id=session_id,
        allow_failover=allow_failover,
    )
    # A timeout carries no 'data' at all, only 'result' with the reason.
    if 'data' not in raw:
        reason = raw.get('result') or 'no data returned'
        raise PageUnavailable(
            f"{reason}. Run list_tabs to see live tabs, then switch_tab to the one you meant.")
    page = raw.get('data')
    if page is None:
        raise PageUnavailable(
            "page returned null instead of HTML. Run list_tabs / switch_tab to confirm the target tab.")
    if text_only:
        return _collapsed_text(page)
    return page

TOP_CHANGE_MAXCHARS = 2000


def _element_signature(element):
    """One line standing for an element's own identity, children excluded.

    Attributes are sorted so the line does not depend on the order the parser
    happened to see them in, and only the element's *direct* text is included --
    a descendant's text belongs to that descendant's own line, and counting it
    here would make every ancestor of a changed node look changed too.
    """
    own_text = ' '.join(
        stripped
        for node in element.children
        if isinstance(node, NavigableString) and (stripped := str(node).strip())
    )
    return f"{element.name}\x00{sorted(element.attrs.items())}\x00{own_text}"


def find_changed_elements(before_html, after_html):
    """Summarise what the page gained or lost between two HTML snapshots.

    The alignment is `difflib`'s. Each element becomes one signature line, the
    two streams are aligned once, and every opcode that is not `equal` is a
    change -- so an insertion, a deletion and a reorder all fall out of the same
    walk. Tallying how many copies of each signature the `after` side holds
    cannot do that: it sees additions only, needs a second positional pass for
    the reorder case, and still reports a pure deletion as zero changes.

    `top_change` is the longest changed element, and nothing has to filter for
    the outermost one first: an element's serialisation contains its children's,
    so a changed element that sits inside another changed element is strictly
    shorter than it and can never be the maximum.
    """
    before = BeautifulSoup(before_html, 'html.parser').find_all(True)
    after = BeautifulSoup(after_html, 'html.parser').find_all(True)
    # autojunk would treat a signature shared by 1% of a long page as noise and
    # stop matching it, which on a list page is most of the document.
    matcher = difflib.SequenceMatcher(
        None,
        [_element_signature(element) for element in before],
        [_element_signature(element) for element in after],
        autojunk=False,
    )
    appeared, vanished = [], 0
    for opcode, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if opcode == 'equal':
            continue
        appeared.extend(after[new_start:new_end])
        vanished += old_end - old_start

    summary = {"changed": len(appeared) + vanished}
    if appeared:
        rendered = str(max(appeared, key=lambda element: len(str(element))))
        summary["top_change"] = (
            rendered
            if len(rendered) <= TOP_CHANGE_MAXCHARS
            else rendered[:TOP_CHANGE_MAXCHARS] + '...[TRUNCATED]'
        )
    return summary

# The mark that tells the truncator a node is the page's own account of what was
# removed rather than page content, so that it survives being cut (`_is_hint`).
_HINT_MARK = '[FAKE ELEMENT]'

# A collapsed list keeps its first few items -- or every item the caller's
# instruction names, and more of those, because they are what was asked for.
_CUTLIST_KEEP = 3
_CUTLIST_HIT_KEEP = 6

# How much of each hidden item is quoted back, so the agent can tell what kind of
# thing it is not being shown.
_CUTLIST_SAMPLES = 5
_CUTLIST_SAMPLE_CHARS = 40

# The hint costs tokens too, so a collapse has to buy back several times what it
# spends. Measuring the hint that was actually built is what makes one number
# enough here: it already accounts for how long the selector turned out to be and
# how much sample text there was, neither of which an absolute character count
# can see. Measured on the shapes this has to separate, the ratio is not near
# either edge -- a ten-link nav bar comes out at 1.3 and a list of
# paragraph-sized cards at 9.8, so the cut falls between them rather than just
# inside one of them.
_CUTLIST_MIN_GAIN = 4


def _cutlist_hint(soup, selector, removed):
    """The note that stands in for the items a collapse is about to hide."""
    samples = [
        text
        for element in removed[:_CUTLIST_SAMPLES]
        if (text := element.get_text(" ", strip=True)[:_CUTLIST_SAMPLE_CHARS])
    ]
    parts = [f'{_HINT_MARK} {len(removed)} more items hidden, selector: "{selector}"']
    if samples:
        parts.append('Hidden items: ' + ','.join(f'"{text}"' for text in samples))
    hint = soup.new_tag('div')
    hint.string = ' '.join(parts)
    return hint


def _collapse_list(soup, selector, instruction):
    """Hide the repetitive tail of one list in place; report whether it happened.

    Whether to collapse is decided against the hint's own measured length, so the
    price of the exchange is known before it is made rather than after: a list
    whose items are shorter than the note describing them is one where hiding
    them makes the page *bigger*, and no absolute item-size threshold can rule
    that out, because it cannot see the note.
    """
    try:
        items = soup.select(selector)
    except Exception:
        logger.debug("cutlist skipped invalid selector: %s", selector)
        return False
    wanted = instruction.strip() if instruction else ''
    asked_for = [
        item for item in items
        if wanted and wanted in item.get_text(" ", strip=True)
    ]
    keep = asked_for[:_CUTLIST_HIT_KEEP] if asked_for else items[:_CUTLIST_KEEP]
    # By identity, not by `in`: two list items that happen to serialise the same
    # -- a repeated "More" link, an empty cell -- compare equal as tags, so a
    # membership test would read a duplicate of a kept item as kept and leave it
    # behind while the hint counted it as hidden.
    kept = {id(item) for item in keep}
    removed = [item for item in items if id(item) not in kept]
    if not removed:
        return False
    hint = _cutlist_hint(soup, selector, removed)
    saved, cost = sum(len(str(item)) for item in removed), len(str(hint))
    logger.debug(
        "cutlist selector=%s items=%d hidden=%d saves=%d hint=%d",
        selector, len(items), len(removed), saved, cost,
    )
    if saved < cost * _CUTLIST_MIN_GAIN:
        return False
    # `keep` is empty only when `items` is, and `removed` is then empty too, so
    # by here there is always an anchor to put the hint after.
    keep[-1].insert_after(hint)
    for item in removed:
        item.decompose()
    return True


def get_html(driver, cutlist=False, maxchars=35000, instruction="", extra_js="",
             text_only=False, timeout=15, link_refs=None, session_id=None):
    if cutlist:
        rr = _execute_in_session(
            driver,
            js_list_groups + "return listGroups(document.body);",
            timeout,
            session_id=session_id,
        ).get('data', [])
    page = get_main_block(
        driver,
        extra_js=extra_js,
        text_only=text_only,
        timeout=timeout,
        session_id=session_id,
    )
    # Hard cap before parsing: BeautifulSoup on a multi-MB page dominates the
    # call's latency, and callers only ever see maxchars of it anyway.
    if isinstance(page, str) and len(page) > 1_500_000: page = page[:1_500_000]
    if text_only: return page
    base_url = None
    if isinstance(page, str):
        m = re.search(r'<!--btap-base:(.*?)-->', page)
        if m:
            base_url = m.group(1)
            # Consumed here; the caller already knows the URL from list_tabs, so
            # don't spend tokens on it. The offscreen marker stays: server.py
            # parses it out of the returned HTML.
            page = page.replace(m.group(0), '', 1)
    soup = optimize_html_for_tokens(page, link_refs=link_refs, base_url=base_url)
    for div in soup.select('div[data-tag="iframe"]'):
        div.name = 'iframe'
        del div['data-tag']
    html = str(soup)
    if not cutlist:
        return html
    # `listGroups` answers with one entry per repeated block, but a single dict
    # and a non-answer are both shapes this has had to read, so the reply is
    # normalised before anything looks for a selector inside it.
    candidates = rr if isinstance(rr, list) else [rr]
    selectors = [
        entry['selector'] for entry in candidates
        if isinstance(entry, dict) and entry.get('selector')
    ]
    if candidates:
        logger.debug("cutlist found %d list(s): %s", len(candidates), selectors)
    # Deliberately not `any(...)`: it short-circuits on the first collapse, and
    # every list the page offered has to be given its own chance to be cut.
    collapsed = [sel for sel in selectors if _collapse_list(soup, sel, instruction)]
    if collapsed:
        # A collapse happened, so the page had content and `html` is not empty --
        # which is what the ratio below needs, and the only reason the old
        # unconditional version of it needed an "empty page" branch at all.
        cut = str(soup)
        logger.debug(
            "get_html cutlist collapsed %d list(s): %d -> %d chars (%d%% saved)",
            len(collapsed), len(html), len(cut), 100 - len(cut) * 100 // len(html),
        )
        html = cut
    if len(html) > maxchars:
        html = str(smart_truncate(soup, maxchars))
    return html

# A child bigger than this keeps its own structure: it is subdivided by another
# round of allocation rather than having its serialised text sliced, so the
# agent still sees where in the tree the content sat.
SUBDIVIDE_ABOVE = 8000


def _is_hint(node):
    return bool(node.string) and _HINT_MARK in node.string


def _clip_markup(element, keep):
    """Cut one element down to ``keep`` characters, hints kept, marker appended."""
    hints = [node.extract() for node in element.find_all(_is_hint)]
    over = len(str(element)) - keep
    if over > 0:
        marker = f' [TRUNCATED {over // 1000}k chars]'
        inner = element.decode_contents()
        room = max(keep - (len(str(element)) - len(inner)) - len(marker), 0)
        element.clear()
        if room:
            element.append(BeautifulSoup(inner[:room], 'html.parser'))
        element.append(NavigableString(marker))
    for hint in hints:
        element.append(hint)


def _allocate(sizes, budget):
    """Largest-remainder allocation of ``budget`` over ``sizes``, biggest first.

    Every child is given what it asks for while the budget covers it; the first
    one that does not fit takes whatever is left and the rest get zero. So the
    result is exact (the shares sum to at most the budget, never over it) and
    total: no child is exempt from being cut, which a fixed top-N rule cannot
    promise -- upstream's spread the overflow across the three biggest children
    and left the fourth untouched no matter how far over budget the page was,
    then needed a separate tail-deletion branch for the case where three were
    not enough. One rule covers both here.

    Ordering by size descending is what makes the outcome useful rather than
    merely exact: the big blocks are the content, so they are served first and
    the boilerplate is what runs out of budget.
    """
    order = sorted(range(len(sizes)), key=lambda index: -sizes[index])
    shares = [0] * len(sizes)
    left = max(budget, 0)
    for index in order:
        shares[index] = min(sizes[index], left)
        left -= shares[index]
    return shares


def smart_truncate(soup, budget, _depth=0):
    """Trim ``soup`` in place until it serialises to about ``budget`` chars.

    Each level spends its budget on its children by `_allocate`, then either
    subdivides a child that is still large (so its structure survives) or clips
    its markup. A lone child is passed straight through, which is what lets a
    deeply wrapped page reach its real content instead of spending the whole
    budget on `<div><div><div>`.
    """
    total = len(str(soup))
    if total <= budget:
        return soup
    children = [node for node in soup.children if node.name and not _is_hint(node)]
    if not children:
        return soup

    sizes = [len(str(node)) for node in children]
    # What the element costs on its own: its tags, and any text sitting directly
    # between the children. The children have to fit in what is left of it.
    inner_budget = max(budget - (total - sum(sizes)), 0)
    logger.debug(
        "%ssmart_truncate tag=%s total=%d budget=%d children=%d",
        '  ' * _depth,
        getattr(soup, 'name', '?'),
        total,
        budget,
        len(children),
    )
    if len(children) == 1:
        smart_truncate(children[0], inner_budget, _depth)
        return soup

    for node, size, keep in zip(children, sizes, _allocate(sizes, inner_budget)):
        if keep >= size:
            continue
        logger.debug(
            "%ssmart_truncate child=%s chars=%d keep=%d",
            '  ' * _depth,
            node.name,
            size,
            keep,
        )
        if not keep:
            node.decompose()
        elif keep > SUBDIVIDE_ABOVE:
            smart_truncate(node, keep, _depth + 1)
        else:
            _clip_markup(node, keep)
    return soup

# The monitor snapshots/diff around execute_js are best-effort context, not the
# result itself; keep them on short timeouts and bounded sizes so a slow or
# hostile page can't multiply the tool call's latency (formerly up to 6 bridge
# roundtrips x 30s each).
MONITOR_TIMEOUT = 6
MONITOR_MAXCHARS = 300000

def no_response_kind(response):
    """Classify a driver timeout pseudo-result.

    'undelivered' is the retry-safe class: either the script provably never left
    the bridge, or it was written to a live socket and never acknowledged, which
    the ACK-before-execute protocol makes overwhelmingly likely to mean "did not
    run". These two are one *kind* on purpose — every caller's retry policy is
    the same for both — but the driver keeps them apart in ``delivery_state``
    ('undelivered' vs 'sent_unconfirmed') for callers whose work is
    irreversible. 'after_ack' means it was delivered and may still be running,
    so retrying could double side effects.
    """
    if not isinstance(response, dict) or 'data' in response: return None
    delivery_state = response.get('delivery_state')
    structured = {
        'undelivered': 'undelivered',
        'sent_unconfirmed': 'undelivered',
        'delivered_no_result': 'after_ack',
        'navigated': 'navigated',
    }.get(delivery_state)
    if structured: return structured
    # Compatibility with bridge versions that predate structured delivery
    # metadata. New callers must not derive policy from this human text.
    msg = response.get('result')
    if not isinstance(msg, str): return None
    if 'no ACK' in msg or 'script not polled' in msg: return 'undelivered'
    if 'ACK received' in msg or 'delivered but no result' in msg: return 'after_ack'
    # The page unloaded before the result came back (click -> navigation, the
    # single most common action). This is NOT a timeout and NOT a success: the
    # script's return value is gone for good. Without this branch the caller
    # fell through to status:"success" with the diagnostic string masquerading
    # as js_return.
    if 'reloaded' in msg: return 'navigated'
    return None

def _remaining(deadline, cap=None):
    remaining = max(0.0, deadline - time.monotonic())
    return min(remaining, cap) if cap is not None else remaining


# Upper bound on the slice of a call's budget held back for the undelivered
# retry. A long script keeps almost all of its window; short calls split
# proportionally instead of losing a fixed 2s they never had.
UNDELIVERED_RETRY_RESERVE = 2.0


def undelivered_retry_split(left):
    """Split a remaining budget into (first attempt, reserved retry) seconds.

    Handing the first attempt the whole budget makes the undelivered retry
    unreachable: the driver can only report 'undelivered' once it has waited out
    everything it was given, so by the time the caller learns the script never
    landed there is nothing left to retry with. Every layer that advertises
    "retries proven-undelivered work" has to reserve that window up front.

    The reserve is only worth taking if the first attempt still gets a usable
    window, so a budget too small to split is spent entirely on the first try.
    """
    left = max(0.0, float(left))
    reserve = min(UNDELIVERED_RETRY_RESERVE, left * 0.2)
    first = left - reserve
    if first <= 0.001:
        return left, 0.0
    return first, reserve


# Fields of a structured browser error that cost tokens and tell an agent nothing
# it can act on. `stack` is the reason this exists at all: a browser stack trace
# is routinely longer than the whole rest of the reply.
_ERROR_NOISE = frozenset({'stack', 'stackTrace'})


def _error_text(exc):
    """Flatten whatever the driver raised into one line an agent can read.

    The bridge raises with a structured dict when the page itself reported the
    failure and with a plain string otherwise. Two things follow, and neither is
    served by `str()` on the dict: its repr is Python's -- single quotes, `True`
    rather than `true` -- inside a reply that is otherwise JSON, and a dict whose
    only remaining content is a message reads better as that message. A copy is
    built rather than keys dropped in place, because the dict belongs to the
    exception the driver raised and may still be read after this.
    """
    detail = exc.args[0] if exc.args else str(exc)
    if not isinstance(detail, dict):
        return str(detail)
    useful = {
        key: value for key, value in detail.items()
        if key not in _ERROR_NOISE and value not in (None, '')
    }
    if set(useful) == {'message'}:
        return str(useful['message'])
    return json.dumps(useful, ensure_ascii=False, default=str)


def execute_js_rich(
    script,
    driver,
    no_monitor=False,
    timeout=15,
    before_sids=None,
    session_id=None,
    deadline=None,
):
    """Execute one script with a single end-to-end deadline.

    ``session_id`` is forwarded to every browser roundtrip, including baseline,
    retry, navigation-location, transient, and DOM-diff reads. ``deadline`` is
    supplied by the server so dialog policy setup and cleanup share the same
    budget; direct callers get a deadline derived from ``timeout``.
    """
    configured_timeout = max(0.001, float(timeout))
    deadline = deadline if deadline is not None else time.monotonic() + configured_timeout
    last_html = None

    def phase_timeout(cap=None):
        remaining = _remaining(deadline, cap)
        return remaining if remaining > 0.001 else 0.0

    monitor_started = False
    if not no_monitor and phase_timeout(MONITOR_TIMEOUT):
        try:
            last_html = get_html(
                driver,
                cutlist=False,
                extra_js=build_temp_monitor_js(_remaining(deadline) + TEMP_MONITOR_TTL_GRACE),
                maxchars=MONITOR_MAXCHARS,
                timeout=phase_timeout(MONITOR_TIMEOUT),
                session_id=session_id,
            )
            monitor_started = True
        except Exception as e:
            logger.debug("Monitor baseline snapshot unavailable: %s", e)
    if before_sids is None:
        try:
            session_timeout = phase_timeout(MONITOR_TIMEOUT)
            if session_timeout:
                before_sids = set(driver.get_session_dict(timeout=session_timeout).keys())
            else:
                before_sids = set()
        except Exception as e:
            logger.debug("Monitor session snapshot unavailable: %s", e)
            before_sids = set()

    result = None
    error_msg = None
    reloaded = False
    newTabs = []
    response = {}
    blocked_dialog = False
    retried = False
    try:
        logger.debug("Executing browser script (%d chars)", len(script))
        # Hold back part of the budget so the undelivered retry below is
        # actually reachable; see undelivered_retry_split.
        call_timeout, _reserved = undelivered_retry_split(phase_timeout())
        if call_timeout:
            response = _execute_in_session(
                driver, script, call_timeout, session_id=session_id
            )
        else:
            response = {
                'result': (
                    f"No response data in {configured_timeout}s "
                    "(no ACK, total execute_js deadline exhausted)"
                )
            }
        if no_response_kind(response) == 'undelivered' and phase_timeout():
            # Never reached the page (session asleep / SW reconnecting): retry
            # only with the time still left in the original budget.
            logger.warning("No ACK; retrying once within the remaining deadline")
            retried = True
            response = _execute_in_session(
                driver,
                script,
                phase_timeout(),
                session_id=session_id,
            )
        result = response['data'] if 'data' in response else None
        blocked_dialog = bool(
            isinstance(result, dict)
            and result.get('__btap_dialog_result') is True
            and result.get('status') == 'blocked_by_dialog'
        )
        if response.get('closed', 0) == 1:
            reloaded = True
        if not blocked_dialog and not no_response_kind(response):
            time.sleep(min(1.0, _remaining(deadline)))
    except Exception as e:
        error_msg = _error_text(e)
        logger.warning("Browser script execution failed: %s", error_msg)

    etab = response.get('executed_tab_id')
    if isinstance(etab, int):
        tab_id_field = etab
    else:
        sid_for_tab = session_id or getattr(driver, 'default_session_id', None)
        if sid_for_tab and ':' in str(sid_for_tab):
            try:
                tab_id_field = int(str(sid_for_tab).rsplit(':', 1)[-1])
            except ValueError:
                tab_id_field = None
        else:
            tab_id_field = None
    rr = {
        "status": "failed" if error_msg else "success",
        "js_return": result,
        "tab_id": tab_id_field,
    }
    if reloaded: rr['reloaded'] = reloaded
    if response.get('switched_session'):
        rr['switched_session'] = response['switched_session']
        rr['switched_from'] = response.get('switched_from')
        rr['switch_note'] = "The original session disconnected, so this ran in another session from the same browser. Verify with list_tabs and switch_tab if needed."
    kind = no_response_kind(response)
    if kind == 'navigated' and not error_msg:
        rr['status'] = 'navigated'
        rr['js_return_lost'] = "The page unloaded before returning the value, so the script result is unavailable."
        try:
            location_timeout = phase_timeout(MONITOR_TIMEOUT)
            if location_timeout:
                landed = _execute_in_session(
                    driver,
                    "JSON.stringify({url: location.href, title: document.title})",
                    location_timeout,
                    session_id=session_id,
                )
                if isinstance(landed, dict) and 'data' in landed:
                    info = json.loads(landed['data']) if isinstance(landed['data'], str) else landed['data']
                    rr['landed_url'] = info.get('url')
                    rr['landed_title'] = info.get('title')
        except Exception as e:
            logger.debug("Monitor post-navigation location unavailable: %s", e)
        rr['suggestion'] = "The script ran and the page navigated. Verify landed_url; rerun only a read operation on the new page if a result is needed."
    elif kind and not error_msg:
        rr['status'] = 'no_response'
        # Pass the driver's own verdict through when it has one: 'undelivered'
        # and 'sent_unconfirmed' share a retry policy but not a guarantee, and
        # collapsing them here would hide that from the caller. Derive from kind
        # only for pre-structured bridges.
        rr['delivery_state'] = response.get('delivery_state') or (
            'undelivered' if kind == 'undelivered' else 'delivered_no_result'
        )
        rr['retry_safe'] = kind == 'undelivered'
        # Report whether the in-deadline retry actually ran instead of asking the
        # caller to trust prose about it. False means the budget was too small to
        # reserve a retry window, so a caller-side retry is the only one there is.
        rr['btap_retried'] = retried
        if kind == 'undelivered':
            rr['suggestion'] = (
                ("BTAP already retried once inside the original deadline and it was still "
                 "not delivered. " if retried else
                 "The deadline was too short for BTAP to retry inside it. ")
                + "Confirm the target with list_tabs, then retry with a longer timeout."
            )
        else:
            rr['suggestion'] = (
                "The script was delivered but did not return before timeout. If it only waited "
                "with setTimeout/sleep, replace that wait with wait_for or wait_for_url; do not "
                "embed waits in execute_js. Otherwise inspect with scan_page before retrying side "
                "effects; only retry read operations with a longer timeout."
            )
    if blocked_dialog:
        if response.get('newTabs'):
            rr['newTabs'] = response['newTabs']
        return rr
    if response.get('newTabs'):
        rr['newTabs'] = response['newTabs']
    elif not error_msg and not kind:
        try:
            session_timeout = phase_timeout(MONITOR_TIMEOUT)
            after = driver.get_session_dict(timeout=session_timeout) if session_timeout else {}
            new_sids = {k: v for k, v in after.items() if k not in before_sids}
        except Exception:
            new_sids = {}
        if new_sids:
            newTabs = [{'id': k, 'url': v} for k, v in new_sids.items()]
            rr['newTabs'] = newTabs
            rr['suggestion'] = "The page refreshed; the listed new tabs connected during execution."
    if error_msg: rr['error'] = error_msg
    if no_monitor or kind:
        # No reader for the transients on this path, but the interval is still
        # ticking on the user's page. Stop it now while there may still be a
        # channel; build_temp_monitor_js' own expiry is the fallback when the
        # tab is already gone (which is what a truthy `kind` usually means).
        if monitor_started and phase_timeout(MONITOR_TIMEOUT):
            stop_temp_monitor(driver, timeout=phase_timeout(MONITOR_TIMEOUT), session_id=session_id)
        return rr
    if not reloaded and phase_timeout(MONITOR_TIMEOUT):
        try:
            rr['transients'] = get_temp_texts(
                driver,
                timeout=phase_timeout(MONITOR_TIMEOUT),
                session_id=session_id,
            )
        except Exception:
            rr['transients'] = []
    if not reloaded and not error_msg and not newTabs and phase_timeout(MONITOR_TIMEOUT):
        try:
            # Checked before the roundtrip, not after it: with no baseline there
            # is nothing a second full-page fetch could be compared against, and
            # the old order paid for one anyway on every call whose opening
            # snapshot had failed. Reported as unavailable rather than as zero
            # changes, which an agent would read as "the script did nothing".
            if last_html is None:
                raise PageUnavailable("no baseline snapshot to compare against")
            diff = find_changed_elements(last_html, get_html(
                driver,
                cutlist=False,
                maxchars=MONITOR_MAXCHARS,
                timeout=phase_timeout(MONITOR_TIMEOUT),
                session_id=session_id,
            ))
        except Exception:
            rr['diff'] = "Page-change monitoring is unavailable."
        else:
            # `changed` is always present and `top_change` only when something
            # appeared, because the producer is this module -- no default needed.
            summary = f"DOM changes: {diff['changed']}"
            if diff.get('top_change'):
                summary += f"\nMost significant change:\n{diff['top_change']}"
            if not diff['changed'] and not rr.get('transients'):
                summary += " (no page changes)"
                rr['suggestion'] = "No visible page changes were detected."
            rr['diff'] = summary
    return rr
