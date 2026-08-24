import json
import logging
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup

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


def optimize_html_for_tokens(html, link_refs=None, base_url=None):
    """Shrink HTML for token budget.

    link_refs: optional dict that collects {ref_id: real_url}. Long hrefs used
    to be replaced by a bare '__link__', which threw the URL away entirely —
    on a search results page the agent could read 30 titles and reach none of
    them. Now each long href becomes a short '#r7'-style ref and the real URL
    is handed back out of band, so the text stays small AND addressable.
    """
    if type(html) is str: soup = BeautifulSoup(html, 'html.parser')
    else: soup = html
    for svg in soup.find_all('svg'):
        svg.clear(); svg.attrs = {}
    [tag.attrs.pop('style', None) for tag in soup.find_all(True)]
    for tag in soup.find_all(True):
        if tag.has_attr('src'):
            if tag['src'].startswith('data:'): tag['src'] = '__img__'
            elif len(tag['src']) > 30: tag['src'] = '__url__'
        if tag.has_attr('href') and len(tag['href']) > 30:
            if link_refs is None:
                tag['href'] = '__link__'
            else:
                url = tag['href']
                if base_url:
                    # Store absolute URLs: a ref of '/en-US/docs/x' is not
                    # something open_url can navigate to.
                    try: url = urljoin(base_url, url)
                    except Exception: pass
                ref = link_refs.get(url)
                if ref is None:
                    ref = f"r{len(link_refs) + 1}"
                    link_refs[url] = ref
                tag['href'] = f"#{ref}"
        if tag.has_attr('action') and len(tag['action']) > 30: tag['action'] = '__url__'
        for a in ('value', 'title', 'alt'):
            if tag.has_attr(a) and isinstance(tag[a], str) and len(tag[a]) > 100: tag[a] = tag[a][:50] + ' ...'
        for attr in list(tag.attrs.keys()):  
            if attr not in ['id', 'class', 'name', 'src', 'href', 'alt', 'value', 'type', 'placeholder',
                          'disabled', 'checked', 'selected', 'readonly', 'required', 'multiple',
                          'role', 'aria-label', 'aria-expanded', 'aria-hidden', 'contenteditable',
                          'title', 'for', 'action', 'method', 'target', 'colspan', 'rowspan']:  
                if attr.startswith('data-v'): tag.attrs.pop(attr, None)
                elif attr.startswith('data-') and isinstance(tag[attr], str) and len(tag[attr]) > 20:  
                    tag[attr] = '__data__'  
                elif not attr.startswith('data-'): tag.attrs.pop(attr, None)  
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


def stop_temp_monitor(driver, timeout=15, session_id=None):
    """Clear the monitor without paying for its payload.

    Used by the return paths that have no reader for the transients but did
    start the monitor; the collected text is discarded, the interval is not.
    """
    js = """if (window.__btap_tm) { clearInterval(window.__btap_tm.id); delete window.__btap_tm; }
        return true;
    """
    try: _execute_in_session(driver, js, timeout, session_id=session_id)
    except Exception as e: logger.debug("Temporary monitor stop failed: %s", e)

def get_temp_texts(driver, timeout=15, session_id=None):
    js = """function stopStrMonitor() {  
        if (!window.__btap_tm) return [];  
        clearInterval(window.__btap_tm.id);  
        const final = window.__btap_tm.extract();  
        const newlySeen = [...window.__btap_tm.all].filter(t => !window.__btap_tm.init.has(t));
        let result;
        if (newlySeen.length < 8) {
            result = newlySeen;
        } else {
            result = newlySeen.filter(t => !final.has(t));
        }
        delete window.__btap_tm;  
        return result;  
        }  
        stopStrMonitor();  
    """  
    try: return list(set(_execute_in_session(
        driver, js, timeout, session_id=session_id).get('data', [])))
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
        page = re.sub(r' {2,}', ' ', page)           # 连续空格→单空格
        page = re.sub(r'^ +', '', page, flags=re.M)   # 去行首空格
        page = re.sub(r'(\n\s*){3,}', '\n\n', page)   # 3+空行→1空行
        return page.strip()
    return page

