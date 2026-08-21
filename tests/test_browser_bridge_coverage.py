from __future__ import annotations

import io
import json
import queue
import time
from pathlib import Path
from types import SimpleNamespace

import bottle
import pytest

from browsertap_mcp import browser_bridge as T
from browsertap_mcp import simphtml


class FakeSocket:
    def __init__(self, *, send_error=None):
        self.messages = []
        self.send_error = send_error

    def send_message(self, message):
        if self.send_error:
            raise self.send_error
        self.messages.append(message)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def driver_stub(*, remote=False):
    driver = T.BrowserBridge.__new__(T.BrowserBridge)
    driver.host = "127.0.0.1"
    driver.port = 18765
    driver.sessions = {}
    driver.results = {}
    driver.acks = {}
    driver.default_session_id = None
    driver.latest_session_id = None
    driver.last_ext_seen = None
    driver.client_last_seen = {}
    driver.ext_clients = {}
    driver.is_remote = remote
    if remote:
        driver.remote = "http://127.0.0.1:18766/link"
    return driver


def wsgi_post(app, path, payload, *, origin=None, headers=None):
    body = json.dumps(payload).encode("utf-8")
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": path,
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    response = {}

    def start_response(status, response_headers, exc_info=None):
        response["status"] = int(status.split()[0])
        response["headers"] = dict(response_headers)

    chunks = app(environ, start_response)
    response["body"] = b"".join(chunks).decode("utf-8")
    close = getattr(chunks, "close", None)
    if callable(close):
        close()
    return response


class DormantThread:
    def __init__(self, target=None, **kwargs):
        self.target = target
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


def test_token_path_uses_configured_and_default_locations(monkeypatch, tmp_path):
    # Pin home: the real one may hold a pre-0.4.0 `.agent-browser-mcp`, which
    # `paths.state_dir` deliberately keeps using, so reading the developer's
    # home would make this assertion depend on the machine.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv(T.TOKEN_FILE_ENV, str(tmp_path / "custom-token"))
    assert T.bridge_token_path() == tmp_path / "custom-token"
    monkeypatch.setenv(T.TOKEN_FILE_ENV, "   ")
    assert T.bridge_token_path() == tmp_path / "home" / ".browsertap" / "bridge-token"


@pytest.mark.parametrize("error", [OSError("unreadable"), UnicodeError("bad encoding")])
def test_read_token_file_returns_empty_for_unreadable_file(monkeypatch, tmp_path, error):
    path = tmp_path / "token"
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    assert T._read_token_file(path) == ""


