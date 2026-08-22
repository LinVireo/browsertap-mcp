"""page_click must prove the point it is about to click belongs to the target.

A resolved bounding rect only says the element exists somewhere in the layout. A
cookie banner, a modal backdrop or a sticky header can own that pixel, and an
element below the fold resolves to a point outside the viewport -- in both cases
``Input.dispatchMouseEvent`` returns success and the click lands somewhere else,
which is the failure shape AGENTS.md section 4 documents for the physical-input
tools, one layer down.

The proof runs in the browser, so these tests run the generated script in Node
against a hand-built DOM, the way ``test_dialog_policy`` runs extension code.
Both resolvers embed the same ``_HIT_TEST_JS`` helper, so exercising it through
the CSS resolver covers the structured one's copy as well; the structured
resolver's own wiring is asserted textually at the end.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from browsertap_mcp.page_input import (
    _HIT_TEST_JS,
    resolve_selector_script,
    structured_locator_script,
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for the DOM harness"
)

HARNESS = """
const vm = require('vm');
const spec = %(spec)s;
const script = %(script)s;
const scrolls = [];
const nodes = {};

function makeElement(def) {
  const el = {
    tagName: def.tagName || 'BUTTON',
    id: def.id || '',
    className: def.className || '',
    disabled: Boolean(def.disabled),
    rect: def.rect,
    getAttribute: name => (def.attributes || {})[name] || null,
    getBoundingClientRect() { return this.rect; },
    scrollIntoView(options) {
      scrolls.push({id: this.id, options: options || null});
      if (def.rectAfterScroll) this.rect = def.rectAfterScroll;
    },
    getRootNode() { return def.rootNode ? nodes[def.rootNode].shadowRoot : document; },
    contains(other) { return other === el || (def.children || []).some(name => nodes[name] === other); },
    matches() { return false; },
  };
  if (def.shadowRoot) el.shadowRoot = {host: el};
  return el;
}

for (const def of spec.elements) nodes[def.id] = makeElement(def);

const document = {
  title: spec.title || 'page',
  querySelector: () => nodes[spec.match] || null,
  elementFromPoint(x, y) {
    for (const id of spec.stack) {
      const rect = nodes[id].rect;
      if (x >= rect.left && x <= rect.left + rect.width &&
          y >= rect.top && y <= rect.top + rect.height) return nodes[id];
    }
    return null;
  },
};
for (const id of Object.keys(nodes)) nodes[id].ownerDocument = document;
const window = {innerWidth: spec.innerWidth, innerHeight: spec.innerHeight};
document.defaultView = window;
const location = {
  origin: 'https://example.test', pathname: '/',
  hostname: 'example.test', href: 'https://example.test/',
};

