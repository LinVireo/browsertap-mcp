"""Failed extension sends retire their socket without retiring its replacement."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Thread

import pytest

from browsertap_mcp import browser_bridge as B
from browsertap_mcp import pending_operations as P
from browsertap_mcp.capture_ownership import CaptureBusyError
from browsertap_mcp.command_scope import TargetBusyError
from tests.test_browser_bridge_coverage import ext_ready, ws_handler_for, ws_peer, wsgi_post
from tests.test_browser_bridge_coverage import http_app as http_app


def deliver(peer, payload):
    peer.data = json.dumps(payload)
    peer.handle()


def reply_on_send(peer):
    def send(message):
        payload = json.loads(message)
        peer.sent.append(payload)
        deliver(peer, {"type": "result", "id": payload["id"], "result": "live"})

    peer.send_message = send


def fail_on_send(peer, before_failure=lambda: None):
    def send(message):
        peer.sent.append(json.loads(message))
        before_failure()
        raise OSError("synthetic closed socket")

    peer.send_message = send


@pytest.mark.parametrize("explicit_target", [False, True])
def test_failed_extension_send_retires_all_its_tabs_before_the_next_call(
    http_app, monkeypatch, explicit_target,
):
    handler = ws_handler_for(http_app, monkeypatch)
    snapshot = ext_ready()
    snapshot["tabs"].append({"id": 6, "url": "https://sibling.test", "generation": "g1"})
    broken = ws_peer(handler, snapshot)
    broken.handle()
    other = ws_peer(handler, ext_ready("edge", tab_id=9))
    other.handle()
    fallback = ws_peer(handler, {"type": "ready", "sessionId": "chrome:8", "url": "https://live.test"})
    fallback.handle()
    reply_on_send(fallback)
    monkeypatch.setattr(B, "HTTP_POLL_SECONDS", 0.001)
    wsgi_post(http_app.app, "/api/longpoll", {"sessionId": "http:1", "url": "https://http.test"})
    fail_on_send(broken)

    with pytest.raises(B.BridgeNoResponseError) as failed:
        http_app.ext_cmd({"cmd": "extensions", "method": "list"}, client_id="chrome")

    assert failed.value.delivery_state == "sent_unconfirmed"
    assert failed.value.retry_safe is False
    assert {s["id"] for s in http_app.get_all_sessions()} == {"chrome:8", "edge:9", "http:1"}
    assert "chrome" not in http_app.ext_clients
    assert http_app.ext_clients["edge"]["ws"] is other
    disconnected_at = http_app.sessions["chrome:7"].disconnect_at
    broken.handle_close()
    assert http_app.sessions["chrome:7"].disconnect_at == disconnected_at

    if explicit_target:
        with pytest.raises(B.SessionNotConnectedError):
            http_app.execute_js("sideEffect()", session_id="chrome:7", timeout=0.001)
        assert fallback.sent == []
    else:
        assert http_app.execute_js("sideEffect()", timeout=0.1)["data"] == "live"
        assert len(fallback.sent) == 1
    assert len(broken.sent) == 1
    assert other.sent == []
    assert http_app.sessions["http:1"].http_queue.empty()


@pytest.mark.parametrize("tab_id", [7, 8])
def test_old_send_failure_and_close_cannot_remove_an_already_connected_replacement(
    http_app, monkeypatch, tab_id,
):
    handler = ws_handler_for(http_app, monkeypatch)
    broken = ws_peer(handler, ext_ready())
    broken.handle()
    fresh = ws_peer(handler, ext_ready(tab_id=tab_id))
    reply_on_send(fresh)

    def replace_socket():
        http_app.ext_clients["chrome"]["ts"] = time.time() - B.CLIENT_TAKEOVER_GRACE_SECONDS - 1
        fresh.handle()

    fail_on_send(broken, replace_socket)
    with pytest.raises(B.BridgeNoResponseError):
        http_app.ext_cmd({"cmd": "extensions", "method": "list"}, client_id="chrome")
    broken.handle_close()

    assert http_app.ext_clients["chrome"]["ws"] is fresh
    assert http_app.sessions[f"chrome:{tab_id}"].is_active()
    with pytest.raises(TargetBusyError):
        http_app.ext_cmd({"cmd": "extensions", "method": "list"}, client_id="chrome")
    assert http_app.ext_cmd({"cmd": "tabs"}, client_id="chrome")["data"] == "live"
    assert http_app.execute_js("read()", session_id=f"chrome:{tab_id}")["data"] == "live"
    assert len(broken.sent) == 1 and len(fresh.sent) == 2


@pytest.mark.parametrize("expired", [False, True])
def test_failed_send_keeps_receipt_reservation_capture_owner_and_original_reply_owner(
    http_app, monkeypatch, expired,
):
    now = [1000.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: now[0])
    handler = ws_handler_for(http_app, monkeypatch)
    broken = ws_peer(handler, ext_ready())
    broken.handle()
    fail_on_send(broken)
    command = {"cmd": "console", "method": "start", "tabId": 7}
    with pytest.raises(B.BridgeNoResponseError) as failed:
        http_app.ext_cmd(command, client_id="chrome", requester_id="owner", operation_id="failed-start", timeout=0.1)
    assert failed.value.diagnostics["operation_id"] == "failed-start"
    assert failed.value.diagnostics["reservation_held"] is True
    assert failed.value.delivery_state == "sent_unconfirmed" and failed.value.retry_safe is False

    fresh = ws_peer(handler, ext_ready())
    fresh.handle()
    for requester in ("owner", "other"):
        with pytest.raises(TargetBusyError):
            http_app.execute_js("sideEffect()", session_id="chrome:7", requester_id=requester)
    if expired:
        now[0] += P.SILENT_GRACE_SECONDS + 1
    receipt = http_app.get_execute_js_result("failed-start", requester_id="owner")
    assert receipt["reservation_held"] is (not expired)
    assert receipt["retry_safe"] is False
    deliver(fresh, {"type": "result", "id": "failed-start", "result": "wrong socket"})
    assert http_app.get_execute_js_result("failed-start", requester_id="owner") == receipt
    deliver(broken, {"type": "result", "id": "failed-start", "result": {"ok": True}})
    result = http_app.get_execute_js_result("failed-start", requester_id="owner")
    if expired:
        assert {key: result[key] for key in receipt} == receipt
        assert result["late_result"]["data"] == {"ok": True}
    else:
        assert result["status"] == "success" and result["data"] == {"ok": True}
    with pytest.raises(CaptureBusyError):
        http_app.ext_cmd(command, client_id="chrome", requester_id="other")
    assert http_app.sessions["chrome:7"].ws_client is fresh
    assert http_app.sessions["chrome:7"].is_active()
    assert len(broken.sent) == 1 and fresh.sent == []


def test_http_can_reconnect_after_failed_extension_send_and_survive_delayed_close(http_app, monkeypatch):
    handler = ws_handler_for(http_app, monkeypatch)
    broken = ws_peer(handler, ext_ready())
    broken.handle()
    fail_on_send(broken)
    with pytest.raises(B.BridgeNoResponseError):
        http_app.ext_cmd({"cmd": "extensions", "method": "list"}, client_id="chrome")
    monkeypatch.setattr(B, "HTTP_POLL_SECONDS", 0.001)
    response = wsgi_post(http_app.app, "/api/longpoll", {"sessionId": "chrome:7", "url": "https://http.test"})
    assert json.loads(response["body"])["ret"] == "next long-poll"
    broken.handle_close()
    assert http_app.sessions["chrome:7"].type == "http"
    assert http_app.sessions["chrome:7"].is_active()


class ObservedLock:
    """Use the real lock, reporting contention so the race needs no timing sleep."""

    def __init__(self, lock, contended):
        self.lock = lock
        self.contended = contended

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.contended.set()
            self.lock.acquire()
        return self

    def __exit__(self, *_):
        self.lock.release()


@pytest.mark.parametrize("transport", ["extension", "ws", "http"])
def test_reconnect_during_disconnect_cleanup_keeps_the_new_channel_online(http_app, monkeypatch, transport):
    handler = ws_handler_for(http_app, monkeypatch)
    broken = ws_peer(handler, ext_ready())
    broken.handle()
    http_app.ext_clients["chrome"]["ts"] = time.time() - B.CLIENT_TAKEOVER_GRACE_SECONDS - 1
    if transport == "http":
        # The long-poll fallback can claim only a socket already known to be gone.
        http_app.sessions["chrome:7"].mark_disconnected()
    fresh = ws_peer(handler, ext_ready() if transport == "extension" else {
        "type": "ready", "sessionId": "chrome:7", "url": "https://new.test",
    })
    paused = threading.Event()
    release = threading.Event()
    new_blocked_or_done = threading.Event()
    monkeypatch.setattr(B, "_DRIVER_STATE_LOCK", ObservedLock(B._DRIVER_STATE_LOCK, new_blocked_or_done))
    monkeypatch.setattr(B.threading, "Thread", Thread)
    monkeypatch.setattr(B, "HTTP_POLL_SECONDS", 0.001)
    session = http_app.sessions["chrome:7"]
    is_active = session.is_active
    pause_once = True

    def pause_after_reading_old_channel():
        nonlocal pause_once
        active = is_active()
        if pause_once:
            pause_once = False
            paused.set()
            assert release.wait(5), "disconnect race was not released"
        return active

    monkeypatch.setattr(session, "is_active", pause_after_reading_old_channel)

    def reconnect():
        try:
            if transport == "http":
                response = wsgi_post(http_app.app, "/api/longpoll", {
                    "sessionId": "chrome:7", "url": "https://new.test",
                })
                assert json.loads(response["body"])["ret"] == "next long-poll"
            else:
                fresh.handle()
        finally:
            new_blocked_or_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        closing = pool.submit(broken.handle_close)
        try:
            assert paused.wait(5), "old channel was not inspected"
            connecting = pool.submit(reconnect)
            assert new_blocked_or_done.wait(5), "replacement neither connected nor waited for cleanup"
        finally:
            release.set()
        closing.result(timeout=5)
        connecting.result(timeout=5)

    assert session.is_active()
    if transport == "http":
        assert session.type == "http" and session.http_queue is not None
    else:
        assert session.ws_client is fresh
        reply_on_send(fresh)
        assert http_app.execute_js("read()", session_id="chrome:7")["data"] == "live"
    if transport == "extension":
        assert http_app.ext_clients["chrome"]["ws"] is fresh
    broken.handle_close()
    assert session.is_active()