def test_persist_token_creates_secure_file_and_ignores_chmod_failure(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "token"
    monkeypatch.setattr(T.os, "chmod", lambda *args: (_ for _ in ()).throw(OSError("readonly")))
    assert T._persist_token(path, "abc") == "abc"
    assert path.read_text(encoding="utf-8") == "abc\n"


def test_persist_token_converges_when_another_process_wins(monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text("winner\n", encoding="utf-8")
    monkeypatch.setattr(T.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(FileExistsError()))
    assert T._persist_token(path, "loser") == "winner"


def test_persist_token_rejects_an_empty_file_left_by_competitor(monkeypatch, tmp_path):
    path = tmp_path / "token"
    monkeypatch.setattr(T.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(FileExistsError()))
    monkeypatch.setattr(T.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="token file is empty"):
        T._persist_token(path, "token")


def test_persist_token_wraps_os_error(monkeypatch, tmp_path):
    path = tmp_path / "token"
    monkeypatch.setattr(T.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(RuntimeError, match="cannot persist") as exc:
        T._persist_token(path, "token")
    assert exc.value.__cause__ is not None


@pytest.mark.parametrize("mode", ["0", "false", "off", "disabled"])
def test_token_auth_can_be_disabled_explicitly(monkeypatch, tmp_path, mode):
    monkeypatch.setenv(T.TOKEN_AUTH_ENV, mode)
    monkeypatch.setenv(T.TOKEN_FILE_ENV, str(tmp_path / "token"))
    assert T.bridge_token() == ""
    assert not (tmp_path / "token").exists()


def test_token_file_wins_over_legacy_environment(monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text("persistent\n", encoding="utf-8")
    monkeypatch.setenv(T.TOKEN_FILE_ENV, str(path))
    monkeypatch.setenv(T.TOKEN_ENV, "legacy")
    assert T.bridge_token() == "persistent"


def test_check_link_token_accepts_empty_expected_and_rejects_wrong_token():
    T.check_link_token({}, "")
    with pytest.raises(bottle.HTTPResponse) as exc:
        T.check_link_token({"X-Bridge-Token": "wrong"}, "wanted")
    assert exc.value.status_code == 401


def test_session_lifecycle_for_ws_http_and_extension_types(monkeypatch, caplog):
    caplog.set_level("INFO", logger="browsertap_mcp.browser_bridge")
    ws = FakeSocket()
    session = T.Session("ws:1", {"url": "https://one", "type": "ws"}, ws)
    assert session.url == "https://one"
    assert session.is_active() is True
    session.mark_disconnected()
    assert session.is_active() is False
    session.reconnect(FakeSocket(), {"url": "https://two", "type": "ext_ws"})
    assert session.type == "ext_ws"
    assert session.http_queue is None
    session.reconnect(queue.Queue(), {"url": "http://three", "type": "http"})
    assert session.type == "http"
    assert session.ws_client is None
    assert session.http_queue is not None
    session.connect_at = time.time() - 61
    assert session.is_active() is False
    assert "Tab disconnected" in caplog.text


def test_http_session_reconnect_and_unknown_type():
    session = T.Session("h:1", {"url": "http://x", "type": "http"}, queue.Queue())
    session.disconnect_at = time.time()
    session.reconnect(queue.Queue(), {"url": "x", "type": "other"})
    assert session.type == "other"
    assert session.disconnect_at is None
    assert session.http_queue is not None


def test_driver_registration_reconnect_and_unregister(caplog):
    caplog.set_level("INFO", logger="browsertap_mcp.browser_bridge")
    driver = driver_stub()
    first = FakeSocket()
    info = {"url": "https://a", "type": "ws"}
    driver._register_client("a:1", first, info)
    assert driver.default_session_id == "a:1"
    assert driver.latest_session_id == "a:1"
    second = FakeSocket()
    driver._register_client("a:1", second, {"url": "https://b", "type": "ws"})
    assert driver.sessions["a:1"].ws_client is second
    driver.ext_clients = {"a": {"ws": second}, "b": {"ws": first}}
    driver._unregister_client(second)
    assert "a" not in driver.ext_clients
    assert driver.sessions["a:1"].is_active() is False
    assert "Tab reconnected" in caplog.text


def test_live_default_reselects_latest_and_handles_no_sessions(caplog):
    caplog.set_level("INFO", logger="browsertap_mcp.browser_bridge")
    driver = driver_stub()
    dead = T.Session("c:dead", {"url": "dead", "type": "ws"}, FakeSocket())
    dead.mark_disconnected()
    live = T.Session("c:live", {"url": "live", "type": "ws"}, FakeSocket())
    driver.sessions = {dead.id: dead, live.id: live}
    driver.default_session_id = dead.id
    driver.latest_session_id = live.id
    assert driver._live_default_session_id() == live.id
    assert driver.default_session_id == live.id
    driver.sessions.clear()
    assert driver._live_default_session_id() == live.id
    assert "selected" in caplog.text


@pytest.mark.parametrize(
    "origin, env, expected",
    [
        ("", "", False),
        ("", "1", True),
        ("chrome-extension://abc", "", True),
        ("moz-extension://abc", "", True),
        ("https://site", "", False),
        ("https://site", "https://site,https://other", True),
    ],
)
def test_origin_allowlist(monkeypatch, origin, env, expected):
    driver = driver_stub()
    sock = SimpleNamespace(request=SimpleNamespace(headers={"Origin": origin}))
    monkeypatch.setenv("BROWSERTAP_WS_ALLOW_NO_ORIGIN", env)
    monkeypatch.delenv("BROWSERTAP_WS_ALLOWED_ORIGINS", raising=False)
    if "," in env:
        monkeypatch.delenv("BROWSERTAP_WS_ALLOW_NO_ORIGIN", raising=False)
        monkeypatch.setenv("BROWSERTAP_WS_ALLOWED_ORIGINS", env)
    assert driver._origin_allowed(sock) is expected


def test_ws_origin_handles_missing_request_headers():
    assert T.BrowserBridge._ws_origin(SimpleNamespace()) == ""


def test_clean_sessions_removes_old_sessions_results_acks_and_clients(monkeypatch):
    driver = driver_stub()
    stale = T.Session("old", {"url": "old", "type": "ws"}, FakeSocket())
    stale.disconnect_at = time.time() - 700
    driver.sessions[stale.id] = stale
    driver.results = {
        "old": {"ts": time.time() - 700},
        "fresh": {"ts": time.time()},
        "not-a-dict": "value",
    }
    driver.acks = {"old": time.time() - 700, "bad": "timestamp", "fresh": time.time()}
    driver.client_last_seen = {
        "old": {"ts": time.time() - 700},
        "bad": None,
        "live": {"ts": time.time() - 700},
    }
    driver.clean_sessions()
    assert "old" not in driver.sessions
    assert "old" not in driver.results
    assert "old" not in driver.acks
    assert "bad" not in driver.acks
    assert "old" not in driver.client_last_seen
    assert "bad" not in driver.client_last_seen


def test_extension_tab_snapshot_isolates_clients_and_replaces_generation():
    driver = driver_stub()
    old_client = FakeSocket()
    driver._apply_extension_tabs(
        "one", "chrome", [{"id": 1, "url": "https://old", "generation": "1"}], old_client
    )
    other = T.Session("two:2", {"url": "https://other", "type": "ext_ws", "client_id": "two"}, old_client)
    driver.sessions[other.id] = other
    new_client = FakeSocket()
    driver._apply_extension_tabs(
        "one", "chrome", [{"id": 1, "url": "https://new", "generation": "2"}], new_client
    )
    assert driver.sessions["one:1"].info["generation"] == "2"
    assert driver.sessions["one:1"].ws_client is new_client
    assert other.is_active() is True


def test_a_closed_tab_is_reported_once_and_can_still_be_reaped(caplog):
    """The snapshot sweep must not keep a dead session looking freshly dead.

    Extensions push a tab snapshot every few seconds, and every one of them
    re-runs the sweep over the whole table. Re-stamping `disconnect_at` there
    held `clean_sessions` permanently short of its reap window, so a daemon that
    outlives every MCP session accumulated one session per tab ever closed and
    re-logged all of them forever.
    """
    caplog.set_level("INFO", logger="browsertap_mcp.browser_bridge")
    driver = driver_stub()
    client = FakeSocket()
    driver._apply_extension_tabs("one", "chrome", [{"id": 1, "url": "https://gone"}], client)

    driver._apply_extension_tabs("one", "chrome", [], client)
    first_seen = driver.sessions["one:1"].disconnect_at
    assert first_seen is not None
    for _ in range(5):
        driver._apply_extension_tabs("one", "chrome", [], client)

    assert driver.sessions["one:1"].disconnect_at == first_seen
    assert caplog.text.count("Tab disconnected: https://gone") == 1

    driver.sessions["one:1"].disconnect_at = time.time() - 700
    driver._apply_extension_tabs("one", "chrome", [], client)
    driver.clean_sessions()

    assert "one:1" not in driver.sessions


def test_find_and_set_session_local_paths(caplog):
    caplog.set_level("INFO", logger="browsertap_mcp.browser_bridge")
    driver = driver_stub()
    driver._register_client("c:1", FakeSocket(), {"url": "https://a.test", "type": "ws"})
    driver._register_client("c:2", FakeSocket(), {"url": "https://a.test/2", "type": "ws"})
    driver.latest_session_id = "c:2"
    assert len(driver.find_session("a.test")) == 2
    with pytest.raises(ValueError, match="matched 2 sessions") as exc:
        driver.set_session("a.test")
    assert "c:1" in str(exc.value)
    assert "c:2" in str(exc.value)
    assert driver.set_session("a.test/2") == "c:2"
    assert driver.set_session("missing") is None


def test_find_session_empty_pattern_uses_latest_or_empty():
    driver = driver_stub()
    assert driver.find_session("") == []
    driver._register_client("c:1", FakeSocket(), {"url": "https://a", "type": "ws"})
    assert driver.find_session("")[0][0] == "c:1"


def test_remote_command_maps_http_statuses_and_json_errors(monkeypatch, tmp_path):
    driver = driver_stub(remote=True)
    driver._http = SimpleNamespace(post=lambda *args, **kwargs: FakeResponse(200, {"r": [1]}))
    monkeypatch.setenv(T.TOKEN_FILE_ENV, str(tmp_path / "token"))
    monkeypatch.setenv(T.TOKEN_ENV, "secret")
    assert driver._remote_cmd({"cmd": "x"}) == {"r": [1]}
    headers_seen = {}
    driver._http = SimpleNamespace(
        post=lambda url, **kwargs: (headers_seen.update(kwargs["headers"]) or FakeResponse(200, {}))
    )
    driver._remote_cmd({"cmd": "x"})
    assert headers_seen["Authorization"] == "Bearer secret"
    assert headers_seen["X-Bridge-Token"] == "secret"
    driver._http = SimpleNamespace(post=lambda *args, **kwargs: FakeResponse(401, None, "unauthorized"))
    with pytest.raises(PermissionError, match="401"):
        driver._remote_cmd({"cmd": "x"})
    driver._http = SimpleNamespace(post=lambda *args, **kwargs: FakeResponse(502, None, "bad\n gateway"))
    with pytest.raises(RuntimeError, match="502: bad +gateway"):
        driver._remote_cmd({"cmd": "x"})
    driver._http = SimpleNamespace(post=lambda *args, **kwargs: FakeResponse(200, ValueError("bad json")))
    with pytest.raises(ValueError, match="bad json"):
        driver._remote_cmd({"cmd": "x"})

    driver._http = SimpleNamespace(
        post=lambda *args, **kwargs: (_ for _ in ()).throw(
            T.requests.exceptions.ReadTimeout("bridge was slow")
        )
    )
    with pytest.raises(TimeoutError, match="bridge HTTP request timed out"):
        driver._remote_cmd({"cmd": "x"}, timeout=1.25)


def test_remote_get_sessions_diagnose_and_set_session(monkeypatch):
    driver = driver_stub(remote=True)
    calls = []

    def remote(cmd, timeout=30):
        calls.append((cmd, timeout))
        if cmd["cmd"] == "find_session":
            return {"r": [["c:1", {"url": "https://x"}]]}
        if cmd["cmd"] == "diagnose":
            return {"r": {"cause": "healthy"}}
        return {"r": [{"id": "c:1", "url": "https://x"}]}

    driver._remote_cmd = remote
    assert driver.get_all_sessions(timeout=2)[0]["id"] == "c:1"
    assert driver.get_session_dict() == {"c:1": "https://x"}
    assert driver.diagnose(timeout=3)["cause"] == "healthy"
    assert driver.set_session("x") == "c:1"
    assert len(calls) == 4


def test_remote_diagnose_classifies_transport_failure():
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("down"))
    result = driver.diagnose()
    assert result["cause"] == "bridge_unreachable"
    assert result["ok"] is False
    assert result["error"] == "down"


def test_remote_execute_js_maps_errors_and_echoed_tab(monkeypatch, capsys):
    driver = driver_stub(remote=True)
    seen = []

    def remote(cmd, timeout=30):
        seen.append((cmd, timeout))
        return {"r": {"data": 3, "tabId": "7"}}

    driver._remote_cmd = remote
    result = driver.execute_js("return 3", timeout=2, session_id="c:7", allow_failover=True)
    assert result["executed_tab_id"] == 7
    assert seen[0][0]["allowFailover"] == "1"
    driver._remote_cmd = lambda *args, **kwargs: {
        "r": {"error": "Session c:7 is not connected", "error_code": "session_not_connected"}
    }
    with pytest.raises(T.SessionNotConnectedError):
        driver.execute_js("x", session_id="c:7")
    driver._remote_cmd = lambda *args, **kwargs: {"r": {"error": "other error"}}
    with pytest.raises(Exception, match="other error"):
        driver.execute_js("x", session_id="c:7")
    assert capsys.readouterr().out == ""


def test_remote_ext_cmd_maps_timeout_and_other_errors():
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *args, **kwargs: {"r": {"data": "ok"}}
    assert driver.ext_cmd({"cmd": "tabs"}) == {"data": "ok"}
    driver._remote_cmd = lambda *args, **kwargs: {"r": {"error": "did not respond within 1s"}}
    with pytest.raises(TimeoutError):
        driver.ext_cmd({"cmd": "tabs"})
    driver._remote_cmd = lambda *args, **kwargs: {"r": {"error": "bad route"}}
    with pytest.raises(Exception, match="bad route"):
        driver.ext_cmd({"cmd": "tabs"})


def test_newtab_requires_operation_id_and_forwards_exact_payload():
    driver = driver_stub()
    calls = []
    driver.ext_cmd = lambda payload, **kwargs: calls.append((payload, kwargs)) or {"data": "created"}
    with pytest.raises(ValueError, match="operation_id"):
        driver.newtab()
    with pytest.raises(ValueError, match="operation_id"):
        driver.newtab(operation_id=" ")
    assert driver.newtab(operation_id="op-1", client_id="edge", active=False) == {"data": "created"}
    payload, kwargs = calls[-1]
    assert payload == {
        "cmd": "tabs",
        "method": "create",
        "url": "about:blank",
        "active": False,
        "operation_id": "op-1",
        "client_id": "edge",
    }
    assert kwargs["client_id"] == "edge"


def test_newtab_defaults_to_background_creation():
    driver = driver_stub()
    calls = []
    driver.ext_cmd = lambda payload, **kwargs: calls.append(payload) or {"data": "created"}

    driver.newtab(url="https://background.test/", operation_id="op-background")

    assert calls[-1]["active"] is False


def test_newtab_does_not_retry_timeout_or_other_error():
    driver = driver_stub()
    driver.ext_cmd = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("late"))
    with pytest.raises(TimeoutError):
        driver.newtab(url="https://x", operation_id="op")
    driver.ext_cmd = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failed"))
    with pytest.raises(RuntimeError):
        driver.newtab(url="https://x", operation_id="op")


def test_constructor_detects_remote_bridge_without_starting_servers(monkeypatch):
    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, value):
            self.timeout = value

        def connect_ex(self, address):
            self.address = address
            return 0

    class HttpSession:
        trust_env = True

    monkeypatch.setattr(T.socket, "socket", Probe)
    monkeypatch.setattr(T.requests, "Session", HttpSession)
    monkeypatch.setattr(T.BrowserBridge, "start_ws_server", lambda self: pytest.fail("must stay remote"))
    monkeypatch.setattr(T.BrowserBridge, "start_http_server", lambda self: pytest.fail("must stay remote"))
    driver = T.BrowserBridge(host="127.0.0.8", port=19000)
    assert driver.is_remote is True
    assert driver.remote == "http://127.0.0.8:19001/link"
    assert driver._http.trust_env is False


def test_constructor_starts_local_servers_when_lock_is_acquired(monkeypatch):
    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, value):
            pass

        def connect_ex(self, address):
            return 1

    calls = []
    host_lock = object()
    monkeypatch.setattr(T.socket, "socket", Probe)
    monkeypatch.setattr(T.BrowserBridge, "_acquire_host_lock", lambda self: host_lock)
    monkeypatch.setattr(T.BrowserBridge, "start_ws_server", lambda self: calls.append("ws"))
    monkeypatch.setattr(T.BrowserBridge, "start_http_server", lambda self: calls.append("http"))
    driver = T.BrowserBridge()
    assert driver.is_remote is False
    assert driver._host_lock is host_lock
    assert calls == ["ws", "http"]


