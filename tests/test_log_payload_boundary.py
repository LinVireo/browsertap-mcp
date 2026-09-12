"""Synthetic JS, exception and protocol payloads must not reach log records."""
from __future__ import annotations

import json
import logging
import queue

import pytest

from browsertap_mcp import browser_bridge as B
from tests.test_browser_bridge_coverage import (
    DormantThread,
    driver_stub,
    ext_ready,
    ws_handler_for,
    ws_peer,
    wsgi_post,
)
from tests.test_browser_bridge_coverage import http_app as http_app

SHORT_SECRET = "p4"
LONG_SECRET = "SYNTHETIC_FABLE_CREDENTIAL_0123456789"
SCRIPT = f"const password = '{SHORT_SECRET}'; throw Error('{LONG_SECRET}');"


def _error(kind):
    if kind == "SyntaxError":
        return SyntaxError("synthetic evaluation failed", ("execute_js", 1, 1, SCRIPT))
    return RuntimeError(SCRIPT)


def _assert_clean(caplog, operation, error_type=None):
    records = [record for record in caplog.records if record.name == B.__name__]
    assert records
    assert operation in caplog.text
    if error_type:
        assert error_type in caplog.text
    for record in records:
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
        for secret in (SHORT_SECRET, LONG_SECRET, SCRIPT):
            assert secret not in record.getMessage()
            assert secret not in repr(record.args)
    assert SHORT_SECRET not in caplog.text
    assert LONG_SECRET not in caplog.text


@pytest.mark.parametrize("kind", ["RuntimeError", "SyntaxError"])
def test_execute_js_reply_exception_is_safe_when_propagated(monkeypatch, caplog, kind):
    caplog.set_level(logging.DEBUG, logger=B.__name__)
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    peer = ws_peer(handler, {"type": "result", "id": "operation", "result": SCRIPT})

    def fail(*args, **kwargs):
        raise _error(kind)

    monkeypatch.setattr(driver, "_record_operation_reply", fail)
    peer.handle()
    _assert_clean(caplog, "Error handling WebSocket message", kind)
    assert "operation=result" in caplog.text
    assert not peer.closed


@pytest.mark.parametrize("kind", ["RuntimeError", "SyntaxError"])
def test_unparseable_execute_js_poll_keeps_delivery_but_not_exception_payload(
    http_app, monkeypatch, caplog, kind,
):
    caplog.set_level(logging.DEBUG, logger=B.__name__)
    pending = queue.Queue()
    payload = json.dumps({"cmd": "execute_js", "code": SCRIPT, "id": "operation"})
    pending.put(payload)
    http_app.sessions["http:1"] = B.Session("http:1", {"type": "http"}, pending)
    original_loads = json.loads

    def parse(text, *args, **kwargs):
        if text == payload:
            raise _error(kind)
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", parse)
    response = wsgi_post(http_app.app, "/api/longpoll", {"sessionId": "http:1"})
    assert response["body"] == payload
    assert response["status"] == 200
    assert http_app.acks == {}
    _assert_clean(caplog, "unparseable long-poll payload", kind)


def test_ws_loop_and_rebuild_exceptions_do_not_log_payloads(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=B.__name__)
    threads, attempts, closes = [], [], []

    class StopLoop(BaseException):
        pass

    class Server:
        def __init__(self, *args):
            attempts.append(True)
            if len(attempts) == 2:
                raise _error("SyntaxError")

        def serve_forever(self):
            if len(attempts) == 3:
                raise StopLoop()
            raise _error("RuntimeError")

        def close(self):
            closes.append(True)

    class Thread(DormantThread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    monkeypatch.setattr(B, "WebSocketServer", Server)
    monkeypatch.setattr(B.threading, "Thread", Thread)
    monkeypatch.setattr(B.time, "sleep", lambda duration: None)
    driver = driver_stub()
    driver.start_ws_server()
    with pytest.raises(StopLoop):
        threads[0].target()
    assert len(attempts) == 3
    assert len(closes) == 2
    _assert_clean(caplog, "WS server loop crashed", "RuntimeError")
    _assert_clean(caplog, "WS server rebuild failed", "SyntaxError")


def test_arbitrary_protocol_identifiers_are_not_raw_log_context(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=B.__name__)
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    payload = ext_ready(f"profile-{SHORT_SECRET}-{LONG_SECRET}")
    payload["browser"] = SCRIPT
    payload["tabs"][0]["generation"] = SCRIPT
    peer = ws_peer(handler, payload)
    peer.handle()
    driver.execute_js = lambda *args, **kwargs: {"data": 7}
    driver.sessions[next(iter(driver.sessions))].mark_disconnected()
    _assert_clean(caplog, "Tab disconnected")


def test_rejected_origin_is_not_logged_as_payload(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=B.__name__)
    monkeypatch.delenv("BROWSERTAP_WS_ALLOWED_ORIGINS", raising=False)
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    peer = ws_peer(handler, {}, origin=f"https://untrusted.test/{SHORT_SECRET}?key={LONG_SECRET}")
    peer.connected()
    assert peer.closed
    _assert_clean(caplog, "Rejected WS connection")


@pytest.mark.parametrize("catchall", [False, True])
def test_unexpected_result_error_does_not_escape_into_wsgi_traceback(http_app, monkeypatch, caplog, catchall):
    caplog.set_level(logging.DEBUG, logger=B.__name__)
    http_app.app.catchall = catchall

    def fail(*args, **kwargs):
        raise _error("SyntaxError")

    monkeypatch.setattr(http_app, "_record_operation_reply", fail)
    errors = []

    def app(environ, start_response):
        errors.append(environ["wsgi.errors"])
        return http_app.app(environ, start_response)

    response = wsgi_post(app, "/api/result", {"type": "result", "id": "operation", "result": SCRIPT})
    assert response["status"] == 500
    assert response["unread_body_bytes"] == 0
    assert SHORT_SECRET not in response["body"]
    assert LONG_SECRET not in response["body"]
    assert all(stream.getvalue() == "" for stream in errors)
    _assert_clean(caplog, "HTTP request failed", "SyntaxError")
