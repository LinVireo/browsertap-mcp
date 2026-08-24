"""Behaviour tests for the two injected page scripts, run under node.

`page_outline.js` and `list_groups.js` are what decides *what the model sees* of a
page. Their predecessors lived inside `simphtml.py` as two `r'''...'''` literals:
ruff saw one string, coverage counted one statement, and no JavaScript tool looked
at them at all. `node --check` and eslint cover their syntax and their dead code;
this file covers what they *do*.

The harness fakes the smallest DOM each path needs rather than pulling in jsdom:
the assertions here are about control flow that a real browser would only show
under a real page, and a dependency that one CI job out of nine installs would
quietly not run everywhere else. The fake earns its keep on one test in
particular -- a real DOM cannot be asked "were you modified?", and a fake can.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from browsertap_mcp import simphtml as S

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed on this machine"
)


def _run_node(script: str) -> dict:
    """Run one harness script under node and parse the JSON it prints."""
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
        completed = subprocess.run(["node", path], capture_output=True, text=True)
        if completed.returncode:
            raise AssertionError(f"node harness failed: {completed.stderr.strip()}")
        return json.loads(completed.stdout)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _page_outline_payload(text_only: bool = False) -> str:
    """Exactly what `get_main_block` injects, minus the driver's own wrapper.

    Built from the real constant rather than a copy: the point is the shape of the
    *shipped* payload, and `extra_js` is empty on the ordinary path.
    """
    return f"{S.js_page_outline}\nreturn pageOutline({str(text_only).lower()});"


def _list_groups_payload(root: str = "document.body") -> str:
    """Exactly what `get_html` injects for the cutlist roundtrip."""
    return S.js_list_groups + f"return listGroups({root});"


# A document whose `body` is null. That is the one branch reaching a return
# without walking a page, which is what makes it usable as a harness, and every
# DOM member below is touched by it.
_EMPTY_DOCUMENT_HARNESS = """
let bodyReads = 0;
const made = [];
globalThis.location = {href: 'https://example.invalid/page'};
globalThis.document = {
    get body() { bodyReads++; return null; },
    documentElement: {scrollHeight: 0},
    createElement: (tag) => {
        const el = {
            tag, attrs: {}, html: '',
            setAttribute(k, v) { this.attrs[k] = v; },
            insertAdjacentHTML(_where, markup) { this.html += markup; },
            get outerHTML() {
                const a = Object.entries(this.attrs)
                    .map(([k, v]) => ` ${k}="${v}"`).join('');
                return `<${this.tag}${a}>${this.html}</${this.tag}>`;
            },
        };
        made.push(el);
        return el;
    },
};
globalThis.window = globalThis;
"""


# A DOM rich enough for the full walk, where every live node records what was done
# to it. Clones are marked `live: false` at birth, so a mutation landing on a clone
# is invisible here and a mutation landing on the user's page is not.
_WALK_HARNESS = r"""
globalThis.Node = {ELEMENT_NODE: 1, TEXT_NODE: 3, COMMENT_NODE: 8};
const mutations = [];
const reads = [];

function text(value) {
    return {nodeType: 3, textContent: value, cloneNode() { return {nodeType: 3, textContent: value, live: false}; }};
}