def test_constructor_loses_host_lock_then_waits_for_winner(monkeypatch):
    results = iter([1, 1, 0])

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, value):
            pass

        def connect_ex(self, address):
            return next(results)

    class HttpSession:
        trust_env = True

    sleeps = []
    monkeypatch.setattr(T.socket, "socket", Probe)
    monkeypatch.setattr(T.BrowserBridge, "_acquire_host_lock", lambda self: None)
    monkeypatch.setattr(T.requests, "Session", HttpSession)
    monkeypatch.setattr(T.time, "sleep", lambda seconds: sleeps.append(seconds))
    driver = T.BrowserBridge()
    assert driver.is_remote is True
    assert sleeps == [0.25, 0.25]


def test_host_lock_success_and_failure(monkeypatch):
    class LockSocket:
        def __init__(self, fail=False):
            self.fail = fail
            self.closed = False
            self.calls = []

        def setsockopt(self, *args):
            self.calls.append(("setsockopt", args))

        def bind(self, address):
            self.calls.append(("bind", address))
            if self.fail:
                raise OSError("busy")

        def listen(self, count):
            self.calls.append(("listen", count))

        def close(self):
            self.closed = True

    driver = driver_stub()
    good = LockSocket()
    monkeypatch.setattr(T.socket, "socket", lambda: good)
    assert driver._acquire_host_lock() is good
    assert ("bind", ("127.0.0.1", 18767)) in good.calls
    bad = LockSocket(fail=True)
    monkeypatch.setattr(T.socket, "socket", lambda: bad)
    assert driver._acquire_host_lock() is None
    assert bad.closed is True


