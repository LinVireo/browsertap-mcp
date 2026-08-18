"""The /link HTTP port is a command channel; who may speak to it.

The WS port keeps web pages out with an Origin prefix check, which works there
because a page cannot forge chrome-extension://. /link has no such handle: any
local process can POST an execute_js and run arbitrary JS in the user's
logged-in tabs. Every ABM process therefore reads one persistent per-user token;
editors do not supply or rotate it.

Every test here binds its OWN ports (see conftest's link_bridge fixture) and
never touches the bridge daemon the user has running.
"""
from __future__ import annotations

import pytest
import requests

from agent_browser_mcp import browser_bridge as T

TOKEN = "s3cret-token"


# --- the pure header parser ------------------------------------------------

@pytest.mark.parametrize("headers,expected", [
    ({"Authorization": f"Bearer {TOKEN}"}, TOKEN),
    ({"Authorization": f"bearer {TOKEN}"}, TOKEN),      # scheme is case-insensitive
    ({"Authorization": f"BEARER {TOKEN}"}, TOKEN),
    ({"X-Bridge-Token": TOKEN}, TOKEN),
    ({"Authorization": f"Bearer  {TOKEN} "}, TOKEN),    # padding is stripped
    ({}, ""),
    ({"Authorization": TOKEN}, ""),                     # no scheme is not a token
    ({"Authorization": "Basic abc"}, ""),
    ({"Authorization": "Bearer"}, ""),
    ({"Authorization": "Bearer   "}, ""),
])
def test_header_token(headers, expected):
    assert T.header_token(headers) == expected


def test_authorization_wins_over_x_bridge_token():
    got = T.header_token({"Authorization": f"Bearer {TOKEN}", "X-Bridge-Token": "other"})
    assert got == TOKEN


def test_explicitly_disabled_auth_lets_everything_through(monkeypatch):
    monkeypatch.setenv(T.TOKEN_AUTH_ENV, "off")
    T.require_link_token({})
    T.require_link_token({"Authorization": "Bearer nonsense"})