function el(tag, opts) {
    opts = opts || {};
    const node = {
        nodeType: 1,
        tagName: tag,
        live: opts.live !== false,
        id: opts.id || '',
        type: opts.type,
        value: opts.value,
        checked: opts.checked,
        src: opts.src,
        attrs: Object.assign({}, opts.attrs),
        childNodes: opts.children || [],
        rect: opts.rect || {left: 0, top: 0, width: 100, height: 20},
        visible: opts.visible !== false,
        getAttribute(key) {
            if (this.live) reads.push([this.tagName, 'getAttribute', key]);
            return key in this.attrs ? this.attrs[key] : null;
        },
        setAttribute(key, value) {
            if (this.live) mutations.push([this.tagName, 'setAttribute', key]);
            this.attrs[key] = value;
        },
        removeAttribute(key) {
            if (this.live) mutations.push([this.tagName, 'removeAttribute', key]);
            delete this.attrs[key];
        },
        appendChild(child) {
            if (this.live) mutations.push([this.tagName, 'appendChild']);
            this.childNodes.push(child);
            return child;
        },
        insertAdjacentHTML(where, markup) {
            if (this.live) mutations.push([this.tagName, 'insertAdjacentHTML']);
            // Real semantics, because the offscreen marker rides out this way:
            // afterbegin lands before the first child, beforeend after the last.
            if (where === 'afterbegin') this.pre = markup + (this.pre || '');
            else this.post = (this.post || '') + markup;
        },
        checkVisibility() {
            if (this.live) reads.push([this.tagName, 'checkVisibility']);
            return this.visible;
        },
        getBoundingClientRect() { return this.rect; },
        // Answers the tag-name arm of the interactive selector and nothing else,
        // so `:-webkit-autofill` is correctly a miss.
        matches(selector) {
            return selector.split(',').map((s) => s.trim())
                .includes(this.tagName.toLowerCase());
        },
        closest() { return null; },
        get children() { return this.childNodes.filter((n) => n.nodeType === 1); },
        get textContent() {
            return this.childNodes
                .map((n) => (n.nodeType === 3 ? n.textContent : n.textContent))
                .join('');
        },
        get outerHTML() {
            const a = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${v}"`).join('');
            const kids = this.childNodes
                .map((n) => (n.nodeType === 3 ? n.textContent : n.outerHTML)).join('');
            const tag = this.tagName.toLowerCase();
            return `<${tag}${a}>${this.pre || ''}${kids}${this.post || ''}</${tag}>`;
        },
        cloneNode() {
            return el(tag, {
                live: false, id: this.id, attrs: this.attrs, type: this.type,
                value: this.value, checked: this.checked, src: this.src,
            });
        },
        querySelectorAll() {
            const out = [];
            const walk = (n) => {
                for (const child of n.childNodes) {
                    if (child.nodeType !== 1) continue;
                    out.push(child);
                    walk(child);
                }
            };
            walk(this);
            return out;
        },
    };
    return node;
}
globalThis.location = {href: 'https://example.invalid/page'};
globalThis.window = globalThis;
globalThis.scrollY = 40;
globalThis.innerHeight = 800;
globalThis.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
globalThis.CSS = {escape: (t) => t};
"""


def _document(body: str, doc_height: int = 2000) -> str:
    """Install `body` as the live document for the walk harness."""
    return _WALK_HARNESS + f"""