@pytest.fixture
def http_app(monkeypatch):
    driver = driver_stub()
    monkeypatch.setattr(T, "bridge_token", lambda: "")
    monkeypatch.setattr(T.threading, "Thread", DormantThread)
    driver.start_http_server()
    return driver


def test_stop_http_server_gives_the_port_back_and_repeats_safely(link_bridge_open):
    """Every test bridge has to be closable.

    Nothing but the owner can close this listener — the daemon exits with its
    process instead — so if this ever became a no-op again each fixture would
    leave a listener running for the rest of the session, and a later bridge that
    picked the same port would find requests answered by its predecessor. The
    fixture's own teardown calls stop again, so it must also be idempotent.
    """
    import socket as _socket

    port = link_bridge_open.port
    with _socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) == 0

    link_bridge_open.driver.stop_http_server()

    with _socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    link_bridge_open.driver.stop_http_server()


def test_stop_http_server_is_a_noop_when_nothing_bound(http_app):
    """A stubbed thread never reaches make_server; closing must not raise."""
    assert http_app.http_server is None
    http_app.stop_http_server()


def test_http_hook_rejects_web_origin_and_allows_extension_or_configured_origin(
    http_app, monkeypatch
):
    rejected = wsgi_post(http_app.app, "/link", {"cmd": "get_all_sessions"}, origin="https://evil")
    assert rejected["status"] == 403
    allowed = wsgi_post(
        http_app.app,
        "/link",
        {"cmd": "get_all_sessions"},
        origin="chrome-extension://abc",
    )
    assert allowed["status"] == 200
    monkeypatch.setenv("BROWSERTAP_WS_ALLOWED_ORIGINS", "https://trusted")
    trusted = wsgi_post(
        http_app.app,
        "/link",
        {"cmd": "get_all_sessions"},
        origin="https://trusted",
    )
    assert trusted["status"] == 200


