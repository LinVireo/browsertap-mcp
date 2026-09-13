"""Native HTTP transport exercises the real bridge router and owner checks."""
from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from browsertap_mcp import browser_bridge as B
from browsertap_mcp import native_bridge as N
from browsertap_mcp.extension_origin import default_extension_origin
from tests.test_browser_bridge_coverage import (
    DormantThread,
    driver_stub,
    ext_ready,
    ws_handler_for,
    ws_peer,
    wsgi_post,
)

TOKEN = "synthetic-native-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def native_app(monkeypatch):
    driver = driver_stub()
    with monkeypatch.context() as setup:
        setup.setattr(B, "bridge_token", lambda: TOKEN)
        setup.setattr(B.threading, "Thread", DormantThread)
        driver.start_http_server()
    driver._activity_condition = threading.Condition()
    driver._activity_serial = 0
    try:
        yield driver
    finally:
        driver.stop_http_server()


def call(driver, route, payload):
    return wsgi_post(driver.app, f"/api/native/{route}", payload, headers=AUTH)


def open_connection(driver, *, client_id=None):
    reply = call(driver, "open", {})
    assert reply["status"] == 200
    identity = json.loads(reply["body"])["connectionId"]
    if client_id:
        assert send(driver, identity, ext_ready(client_id))["status"] == 200
    return identity


def send(driver, identity, message):
    return call(driver, "message", {"connectionId": identity, "message": message})


def raw_request(driver, path, raw=b"", *, method="POST", content_type="application/json",
                origin=None, token=TOKEN, content_length=None, transfer_encoding=None):
    stream, errors, result = io.BytesIO(raw), io.StringIO(), {}
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "SERVER_NAME": "127.0.0.1", "SERVER_PORT": "80", "SERVER_PROTOCOL": "HTTP/1.1",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(raw)) if content_length is None else content_length,
        "wsgi.version": (1, 0), "wsgi.url_scheme": "http", "wsgi.input": stream,
        "wsgi.errors": errors, "wsgi.multithread": True,
        "wsgi.multiprocess": False, "wsgi.run_once": False,
    }
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if transfer_encoding is not None:
        environ["HTTP_TRANSFER_ENCODING"] = transfer_encoding

    def start_response(status, headers, exc_info=None):
        result.update(status=int(status.split()[0]), headers=dict(headers))

    chunks = driver.app(environ, start_response)
    try:
        result["body"] = b"".join(chunks).decode("utf-8")
    finally:
        if hasattr(chunks, "close"):
            chunks.close()
    result.update(unread=len(raw) - stream.tell(), errors=errors.getvalue())
    return result


@pytest.mark.parametrize("value, expected", [(None, "native"), (" native ", "native"),
                                             ("WEBSOCKET", "websocket")])
def test_transport_validation(monkeypatch, value, expected):
    monkeypatch.delenv(N.TRANSPORT_ENV, raising=False)
    assert N.extension_transport(value) == expected
    monkeypatch.setenv(N.TRANSPORT_ENV, "websocket")
    assert N.extension_transport() == "websocket"


@pytest.mark.parametrize("value", ["", "auto", "ws", "false", 1, [], {}])
def test_invalid_transport_is_explicit(value):
    with pytest.raises(ValueError, match="BROWSERTAP_TRANSPORT must be native or websocket"):
        N.extension_transport(value)


def test_invalid_transport_precedes_network_and_token_effects(monkeypatch):
    monkeypatch.setenv(N.TRANSPORT_ENV, "invalid")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid transport reached a startup effect")

    monkeypatch.setattr(B, "tcp_port_open", forbidden)
    monkeypatch.setattr(B, "bridge_token", forbidden)
    with pytest.raises(ValueError, match=N.TRANSPORT_ENV):
        B.BrowserBridge()
    with pytest.raises(ValueError, match=N.TRANSPORT_ENV):
        driver_stub().start_http_server()