globalThis.document = {{
    body: {body},
    documentElement: {{scrollHeight: {doc_height}}},
    createElement: (tag) => el(tag.toUpperCase(), {{live: false}}),
}};
"""


# --- page_outline.js ---------------------------------------------------------


def test_the_injected_payload_runs_the_page_analysis_exactly_once():
    """The whole analysis used to run twice per call, and nothing could see it.

    Each page script ends with a bare statement, and the call site appends
    `return pageOutline(...)`. When that trailing statement is a *call*, the
    payload reads:

        function pageOutline(...) { ... }
        pageOutline()               <- clones and analyses the whole document
        return pageOutline(false);  <- does all of it again

    The first result is discarded, so the only symptom was that every
    `scan_page`, `get_html` and page screenshot paid twice for the most expensive
    thing this package does. It survived for months because that line was the
    358th line of a Python string literal.

    `document.body` is read once per invocation, so counting that read counts the
    invocations.
    """
    report = _run_node(
        _EMPTY_DOCUMENT_HARNESS
        + """
        const run = new Function(%s);
        run();
        console.log(JSON.stringify({bodyReads}));
        """
        % json.dumps(_page_outline_payload())
    )

    assert report["bodyReads"] == 1, (
        f"the injected payload analysed the document {report['bodyReads']} times; "
        "the trailing statement in page_outline.js must reference the entry point, "
        "not call it"
    )


def test_the_cutlist_payload_also_runs_its_analysis_exactly_once():
    """Same rule, second file. The first one to break it did so undetected.

    `list_groups.js` walks every element on the page, so a trailing call costs a
    second full `querySelectorAll('*')` plus an `outerHTML` read per candidate
    item -- on the same request that already paid for the outline.
    """
    report = _run_node(
        _WALK_HARNESS
        + """
        let bodyReads = 0;
        globalThis.document = {
            get body() { bodyReads++; return el('BODY'); },
            createElement: (tag) => el(tag.toUpperCase(), {live: false}),
        };
        const run = new Function(%s);
        run();
        console.log(JSON.stringify({bodyReads}));
        """
        % json.dumps(_list_groups_payload())
    )

    assert report["bodyReads"] == 1, (
        f"listGroups ran {report['bodyReads']} times; the trailing statement in "
        "list_groups.js must reference the entry point, not call it"
    )


def test_an_empty_document_is_reported_as_a_marked_body_not_an_exception():
    """A page with no body has to come back as readable HTML, not a raised error.

    `get_html` hands whatever this returns straight to BeautifulSoup, so an
    exception here surfaces to the caller as a failed tool call on a page that
    merely has not finished loading. The marker is the only way the caller can
    tell "the page was empty" from "the page was tiny".
    """
    report = _run_node(
        _EMPTY_DOCUMENT_HARNESS
        + """
        const run = new Function(%s);
        console.log(JSON.stringify({html: run(), made: made.length}));
        """
        % json.dumps(_page_outline_payload())
    )

    assert 'data-btap-state="empty-document"' in report["html"]
    # The base URL rides along in a comment because the caller resolves relative
    # links against it, and an empty document still has a location.
    assert "btap-base:https://example.invalid/page" in report["html"]


def test_text_only_returns_an_empty_string_for_an_empty_document():
    """The text path has its own early return and must not emit the marker HTML."""
    report = _run_node(
        _EMPTY_DOCUMENT_HARNESS
        + """
        const run = new Function(%s);
        console.log(JSON.stringify({text: run()}));
        """
        % json.dumps(_page_outline_payload(text_only=True))
    )

    assert report["text"] == ""


def test_console_is_restored_after_the_analysis_returns():
    """The analysis silences four console methods and must always put them back.

    It replaces `console.log/warn/info/debug` so its own diagnostics do not land
    in whatever `console_capture_start` is recording. A path that returns without
    restoring them leaves the user's page unable to log for as long as it lives,
    and `get_console_messages` would then report a quiet page as evidence that
    nothing happened.
    """
    report = _run_node(
        _EMPTY_DOCUMENT_HARNESS
        + """
        const original = {log: console.log, warn: console.warn,
                          info: console.info, debug: console.debug};
        const run = new Function(%s);
        run();
        console.log(JSON.stringify({
            restored: ['log', 'warn', 'info', 'debug']
                .every((k) => console[k] === original[k]),
        }));
        """
        % json.dumps(_page_outline_payload())
    )

    assert report["restored"] is True


def test_the_analysis_leaves_every_live_node_untouched():
    """scan_page is documented as not modifying the page. This is that, measured.

    `tests/test_documentation_contract.py` asks the same question statically, by
    checking that no mutating call in either script targets a node the script did
    not create. That catches the shape of the old defect but cannot prove the
    running code is clean, because a receiver's identity is decided at runtime.
    Here every live node records what was done to it, and the clone it hands back
    records nothing -- so the expected mutation list is empty, and any write that
    lands on the user's page shows up as its tag name.
    """
    report = _run_node(
        _document(
            """el('BODY', {children: [
                el('DIV', {attrs: {class: 'wrap'}, children: [
                    text('hello'),
                    el('INPUT', {type: 'checkbox', checked: true, value: 'on'}),
                    el('SELECT', {value: 'b'}),
                ]}),
                el('DIV', {visible: false, children: [text('hidden text')]}),
            ]})"""
        )
        + """
        const run = new Function(%s);
        const html = run();
        console.log(JSON.stringify({mutations, reads: reads.length, html}));
        """
        % json.dumps(_page_outline_payload())
    )

    assert report["mutations"] == [], (
        "the page analysis wrote to the user's live document: "
        f"{report['mutations']}. scan_page's own description, both README tool "
        "tables and the offline disclosure check all state that it does not"
    )
    # Not vacuous: the walk really did happen, so "no mutations" is a finding
    # rather than a harness that was never reached.
    assert report["reads"] > 0
    assert "hidden text" not in report["html"]
    # Form state lives in properties, so it only reaches the model if the clone
    # carries it as an attribute.
    assert 'checked=""' in report["html"]
    assert 'data-selected="b"' in report["html"]


def test_content_beyond_the_range_window_is_counted_rather_than_dropped_silently():
    """A dropped element the caller is not told about reads as "not on the page".

    `server.py::_offscreen_note` parses this marker out of the returned HTML and
    turns it into the `offscreen` count and the "scroll and scan again" hint. If
    the count never reaches the marker, an agent looking for a button 6000px down
    concludes it does not exist.
    """
    report = _run_node(
        _document(
            """el('BODY', {children: [
                el('P', {children: [text('near')]}),
                el('P', {rect: {left: 0, top: 9000, width: 100, height: 20},
                         children: [text('far away')]}),
            ]})""",
            doc_height=12000,
        )
        + """
        const run = new Function(%s);
        console.log(JSON.stringify({html: run()}));
        """
        % json.dumps(_page_outline_payload())
    )

    html = report["html"]
    assert "far away" not in html
    assert "<!--btap-offscreen:1 scrollY:40 viewH:800 docH:12000-->" in html


def test_an_element_the_engine_calls_invisible_is_dropped_with_its_text():
    """The leak this replaced shipped because text nodes clone even when hidden.

    A `display:none` block's text survives `cloneNode`, so counting *any* kept
    child made the hidden parent look like it still held content, and the whole
    block reached the model. Only a kept child *element* counts as surviving
    content now, which is what keeps hidden text out.
    """
    report = _run_node(
        _document(
            """el('BODY', {children: [
                el('DIV', {visible: false, children: [
                    text('SHOULD_NOT_APPEAR'),
                    el('SPAN', {visible: false, children: [text('NOR_THIS')]}),
                ]}),
                el('P', {children: [text('visible copy')]}),
            ]})"""
        )
        + """
        const run = new Function(%s);
        console.log(JSON.stringify({html: run()}));
        """
        % json.dumps(_page_outline_payload())
    )

    assert "SHOULD_NOT_APPEAR" not in report["html"]
    assert "NOR_THIS" not in report["html"]
    assert "visible copy" in report["html"]


# --- list_groups.js ---------------------------------------------------------


def test_list_groups_returns_an_empty_list_for_a_missing_root():
    """`get_main_block` passes `document.body`, which is null before the parse.

    Returning `[]` is what makes the caller fall back to the whole page instead
    of raising.
    """
    report = _run_node(
        """
        globalThis.document = {body: null};
        globalThis.window = globalThis;
        const run = new Function(%s);
        console.log(JSON.stringify({result: run()}));
        """
        % json.dumps(_list_groups_payload("null"))
    )

    assert report["result"] == []


def test_list_groups_restores_console_before_returning():
    """Same contract as the outline pass, on the same user-visible console."""
    report = _run_node(
        """
        globalThis.document = {body: null};
        globalThis.window = globalThis;
        const original = console.log;
        const run = new Function(%s);
        run();
        console.log(JSON.stringify({restored: console.log === original}));
        """
        % json.dumps(_list_groups_payload("null"))
    )

    assert report["restored"] is True


def test_repetition_is_recognised_by_signature_not_by_score():
    """Six `li.item` siblings are a list; the two `li.ad` among them are not.

    This is the whole reason the scored search was replaced. Upstream ranked
    candidates with a weighted child/grandchild count and a tuned floor, which
    meant the answer depended on constants nobody could re-derive. Grouping
    children by tag-plus-class makes the repeat a fact about the markup, and the
    odd items out simply land in a different bucket.
    """
    items = "".join(
        f"el('LI', {{attrs: {{class: 'item'}}, children: [text('record {i} "
        f"with a body long enough to be worth collapsing')]}}), "
        for i in range(6)
    )
    report = _run_node(
        _document(
            f"""el('BODY', {{children: [
                el('UL', {{id: 'main', children: [
                    {items}
                    el('LI', {{attrs: {{class: 'ad'}}, children: [text('promo')]}}),
                    el('LI', {{attrs: {{class: 'ad'}}, children: [text('promo')]}}),
                ]}}),
            ]}})"""
        )
        + """
        const run = new Function(%s);
        console.log(JSON.stringify({groups: run()}));
        """
        % json.dumps(_list_groups_payload())
    )

    groups = report["groups"]
    assert len(groups) == 1, groups
    assert groups[0]["itemCount"] == 6
    assert groups[0]["signature"] == "LI.item"
    # `simphtml` hands this selector straight to `soup.select`, so the container
    # has to be addressable and the items have to be a child step of it.
    assert groups[0]["selector"] == "#main > li.item"


def test_a_repeat_shorter_than_the_floor_is_not_reported():
    """Four siblings are not a list worth collapsing, and a group of them would
    cost the caller a roundtrip to find that out."""
    items = "".join(
        f"el('LI', {{attrs: {{class: 'item'}}, children: [text('row {i}')]}}), "
        for i in range(4)
    )
    report = _run_node(
        _document(
            f"""el('BODY', {{children: [
                el('UL', {{id: 'short', children: [{items}]}}),
            ]}})"""
        )
        + """
        const run = new Function(%s);
        console.log(JSON.stringify({groups: run()}));
        """
        % json.dumps(_list_groups_payload())
    )

    assert report["groups"] == []


def test_the_group_holding_the_most_markup_is_reported_first():
    """`simphtml` applies these in order, so the order is what decides savings.

    A row of six icons and a list of six articles are equally repetitive. Sorting
    by the markup a group occupies is what separates them without a weight: the
    icons are cheap to keep and collapsing them saves nothing.
    """
    icons = "".join(
        f"el('SPAN', {{attrs: {{class: 'ico'}}, children: [text('{i}')]}}), "
        for i in range(6)
    )
    posts = "".join(
        f"el('DIV', {{attrs: {{class: 'post'}}, children: [text('{'body ' * 40}')]}}), "
        for i in range(6)
    )
    report = _run_node(
        _document(
            f"""el('BODY', {{children: [
                el('NAV', {{id: 'icons', children: [{icons}]}}),
                el('SECTION', {{id: 'feed', children: [{posts}]}}),
            ]}})"""
        )
        + """
        const run = new Function(%s);
        console.log(JSON.stringify({groups: run()}));
        """
        % json.dumps(_list_groups_payload())
    )

    groups = report["groups"]
    assert [g["signature"] for g in groups] == ["DIV.post", "SPAN.ico"], groups
    assert groups[0]["chars"] > groups[1]["chars"]