def test_configured_token_rejects_a_missing_header(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    with pytest.raises(Exception) as exc:
        T.require_link_token({})
    assert getattr(exc.value, "status_code", None) == 401


def test_configured_token_rejects_a_wrong_header(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    with pytest.raises(Exception) as exc:
        T.require_link_token({"Authorization": "Bearer wrong"})
    assert getattr(exc.value, "status_code", None) == 401


def test_configured_token_accepts_either_header(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    T.require_link_token({"Authorization": f"Bearer {TOKEN}"})
    T.require_link_token({"X-Bridge-Token": TOKEN})


def test_a_prefix_of_the_token_is_not_enough(monkeypatch):
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    for bad in (TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        with pytest.raises(Exception):
            T.require_link_token({"X-Bridge-Token": bad})


def test_whitespace_only_env_counts_as_unset(monkeypatch):
    """A blank legacy env value creates a persistent token instead of disabling auth."""
    monkeypatch.setenv(T.TOKEN_ENV, "   ")
    token = T.bridge_token()
    assert token
    assert T.bridge_token_path().read_text(encoding="utf-8").strip() == token


# --- end to end over real HTTP, on a throwaway port ------------------------

def _post(port, body, headers=None, timeout=5):
    return requests.post(f"http://127.0.0.1:{port}/link", json=body,
                         headers=headers or {}, timeout=timeout)


class TestUnauthenticatedBridge:
    """Explicit auth=off keeps the historical unauthenticated behaviour."""

    def test_bare_post_is_served(self, link_bridge_open):
        r = _post(link_bridge_open.port, {"cmd": "get_all_sessions"})
        assert r.status_code == 200
        assert r.json()["r"] == []


@pytest.fixture(autouse=True)
def isolated_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv(T.TOKEN_FILE_ENV, str(tmp_path / "bridge-token"))
    monkeypatch.delenv(T.TOKEN_AUTH_ENV, raising=False)


def test_legacy_env_token_is_migrated_once_and_file_wins(monkeypatch, tmp_path):
    path = tmp_path / "bridge-token"
    monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
    assert T.bridge_token() == TOKEN
    assert path.read_text(encoding="utf-8").strip() == TOKEN
    monkeypatch.setenv(T.TOKEN_ENV, "rotated-in-editor")
    assert T.bridge_token() == TOKEN


def test_fresh_install_generates_one_stable_token(monkeypatch):
    monkeypatch.delenv(T.TOKEN_ENV, raising=False)
    first = T.bridge_token()
    second = T.bridge_token()
    assert len(first) >= 32
    assert second == first


def test_reinstall_reuses_leftover_token_despite_stale_editor_env(monkeypatch):
    """Uninstalling packages/extensions leaves user data; that is reusable state."""
    path = T.bridge_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("token-from-previous-install\n", encoding="utf-8")
    monkeypatch.setenv(T.TOKEN_ENV, "stale-token-from-an-editor")

    assert T.bridge_token() == "token-from-previous-install"
    assert path.read_text(encoding="utf-8") == "token-from-previous-install\n"


class TestUnknownCommandIsAnError:
    """A /link cmd that this bridge does not know must answer with a
    structured error, never the bare string "ok" that looks like success."""

    def test_unknown_cmd_reports_error_not_ok(self, link_bridge_open):
        r = _post(link_bridge_open.port, {"cmd": "open_new_tab", "url": "https://x"})
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict) and "r" in body
        assert "error" in body["r"]
        assert "unknown cmd" in body["r"]["error"]

    def test_ext_cmd_is_not_confused_with_top_level_payload(self, link_bridge_open):
        """/link expects {"cmd":"ext_cmd","payload":{...}}. A caller that puts the
        extension payload at the top level ({"cmd":"tabs",...}) is a protocol
        mistake and must be told, not silently acknowledged."""
        r = _post(link_bridge_open.port, {"cmd": "tabs", "method": "create", "url": "https://x"})
        assert r.status_code == 200
        body = r.json()
        assert "unknown cmd" in body["r"]["error"]

    def test_non_object_body_is_an_error(self, link_bridge_open):
        r = requests.post(f"http://127.0.0.1:{link_bridge_open.port}/link",
                          data="not json", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict) and "error" in body.get("r", {})


class TestAuthenticatedBridge:
    def test_missing_token_is_401(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"})
        assert r.status_code == 401
        assert "token" in r.text.lower()

    def test_wrong_token_is_401(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"},
                  {"Authorization": "Bearer nope"})
        assert r.status_code == 401

    def test_bearer_token_is_200(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"},
                  {"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        assert r.json()["r"] == []

    def test_x_bridge_token_is_200(self, link_bridge_auth):
        r = _post(link_bridge_auth.port, {"cmd": "get_all_sessions"},
                  {"X-Bridge-Token": TOKEN})
        assert r.status_code == 200

    def test_execute_js_is_refused_before_it_reaches_a_tab(self, link_bridge_auth):
        """The actual hole: an unauthenticated execute_js used to run arbitrary
        JS in the user's logged-in Chrome. It must be rejected at the door, not
        answered with a bridge-level error that proves it got parsed."""
        r = _post(link_bridge_auth.port,
                  {"cmd": "execute_js", "code": "document.cookie", "sessionId": None})
        assert r.status_code == 401

    def test_diagnose_is_guarded_too(self, link_bridge_auth):
        """Diagnostics leak the tab inventory, so they are not a free route."""
        r = _post(link_bridge_auth.port, {"cmd": "diagnose"})
        assert r.status_code == 401

    def test_result_channel_is_guarded_too(self, link_bridge_auth):
        """/api/result can inject fake execution results into the daemon. A
        token-configured bridge must demand the token there as well, or a local
        process could forge results without ever touching /link."""
        r2 = requests.post(
            f"http://127.0.0.1:{link_bridge_auth.port}/api/result",
            json={"type": "result", "id": "forged", "result": "x"},
            timeout=5)
        assert r2.status_code == 401
        r3 = requests.post(
            f"http://127.0.0.1:{link_bridge_auth.port}/api/longpoll",
            json={"sessionId": "attacker:1"},
            timeout=5)
        assert r3.status_code == 401

    def test_result_channel_still_open_without_token(self, link_bridge_open):
        """Backwards compatibility: with no token configured the result
        channel behaves exactly as before."""
        r = requests.post(
            f"http://127.0.0.1:{link_bridge_open.port}/api/result",
            json={"type": "result", "id": "x", "result": "y"},
            timeout=5)
        assert r.status_code == 200

    def test_result_channel_accepts_token(self, link_bridge_auth):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        r = requests.post(
            f"http://127.0.0.1:{link_bridge_auth.port}/api/result",
            json={"type": "result", "id": "ok-id", "result": "y"},
            headers=headers, timeout=5)
        assert r.status_code == 200

    @pytest.mark.parametrize("path", ["/link", "/api/result", "/api/longpoll"])
    def test_a_rejected_request_answers_401_instead_of_resetting(
            self, link_bridge_auth, path):
        """The 401 has to survive the body it just refused to read.

        wsgiref on Windows resets the connection when a rejected request leaves
        unread bytes in its socket, and the caller then cannot tell "your token
        is wrong" from "the bridge died". A small rejected body aborts a few
        percent of the time — flaky enough to have been dismissed as noise — so
        send one larger than the socket buffer, where the undrained version
        aborts on every attempt.
        """
        payload = {"type": "result", "id": "forged", "result": "x" * (2 * 1024 * 1024)}
        r = requests.post(
            f"http://127.0.0.1:{link_bridge_auth.port}{path}",
            json=payload, headers={"Authorization": "Bearer nope"}, timeout=30)
        assert r.status_code == 401
        assert "token" in r.text.lower()


class TestRemoteClientCarriesTheToken:
    def test_remote_cmd_authenticates_itself(self, link_bridge_auth, monkeypatch):
        """A remote MCP instance reads the same env var, so a token-protected
        bridge stays usable without any per-client configuration."""
        monkeypatch.setenv(T.TOKEN_ENV, TOKEN)
        client = _remote_client(link_bridge_auth.port)
        assert client._remote_cmd({"cmd": "get_all_sessions"})["r"] == []

    def test_remote_cmd_without_the_token_says_what_is_wrong(
            self, link_bridge_auth, monkeypatch):
        """A client without editor env reads the same persistent token file."""
        monkeypatch.delenv(T.TOKEN_ENV, raising=False)
        client = _remote_client(link_bridge_auth.port)
        assert client._remote_cmd({"cmd": "get_all_sessions"})["r"] == []

    def test_remote_cmd_with_a_stale_token_also_reports_401(
            self, link_bridge_auth, monkeypatch):
        """Once migrated, a stale editor env cannot replace the shared token."""
        monkeypatch.setenv(T.TOKEN_ENV, "an-old-token")
        client = _remote_client(link_bridge_auth.port)
        assert client._remote_cmd({"cmd": "get_all_sessions"})["r"] == []

    def test_token_file_changed_under_old_bridge_reports_restart_hint(
            self, link_bridge_auth):
        T.bridge_token_path().write_text("new-token\n", encoding="utf-8")
        client = _remote_client(link_bridge_auth.port)
        with pytest.raises(
            PermissionError,
            match=r"agent-browser-mcp bridge --restart",
        ):
            client._remote_cmd({"cmd": "get_all_sessions"})

    def test_no_token_anywhere_still_works(self, link_bridge_open, monkeypatch):
        monkeypatch.delenv(T.TOKEN_ENV, raising=False)
        monkeypatch.setenv(T.TOKEN_AUTH_ENV, "off")
        client = _remote_client(link_bridge_open.port)
        assert client._remote_cmd({"cmd": "get_all_sessions"})["r"] == []


def _remote_client(port):
    """A driver in remote mode pointed at a test bridge, with no ports bound.

    __init__ probes and may bind, so build the remote half by hand — the same
    two attributes remote mode actually uses.
    """
    client = T.BrowserBridge.__new__(T.BrowserBridge)
    client.is_remote = True
    client.remote = f"http://127.0.0.1:{port}/link"
    client._http = requests.Session()
    client._http.trust_env = False
    return client


# --- the same channel, the other direction ---------------------------------
# Who may speak to /link is above; this is what comes back. An MCP server
# process is normally the remote half (get_driver probes and finds a daemon), so
# these paths are the ordinary ones, not exotic — but every other offline test
# builds its driver in host form, where the HTTP hop cannot lose anything.

class _NeverAnsweringSocket:
    """A live extension socket that accepts the frame and returns no result."""

    def __init__(self, host=None):
        self.host = host          # set to ACK like background.js does
        self.sent = []

    def send_message(self, payload):
        self.sent.append(payload)
        if self.host is not None:
            import json as _json
            import time as _time

            # background.js ACKs before it executes; the ACK is what turns
            # 'sent_unconfirmed' into 'delivered_no_result'.
            self.host.acks[_json.loads(payload)["id"]] = _time.time()


def _register_session(host, session_id, info, client):
    from agent_browser_mcp.browser_bridge import Session

    session = Session(session_id, info, client)
    host.sessions[session_id] = session
    host.latest_session_id = session_id
    return session


@pytest.mark.parametrize("kind,delivery_state,retry_safe,executed_tab_id", [
    ("unpolled", "undelivered", True, None),
    ("no_ack", "sent_unconfirmed", True, 7),
    ("acked", "delivered_no_result", False, 7),
])
def test_remote_execute_js_keeps_the_daemons_delivery_verdict(
    link_bridge_open, kind, delivery_state, retry_safe, executed_tab_id,
):
    """A command that gets no result must arrive as a verdict, not a TimeoutError.

    The daemon answers *at* the command deadline, so without
    REMOTE_TRANSPORT_MARGIN the socket read expires first and the caller sees a
    bare transport TimeoutError — throwing away delivery_state / retry_safe /
    executed_tab_id, which is exactly what decides whether a retry may repeat a
    side effect. This is the natural remote form: __init__ probes the port and
    goes remote by itself.
    """
    # The fixture disabled authentication; if that ever regresses this test would
    # silently start reading the developer's own token file.
    assert T.bridge_token() == ""
    host = link_bridge_open.driver
    client = T.BrowserBridge("127.0.0.1", link_bridge_open.base)
    assert client.is_remote is True
    assert client.remote.endswith(f":{link_bridge_open.port}/link")

    if kind == "unpolled":
        import queue

        session_id = "http-client:1"
        # A long-poll session nobody polls: the frame never leaves the queue.
        _register_session(
            host, session_id,
            {"url": "https://example.test/", "title": "t", "type": "http"},
            queue.Queue(),
        )
    else:
        session_id = "chrome:7"
        _register_session(
            host, session_id,
            {"url": "https://example.test/", "title": "t", "type": "ext_ws",
             "tab_id": 7, "client_id": "chrome"},
            _NeverAnsweringSocket(host if kind == "acked" else None),
        )

    result = client.execute_js("return 1", timeout=0.6, session_id=session_id)

    assert isinstance(result, dict), result
    assert result["error_code"] == "no_response"
    assert result["delivery_state"] == delivery_state
    assert result["retry_safe"] is retry_safe
    assert result["executed_tab_id"] == executed_tab_id
    assert "data" not in result          # no result is never a result
    assert result.get("closed") is None  # and this tab did not navigate away