@pytest.mark.parametrize(
    "cmd, method_name, result",
    [
        ({"cmd": "get_all_sessions"}, "get_all_sessions", [{"id": "c:1"}]),
        ({"cmd": "diagnose"}, "diagnose", {"cause": "healthy"}),
        ({"cmd": "find_session", "url_pattern": "x"}, "find_session", [["c:1", {"url": "x"}]]),
    ],
)
def test_http_link_query_routes_success(http_app, cmd, method_name, result):
    setattr(http_app, method_name, lambda *args, **kwargs: result)
    response = wsgi_post(http_app.app, "/link", cmd)
    assert response["status"] == 200
    assert json.loads(response["body"])["r"] == result


@pytest.mark.parametrize(
    "cmd, method_name, prefix",
    [
        ({"cmd": "get_all_sessions"}, "get_all_sessions", "get_all_sessions"),
        ({"cmd": "diagnose"}, "diagnose", "diagnose"),
        ({"cmd": "find_session"}, "find_session", "find_session"),
    ],
)
def test_http_link_query_routes_wrap_errors(http_app, cmd, method_name, prefix):
    setattr(
        http_app,
        method_name,
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    body = json.loads(wsgi_post(http_app.app, "/link", cmd)["body"])
    assert prefix in body["r"]["error"]
    assert "failed" in body["r"]["error"]


def test_http_link_ext_cmd_success_and_error(http_app):
    calls = []
    http_app.ext_cmd = lambda payload, **kwargs: calls.append((payload, kwargs)) or {"data": "ok"}
    command = {
        "cmd": "ext_cmd",
        "payload": {"cmd": "tabs"},
        "clientId": "chrome",
        "timeout": "2.5",
    }
    body = json.loads(wsgi_post(http_app.app, "/link", command)["body"])
    assert body["r"] == {"data": "ok"}
    assert calls == [({"cmd": "tabs"}, {"client_id": "chrome", "timeout": 2.5})]
    http_app.ext_cmd = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ext failed"))
    body = json.loads(wsgi_post(http_app.app, "/link", command)["body"])
    assert body["r"]["error"] == "ext failed"


def test_http_link_execute_js_success_and_error(http_app):
    calls = []
    http_app.execute_js = lambda code, **kwargs: calls.append((code, kwargs)) or {"data": 7}
    command = {
        "cmd": "execute_js",
        "sessionId": "c:7",
        "code": "return 7",
        "timeout": "3",
        "allowFailover": "1",
    }
    body = json.loads(wsgi_post(http_app.app, "/link", command)["body"])
    assert body["r"] == {"data": 7}
    assert calls[0][1] == {"timeout": 3.0, "session_id": "c:7", "allow_failover": True}
    http_app.execute_js = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("dead tab"))
    body = json.loads(wsgi_post(http_app.app, "/link", command)["body"])
    assert body["r"]["error"] == "dead tab"


def test_http_result_route_records_success_error_and_ignores_other(http_app):
    assert wsgi_post(
        http_app.app,
        "/api/result",
        {"type": "result", "id": "ok", "result": 3, "newTabs": [{"id": 2}], "tabId": 1},
    )["body"] == "ok"
    assert http_app.results["ok"]["success"] is True
    wsgi_post(
        http_app.app,
        "/api/result",
        {"type": "error", "id": "bad", "error": "boom"},
    )
    assert http_app.results["bad"]["success"] is False
    before = dict(http_app.results)
    wsgi_post(http_app.app, "/api/result", {"type": "other", "id": "ignored"})
    assert http_app.results == before


def test_http_longpoll_registers_session_returns_command_and_ack(http_app, monkeypatch):
    class ReadyQueue(queue.Queue):
        def __init__(self):
            super().__init__()
            self.put(json.dumps({"id": "exec-1", "code": "return 1"}))

    monkeypatch.setattr(T.queue, "Queue", ReadyQueue)
    response = wsgi_post(
        http_app.app,
        "/api/longpoll",
        {"sessionId": "http:1", "url": "https://x", "title": "X"},
    )
    assert json.loads(response["body"])["id"] == "exec-1"
    assert "exec-1" in http_app.acks
    assert http_app.sessions["http:1"].type == "http"


def test_http_longpoll_reconnects_disconnected_ws_or_refuses_active_ws(http_app, monkeypatch):
    disconnected = T.Session("c:1", {"url": "x", "type": "ws"}, FakeSocket())
    disconnected.mark_disconnected()
    http_app.sessions["c:1"] = disconnected

    class ReadyQueue(queue.Queue):
        def __init__(self):
            super().__init__()
            self.put("not-json")

    monkeypatch.setattr(T.queue, "Queue", ReadyQueue)
    response = wsgi_post(http_app.app, "/api/longpoll", {"sessionId": "c:1", "url": "x"})
    assert response["body"] == "not-json"
    assert http_app.sessions["c:1"].type == "http"
    active = T.Session("c:2", {"url": "x", "type": "ws"}, FakeSocket())
    http_app.sessions["c:2"] = active
    response = wsgi_post(http_app.app, "/api/longpoll", {"sessionId": "c:2", "url": "x"})
    assert json.loads(response["body"])["ret"] == "use ws"


def test_start_ws_server_exposes_handler_and_handles_protocol_messages(monkeypatch):
    driver = driver_stub()
    captured = {}

    class Server:
        def __init__(self, host, port, handler):
            captured["handler"] = handler
            self.host = host
            self.port = port

    monkeypatch.setattr(T, "WebSocketServer", Server)
    monkeypatch.setattr(T.threading, "Thread", DormantThread)
    driver.start_ws_server()
    handler = captured["handler"]

    def peer(data, origin="chrome-extension://abc"):
        item = object.__new__(handler)
        item.data = json.dumps(data) if not isinstance(data, str) else data
        item.request = SimpleNamespace(headers={"Origin": origin})
        item.address = ("local", 1)
        item.sent = []
        item.closed = False
        item.send_message = lambda message: item.sent.append(json.loads(message))
        item.close = lambda: setattr(item, "closed", True)
        return item

    ready = peer({"type": "ready", "sessionId": "legacy:1", "url": "https://x"})
    ready.connected()
    ready.handle()
    assert "legacy:1" in driver.sessions
    ext = peer(
        {
            "type": "ext_ready",
            "clientId": "chrome",
            "browser": "chrome",
            "tabs": [{"id": 7, "url": "https://x", "generation": "g1"}],
        }
    )
    ext.handle()
    assert driver.ext_clients["chrome"]["ws"] is ext
    assert "chrome:7" in driver.sessions
    ping = peer({"type": "ping"})
    ping.handle()
    assert ping.sent == [{"type": "pong"}]
    ack = peer({"type": "ack", "id": "a"})
    ack.handle()
    assert "a" in driver.acks
    result = peer({"type": "result", "id": "r", "result": 3, "tabId": 7})
    result.handle()
    assert driver.results["r"]["success"] is True
    error = peer({"type": "error", "id": "e", "error": "bad"})
    error.handle()
    assert driver.results["e"]["success"] is False
    bad = peer("not-json")
    bad.handle()
    rejected = peer({"type": "ping"}, origin="https://site")
    rejected.connected()
    rejected.handle()
    assert rejected.closed is True
    ext.handle_close()
    assert "chrome" not in driver.ext_clients


