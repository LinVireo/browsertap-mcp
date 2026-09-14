"""A local SPA checkout with cross-origin hosted fields, using the real bridge."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from browsertap_mcp import server as S

pytestmark = pytest.mark.live

FIELDS = {
    "number": ("Card number", "cc-number", "4242424242424242"),
    "expiry": ("Expiration date", "cc-exp", "1230"),
    "security": ("CVV/CVC", "cc-csc", "123"),
}


class _CheckoutHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        port = self.server.server_port
        parent_origin = f"http://127.0.0.1:{port}"
        field_origin = f"http://localhost:{port}"
        request = urlsplit(self.path)
        if request.path == "/field":
            kind = parse_qs(request.query)["kind"][0]
            label, autocomplete, _value = FIELDS[kind]
            body = f"""<!doctype html><title>Hosted test field</title>
<label for="field">{label}</label><input id="field" type="tel"
 aria-label="{label}" autocomplete="{autocomplete}">
<script>
const field = document.querySelector('input');
field.addEventListener('input', event => parent.postMessage({{
  kind: {json.dumps(kind)}, value: field.value, trusted: event.isTrusted
}}, {json.dumps(parent_origin)}));
</script>"""
        elif request.path == "/":
            frames = "".join(
                f'<iframe id="{kind}" title="{label}" src="{field_origin}/field?kind={kind}"></iframe>'
                for kind, (label, _autocomplete, _value) in FIELDS.items()
            )
            body = """<!doctype html><title>BTAP synthetic checkout</title>
<main id="app">Loading!</main>
<script>
window.fields = {};
window.addEventListener('message', event => {
  if (event.origin !== FIELD_ORIGIN) return;
  const frame = document.getElementById(event.data.kind);
  if (!frame || event.source !== frame.contentWindow) return;
  fields[event.data.kind] = event.data;
  document.querySelector('#submit').disabled = Object.keys(fields).length !== 3;
});
setTimeout(() => {
  document.querySelector('#app').innerHTML = FORM;
  document.querySelector('#checkout').addEventListener('submit', async event => {
    event.preventDefault();
    const response = await fetch('/submit', {method:'POST', body:JSON.stringify({
      fields, name: document.querySelector('#holder').value
    })});
    document.querySelector('#result').textContent = response.ok ? 'Simulation complete' : 'Simulation failed';
  });
}, 100);
</script>"""
            form = (
                '<h1>Synthetic checkout</h1><form id="checkout">'
                '<label for="holder">Cardholder</label><input id="holder" autocomplete="cc-name">'
                + frames
                + '<button id="submit" disabled>Submit payment</button></form>'
                '<section hidden><button>Submit payment</button></section><p id="result"></p>'
            )
            body = body.replace("FIELD_ORIGIN", json.dumps(field_origin)).replace("FORM", json.dumps(form))
        else:
            self.send_error(404)
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if self.path != "/submit":
            self.send_error(404)
            return
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        valid = data.get("name") == "BTAP TEST"
        for kind, (_label, _autocomplete, value) in FIELDS.items():
            field = data.get("fields", {}).get(kind, {})
            valid = valid and field.get("value") == value and field.get("trusted") is True
        with self.server.counter_lock:
            self.server.submissions += 1
            self.server.valid_submission = bool(valid)
        self.send_response(200 if valid else 400)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextmanager
def _checkout_fixture():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CheckoutHandler)
    server.submissions = 0
    server.valid_submission = False
    server.counter_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _frame_command(sid, target, method, params):
    response = S.cdp_command(
        method, params_json=json.dumps(params), target_id=target, session_id=sid,
    )
    assert "error" not in response, response
    return response.get("data", response)


def _frame_evaluate(sid, target, expression):
    response = _frame_command(sid, target, "Runtime.evaluate", {
        "expression": expression, "returnByValue": True,
    })
    assert "exceptionDetails" not in response, response
    return response["result"].get("value")


def test_spa_checkout_cross_origin_fields_and_one_submission(scratch_session, record_property):
    sid = scratch_session
    with _checkout_fixture() as fixture:
        origin = f"http://127.0.0.1:{fixture.server_port}"
        field_origin = f"http://localhost:{fixture.server_port}"
        S.open_url(origin, session_id=sid)
        button = {"role": "button", "name": "Submit payment", "exact": True}
        assert S.wait_for(selector=button, session_id=sid)["status"] == "success"
        scan = S.scan_page(session_id=sid, text_only=True)
        assert scan["content_ready"] is True
        typed = S.page_type("BTAP TEST", selector={"label": "Cardholder"}, session_id=sid)
        assert typed["status"] == "success", typed
        for kind, (label, autocomplete, value) in FIELDS.items():
            # Bind the target to an element in this exact parent document, never
            # to a URL, title, or guessed iframe ordering from getTargets().
            batch = {"cmd": "batch", "commands": [
                {"cmd": "cdp", "method": "Runtime.evaluate", "params": {
                    "expression": f"document.querySelector('iframe#{kind}')",
                    "objectGroup": "btap-checkout-test",
                }},
                {"cmd": "cdp", "method": "DOM.describeNode", "params": {
                    "objectId": "$0.result.objectId", "depth": 0,
                }},
                {"cmd": "cdp", "method": "Runtime.releaseObjectGroup", "params": {
                    "objectGroup": "btap-checkout-test",
                }},
            ]}
            described = S.cdp_batch(json.dumps(batch), session_id=sid)["data"]
            frame_id = described[1]["node"]["frameId"]
            # Wait for the hosted document rather than assuming parent ready
            # means every iframe has completed its own navigation.
            assert S.wait_for(
                js=f"document.querySelector('iframe#{kind}').contentDocument === null",
                session_id=sid,
            )["status"] == "success"
            focused = _frame_evaluate(sid, frame_id, f"""(() => {{
              if (location.origin !== {json.dumps(field_origin)}) return false;
              const fields = document.querySelectorAll('input[autocomplete="{autocomplete}"]');
              if (fields.length !== 1 || fields[0].disabled || fields[0].readOnly) return false;
              fields[0].focus();
              return document.activeElement === fields[0];
            }})()""")
            assert focused is True, label
            _frame_command(sid, frame_id, "Input.insertText", {"text": value})
            matched = _frame_evaluate(sid, frame_id, f"""(() => {{
              const field = document.querySelector('input[autocomplete="{autocomplete}"]');
              return !!field && field.value === {json.dumps(value)};
            }})()""")
            assert matched is True, label
        assert S.wait_for(
            js="Object.keys(window.fields).length === 3 && Object.values(window.fields).every(f => f.trusted)",
            session_id=sid,
        )["status"] == "success"
        clicked = S.page_click(selector=button, session_id=sid)
        assert clicked["status"] == "success", clicked
        assert S.wait_for(text="Simulation complete", session_id=sid)["status"] == "success"
        with fixture.counter_lock:
            assert fixture.submissions == 1
            assert fixture.valid_submission is True
        record_property("cross_origin_fields", len(FIELDS))
        record_property("trusted_input_events", True)
        record_property("submission_count", fixture.submissions)
        record_property("real_payment", False)
