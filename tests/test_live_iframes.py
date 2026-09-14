"""Generic iframe input acceptance, using only the suite's owned scratch tab.

Run explicitly with ``pytest tests/test_live_iframes.py -m live``. All pages,
forms, and event collectors are local fixtures; no external form is submitted.
The shared driver fixture owns build preflight and its evidence destination.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from browsertap_mcp import server as S

pytestmark = pytest.mark.live

_MOUSE_EVENTS = {"pointerdown", "mousedown", "mouseup", "click"}
_INPUT_EVENTS = _MOUSE_EVENTS | {"beforeinput", "input", "keydown", "keypress", "keyup", "submit"}
_EVENT_SCRIPT = r"""
let sequence = 0;
const snapshot = () => ({
  query_value: document.querySelector('#query')?.value ?? null,
  hidden_values: [...document.querySelectorAll('[data-template] input')].map(el => el.value)
});
const report = (kind, event = null) => {
  const message = {
    token: fixture.token, document: fixture.document, sequence: sequence++, kind,
    origin: location.origin, ready_state: document.readyState,
    id: event?.target?.id || '', key: event?.key || '',
    trusted: event ? event.isTrusted : null, ...snapshot()
  };
  fetch(fixture.endpoint, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(message), keepalive: true
  }).catch(error => { window.__fixtureEventError = String(error); });
};
for (const kind of ['pointerdown', 'mousedown', 'mouseup', 'click', 'beforeinput',
                    'input', 'keydown', 'keypress', 'keyup', 'submit']) {
  document.addEventListener(kind, event => {
    if (kind === 'submit') event.preventDefault();
    report(kind, event);
  }, true);
}
document.querySelector('#remove-result')?.addEventListener('click', () => {
  document.querySelector('#transient-result')?.remove();
});
// Inline-script completion can precede both the document and child-frame load.
addEventListener('load', () => report('ready'), {once: true});
"""
_LEAF_BODY = """
<div hidden data-template>
  <label>Search terms <input class="query" value="hidden seed"></label>
  <button class="action" type="button">Run lookup</button>
</div>
<div hidden inert data-template>
  <label>Search terms <input class="query" value="inert seed"></label>
  <button class="action" type="button">Run lookup</button>
</div>
<form id="search-form" action="/unused-search" method="get">
  <label for="query">Search terms</label>
  <input id="query" class="query" name="q" value="seed query" autocomplete="off">
  <button id="search-submit" type="submit">Search</button>
  <button id="action" class="action" type="button">Run lookup</button>
</form>
<div>
  <button id="duplicate-a" class="duplicate" type="button">Duplicate choice</button>
  <button id="duplicate-b" class="duplicate" type="button">Duplicate choice</button>
  <input id="duplicate-input-a" class="duplicate-query" value="left seed">
  <input id="duplicate-input-b" class="duplicate-query" value="right seed">