@pytest.mark.parametrize("origin", [None, "trusted"])
def test_configuration_is_the_only_token_free_route_and_has_no_secrets(native_app, origin):
    if origin == "trusted":
        origin = default_extension_origin()
    response = raw_request(native_app, "/api/extension/config", method="GET", origin=origin, token=None)
    assert response["status"] == 200
    assert json.loads(response["body"]) == {
        "transport": N.extension_transport(), "nativeHost": "io.browsertap.host", "protocol": 1,
    }
    assert response["headers"]["Cache-Control"] == "no-store"
    assert TOKEN not in response["body"]
    denied = raw_request(native_app, "/link", b'{"cmd":"get_clients"}', token=None)
    assert denied["status"] == 401


def test_configuration_publishes_websocket_override(monkeypatch):
    monkeypatch.setenv(N.TRANSPORT_ENV, "websocket")
    driver = driver_stub()
    monkeypatch.setattr(B, "bridge_token", lambda: TOKEN)
    monkeypatch.setattr(B.threading, "Thread", DormantThread)
    driver.start_http_server()
    try:
        response = raw_request(driver, "/api/extension/config", method="GET", token=None)
        assert json.loads(response["body"])["transport"] == "websocket"
    finally:
        driver.stop_http_server()


@pytest.mark.parametrize("path", ["/api/extension/config", "/api/native/open"])
@pytest.mark.parametrize("origin", ["https://example.test", "chrome-extension://untrusted",
                                    "chrome-extension://untrusted/", "null"])
def test_exact_origin_guard_applies_to_native_routes(native_app, path, origin):
    response = raw_request(native_app, path, b"{}", method="GET" if path.endswith("config") else "POST",
                           origin=origin)
    assert response["status"] == 403
    assert response["body"] == "forbidden origin"
    assert response["unread"] == 0
    assert not any(key.lower().startswith("access-control-") for key in response["headers"])
    assert native_app._native_connections._connections == {}


@pytest.mark.parametrize("route", ["open", "message", "poll", "close"])
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_native_authentication_drains_large_rejected_bodies(native_app, route, token):
    response = raw_request(native_app, f"/api/native/{route}", b"x" * 200_000, token=token)
    assert response["status"] == 401
    assert response["body"] == "unauthorized: missing or bad bridge token"
    assert response["unread"] == 0
    assert native_app._native_connections._connections == {}


@pytest.mark.parametrize("route", ["open", "message", "poll", "close"])
@pytest.mark.parametrize("token", [None, TOKEN])
def test_native_requires_authentication_even_when_legacy_token_is_disabled(monkeypatch, route, token):
    driver = driver_stub()
    with monkeypatch.context() as setup:
        setup.setattr(B, "bridge_token", lambda: "")
        setup.setattr(B.threading, "Thread", DormantThread)
        driver.start_http_server()
    try:
        for raw in (b"{}", b"x" * 200_000):
            response = raw_request(driver, f"/api/native/{route}", raw, token=token)
            assert response["status"] == 503
            assert json.loads(response["body"])["error_code"] == "native_auth_required"
            assert response["unread"] == 0
            assert driver._native_connections._connections == {}
        config = raw_request(driver, "/api/extension/config", method="GET", token=None)
        assert config["status"] == 200
        legacy = raw_request(driver, "/link", b'{"cmd":"get_clients"}', token=None)
        assert legacy["status"] == 200
    finally:
        driver.stop_http_server()


@pytest.mark.parametrize("raw, content_type", [
    (b"{", "application/json"), (b"[]", "application/json"), (b"null", "application/json"),
    (b"{}", "text/plain"), (b'{"bad":NaN}', "application/json"), (b"\xff", "application/json"),
])
def test_invalid_native_json_has_a_drained_payload_free_error(native_app, raw, content_type):
    response = raw_request(native_app, "/api/native/open", raw, content_type=content_type)
    assert response["status"] == 400
    assert json.loads(response["body"])["error_code"] == "native_invalid_request"
    assert response["unread"] == 0
    assert response["errors"] == ""
    assert native_app._native_connections._connections == {}