def find_changed_elements(before_html, after_html):
    before_soup = BeautifulSoup(before_html, 'html.parser')
    after_soup = BeautifulSoup(after_html, 'html.parser')
    def direct_text(el):
        return ''.join(t.strip() for t in el.find_all(string=True, recursive=False)).strip()
    def get_sig(el):
        attrs = {k:v for k,v in el.attrs.items() if k != 'data-track-id'}
        return f"{el.name}:{attrs}:{direct_text(el)}"
    def build_sigs(soup):
        result = {}
        for el in soup.find_all(True):
            sig = get_sig(el)
            result.setdefault(sig, []).append(el)
        return result
    before_sigs, after_sigs = build_sigs(before_soup), build_sigs(after_soup)
    changed = []
    for sig, els in after_sigs.items():
        if sig not in before_sigs: changed.extend(els)
        elif len(els) > len(before_sigs[sig]): changed.extend(els[:len(els) - len(before_sigs[sig])])
    if len(changed) == 0 and str(before_soup) != str(after_soup):
        before_els, after_els = before_soup.find_all(True), after_soup.find_all(True)
        for i in range(min(len(before_els), len(after_els))):
            if get_sig(before_els[i]) != get_sig(after_els[i]): changed.append(after_els[i])
    # 变化边界: parent不在changed中的元素
    cids = set(id(el) for el in changed)
    boundaries = [el for el in changed if el.parent is None or id(el.parent) not in cids]
    top = max(boundaries, key=lambda el: len(str(el))) if boundaries else None
    result = {"changed": len(changed)}
    if top:
        h = str(top)
        result["top_change"] = h if len(h) <= 2000 else h[:2000] + '...[TRUNCATED]'
    return result

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
        div.name = 'iframe'; del div['data-tag']
    html = str(soup)
    if not cutlist: return html
    lists = rr if isinstance(rr, list) else ([rr] if isinstance(rr, dict) and rr.get('selector') else [])
    if lists:
        logger.debug(
            "cutlist found %d list(s): %s",
            len(lists),
            [e.get('selector', '?') if isinstance(e, dict) else '?' for e in lists],
        )
    for entry in lists:
        sel = entry.get('selector') if isinstance(entry, dict) else None
        if not sel: continue
        try:
            items = soup.select(sel)
        except Exception:
            logger.debug("cutlist skipped invalid selector: %s", sel)
            continue
        if len(items) < 5: continue
        total_len = sum(len(str(it)) for it in items)
        avg_len = total_len / len(items)
        logger.debug(
            "cutlist selector=%s items=%d avg_chars=%.0f total_chars=%d estimated_saved=%.0f",
            sel,
            len(items),
            avg_len,
            total_len,
            total_len - 3 * avg_len,
        )
        if avg_len < 200 or (avg_len < 700 and total_len < 2500): continue
        hit = [it for it in items if instruction and instruction.strip() and instruction in it.get_text(" ",strip=True)]
        keep = hit[:6] if hit else items[:3]
        removed = [it for it in items if it not in keep]
        sample_texts = []
        for rm in removed[:5]:
            txt = rm.get_text(" ", strip=True)[:40]
            if txt: sample_texts.append(txt)
        hint_parts = [f'[FAKE ELEMENT] {len(removed)} more items hidden, selector: "{sel}"']
        if sample_texts: hint_parts.append('Hidden items: ' + ','.join(f'"{t}"' for t in sample_texts))
        hint_tag = soup.new_tag("div")
        hint_tag.string = ' '.join(hint_parts)
        if keep: keep[-1].insert_after(hint_tag)
        for it in removed: it.decompose()
    ss = str(optimize_html_for_tokens(soup, link_refs=link_refs, base_url=base_url)) if lists else html
    saved = f"{100 - len(ss) * 100 // len(html)}% saved" if html else "empty page"
    logger.debug("get_html cutlist result: %d -> %d chars (%s)", len(html), len(ss), saved)
    if len(ss) > maxchars: ss = str(smart_truncate(soup, maxchars))
    return ss

