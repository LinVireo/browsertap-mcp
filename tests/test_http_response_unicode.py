"""Browser strings must survive Bottle's real UTF-8 response encoding."""
from __future__ import annotations

import copy
import json
import queue
from unittest.mock import Mock

import pytest

from browsertap_mcp import browser_bridge as B
from tests.test_browser_bridge_coverage import driver_stub, wsgi_post

TEXT = '中文\U0001f600 high:\ud800 low:\udfff literal:\\ud800 "\n\t\x00'
VALUE = {"key-\ud800": TEXT, "nested": [TEXT, {"low-\udfff": [None, True, False, 7, 2.5]}]}
ROUTES = (
    ("get_all_sessions", "get_all_sessions", {}),
    ("diagnose", "diagnose", {}),
    ("find_session", "find_session", {"url_pattern": TEXT}),
    ("resolve_session", "_resolve_local_session_target", {"sessionId": "browser:7"}),
    ("ext_cmd", "ext_cmd", {"clientId": "browser", "payload": {"cmd": "bookmarks", "method": "tree"}}),
    ("execute_js", "execute_js", {"sessionId": "browser:7", "code": TEXT}),
    ("get_execute_js_result", "get_execute_js_result", {"operationId": "operation"}),
)


@pytest.fixture(params=[False, True], ids=["propagate", "catchall"])
def http_app(monkeypatch, request):
    import wsgiref.simple_server

    driver = driver_stub()
    starts = []

    def forbidden(*args, **kwargs):
        pytest.fail("this WSGI test must not start a listener or touch a token file")

    class DormantThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            starts.append(True)

    token_stub = Mock(return_value="")
    monkeypatch.setattr(B, "bridge_token", token_stub)
    monkeypatch.setattr(B, "bridge_token_path", forbidden)
    monkeypatch.setattr(B, "_persist_token", forbidden)
    monkeypatch.setattr(B.threading, "Thread", DormantThread)
    monkeypatch.setattr(B.socket, "socket", forbidden)
    monkeypatch.setattr(B.socket, "create_connection", forbidden)
    monkeypatch.setattr(wsgiref.simple_server, "make_server", forbidden)
    monkeypatch.delenv("BROWSERTAP_WS_ALLOWED_ORIGINS", raising=False)
    driver.start_http_server()
    driver.app.catchall = request.param
    yield driver
    token_stub.assert_called_once_with()
    assert starts == [True]
    assert driver.http_server is None


def _json_response(app, payload):
    response = wsgi_post(app, "/link", payload)
    assert response["status"] == 200
    # Keep the existing HTTP contract, including Bottle's default media type.
    assert response["headers"]["Content-Type"] == "text/html; charset=UTF-8"
    assert not any(name.lower().startswith("access-control-") for name in response["headers"])
    encoded = response["body"].encode("utf-8", errors="strict")
    assert len(encoded) == int(response["headers"]["Content-Length"])
    assert encoded.isascii()
    assert response["unread_body_bytes"] == 0
    return json.loads(encoded)


@pytest.mark.parametrize("command,method,arguments", ROUTES, ids=[route[0] for route in ROUTES])
@pytest.mark.parametrize("is_error", [False, True])
def test_dynamic_link_results_preserve_unicode_keys_values_and_flags(
    http_app, command, method, arguments, is_error,
):
    value = {
        "data": copy.deepcopy(VALUE), "isError": is_error,
        "retry_safe": False, "reservation_held": True,
        "operation_id": "operation-\udfff", "requester_id": "owner-\ud800",
    }
    before = copy.deepcopy(value)
    callback = Mock(return_value=value)
    setattr(http_app, method, callback)
    http_app.select_client_id = Mock(return_value="browser")
    decoded = _json_response(http_app.app, {"cmd": command, **arguments})
    assert decoded == {"r": before}
    assert value == before
    callback.assert_called_once()
    if command == "find_session":
        callback.assert_called_once_with(TEXT)
    elif command == "execute_js":
        assert callback.call_args.args == (TEXT,)
        assert callback.call_args.kwargs["session_id"] == "browser:7"
        assert callback.call_args.kwargs["allow_failover"] is False


def test_client_names_and_identifiers_survive_the_http_response(http_app):
    http_app.ext_clients = {"client-\udfff": {"browser": TEXT, "private": "not a public field"}}
    assert _json_response(http_app.app, {"cmd": "get_clients"}) == {
        "r": [{"client_id": "client-\udfff", "browser": TEXT}],
    }


@pytest.mark.parametrize("command,method,arguments", ROUTES, ids=[route[0] for route in ROUTES])
def test_link_errors_preserve_unicode_diagnostics_and_nonretryable_outcomes(
    http_app, command, method, arguments,
):
    diagnostics = copy.deepcopy(VALUE)
    error = B.BridgeNoResponseError(
        TEXT, error_code="synthetic_unknown_outcome", delivery_state="delivered_no_result",
        retry_safe=False, operation_id="operation-\ud800", reservation_held=True,
        poll_with="get_execute_js_result", diagnostics=diagnostics,
    )
    callback = Mock(side_effect=error)
    setattr(http_app, method, callback)
    http_app.select_client_id = Mock(return_value="browser")
    expected_message = f"{command} failed: {TEXT}" if command in {
        "get_all_sessions", "diagnose", "find_session", "resolve_session",
    } else TEXT
    assert _json_response(http_app.app, {"cmd": command, **arguments}) == {"r": {
        "error": expected_message, "error_code": "synthetic_unknown_outcome",
        "diagnostics": diagnostics, "delivery_state": "delivered_no_result",
        "retry_safe": False, "operation_id": "operation-\ud800", "reservation_held": True,
        "poll_with": "get_execute_js_result",
    }}
    callback.assert_called_once()
    assert diagnostics == VALUE