@pytest.mark.parametrize("length", ["invalid", "-1"])
def test_invalid_content_length_is_a_request_error(native_app, length):
    response = raw_request(native_app, "/api/native/open", content_length=length)
    assert response["status"] == 400
    assert json.loads(response["body"])["error_code"] == "native_invalid_request"


def test_chunked_body_cannot_bypass_the_native_size_limit(native_app, monkeypatch):
    monkeypatch.setattr(N, "NATIVE_ENVELOPE_BYTES", 16)
    payload = b"{" + b"x" * 1000
    chunked = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
    response = raw_request(native_app, "/api/native/open", chunked,
                           content_length="1", transfer_encoding="chunked")
    assert response["status"] == 413
    assert json.loads(response["body"])["error_code"] == "native_message_too_large"
    assert native_app._native_connections._connections == {}


@pytest.mark.parametrize("route, payload", [
    ("open", {"connectionId": "caller-chosen"}), ("poll", {}), ("close", {"connectionId": []}),
    ("poll", {"connectionId": "guess"}), ("message", {"connectionId": "a" * 43}),
    ("message", {"connectionId": "a" * 43, "message": {}, "extra": True}),
])
def test_invalid_envelopes_cannot_select_or_create_connections(native_app, route, payload):
    response = call(native_app, route, payload)
    assert response["status"] == 400
    assert json.loads(response["body"])["error_code"] == "native_invalid_request"
    assert native_app._native_connections._connections == {}


@pytest.mark.parametrize("message", [None, [], "x", {"type": "unknown"},
                                     {"type": "ext_ready", "clientId": []},
                                     {"type": "tabs_update", "tabs": [{"id": True}]}])
def test_invalid_frames_do_not_publish_browser_state(native_app, message):
    identity = open_connection(native_app)
    response = send(native_app, identity, message)
    assert response["status"] == 400
    assert json.loads(response["body"])["error_code"] == "native_invalid_message"
    assert native_app.ext_clients == {}
    assert native_app.sessions == {}
    assert native_app.last_ext_seen is None