def smart_truncate(soup, budget, _depth=0):
    """原地截断 soup 使其接近 budget 字符。
    策略：穿透单子元素找分叉点；top3 能扛住 over 则按比例分担，否则从尾部删子元素。"""
    CUT_THRESHOLD = 8000  # 小于此值直接去尾，大于则继续递归找分叉点
    indent = '  ' * _depth
    def cut(ele, keep):
        from bs4 import NavigableString
        s = str(ele)
        over = len(s) - keep
        if over <= 0: return
        # 保护 FAKE ELEMENT 提示标签
        protected = [c.extract() for c in ele.find_all(lambda tag: tag.string and '[FAKE ELEMENT]' in tag.string)]
        s = str(ele)
        over = len(s) - keep
        if over <= 0:
            for p in protected: ele.append(p)
            return
        marker = f' [TRUNCATED {over//1000}k chars]'
        inner = ele.decode_contents()
        tag_overhead = len(s) - len(inner)
        inner_keep = max(keep - tag_overhead - len(marker), 0)
        ele.clear()
        if inner_keep > 0:
            ele.append(BeautifulSoup(inner[:inner_keep], 'html.parser'))
        ele.append(NavigableString(marker))
        for p in protected: ele.append(p)
    total = len(str(soup))
    if total <= budget: return soup
    kids = [(c, len(str(c))) for c in soup.children if c.name and not (c.string and '[FAKE ELEMENT]' in c.string)]
    if not kids: return soup
    selflen = total - sum(l for _, l in kids)
    remaining_budget = max(budget - selflen, 0)
    tag = getattr(soup, 'name', '?')
    logger.debug(
        "%ssmart_truncate tag=%s total=%d budget=%d selflen=%d children=%d",
        indent,
        tag,
        total,
        budget,
        selflen,
        len(kids),
    )
    # === 1 kid: 穿透 ===
    if len(kids) == 1:
        logger.debug("%ssmart_truncate recursing into single child tag=%s", indent, kids[0][0].name)
        smart_truncate(kids[0][0], remaining_budget, _depth)
        return soup
    over = sum(l for _, l in kids) - remaining_budget
    if over <= 0: return soup
    # 看 top 3 能否承担 over
    ranked = sorted(range(len(kids)), key=lambda i: kids[i][1], reverse=True)
    tops = list(ranked[:min(3, len(ranked))])
    top_total = sum(kids[i][1] for i in tops)
    if top_total < over:
        # === top 3 扛不住，从尾部删子元素 ===
        removed = 0
        removed_count = 0
        while kids and removed < over:
            c, l = kids.pop(); c.decompose()
            removed += l; removed_count += 1
        logger.debug(
            "%ssmart_truncate tail cut removed_children=%d removed_chars=%d",
            indent,
            removed_count,
            removed,
        )
        return soup
    # === top 2-3 按比例分担 ===
    # 过滤掉太小的 kid（不到最大的 10%），让大的全扛
    max_size = kids[ranked[0]][1]
    filtered = [i for i in tops if kids[i][1] >= max_size * 0.1]
    filtered_total = sum(kids[i][1] for i in filtered)
    if filtered_total >= over:
        tops, top_total = filtered, filtered_total
    # 先打印所有分配计划
    actions = []
    for i in tops:
        c, l = kids[i]
        share = int(over * l / top_total)
        new_keep = l - share
        logger.debug(
            "%ssmart_truncate child=%s chars=%d keep=%d share=%d",
            indent,
            c.name,
            l,
            new_keep,
            share,
        )
        actions.append((c, l, new_keep))
    # 再统一执行
    for c, l, new_keep in actions:
        if new_keep <= 0: c.decompose()
        elif new_keep > CUT_THRESHOLD: smart_truncate(c, new_keep, _depth + 1)
        else: cut(c, new_keep)
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
        error = e.args[0] if e.args else str(e)
        if isinstance(error, dict): error.pop('stack', None)
        error_msg = str(error)
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
    if not reloaded and not error_msg and len(newTabs) == 0 and phase_timeout(MONITOR_TIMEOUT):
        try:
            current_html = get_html(
                driver,
                cutlist=False,
                maxchars=MONITOR_MAXCHARS,
                timeout=phase_timeout(MONITOR_TIMEOUT),
                session_id=session_id,
            )
            if last_html is None: raise Exception("no baseline")
            diff_data = find_changed_elements(last_html, current_html)
            change_count = diff_data.get('changed', 0)
            top_change = diff_data.get('top_change', '')
            diff_summary = f"DOM changes: {change_count}"
            if top_change: diff_summary += f"\nMost significant change:\n{top_change}"
            transients = rr.get('transients', [])
            if change_count == 0 and not transients and len(newTabs) == 0:
                diff_summary += " (no page changes)"
                rr['suggestion'] = "No visible page changes were detected."
        except Exception:
            diff_summary = "Page-change monitoring is unavailable."
        rr['diff'] = diff_summary
    return rr
