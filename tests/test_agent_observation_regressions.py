"""Agent-visible snapshots and text input must agree with the actual target."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from browsertap_mcp import server as S
from browsertap_mcp import simphtml
from browsertap_mcp.page_input import structured_locator_script, type_target_script

_NEEDS_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")


def _node(source):
    completed = subprocess.run(
        ["node", "-"], input=source, text=True, capture_output=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


_TYPE_DOM = r"""
const spec = SPEC;
const previous = {tagName: 'INPUT', id: 'previous-field'};
const view = {getComputedStyle: () => ({display:'block', visibility:'visible', opacity:'1'})};
const document = {activeElement: previous, body: {}, documentElement: {}, defaultView: view};
const innerDocument = {defaultView: view, activeElement: previous};
const shadowRoot = {activeElement: previous};
const frame = {
  tagName:'IFRAME', contentDocument:innerDocument,
  getBoundingClientRect: () => ({left:0, top:0, width:500, height:300}),
};
const host = {tagName:'EDITOR-HOST', shadowRoot};
const target = {
  tagName:spec.tagName || 'INPUT', type:spec.type || 'text', id:'target',
  disabled:!!spec.disabled, readOnly:!!spec.readOnly,
  isContentEditable:!!spec.contentEditable,
  ownerDocument:spec.nested === 'frame' ? innerDocument : document,
  getAttribute: name => (spec.attributes || {})[name] || null,
  getBoundingClientRect: () => ({left:10, top:10, width:spec.xterm ? 0 : 100, height:20}),
  matches(selector) {
    if (selector === ':disabled') return !!spec.effectiveDisabled;
    return selector === '.xterm-helper-textarea' && !!spec.xterm;
  },
  closest() { return null; },
  focus() {
    if (spec.focusFails) return;
    if (spec.nested === 'shadow') { document.activeElement = host; shadowRoot.activeElement = target; }
    else if (spec.nested === 'frame') { document.activeElement = frame; innerDocument.activeElement = target; }
    else document.activeElement = target;
    if (spec.redirectFocus) document.activeElement = previous;
    if (spec.disableOnFocus) target.disabled = true;
  },
  select() { if (spec.redirectSelection) document.activeElement = previous; },
};
const terminal = {
  tagName:'DIV', matches: selector => selector === '.xterm',
  querySelector: () => target, closest: () => null,
};
function query(selector) {
  if (selector === 'iframe') return [frame];
  if (selector === '#host') return [host];
  if (selector === '.xterm-helper-textarea') return spec.xterm ? [target] : [];
  if (selector === '.xterm') return [terminal];
  return [target];
}
for (const root of [document, innerDocument, shadowRoot]) {
  root.querySelectorAll = query;
  root.querySelector = selector => query(selector)[0] || null;
}
if (spec.contentEditable) {
  delete target.select;
  const expectedDocument = target.ownerDocument;
  expectedDocument.createRange = () => ({selectNodeContents(node) {
    if (node !== target) throw new Error('selected a different editor');
  }});
  expectedDocument.defaultView = {...view, getSelection:() => ({removeAllRanges() {}, addRange() {}})};
}
if (spec.initiallyFocused) target.focus();
const window = view;
const result = eval(SCRIPT);
process.stdout.write(JSON.stringify({result, active:document.activeElement.id || ''}));
"""


def _type_result(script, **spec):
    return _node(
        _TYPE_DOM.replace("SPEC", json.dumps(spec)).replace("SCRIPT", json.dumps(script))
    )["result"]


@_NEEDS_NODE
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize(
    "state,status",
    [
        ({"disabled": True}, "not_interactable"),
        ({"readOnly": True}, "not_interactable"),
        ({"effectiveDisabled": True}, "not_interactable"),
        ({"attributes": {"aria-disabled": "true"}}, "not_interactable"),
        ({"tagName": "BUTTON"}, "not_interactable"),
        ({"type": "checkbox"}, "not_interactable"),
        ({"focusFails": True}, "focus_failed"),
        ({"redirectFocus": True}, "focus_failed"),
        ({"redirectSelection": True}, "focus_failed"),
        ({"disableOnFocus": True}, "not_interactable"),
    ],
)
def test_type_refuses_when_the_requested_editor_cannot_receive_text(structured, state, status):
    script = (
        structured_locator_script({"css": "#target"}, purpose="type", select_all=True)
        if structured else type_target_script("#target", select_all=True)
    )
    result = _type_result(script, **state)
    assert result["found"] is False
    assert result["status"] == status


@_NEEDS_NODE
@pytest.mark.parametrize("nested", ["shadow", "frame"])
@pytest.mark.parametrize("omitted_selector", [False, True])
def test_deep_focused_editor_is_a_valid_type_target(nested, omitted_selector):
    locator = {"css": "#target"}
    locator.update({"shadow": ["#host"]} if nested == "shadow" else {"frame": ["iframe"]})
    script = (
        type_target_script("") if omitted_selector
        else structured_locator_script(locator, purpose="type")
    )
    result = _type_result(script, nested=nested, initiallyFocused=omitted_selector)
    assert result["found"] is True
    assert result["focusConfirmed"] is True
    assert result["activeElement"]["id"] == "target"


@_NEEDS_NODE
@pytest.mark.parametrize("omitted_selector", [False, True])
def test_clear_uses_the_contenteditable_frames_own_selection(omitted_selector):
    script = (
        type_target_script("", select_all=True) if omitted_selector
        else structured_locator_script(
            {"css": "#target", "frame": ["iframe"]}, purpose="type", select_all=True
        )
    )
    result = _type_result(
        script, nested="frame", contentEditable=True, tagName="DIV", initiallyFocused=True
    )
    assert result["found"] is True
    assert result["focusConfirmed"] is True


@_NEEDS_NODE
@pytest.mark.parametrize("structured", [False, True])
def test_xterm_container_still_retargets_its_hidden_helper(structured):
    script = (
        structured_locator_script({"css": ".xterm"}, purpose="type")
        if structured else type_target_script(".xterm")
    )
    result = _type_result(script, xterm=True, tagName="TEXTAREA")
    assert result["found"] is True
    assert result["focusConfirmed"] is True
    assert result["targetKind"] == "xterm"


def _server_target(monkeypatch, driver):
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S, "ensure_sessions", lambda *args, **kwargs: [{"id": "chrome:test:7"}]
    )


@_NEEDS_NODE
def test_server_disabled_target_never_dispatches_text(monkeypatch):
    driver = SimpleNamespace(default_session_id="chrome:test:7")
    _server_target(monkeypatch, driver)
    monkeypatch.setattr(
        S, "exec_js", lambda script, **kwargs: {"data": _type_result(script, disabled=True)}
    )
    monkeypatch.setattr(
        S, "_run_page_input", lambda *args, **kwargs: pytest.fail("input was dispatched")
    )
    result = S.page_type("PAYLOAD", selector="#target", session_id="chrome:test:7")
    assert result["status"] == "not_interactable"
    assert result["typed_chars"] == 0
    assert result["active_element"]["id"] == "previous-field"


@_NEEDS_NODE
@pytest.mark.parametrize("selector", ["#target", {"css": "#target"}])
def test_final_batch_guard_refuses_a_target_that_became_readonly(monkeypatch, selector):
    sent = []

    def ext_cmd(payload, **kwargs):
        if payload["cmd"] == "bridge_status":
            assert kwargs["client_id"] == "chrome:test"
            return {"data": {"capabilities": {"batch_result_guard": True}}}
        results = []
        for index, command in enumerate(payload["commands"]):
            if command.get("assertTruthy"):
                value = _type_result(command["params"]["expression"], readOnly=True)
                assert value is False
                return {"data": {
                    "ok": False, "code": "batch_guard_failed", "results": results,
                    "error": {"code": "batch_guard_failed", "message": "target lost focus"},
                    "failed_command_index": index, "dispatched": True, "retryable": False,
                }}
            if command["method"].startswith("Input."):
                sent.append(command)
            results.append({"result": {"value": True}})
        pytest.fail("a guarded evaluation was not sent before text input")

    driver = SimpleNamespace(default_session_id="chrome:test:7", ext_cmd=ext_cmd)
    _server_target(monkeypatch, driver)
    monkeypatch.setattr(S, "exec_js", lambda script, **kwargs: {"data": _type_result(script)})
    result = S.page_type(
        "PAYLOAD", selector=selector, submit_key="Enter", session_id="chrome:test:7"
    )
    assert result["status"] == "focus_failed"
    assert result["error_code"] == "batch_guard_failed"
    assert result["typed_chars"] == 0
    assert result["input_dispatched"] is False
    assert result["focus_confirmed"] is False
    assert sent == []


@pytest.mark.parametrize("has_transport", [False, True])
def test_type_requires_guard_support_before_sending_input(monkeypatch, has_transport):
    seen = []
    driver = SimpleNamespace(default_session_id="chrome:test:7")
    if has_transport:
        def ext_cmd(payload, **kwargs):
            seen.append(payload["cmd"])
            return {"data": {"capabilities": {}}}
        driver.ext_cmd = ext_cmd
    _server_target(monkeypatch, driver)
    monkeypatch.setattr(S, "_page_type_target_info", lambda *args, **kwargs: {
        "found": True, "focusConfirmed": True, "targetKind": "element",
    })
    monkeypatch.setattr(
        S, "_run_page_input", lambda *args, **kwargs: pytest.fail("input was dispatched")
    )
    result = S.page_type("PAYLOAD", selector="#target", session_id="chrome:test:7")
    assert result["status"] == "stale_extension"
    assert result["typed_chars"] == 0
    assert result["input_dispatched"] is False
    assert seen == (["bridge_status"] if has_transport else [])


@_NEEDS_NODE
def test_outline_carries_current_form_properties_without_mutating_the_page():
    from test_page_scripts import _WALK_HARNESS, _page_outline_payload

    source = _WALK_HARNESS + r"""