def test_native_ext_cmd_round_trip_uses_shared_link_router_and_exact_reply_owner(native_app, monkeypatch):
    identity = open_connection(native_app, client_id="native-owner")
    foreign = open_connection(native_app, client_id="native-other")
    session = native_app.sessions["native-owner:7"]
    assert session.info["generation"] == "g1"
    assert session.ws_client is native_app.ext_clients["native-owner"]["ws"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(wsgi_post, native_app.app, "/link", {
            "cmd": "ext_cmd", "clientId": "native-owner", "timeout": 3,
            "payload": {"cmd": "tabs"}, "requesterId": "synthetic-requester",
        }, headers=AUTH)
        response = call(native_app, "poll", {"connectionId": identity})
        assert response["status"] == 200
        command = json.loads(response["body"])["message"]
        assert command["cmd"] == {"cmd": "tabs"}
        operation_id = command["id"]
        # Reading a Native queue is not an ACK. Neither an unrelated host nor a
        # page-result route may acknowledge or finish this operation.
        assert operation_id not in native_app.acks
        for kind in ("ack", "result"):
            forged = send(native_app, foreign, {"type": kind, "id": operation_id, "result": "forged"})
            # HTTP acknowledges the frame, while the common router rejects its
            # operation ownership. A late reply must not force transport fallback.
            assert forged["status"] == 200
        assert native_app.rejected_operation_replies == 2
        assert operation_id not in native_app.acks
        assert operation_id not in native_app.results
        assert not pending.done()
        assert send(native_app, identity, {"type": "ack", "id": operation_id})["status"] == 200
        assert operation_id in native_app.acks
        result = {"tabs": [7], "unicode": "中文😀\ud800", "\udfff": "literal\\u1234"}
        assert send(native_app, identity, {"type": "result", "id": operation_id, "result": result})["status"] == 200
        completed = pending.result(timeout=3)
    assert completed["status"] == 200
    assert json.loads(completed["body"])["r"] == {"data": result, "client_id": "native-owner"}
    assert send(native_app, identity, {"type": "result", "id": operation_id, "result": "duplicate"})["status"] == 200
    assert native_app.sessions["native-owner:7"].is_active()
    monkeypatch.setattr(N, "NATIVE_POLL_SECONDS", 0)
    assert json.loads(call(native_app, "poll", {"connectionId": identity})["body"]) == {"message": None}


def test_native_ping_has_no_tab_liveness_side_effect_or_command_ack(native_app):
    identity = open_connection(native_app, client_id="native-owner")
    seen = native_app.last_ext_seen
    assert send(native_app, identity, {"type": "ping"})["status"] == 200
    reply = call(native_app, "poll", {"connectionId": identity})
    assert json.loads(reply["body"]) == {"message": {"type": "pong"}}
    assert native_app.last_ext_seen == seen
    assert native_app.acks == {}


def test_native_result_error_uses_existing_pending_error_semantics(native_app):
    identity = open_connection(native_app, client_id="native-owner")
    owner = native_app.ext_clients["native-owner"]["ws"]
    native_app._operation_state().reserve(
        "synthetic-operation", ["native-owner:7"], "requester", reply_transport="ws", reply_owner=owner,
    )
    response = send(native_app, identity, {
        "type": "error", "id": "synthetic-operation", "error": "synthetic browser failure",
    })
    assert response["status"] == 200
    assert native_app.results["synthetic-operation"]["success"] is False
    assert native_app.results["synthetic-operation"]["data"] == "synthetic browser failure"


def test_native_result_can_exceed_bottles_default_json_memory_limit(native_app):
    identity = open_connection(native_app, client_id="native-owner")
    owner = native_app.ext_clients["native-owner"]["ws"]
    native_app._operation_state().reserve(
        "large-result", ["native-owner:7"], "requester", reply_transport="ws", reply_owner=owner,
    )
    value = "synthetic-large-result" * 20_000
    response = send(native_app, identity, {"type": "result", "id": "large-result", "result": value})
    assert response["status"] == 200
    assert native_app.results["large-result"]["data"] == value


def test_native_tabs_update_obeys_generation_and_tab_removal(native_app):
    identity = open_connection(native_app, client_id="native-owner")
    original = native_app.sessions["native-owner:7"]
    native_app._operation_state().reserve("pending", ["native-owner:7"], "requester")
    update = ext_ready("native-owner")
    update["type"] = "tabs_update"
    update["tabs"][0]["generation"] = "g2"
    assert send(native_app, identity, update)["status"] == 200
    assert not original.is_active()
    assert native_app.sessions["native-owner:7"] is not original
    receipt = native_app._operation_state().read("pending", "requester")
    assert receipt["lifecycle_reason"] == "generation_changed"
    assert send(native_app, identity, {**update, "tabs": []})["status"] == 200
    assert not native_app.sessions["native-owner:7"].is_active()


@pytest.mark.parametrize("incumbent_native", [True, False])
def test_native_and_websocket_cannot_take_each_others_live_identity(native_app, monkeypatch, incumbent_native):
    with monkeypatch.context() as setup:
        handler = ws_handler_for(native_app, setup)
    identity = open_connection(native_app)
    peer = ws_peer(handler, ext_ready("shared-profile"))
    if incumbent_native:
        assert send(native_app, identity, ext_ready("shared-profile"))["status"] == 200
        original = native_app.ext_clients["shared-profile"]["ws"]
        seen = native_app.last_ext_seen
        peer.handle()
        assert peer.closed
    else:
        peer.handle()
        original = peer
        seen = native_app.last_ext_seen
        response = send(native_app, identity, ext_ready("shared-profile"))
        assert response["status"] == 410
    assert native_app.ext_clients["shared-profile"]["ws"] is original
    assert native_app.last_ext_seen == seen
    assert native_app.sessions["shared-profile:7"].ws_client is original
    assert native_app.rejected_client_takeovers == 1
    assert identity not in repr(native_app.last_rejected_takeover)


def test_late_native_close_preserves_a_websocket_successor(native_app, monkeypatch):
    identity = open_connection(native_app, client_id="shared-profile")
    native_app.ext_clients["shared-profile"]["ts"] = 0
    with monkeypatch.context() as setup:
        handler = ws_handler_for(native_app, setup)
    peer = ws_peer(handler, ext_ready("shared-profile"))
    peer.handle()
    assert call(native_app, "close", {"connectionId": identity})["status"] == 200
    assert native_app.ext_clients["shared-profile"]["ws"] is peer
    assert native_app.sessions["shared-profile:7"].is_active()


def test_close_is_idempotent_and_only_retires_its_own_registrations(native_app):
    identity = open_connection(native_app, client_id="native-owner")
    other = open_connection(native_app, client_id="native-other")
    peer = native_app.ext_clients["native-owner"]["ws"]
    native_app._operation_state().reserve("pending", ["native-owner:7"], "requester")
    for _ in range(2):
        assert json.loads(call(native_app, "close", {"connectionId": identity})["body"]) == {"ok": True}
    peer.close()
    assert "native-owner" not in native_app.ext_clients
    assert not native_app.sessions["native-owner:7"].is_active()
    assert native_app.sessions["native-other:7"].is_active()
    assert other in native_app._native_connections._connections
    assert native_app._operation_state().read("pending", "requester")["reservation_held"]
    assert send(native_app, identity, ext_ready("native-owner"))["status"] == 410
    assert call(native_app, "poll", {"connectionId": identity})["status"] == 410


@pytest.mark.parametrize("route", ["message", "poll", "close"])
def test_unknown_opaque_id_cannot_touch_any_connection(native_app, route):
    identity = open_connection(native_app, client_id="native-owner")
    payload = {"connectionId": "a" * 43}
    if route == "message":
        payload["message"] = {"type": "ping"}
    response = call(native_app, route, payload)
    assert response["status"] == (200 if route == "close" else 410)
    assert identity in native_app._native_connections._connections
    assert native_app.sessions["native-owner:7"].is_active()


def test_expiry_bounds_abandoned_hosts_and_discards_their_unsent_queue(native_app):
    now = [10.0]
    registry = native_app._native_connections
    registry._clock = lambda: now[0]
    identity = open_connection(native_app, client_id="native-owner")
    peer = native_app.ext_clients["native-owner"]["ws"]
    peer.send_message('{"id":"never-replay","cmd":{"cmd":"tabs"}}')
    now[0] += N.NATIVE_IDLE_SECONDS
    registry.expire()
    assert registry._connections == {}
    assert registry._queued_bytes == 0
    assert peer.closed
    assert not peer._queue
    assert native_app.ext_clients == {}
    assert not native_app.sessions["native-owner:7"].is_active()
    assert call(native_app, "poll", {"connectionId": identity})["status"] == 410


def test_http_maintenance_reaps_native_hosts_without_any_followup_request(link_bridge_auth):
    registry = link_bridge_auth.driver._native_connections
    now = [0.0]
    registry._clock = lambda: now[0]
    identity = registry.open()
    registry.receive(identity, ext_ready("native-owner"))
    retired = threading.Event()
    original_close = registry._on_close

    def closed(peer):
        original_close(peer)
        retired.set()

    registry._on_close = closed
    now[0] = N.NATIVE_IDLE_SECONDS
    assert retired.wait(3)
    assert identity not in registry._connections
    assert not link_bridge_auth.driver.sessions["native-owner:7"].is_active()


def test_connection_id_collision_never_overwrites_an_existing_owner(native_app, monkeypatch):
    values = iter(["a" * 43, "a" * 43, "b" * 43])
    monkeypatch.setattr(N.secrets, "token_urlsafe", lambda size: next(values))
    first = open_connection(native_app, client_id="native-owner")
    owner = native_app.ext_clients["native-owner"]["ws"]
    second = open_connection(native_app, client_id="native-other")
    assert first != second
    assert native_app._native_connections._connections[first] is owner
    assert native_app.sessions["native-owner:7"].ws_client is owner


def test_open_reaps_stale_connections_before_enforcing_the_count(native_app, monkeypatch):
    now = [10.0]
    registry = native_app._native_connections
    registry._clock = lambda: now[0]
    monkeypatch.setattr(N, "NATIVE_MAX_CONNECTIONS", 1)
    original = open_connection(native_app)
    refused = call(native_app, "open", {})
    assert refused["status"] == 503
    assert json.loads(refused["body"])["error_code"] == "native_connection_limit"
    now[0] += N.NATIVE_IDLE_SECONDS
    replacement = open_connection(native_app)
    assert original != replacement
    assert set(registry._connections) == {replacement}


@pytest.mark.parametrize("limit", ["NATIVE_MAX_QUEUE_MESSAGES", "NATIVE_MAX_QUEUE_BYTES",
                                   "NATIVE_MAX_TOTAL_QUEUE_BYTES"])
def test_queue_limits_close_and_clear_without_replaying(native_app, monkeypatch, limit):
    identity = open_connection(native_app, client_id="native-owner")
    peer = native_app.ext_clients["native-owner"]["ws"]
    payload = json.dumps({"cmd": {"cmd": "tabs"}})
    monkeypatch.setattr(N, limit, 1 if limit.endswith("MESSAGES") else len(payload))
    peer.send_message(payload)
    with pytest.raises(N.NativeBridgeError) as error:
        peer.send_message(payload)
    assert error.value.error_code == "native_queue_full"
    assert error.value.status == 503
    assert peer.closed and not peer._queue
    assert native_app._native_connections._queued_bytes == 0
    assert call(native_app, "poll", {"connectionId": identity})["status"] == 410


def test_queue_full_during_ext_cmd_retains_uncertainty_and_never_retries(native_app, monkeypatch):
    identity = open_connection(native_app, client_id="native-owner")
    monkeypatch.setattr(N, "NATIVE_MAX_QUEUE_MESSAGES", 0)
    with pytest.raises(B.BridgeNoResponseError) as error:
        native_app.ext_cmd({"cmd": "tabs", "method": "create"}, client_id="native-owner", requester_id="requester")
    assert error.value.delivery_state == "sent_unconfirmed"
    assert error.value.retry_safe is False
    receipt = native_app._operation_state().read(error.value.operation_id, "requester")
    assert receipt["reservation_held"]
    assert call(native_app, "poll", {"connectionId": identity})["status"] == 410


def test_global_queue_budget_preserves_other_hosts_messages(native_app, monkeypatch):
    first = open_connection(native_app, client_id="first")
    open_connection(native_app, client_id="second")
    payload = json.dumps({"id": "retained", "cmd": {"cmd": "tabs"}})
    monkeypatch.setattr(N, "NATIVE_MAX_TOTAL_QUEUE_BYTES", len(payload))
    native_app.ext_clients["first"]["ws"].send_message(payload)
    with pytest.raises(N.NativeBridgeError, match="queue is full"):
        native_app.ext_clients["second"]["ws"].send_message(payload)
    assert native_app.sessions["first:7"].is_active()
    assert not native_app.sessions["second:7"].is_active()
    assert native_app._native_connections.poll(first) == json.loads(payload)
    assert native_app._native_connections._queued_bytes == 0


def test_native_http_and_logical_message_size_limits(native_app, monkeypatch):
    identity = open_connection(native_app)
    monkeypatch.setattr(N, "NATIVE_MAX_MESSAGE_BYTES", 128)
    payload = {"type": "ping", "value": "x" * 200}
    response = send(native_app, identity, payload)
    assert response["status"] == 413
    assert json.loads(response["body"])["error_code"] == "native_message_too_large"
    oversized = raw_request(native_app, "/api/native/message", b"x" * 5000)
    assert oversized["status"] == 413 and oversized["unread"] == 0
    peer = native_app._native_connections._connections[identity]
    with pytest.raises(N.NativeBridgeError, match="size limit"):
        peer.send_message(json.dumps(payload))
    assert peer.closed


@pytest.mark.parametrize("payload", ["{", "[]", '{"x":NaN}', None])
def test_invalid_outbound_frames_fail_closed(native_app, payload):
    identity = open_connection(native_app)
    peer = native_app._native_connections._connections[identity]
    with pytest.raises(N.NativeBridgeError, match="Invalid Native Messaging frame"):
        peer.send_message(payload)
    assert peer.closed


@pytest.mark.parametrize("value", [float("nan"), object()])
def test_non_json_values_are_rejected_before_frame_dispatch(native_app, value):
    identity = open_connection(native_app)
    with pytest.raises(N.NativeBridgeError, match="Invalid Native Messaging frame"):
        native_app._native_connections.receive(identity, {"type": "ping", "value": value})
    assert not native_app._native_connections._connections[identity]._queue


def test_outbound_byte_limit_counts_unicode_escapes_and_closes_on_overflow(native_app, monkeypatch):
    identity = open_connection(native_app)
    peer = native_app._native_connections._connections[identity]
    monkeypatch.setattr(N, "NATIVE_MAX_MESSAGE_BYTES", 128)
    with pytest.raises(N.NativeBridgeError, match="size limit"):
        peer.send_message(json.dumps({"value": "中" * 30}, ensure_ascii=False))
    assert peer.closed


def test_poll_is_bounded_and_refreshes_activity_without_resetting_session_age(native_app, monkeypatch):
    now = [100.0]
    registry = native_app._native_connections
    registry._clock = lambda: now[0]
    identity = open_connection(native_app, client_id="native-owner")
    peer = native_app.ext_clients["native-owner"]["ws"]
    connected_at = native_app.sessions["native-owner:7"].connect_at
    now[0] += 30
    monkeypatch.setattr(N, "NATIVE_POLL_SECONDS", 0)
    assert registry.poll(identity) is None
    assert peer._last_seen == now[0]
    assert native_app.sessions["native-owner:7"].connect_at == connected_at
    now[0] += N.NATIVE_IDLE_SECONDS
    with pytest.raises(N.NativeBridgeError) as error:
        registry.poll(identity)
    assert error.value.status == 410


def test_only_one_poll_per_connection_and_close_wakes_the_waiter(native_app, monkeypatch):
    registry = native_app._native_connections
    identity = open_connection(native_app)
    waiting = threading.Event()
    original_wait = registry._condition.wait

    def wait(timeout):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(registry._condition, "wait", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call, native_app, "poll", {"connectionId": identity})
        assert waiting.wait(2)
        refused = call(native_app, "poll", {"connectionId": identity})
        assert refused["status"] == 409
        assert json.loads(refused["body"])["error_code"] == "native_poll_in_progress"
        call(native_app, "close", {"connectionId": identity})
        assert pending.result(timeout=2)["status"] == 410


def test_shutdown_closes_queues_and_refuses_new_hosts(native_app):
    identity = open_connection(native_app, client_id="native-owner")
    native_app.stop_http_server()
    assert call(native_app, "poll", {"connectionId": identity})["status"] == 410
    response = call(native_app, "open", {})
    assert response["status"] == 503
    assert json.loads(response["body"])["error_code"] == "native_bridge_stopped"


def test_native_dispatch_exceptions_never_reach_wsgi_or_logs_as_payload(native_app, monkeypatch, caplog):
    secret = "SYNTHETIC_NATIVE_SECRET_938272"
    identity = open_connection(native_app)

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(native_app, "_apply_extension_tabs", fail)
    response = raw_request(native_app, "/api/native/message", json.dumps({
        "connectionId": identity, "message": ext_ready(secret),
    }).encode())
    assert response["status"] == 500
    assert response["errors"] == ""
    assert secret not in response["body"] and secret not in caplog.text
    assert "operation=native_message" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