def test_ws_handler_assigns_stable_fallback_client_and_tolerates_ping_send_error(monkeypatch):
    driver = driver_stub()
    captured = {}

    class Server:
        def __init__(self, host, port, handler):
            captured["handler"] = handler

    monkeypatch.setattr(T, "WebSocketServer", Server)
    monkeypatch.setattr(T.threading, "Thread", DormantThread)
    driver.start_ws_server()
    handler = captured["handler"]
    item = object.__new__(handler)
    item.request = SimpleNamespace(headers={"Origin": "chrome-extension://abc"})
    item.address = ("local", 1)
    item.data = json.dumps({"type": "tabs_update", "tabs": []})
    item.handle()
    first = item._fallback_cid
    item.handle()
    assert item._fallback_cid == first
    item.data = json.dumps({"type": "ping"})
    item.send_message = lambda message: (_ for _ in ()).throw(RuntimeError("closed"))
    item.handle()


def test_ws_server_loop_rebuilds_after_crash(monkeypatch):
    driver = driver_stub()
    instances = []

    class Server:
        def __init__(self, host, port, handler):
            self.closed = False
            instances.append(self)

        def serve_forever(self):
            if len(instances) == 1:
                raise RuntimeError("crash")
            raise KeyboardInterrupt()

        def close(self):
            self.closed = True

    class RunningThread(DormantThread):
        def start(self):
            self.target()

    monkeypatch.setattr(T, "WebSocketServer", Server)
    monkeypatch.setattr(T.threading, "Thread", RunningThread)
    monkeypatch.setattr(T.time, "sleep", lambda seconds: None)
    with pytest.raises(KeyboardInterrupt):
        driver.start_ws_server()
    assert len(instances) == 2
    assert instances[0].closed is True


@pytest.mark.parametrize(
    "active, last_seen, expected",
    [
        (True, None, "healthy"),
        (False, None, "ext_never_registered"),
        (False, 800.0, "sw_slept_or_dropped"),
        (False, 980.0, "registering"),
    ],
)
def test_local_diagnose_classifies_every_lifecycle_state(
    monkeypatch, active, last_seen, expected
):
    driver = driver_stub()
    if active:
        driver.sessions["c:1"] = T.Session(
            "c:1", {"url": "https://x", "type": "ws", "client_id": "c"}, FakeSocket()
        )
    driver.last_ext_seen = last_seen
    driver.client_last_seen = {"c": {"browser": "chrome", "ts": 990.0}}
    driver.clean_sessions = lambda: None
    driver.ext_cmd = lambda *args, **kwargs: {
        "data": {
            "extension_version": "0.3.1",
            "protocol_version": "2",
            "capabilities": {"tabs": True},
        }
    }
    monkeypatch.setattr(T.time, "time", lambda: 1000.0)
    result = driver.diagnose(timeout=2)
    assert result["cause"] == expected
    assert result["ok"] is active
    assert result["extension_version"] == "0.3.1"
    assert result["protocol_version"] == "2"
    assert result["extension_capabilities"] == {"tabs": True}
    assert result["clients"]["c"]["seconds_ago"] == 10.0


def test_local_diagnose_records_extension_status_failure(monkeypatch):
    driver = driver_stub()
    driver.clean_sessions = lambda: None
    driver.ext_cmd = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no extension"))
    result = driver.diagnose()
    assert result["cause"] == "ext_never_registered"
    assert result["extension_status_error"] == "no extension"


def test_local_diagnose_accepts_manifest_fallback_and_non_mapping_status():
    driver = driver_stub()
    driver.clean_sessions = lambda: None
    driver.ext_cmd = lambda *args, **kwargs: {"manifest_version": "legacy"}
    result = driver.diagnose()
    assert result["extension_version"] == "legacy"
    driver.ext_cmd = lambda *args, **kwargs: {"data": "old-extension"}
    assert "extension_version" not in driver.diagnose()


def test_ext_cmd_local_success_prefers_default_browser_and_cleans_ack(monkeypatch):
    driver = driver_stub()
    driver.default_session_id = "one:7"

    class ReplySocket(FakeSocket):
        def send_message(self, message):
            payload = json.loads(message)
            self.messages.append(payload)
            driver.acks[payload["id"]] = time.time()
            driver.results[payload["id"]] = {"success": True, "data": {"ok": True}}

    one = ReplySocket()
    two = FakeSocket()
    driver.ext_clients = {
        "one": {"ws": one, "ts": 1},
        "two": {"ws": two, "ts": 2},
    }
    monkeypatch.setattr(T.uuid, "uuid4", lambda: "cmd-1")
    result = driver.ext_cmd({"cmd": "tabs"})
    assert result == {"data": {"ok": True}, "client_id": "one"}
    assert one.messages[0]["code"] == {"cmd": "tabs"}
    assert "cmd-1" not in driver.acks


