"""Role queries must ignore the hidden payment forms kept by SPA renderers."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from browsertap_mcp.page_input import locator_query_script

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _query(buttons: list[dict], selector: dict | None = None) -> dict:
    script = locator_query_script(selector or {"role": "button", "name": "Submit payment"})
    harness = r"""
const vm = require('vm');
const specs = BUTTONS;
const view = { getComputedStyle: e => ({display:'block',visibility:'visible'}) };
const document = {
  title: 'Synthetic checkout', defaultView: view,
  querySelector: () => null,
  querySelectorAll: () => buttons,
};
function element(spec) {
  return {
    nodeType: 1, tagName: 'BUTTON', childNodes: [], children: [],
    textContent: 'Submit payment', ownerDocument: document,
    disabled: !!spec.disabled, inert: !!spec.inert,
    getAttribute: name => name === 'aria-hidden' ? (spec.ariaHidden || null) : null,
    getBoundingClientRect: () => ({left:20,top:40,width:100,height:30}),
    checkVisibility: () => spec.visible !== false,
    getRootNode: () => spec.shadowHost ? {host: element(spec.shadowHost)} : document,
    parentElement: spec.parent ? element(spec.parent) : null,
  };
}
const buttons = specs.map(element);
const context = {document, window:view, location:{
  hostname:'checkout.test',href:'https://checkout.test/',origin:'https://checkout.test',pathname:'/'
}};
process.stdout.write(JSON.stringify(vm.runInNewContext(SCRIPT, context)));
"""
    harness = harness.replace("BUTTONS", json.dumps(buttons)).replace("SCRIPT", json.dumps(script))
    completed = subprocess.run(
        ["node", "-"], input=harness, text=True, capture_output=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("hidden", [
    {"visible": False},
    {"ariaHidden": "true"},
    {"parent": {"ariaHidden": "TRUE"}},
    {"shadowHost": {"ariaHidden": "true"}},
    {"inert": True},
    {"parent": {"inert": True}},
])
def test_role_wait_selects_only_accessible_button_in_spa(hidden):
    result = _query([hidden, {}])
    assert result["found"] is True
    assert result["status"] == "found"


def test_role_wait_for_hidden_only_button_is_not_satisfied():
    assert _query([{"visible": False}]) == {"found": False, "status": "not_found"}


def test_role_wait_does_not_choose_between_two_visible_payment_buttons():
    assert _query([{}, {}]) == {"found": False, "status": "ambiguous", "matches": 2}


def test_role_wait_can_observe_disabled_button_before_it_becomes_enabled():
    assert _query([{"disabled": True}])["found"] is True


def test_css_query_preserves_dom_presence_and_strict_duplicate_semantics():
    assert _query([{"visible": False}], {"css": "button"})["found"] is True
    assert _query([{"visible": False}, {}], {"css": "button"}) == {
        "found": False, "status": "ambiguous", "matches": 2,
    }