</div>
<span id="transient-result">A temporary search result</span>
<button id="remove-result" type="button">Dismiss result</button>
"""


def _document(token, name, body):
    config = json.dumps({
        "token": token, "document": name, "endpoint": f"/case/{token}/events",
    })
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>BTAP iframe search fixture</title><style>'
        'body{font:14px system-ui;margin:10px}label{display:block}'
        'input{width:145px;padding:5px}button{padding:6px;margin:3px}'
        'form{margin-bottom:7px}iframe{display:block;width:620px;max-width:94%;'
        'height:260px;border:7px solid #345;margin:8px}'
        '#parent-overlay{position:fixed;inset:0;z-index:1000;background:#eee}'
        '</style></head><body>' + body + '<script>const fixture=' + config + ';'
        + _EVENT_SCRIPT + '</script></body></html>'
    ).encode("utf-8")


def _frame(frame_id, url, *, height=260):
    return (
        f'<iframe id="{frame_id}" class="branch" title="Fixture search frame" '
        f'src="{escape(url, quote=True)}" style="height:{height}px"></iframe>'
    )


@dataclass
class _ExpectedFrame:
    element_id: str
    url: str
    oopif: bool


@dataclass
class _Case:
    site: _Site
    token: str
    url: str
    frames: list[dict[str, str]]
    ready_documents: set[str]
    topology: list[_ExpectedFrame]

    def locator(self, **selector):
        return {"frame": self.frames, **selector}

    def events(self, document=None, kind=None):
        with self.site.condition:
            events = list(self.site.events[self.token])
        return sorted(
            (event for event in events
             if (document is None or event["document"] == document)
             and (kind is None or event["kind"] == kind)),
            key=lambda event: (event["document"], event["sequence"]),
        )

    def until(self, predicate, *, timeout=10):
        deadline = time.monotonic() + timeout
        with self.site.condition:
            while True:
                events = self.events()
                if predicate(events):
                    return events
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"local iframe observations did not arrive: {events}"
                self.site.condition.wait(remaining)


class _Site:
    def __init__(self):
        self.condition = threading.Condition()
        self.pages = {}
        self.events = {}
        self.ports = []

    def case(self, shape, *, overlay=None, transform=False, ambiguous_frames=False):
        token = uuid.uuid4().hex
        prefix = f"/case/{token}"
        primary = f"http://127.0.0.1:{self.ports[0]}"
        same_site = f"http://127.0.0.1:{self.ports[1]}"
        cross_site = f"http://localhost:{self.ports[0]}"
        cross_site_sibling = f"http://localhost:{self.ports[1]}"
        leaf_origin = {
            "same-origin": primary, "same-site-cross-origin": same_site,
            "cross-site-oopif": cross_site, "nested-mixed": cross_site,
            "nested-oopif-same-process": cross_site_sibling,
        }[shape]
        leaf_url = leaf_origin + prefix + "/leaf"
        frames = [{"css": "#outer-frame"}]
        documents = {"root", "leaf"}
        pages = {prefix + "/leaf": _document(token, "leaf", _LEAF_BODY)}
        if shape in {"nested-mixed", "nested-oopif-same-process"}:
            outer_origin = same_site if shape == "nested-mixed" else cross_site
            outer_url = outer_origin + prefix + "/outer"
            outer_body = _frame("inner-frame", leaf_url)
            if overlay == "middle":
                outer_body += '<div id="parent-overlay">Local parent overlay</div>'
            pages[prefix + "/outer"] = _document(token, "outer", outer_body)
            body = _frame("outer-frame", outer_url, height=320)
            frames.append({"css": "#inner-frame"})
            documents.add("outer")
            topology = [
                _ExpectedFrame("outer-frame", outer_url, shape == "nested-oopif-same-process"),
                _ExpectedFrame("inner-frame", leaf_url, shape == "nested-mixed"),
            ]
        else:
            body = _frame("outer-frame", leaf_url)
            topology = [_ExpectedFrame("outer-frame", leaf_url, shape == "cross-site-oopif")]
        if ambiguous_frames:
            assert shape == "cross-site-oopif"
            pages[prefix + "/other"] = _document(token, "other", _LEAF_BODY)
            body += _frame("other-frame", leaf_origin + prefix + "/other")
            frames = [{"css": ".branch"}]
            documents.add("other")
        if transform:
            body = '<section style="transform:scale(.9);transform-origin:0 0">' + body + '</section>'
        if overlay == "top":
            body += '<div id="parent-overlay">Local parent overlay</div>'
        # These decoys make a silent fallback to the top document observable.
        body += '<input class="query" id="root-query" value="untouched root">'
        body += '<button class="action" id="root-action" type="button">Run lookup</button>'
        pages[prefix + "/root"] = _document(token, "root", body)
        with self.condition:
            self.pages.update(pages)
            self.events[token] = []
        return _Case(self, token, primary + prefix + "/root", frames, documents,
                     topology)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.site.condition:
            content = self.server.site.pages.get(urlsplit(self.path).path)
        if content is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        # Keep different ports on one site eligible for the same renderer.
        # Site isolation still separates localhost from 127.0.0.1.
        self.send_header("Origin-Agent-Cluster", "?0")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Navigation can retire a fixture response during teardown.

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 32768:
                raise ValueError("invalid event body size")
            event = json.loads(self.rfile.read(size))
            token = event["token"]
            with self.server.site.condition:
                if urlsplit(self.path).path != f"/case/{token}/events":
                    raise ValueError("wrong event destination")
                self.server.site.events[token].append(event)
                self.server.site.condition.notify_all()
        except (KeyError, TypeError, ValueError):
            self.send_error(400)
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format, *_args):
        return


@pytest.fixture(scope="module")
def iframe_site():
    site = _Site()
    servers = []
    workers = []
    try:
        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            server.daemon_threads = True
            server.site = site
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            servers.append(server)
            workers.append(worker)
            site.ports.append(server.server_port)
        yield site
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive(), "local iframe HTTP worker leaked"


def _wait(sid, request, **condition):
    result = S.wait_for(session_id=sid, **condition)
    operation_id = result.get("operation_id")
    if operation_id:
        try:
            receipt = S.get_execute_js_result(operation_id, timeout=5)
            assert receipt.get("operation_id") == operation_id, receipt
            assert receipt.get("status") == "success", receipt
            assert receipt.get("reservation_held") is False, receipt
        except Exception as error:
            reason = f"iframe wait receipt did not settle safely: {error}"
            request.session.shouldstop = reason
            pytest.fail(reason)
    return result


def _load(case, sid):
    result = S.open_url(case.url, session_id=sid, timeout=25)
    assert result["status"] in {"ok", "redirected"}, result
    case.until(lambda events: case.ready_documents <= {
        event["document"] for event in events
        if event["kind"] == "ready" and event["ready_state"] == "complete"
    })


def _cdp(sid, method, *, target_id=None, **params):
    result = S.cdp_command(method, json.dumps(params), session_id=sid,
                           target_id=target_id, timeout=15)
    if target_id is None:
        assert result["status"] == "ok", result
    else:
        # The WebSocket envelope unwraps the extension's {ok, data} reply.
        # ext_cmd raises on failure and returns {data, client_id} on success.
        assert result["client_id"] == sid.rsplit(":", 1)[0], result
        assert result.get("reservation_held") is not True, result
    assert isinstance(result["data"], dict), result
    return result["data"]


def _target_document(sid, url, *, target_id=None):
    tree = _cdp(sid, "Page.getFrameTree", target_id=target_id)["frameTree"]
    assert tree["frame"]["url"] == url, tree
    if target_id is not None:
        assert tree["frame"]["id"] == target_id, tree
    document = _cdp(sid, "DOM.getDocument", target_id=target_id,
                    depth=-1, pierce=True)["root"]
    assert document["documentURL"] == url, document
    # One command in the target's default world proves only its root page.
    # Same-process child contexts are exercised by the high-level operations.
    observed = _cdp(sid, "Runtime.evaluate", target_id=target_id,
                    expression="({url: location.href, origin: location.origin})",
                    returnByValue=True)
    assert "exceptionDetails" not in observed, observed
    parsed = urlsplit(url)
    assert observed["result"]["value"] == {
        "url": url, "origin": f"{parsed.scheme}://{parsed.netloc}",
    }, observed
    return document


def _dom_nodes(node):
    # Search only this document. A child contentDocument is a separate scope.
    yield node
    for child in node.get("children", []):
        yield from _dom_nodes(child)


def _dom_attributes(node):
    attributes = node.get("attributes", [])
    return dict(zip(attributes[::2], attributes[1::2], strict=True))


def _assert_topology(case, sid):
    target_id = None
    document = _target_document(sid, case.url)
    for expected in case.topology:
        owners = [node for node in _dom_nodes(document)
                  if node["nodeName"] == "IFRAME"
                  and _dom_attributes(node).get("id") == expected.element_id]
        assert len(owners) == 1, f"expected one owned iframe element: {expected}"
        # Backend IDs survive a short CDP attachment; RemoteObjectIds do not.
        owner = _cdp(sid, "DOM.describeNode", target_id=target_id,
                     backendNodeId=owners[0]["backendNodeId"], depth=-1, pierce=True)["node"]
        assert owner["nodeName"] == "IFRAME", owner
        attributes = _dom_attributes(owner)
        assert attributes["id"] == expected.element_id and attributes["src"] == expected.url
        frame_id = owner["frameId"]
        assert frame_id, owner
        if expected.oopif:
            targets = S.debugger_targets(session_id=sid)
            assert targets["client_id"] == sid.rsplit(":", 1)[0], targets
            assert targets.get("reservation_held") is not True, targets
            assert isinstance(targets["data"], list), targets
            matching = [info for info in targets["data"] if info["id"] == frame_id]
            assert len(matching) == 1, f"expected one debugger target for DOM frame ID: {frame_id}"
            info = matching[0]
            # chrome.debugger.TargetInfo uses "other" for OOPIFs, unlike the
            # CDP TargetInfo schema. The exact DOM ID and target root below
            # prove identity; a URL match alone would not.
            assert info["type"] in {"iframe", "other"}, info
            # debugger_targets truncates URLs to 80 characters. These local
            # fixture URLs fit, so equality checks the complete expected URL.
            assert len(expected.url) <= 80, expected.url
            assert info["url"] == expected.url, info
            target_id = frame_id
            document = _target_document(sid, expected.url, target_id=target_id)
        else:
            assert "contentDocument" in owner, f"expected same-process child: {expected}"
            document = owner["contentDocument"]
        assert document["documentURL"] == expected.url, document


def _input_cursor(case):
    return max((event["sequence"] for event in case.events("leaf")), default=-1)


def _input_value(case, value, *, after=-1):
    case.until(lambda events: any(
        event["document"] == "leaf" and event["kind"] == "input"
        and event["id"] == "query" and event["query_value"] == value
        and event["sequence"] > after
        for event in events
    ))
    matching = [event for event in case.events("leaf", "input")
                if event["id"] == "query" and event["query_value"] == value
                and event["sequence"] > after]
    assert matching and all(event["trusted"] for event in matching), matching
    assert all(event["hidden_values"] == ["hidden seed", "inert seed"] for event in matching)


def _no_events(case, kinds=_INPUT_EVENTS):
    # Allow outstanding local fetches to arrive before declaring zero dispatch.
    deadline = time.monotonic() + 0.3
    while True:
        events = [event for event in case.events() if event["kind"] in kinds]
        assert events == [], events
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        with case.site.condition:
            case.site.condition.wait(min(0.05, remaining))


@pytest.mark.parametrize("shape", [
    "same-origin", "same-site-cross-origin", "cross-site-oopif", "nested-mixed",
    "nested-oopif-same-process",
])
def test_frame_locators_deliver_trusted_click_clear_and_enter_submit(
    scratch_session, iframe_site, request, shape,
):
    case = iframe_site.case(shape)
    _load(case, scratch_session)
    _assert_topology(case, scratch_session)
    textbox = case.locator(role="textbox", name="Search terms", exact=True)
    assert _wait(scratch_session, request, selector=textbox, timeout=10)["status"] == "success"

    clicked = S.page_click(selector=case.locator(css=".action"), session_id=scratch_session)
    assert clicked["status"] == "success", clicked
    case.until(lambda events: any(event["document"] == "leaf" and event["id"] == "action"
                                 and event["kind"] == "click" for event in events))
    clicks = [event for event in case.events("leaf", "click") if event["id"] == "action"]
    assert len(clicks) == 1 and clicks[0]["trusted"], clicks

    cursor = _input_cursor(case)
    typed = S.page_type("first search", selector=case.locator(css=".query"),
                        clear=True, session_id=scratch_session)
    assert typed["status"] == "success", typed
    _input_value(case, "first search", after=cursor)
    # An earlier clear can already have emitted an empty input event. Require a
    # new event from this call instead of accepting that old observation.
    cursor = _input_cursor(case)
    cleared = S.page_type("", selector=case.locator(css=".query"),
                          clear=True, session_id=scratch_session)
    assert cleared["status"] == "success", cleared
    _input_value(case, "", after=cursor)

    query = f"local search {shape}"
    cursor = _input_cursor(case)
    submitted = S.page_type(query, selector=textbox, clear=True, submit_key="Enter",
                            session_id=scratch_session)
    assert submitted["status"] == "success", submitted
    _input_value(case, query, after=cursor)
    case.until(lambda events: any(event["document"] == "leaf" and event["kind"] == "submit"
                                 for event in events))
    submits = case.events("leaf", "submit")
    assert len(submits) == 1 and submits[0]["trusted"], submits
    assert submits[0]["id"] == "search-form" and submits[0]["query_value"] == query
    enter = [event for event in case.events("leaf", "keypress") if event["key"] == "Enter"]
    assert len(enter) == 1 and enter[0]["trusted"] and enter[0]["id"] == "query", enter
    assert not [event for event in case.events("root") if event["kind"] in _INPUT_EVENTS]


@pytest.mark.parametrize(("shape", "overlay"), [
    ("cross-site-oopif", "top"), ("nested-mixed", "middle"),
])
def test_parent_overlay_refuses_frame_click_without_dispatch(
    scratch_session, iframe_site, request, shape, overlay,
):
    case = iframe_site.case(shape, overlay=overlay)
    _load(case, scratch_session)
    locator = case.locator(role="button", name="Run lookup", exact=True)
    assert _wait(scratch_session, request, selector=locator, timeout=10)["status"] == "success"
    result = S.page_click(selector=locator, session_id=scratch_session)
    assert result["status"] == "obscured" and result["attempts"] == 0, result
    _no_events(case)


def test_transformed_frame_queries_and_types_but_refuses_click(
    scratch_session, iframe_site, request,
):
    case = iframe_site.case("nested-mixed", transform=True)
    _load(case, scratch_session)
    field = case.locator(role="textbox", name="Search terms", exact=True)
    assert _wait(scratch_session, request, selector=field, timeout=10)["status"] == "success"
    typed = S.page_type("transformed search", selector=field, clear=True, session_id=scratch_session)
    assert typed["status"] == "success", typed
    _input_value(case, "transformed search")
    result = S.page_click(selector=case.locator(css=".action"), session_id=scratch_session)
    assert result["status"] == "unsupported_frame_transform" and result["attempts"] == 0, result
    _no_events(case, _MOUSE_EVENTS)


def test_ambiguous_frame_contents_are_not_gone_and_dispatch_nothing(
    scratch_session, iframe_site, request,
):
    case = iframe_site.case("cross-site-oopif")
    _load(case, scratch_session)
    locator = case.locator(css=".duplicate")
    waited = _wait(scratch_session, request, selector=locator, gone=True, timeout=10)
    assert waited["status"] == "timeout" and waited["locator_status"] == "ambiguous", waited
    clicked = S.page_click(selector=locator, session_id=scratch_session)
    assert clicked["status"] == "ambiguous" and clicked["attempts"] == 0, clicked
    typed = S.page_type("must not appear", selector=case.locator(css=".duplicate-query"),
                        clear=True, session_id=scratch_session)
    assert typed["status"] == "ambiguous", typed
    _no_events(case)


def test_ambiguous_frame_path_is_not_gone_and_dispatches_nothing(
    scratch_session, iframe_site, request,
):
    case = iframe_site.case("cross-site-oopif", ambiguous_frames=True)
    _load(case, scratch_session)
    locator = case.locator(css=".action")
    waited = _wait(scratch_session, request, selector=locator, gone=True, timeout=10)
    assert waited["status"] == "timeout" and waited["locator_status"] == "ambiguous", waited
    assert waited["stage"] == "frame", waited
    clicked = S.page_click(selector=locator, session_id=scratch_session)
    assert clicked["status"] == "ambiguous" and clicked["attempts"] == 0, clicked
    assert clicked["stage"] == "frame", clicked
    typed = S.page_type("must not appear", selector=case.locator(css=".query"),
                        clear=True, session_id=scratch_session)
    assert typed["status"] == "ambiguous", typed
    _no_events(case)


def test_wait_for_gone_observes_removed_content_and_removed_frame(
    scratch_session, iframe_site, request,
):
    case = iframe_site.case("cross-site-oopif")
    _load(case, scratch_session)
    locator = case.locator(css="#transient-result")
    assert _wait(scratch_session, request, selector=locator, timeout=10)["status"] == "success"
    clicked = S.page_click(selector=case.locator(css="#remove-result"), session_id=scratch_session)
    assert clicked["status"] == "success", clicked
    assert _wait(scratch_session, request, selector=locator, gone=True, timeout=10)["status"] == "success"
    removed = S.execute_js("document.querySelector('#outer-frame').remove(); return true;",
                           session_id=scratch_session, no_monitor=True, timeout=10)
    assert removed["js_return"] is True, removed
    gone = _wait(scratch_session, request, selector=case.locator(css=".query"), gone=True, timeout=10)
    assert gone["status"] == "success", gone
    missing = S.page_click(selector=case.locator(css=".action"), session_id=scratch_session)
    assert missing["status"] == "not_found" and missing["attempts"] == 0, missing


def test_missing_element_and_frame_locators_never_fall_back_to_root(
    scratch_session, iframe_site, request,
):
    case = iframe_site.case("cross-site-oopif")
    _load(case, scratch_session)
    locators = [case.locator(css="#absent-control"),
                {"frame": [{"css": "#absent-frame"}], "css": ".action"}]
    for locator in locators:
        # This verifies locator scope, not a two-second browser latency target.
        waited = _wait(scratch_session, request, selector=locator, timeout=10)
        assert waited["status"] == "timeout" and waited["locator_status"] == "not_found", waited
        assert _wait(scratch_session, request, selector=locator, gone=True, timeout=10)["status"] == "success"
        clicked = S.page_click(selector=locator, session_id=scratch_session)
        assert clicked["status"] == "not_found" and clicked["attempts"] == 0, clicked
        typed = S.page_type("must not appear", selector=locator, clear=True, session_id=scratch_session)
        assert typed["status"] == "not_found", typed
    _no_events(case)