def test_ext_cmd_local_uses_newest_client_and_surfaces_extension_error(monkeypatch):
    driver = driver_stub()

    class ErrorSocket(FakeSocket):
        def send_message(self, message):
            payload = json.loads(message)
            driver.results[payload["id"]] = {"success": False, "data": "denied"}

    driver.ext_clients = {
        "old": {"ws": FakeSocket(), "ts": 1},
        "new": {"ws": ErrorSocket(), "ts": 9},
    }
    monkeypatch.setattr(T.uuid, "uuid4", lambda: "cmd-2")
    with pytest.raises(Exception, match="denied"):
        driver.ext_cmd({"cmd": "tabs"})


def test_ext_cmd_local_validates_route_and_drops_broken_socket():
    driver = driver_stub()
    with pytest.raises(ValueError, match="timeout"):
        driver.ext_cmd({}, timeout=0)
    with pytest.raises(T.ExtensionNotConnectedError, match="No browser extension"):
        driver.ext_cmd({"cmd": "tabs"})
    driver.ext_clients = {"bad": {"ws": FakeSocket(send_error=RuntimeError("closed")), "ts": 1}}
    with pytest.raises(T.ExtensionNotConnectedError, match="disconnected"):
        driver.ext_cmd({"cmd": "tabs"}, client_id="bad")
    assert "bad" not in driver.ext_clients


def test_ext_cmd_local_timeout_cleans_late_state(monkeypatch):
    driver = driver_stub()
    driver.ext_clients = {"c": {"ws": FakeSocket(), "ts": 1}}
    monkeypatch.setattr(T.uuid, "uuid4", lambda: "cmd-timeout")
    with pytest.raises(TimeoutError, match="did not respond"):
        driver.ext_cmd({"cmd": "tabs"}, client_id="c", timeout=0.001)
    assert "cmd-timeout" not in driver.results
    assert "cmd-timeout" not in driver.acks


def _install_exec_session(driver, *, session_type="ext_ws", socket=None, session_id="c:7"):
    info = {"url": "https://x", "type": session_type}
    if session_type == "ext_ws":
        info["tab_id"] = 7
    client = socket if socket is not None else (queue.Queue() if session_type == "http" else FakeSocket())
    session = T.Session(session_id, info, client)
    driver.sessions[session_id] = session
    driver.default_session_id = session_id
    driver.latest_session_id = session_id
    driver.clean_sessions = lambda: None
    return session


def test_execute_js_local_success_reports_tab_and_strips_newtab_timestamps(monkeypatch):
    driver = driver_stub()

    class ReplySocket(FakeSocket):
        def send_message(self, message):
            payload = json.loads(message)
            driver.acks[payload["id"]] = time.time()
            driver.results[payload["id"]] = {
                "success": True,
                "data": 9,
                "tabId": 8,
                "newTabs": [{"id": 10, "ts": 123}],
            }

    _install_exec_session(driver, socket=ReplySocket())
    monkeypatch.setattr(T.uuid, "uuid4", lambda: "exec-ok")
    result = driver.execute_js("return 9", session_id="c:7")
    assert result == {"data": 9, "executed_tab_id": 8, "newTabs": [{"id": 10}]}
    assert "exec-ok" not in driver.acks


def test_execute_js_local_http_queue_and_extension_error(monkeypatch):
    driver = driver_stub()

    class ReplyQueue(queue.Queue):
        def put(self, message, *args, **kwargs):
            payload = json.loads(message)
            driver.results[payload["id"]] = {
                "success": False,
                "data": "script failed",
                "newTabs": [],
            }
            return super().put(message, *args, **kwargs)

    _install_exec_session(driver, session_type="http", socket=ReplyQueue(), session_id="http:1")
    monkeypatch.setattr(T.uuid, "uuid4", lambda: "exec-http")
    with pytest.raises(Exception, match="script failed"):
        driver.execute_js("bad()", session_id="http:1")


def test_execute_js_local_marks_broken_socket_disconnected():
    driver = driver_stub()
    session = _install_exec_session(
        driver, socket=FakeSocket(send_error=RuntimeError("closed"))
    )
    with pytest.raises(T.SessionDisconnectedError, match="disconnected"):
        driver.execute_js("return 1", session_id="c:7")
    assert session.is_active() is False


def test_execute_js_rejects_explicit_dead_tab_but_can_fail_over(monkeypatch):
    driver = driver_stub()

    class ReplySocket(FakeSocket):
        def send_message(self, message):
            payload = json.loads(message)
            driver.results[payload["id"]] = {"success": True, "data": "live", "newTabs": []}

    _install_exec_session(driver, socket=ReplySocket(), session_id="c:live")
    monkeypatch.setattr(T.time, "sleep", lambda seconds: None)
    with pytest.raises(T.SessionNotConnectedError, match="refused to execute"):
        driver.execute_js("sideEffect()", session_id="c:dead")
    result = driver.execute_js("sideEffect()", session_id="c:dead", allow_failover=True)
    assert result["data"] == "live"
    assert result["switched_from"] == "c:dead"
    assert result["switched_session"] == "c:live"


def _failover_pool(driver, ages):
    """Register one session per (id, age-in-seconds) pair and return them in order."""
    now = time.time()
    pool = []
    for session_id, age in ages:
        session = T.Session(session_id, {"url": "https://x", "type": "ext_ws", "tab_id": 1}, FakeSocket())
        session.connect_at = now - age
        driver.sessions[session_id] = session
        pool.append(session)
    return pool


def test_failover_skips_a_tab_that_only_just_registered():
    driver = driver_stub()
    settled, fresh = _failover_pool(driver, [("c:settled", 30.0), ("c:fresh", 0.0)])
    driver.latest_session_id = fresh.id
    # The newest tab is the worst substitute: it registered at document_idle, so
    # its next commit detaches the CDP fallback's debugger mid-command.
    assert driver._pick_failover_session([settled, fresh]) is settled


def test_failover_keeps_the_old_order_when_nothing_has_settled():
    driver = driver_stub()
    first, second = _failover_pool(driver, [("c:a", 0.0), ("c:b", 0.1)])
    driver.latest_session_id = second.id
    # A browser that just started has no settled tab at all. Refusing there would
    # be worse than the race, so the previous rule still applies unchanged.
    assert driver._pick_failover_session([first, second]) is second
    driver.latest_session_id = "c:gone"
    assert driver._pick_failover_session([first, second]) is second