@pytest.mark.parametrize("payload,code", [
    ([TEXT], "invalid_request"),
    ({"cmd": "ext_cmd", "payload": {"cmd": ""}}, "invalid_payload"),
    ({"cmd": "未知-\ud800"}, "unknown_command"),
])
def test_control_error_responses_keep_the_existing_shape(http_app, payload, code):
    http_app.ext_cmd = Mock(side_effect=AssertionError("invalid input cannot dispatch"))
    decoded = _json_response(http_app.app, payload)
    assert set(decoded) == {"r"}
    assert decoded["r"]["error_code"] == code
    assert isinstance(decoded["r"]["error"], str)
    http_app.ext_cmd.assert_not_called()


def test_missing_resolution_target_stays_null_without_dispatch(http_app):
    http_app._resolve_local_session_target = Mock(side_effect=AssertionError("no target"))
    assert _json_response(http_app.app, {"cmd": "resolve_session"}) == {"r": None}
    http_app._resolve_local_session_target.assert_not_called()


def test_longpoll_preserves_preencoded_json_and_records_one_delivery_ack(http_app):
    operation_id = "queued-operation"
    payload = {"id": operation_id, "code": TEXT, "data": VALUE}
    message = json.dumps(payload)
    messages = queue.Queue()
    messages.put(message)
    session = B.Session("http:7", {"url": "https://example.test/", "type": "http"}, messages)
    http_app.sessions[session.id] = session
    operations = http_app._operation_state()
    operations.reserve(
        operation_id, [session.id], "owner", reply_transport="http", reply_owner=session.id,
    )
    response = wsgi_post(http_app.app, "/api/longpoll", {"sessionId": session.id, "title": TEXT})
    assert response["status"] == 200
    assert response["body"] == message
    assert json.loads(response["body"]) == payload
    assert response["unread_body_bytes"] == 0
    assert messages.empty()
    assert session.info["title"] == TEXT
    assert operations.read(operation_id, "owner")["acknowledged"] is True
    assert list(http_app.acks) == [operation_id]


def test_retained_unicode_result_is_readable_only_by_its_owner_without_replay(http_app):
    operation_id, owner = "retained-operation", "synthetic-owner"
    operations = http_app._operation_state()
    operations.reserve(
        operation_id, ["http:7"], owner, reply_transport="http", reply_owner="http:7",
    )
    reply = {"type": "result", "id": operation_id, "sessionId": "http:7", "result": VALUE}
    wrong = wsgi_post(http_app.app, "/api/result", {**reply, "sessionId": "http:8"})
    assert wrong["status"] == 200 and wrong["body"] == "ok"
    assert operations.read(operation_id, owner)["reservation_held"] is True
    accepted = wsgi_post(http_app.app, "/api/result", reply)
    assert accepted["status"] == 200 and accepted["body"] == "ok"
    assert accepted["unread_body_bytes"] == 0
    http_app.execute_js = Mock(side_effect=AssertionError("result reads cannot dispatch"))
    http_app.ext_cmd = Mock(side_effect=AssertionError("result reads cannot dispatch"))
    payload = {"cmd": "get_execute_js_result", "operationId": operation_id, "requesterId": owner}
    first = _json_response(http_app.app, payload)["r"]
    assert first["status"] == "success" and first["data"] == VALUE
    assert first["reservation_held"] is False
    denied = _json_response(http_app.app, {**payload, "requesterId": "other"})["r"]
    assert denied["error_code"] == "operation_owner_mismatch"
    assert _json_response(http_app.app, payload)["r"] == first
    http_app.execute_js.assert_not_called()
    http_app.ext_cmd.assert_not_called()


@pytest.mark.parametrize("path", ["/link", "/api/longpoll", "/api/result"])
@pytest.mark.parametrize("refusal", ["token", "origin"])
def test_rejected_unicode_requests_keep_status_headers_and_body_drain(http_app, path, refusal):
    http_app.link_token = "synthetic-token"
    http_app.execute_js = Mock(side_effect=AssertionError("refused request cannot dispatch"))
    headers = {"Authorization": "Bearer synthetic-token"} if refusal == "origin" else {}
    response = wsgi_post(
        http_app.app, path, {"cmd": "execute_js", "code": TEXT, "padding": "x" * (2 * 1024 * 1024)},
        headers=headers, origin="https://untrusted.example" if refusal == "origin" else None,
    )
    assert response["status"] == (403 if refusal == "origin" else 401)
    assert response["body"] == (
        "forbidden origin" if refusal == "origin" else "unauthorized: missing or bad bridge token"
    )
    assert response["unread_body_bytes"] == 0
    assert not any(name.lower().startswith("access-control-") for name in response["headers"])
    assert http_app.sessions == {}
    assert http_app.results == {}
    http_app.execute_js.assert_not_called()