const context = vm.createContext({document, window, location, console});
const result = vm.runInContext(script, context);
process.stdout.write(JSON.stringify({result, scrolls}));
"""


def _run(script: str, spec: dict) -> dict:
    source = HARNESS % {"spec": json.dumps(spec), "script": json.dumps(script)}
    completed = subprocess.run(
        ["node", "-"], input=source, text=True, capture_output=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _click_script(**kwargs) -> str:
    return resolve_selector_script(
        "#target", 0, 0, require_interactable=True, verify_hit=True, **kwargs
    )


def _spec(**overrides) -> dict:
    spec = {
        "match": "target",
        "stack": ["target"],
        "innerWidth": 800,
        "innerHeight": 600,
        "elements": [
            {"id": "target", "rect": {"left": 100, "top": 100, "width": 80, "height": 40}}
        ],
    }
    spec.update(overrides)
    return spec


def test_a_visible_unobstructed_element_is_verified():
    out = _run(_click_script(center_x=True, center_y=True), _spec())
    assert out["result"]["found"] is True
    assert out["result"]["hitVerified"] is True
    assert out["result"]["scrolledIntoView"] is False
    assert out["scrolls"] == []


def test_an_overlay_that_owns_the_pixel_refuses_instead_of_clicking_it():
    # The old resolver returned found:true here -- the rect is non-zero and the
    # element is not disabled -- so the click was dispatched into the overlay.
    spec = _spec(
        stack=["overlay", "target"],
        elements=[
            {"id": "target", "rect": {"left": 100, "top": 100, "width": 80, "height": 40}},
            {
                "id": "overlay",
                "tagName": "DIV",
                "className": "cookie-banner  sticky",
                "rect": {"left": 0, "top": 0, "width": 800, "height": 600},
            },
        ],
    )
    out = _run(_click_script(center_x=True, center_y=True), spec)
    assert out["result"] == {
        "found": False,
        "status": "obscured",
        "occludedBy": "div#overlay.cookie-banner.sticky",
        "scrolledIntoView": False,
    }


def test_a_child_of_the_target_on_top_still_counts_as_a_hit():
    # Clicking a button whose label span is the topmost node is a hit, not an
    # occlusion. Refusing here would break most real buttons.
    spec = _spec(
        stack=["label", "target"],
        elements=[
            {
                "id": "target",
                "rect": {"left": 100, "top": 100, "width": 80, "height": 40},
                "children": ["label"],
            },
            {
                "id": "label",
                "tagName": "SPAN",
                "rect": {"left": 110, "top": 110, "width": 40, "height": 20},
            },
        ],
    )
    out = _run(_click_script(center_x=True, center_y=True), spec)
    assert out["result"]["found"] is True
    assert out["result"]["hitVerified"] is True


def test_a_shadow_host_on_top_of_its_own_shadow_child_counts_as_a_hit():
    # elementFromPoint stops at the host, so a naive identity check would refuse
    # every click inside a web component.
    spec = _spec(
        stack=["host"],
        elements=[
            {
                "id": "target",
                "rect": {"left": 100, "top": 100, "width": 80, "height": 40},
                "rootNode": "host",
            },
            {
                "id": "host",
                "tagName": "MY-WIDGET",
                "rect": {"left": 90, "top": 90, "width": 200, "height": 100},
                "shadowRoot": True,
            },
        ],
    )
    out = _run(_click_script(center_x=True, center_y=True), spec)
    assert out["result"]["found"] is True


def test_an_element_below_the_fold_is_scrolled_into_view_and_then_verified():
    spec = _spec(
        elements=[
            {
                "id": "target",
                "rect": {"left": 100, "top": 2000, "width": 80, "height": 40},
                "rectAfterScroll": {"left": 100, "top": 280, "width": 80, "height": 40},
            }
        ]
    )
    out = _run(_click_script(center_x=True, center_y=True), spec)
    assert out["result"]["found"] is True
    assert out["result"]["scrolledIntoView"] is True
    assert out["result"]["y"] == 280
    assert out["scrolls"] == [
        {"id": "target", "options": {"block": "center", "inline": "center"}}
    ]


def test_an_element_that_stays_off_screen_refuses_rather_than_dispatching():
    spec = _spec(
        elements=[
            {"id": "target", "rect": {"left": 100, "top": 2000, "width": 80, "height": 40}}
        ]
    )
    out = _run(_click_script(center_x=True, center_y=True), spec)
    assert out["result"]["found"] is False
    assert out["result"]["status"] == "outside_viewport"
    assert out["result"]["scrolledIntoView"] is True  # tried, and says so


def test_an_explicit_offset_is_tested_at_that_offset_not_the_centre():
    # offset_x/offset_y are measured from the element's top-left corner, so the
    # point under test moves with them.
    spec = _spec(
        stack=["overlay", "target"],
        elements=[
            {"id": "target", "rect": {"left": 100, "top": 100, "width": 200, "height": 40}},
            {
                "id": "overlay",
                "tagName": "HEADER",
                "rect": {"left": 0, "top": 0, "width": 150, "height": 200},
            },
        ],
    )
    # The centre (x=200) clears the overlay; offset 10 (x=110) does not.
    clear = _run(_click_script(center_x=True, center_y=True), spec)
    assert clear["result"]["found"] is True
    covered = _run(
        resolve_selector_script(
            "#target", 10, 10, require_interactable=True, verify_hit=True
        ),
        spec,
    )
    assert covered["result"]["status"] == "obscured"
    assert covered["result"]["occludedBy"] == "header#overlay"


def test_verification_is_opt_in_so_query_and_type_paths_are_unchanged():
    spec = _spec(
        stack=["overlay", "target"],
        elements=[
            {"id": "target", "rect": {"left": 100, "top": 100, "width": 80, "height": 40}},
            {
                "id": "overlay",
                "tagName": "DIV",
                "rect": {"left": 0, "top": 0, "width": 800, "height": 600},
            },
        ],
    )
    out = _run(resolve_selector_script("#target"), spec)
    assert out["result"]["found"] is True
    assert out["result"]["hitVerified"] is False


def test_the_structured_resolver_carries_the_same_proof():
    script = structured_locator_script(
        {"role": "button", "name": "Pay"}, purpose="click", verify_hit=True
    )
    assert _HIT_TEST_JS.strip().splitlines()[0] in script
    assert "if (purpose === 'click' && verifyHit)" in script
    # Inside a frame the accumulated frame offsets go stale after a scroll, so
    # that path refuses instead of scrolling.
    assert "&& !framed" in script
    framed = structured_locator_script(
        {"frame": ["iframe"], "css": "#pay"}, purpose="click", verify_hit=True
    )
    assert "const framed = true" in framed