const createElement = el;
el = function(tag, opts) {
    const node = createElement(tag, opts);
    const readText = Object.getOwnPropertyDescriptor(node, 'textContent').get;
    Object.defineProperty(node, 'textContent', {
        get: readText,
        set(value) {
            if (this.live) mutations.push([this.tagName, 'textContent']);
            this.childNodes = [text(value)];
        },
    });
    node.selected = !!(opts && opts.selected);
    return node;
};
const cleared = el('INPUT', {value:'', attrs:{id:'cleared', value:'initial'}});
const unchecked = el('INPUT', {type:'checkbox', checked:false, attrs:{id:'unchecked', checked:''}});
const memo = el('TEXTAREA', {value:'updated memo', attrs:{id:'memo'}, children:[text('initial memo')]});
const emptyMemo = el('TEXTAREA', {value:'', attrs:{id:'empty-memo'}, children:[text('initial text')]});
const oldOption = el('OPTION', {selected:false, attrs:{id:'old', selected:''}, children:[text('Old')]});
const newOption = el('OPTION', {selected:true, attrs:{id:'new'}, children:[text('New')]});
const select = el('SELECT', {value:'', attrs:{id:'choice', 'data-selected':'initial'}, children:[oldOption,newOption]});
globalThis.document = {
    body:el('BODY', {children:[cleared,unchecked,memo,emptyMemo,select]}),
    documentElement:{scrollHeight:500},
    createElement:tag => el(tag.toUpperCase(), {live:false}),
};
const run = new Function(PAYLOAD);
console.log(JSON.stringify({html:run(), mutations}));
"""
    report = _node(source.replace("PAYLOAD", json.dumps(_page_outline_payload())))
    soup = BeautifulSoup(report["html"], "html.parser")
    assert soup.select_one("#cleared")["value"] == ""
    assert not soup.select_one("#unchecked").has_attr("checked")
    assert soup.select_one("#memo").get_text() == "updated memo"
    assert soup.select_one("#empty-memo").get_text() == ""
    assert not soup.select_one("#memo").has_attr("value")
    assert soup.select_one("#choice")["data-selected"] == ""
    assert not soup.select_one("#old").has_attr("selected")
    assert soup.select_one("#new").has_attr("selected")
    assert report["mutations"] == []


@pytest.mark.parametrize("cutlist,text_only,extra_js", [(False, False, ""), (True, False, ""), (True, False, "void 0;"), (False, True, "")])
@pytest.mark.parametrize("budget", [0, 5, 25, 1000])
def test_all_snapshot_modes_obey_the_output_budget(monkeypatch, cutlist, text_only, extra_js, budget):
    content = "content " * 15000
    page = content if text_only else f"<body><main><section><p>{content}</p></section></main></body>"
    monkeypatch.setattr(simphtml, "get_main_block", lambda *args, **kwargs: page)
    monkeypatch.setattr(simphtml, "_get_cutlist_page", lambda *args, **kwargs: ([], page))
    driver = SimpleNamespace(execute_js=lambda *args, **kwargs: {"data": []})
    result = simphtml.get_html(
        driver, cutlist=cutlist, text_only=text_only, extra_js=extra_js, maxchars=budget
    )
    assert len(result) <= budget
    if budget >= 25:
        assert "[TRUNCATED" in result


def test_output_cap_does_not_return_a_partial_tag(monkeypatch):
    page = '<input id="' + "x" * 5000 + '" value="pending">'
    monkeypatch.setattr(simphtml, "get_main_block", lambda *args, **kwargs: page)
    result = simphtml.get_html(object(), maxchars=40)
    assert len(result) <= 40
    assert "[TRUNCATED" in result
    assert "<" not in result


@pytest.mark.parametrize("response", [
    {"delivery_state": "sent_unconfirmed", "result": "no ACK"},
    {"result": "No response data in 2s (no ACK, script may not have been delivered)"},
])
def test_a_lost_ack_never_repeats_the_script(response):
    calls = []

    def execute_js(script, **kwargs):
        calls.append(script)
        return response

    driver = SimpleNamespace(default_session_id="chrome:test:7", execute_js=execute_js)
    result = simphtml.execute_js_rich(
        "submitOrder()", driver, no_monitor=True, timeout=2,
        before_sids=set(), session_id="chrome:test:7",
    )
    assert result["status"] == "no_response"
    assert result["retry_safe"] is False
    assert calls == ["submitOrder()"]
    assert result["delivery_state"] == "sent_unconfirmed"
    assert "may already have executed" in result["suggestion"]