def test_failover_still_prefers_the_newest_among_settled_tabs():
    driver = driver_stub()
    older, newer = _failover_pool(driver, [("c:older", 90.0), ("c:newer", 5.0)])
    driver.latest_session_id = newer.id
    assert driver._pick_failover_session([older, newer]) is newer


def test_failover_tolerates_a_session_without_connect_at():
    driver = driver_stub()
    legacy = T.Session("c:legacy", {"url": "https://x", "type": "ext_ws", "tab_id": 1}, FakeSocket())
    del legacy.connect_at
    driver.sessions[legacy.id] = legacy
    driver.latest_session_id = legacy.id
    # connect_at missing reads as epoch 0, i.e. settled -- never as "skip me",
    # which would leave failover with an empty candidate list.
    assert driver._pick_failover_session([legacy]) is legacy


def test_execute_js_implicit_default_reselects_live_session(monkeypatch):
    driver = driver_stub()

    class ReplySocket(FakeSocket):
        def send_message(self, message):
            payload = json.loads(message)
            driver.results[payload["id"]] = {"success": True, "data": 1, "newTabs": []}

    live = _install_exec_session(driver, socket=ReplySocket(), session_id="c:live")
    dead = T.Session("c:dead", {"url": "dead", "type": "ws"}, FakeSocket())
    dead.mark_disconnected()
    driver.sessions[dead.id] = dead
    driver.default_session_id = dead.id
    driver.latest_session_id = live.id
    assert driver.execute_js("return 1")["data"] == 1
    assert driver.default_session_id == live.id


def test_execute_js_rejects_missing_session_and_invalid_timeout(monkeypatch):
    driver = driver_stub()
    with pytest.raises(ValueError, match="timeout"):
        driver.execute_js("x", timeout=0)
    monkeypatch.setattr(T.time, "sleep", lambda seconds: None)
    with pytest.raises(T.SessionNotConnectedError, match="not connected"):
        driver.execute_js("x", session_id="missing")


@pytest.mark.parametrize("session_type", ["ext_ws", "http"])
def test_execute_js_deadline_can_expire_before_dispatch(monkeypatch, session_type):
    driver = driver_stub()
    _install_exec_session(
        driver,
        session_type=session_type,
        session_id="c:7" if session_type == "ext_ws" else "http:1",
    )
    values = iter([0.0, 2.0])
    monkeypatch.setattr(T.time, "monotonic", lambda: next(values))
    result = driver.execute_js(
        "return 1", timeout=1, session_id="c:7" if session_type == "ext_ws" else "http:1"
    )
    assert "no response" in result["result"].lower()


@pytest.mark.parametrize(
    "session_type, ack, expected, state, safe",
    [
        # A payload written to a live socket with no ACK back is NOT provably
        # undelivered — only the ACK-before-execute protocol says it did not run.
        ("ext_ws", False, "no ACK", "sent_unconfirmed", True),
        ("ext_ws", True, "ACK received", "delivered_no_result", False),
        # An http session's queue lives in this process: never polled is proof.
        ("http", False, "script not polled", "undelivered", True),
        ("http", True, "delivered but no result", "delivered_no_result", False),
    ],
)
def test_execute_js_timeout_classifies_delivery(
    monkeypatch, session_type, ack, expected, state, safe
):
    driver = driver_stub()
    monkeypatch.setattr(T.uuid, "uuid4", lambda: "exec-timeout")

    class AckSocket(FakeSocket):
        def send_message(self, message):
            if ack:
                driver.acks["exec-timeout"] = time.time()

    class AckQueue(queue.Queue):
        def put(self, message, *args, **kwargs):
            if ack:
                driver.acks["exec-timeout"] = time.time()
            return super().put(message, *args, **kwargs)

    client = AckSocket() if session_type == "ext_ws" else AckQueue()
    sid = "c:7" if session_type == "ext_ws" else "http:1"
    _install_exec_session(driver, session_type=session_type, socket=client, session_id=sid)
    result = driver.execute_js("slow()", timeout=0.05, session_id=sid)
    assert expected in result["result"]
    assert result["delivery_state"] == state
    assert result["retry_safe"] is safe
    # Both retry-safe states must classify into the one retry policy callers use.
    assert simphtml.no_response_kind(result) == ("undelivered" if safe else "after_ack")


def test_execute_js_detects_reload_during_wait(monkeypatch):
    driver = driver_stub()

    class FlappingSession:
        id = "c:7"
        type = "ext_ws"
        info = {"tab_id": 7}
        ws_client = FakeSocket()

        def __init__(self):
            self.states = iter([True, False, True])

        def is_active(self):
            return next(self.states, True)

    driver.sessions["c:7"] = FlappingSession()
    driver.default_session_id = driver.latest_session_id = "c:7"
    driver.clean_sessions = lambda: None
    result = driver.execute_js("navigate()", timeout=0.1, session_id="c:7")
    assert result["closed"] == 1
    assert result["result"] == "Session c:7 reloaded."


def test_find_session_skips_inactive_and_url_less_entries():
    driver = driver_stub()
    inactive = T.Session("c:1", {"url": "https://x", "type": "ws"}, FakeSocket())
    inactive.mark_disconnected()
    driver.sessions = {
        "c:1": inactive,
        "c:2": T.Session("c:2", {"type": "ws"}, FakeSocket()),
    }
    assert driver.find_session("x") == []


def test_jump_delegates_to_execute_js_with_json_quoted_url():
    driver = driver_stub()
    calls = []
    driver.execute_js = lambda code, **kwargs: calls.append((code, kwargs)) or "ok"
    assert driver.jump('https://x.test/a"b', timeout=4) is None
    assert calls == [('window.location.href="https://x.test/a\\\"b"', {"timeout": 4})]
    assert calls == [
        ('window.location.href="https://x.test/a\\"b"', {"timeout": 4})
    ]
