from __future__ import annotations

import time
from types import SimpleNamespace

from browsertap_mcp import browser_bridge as T
from browsertap_mcp import server as S
from browsertap_mcp.command_scope import command_scope


def _driver(*, remote: bool = False) -> T.BrowserBridge:
    driver = T.BrowserBridge.__new__(T.BrowserBridge)
    driver.host = "127.0.0.1"
    driver.port = 18765
    driver.is_remote = remote
    driver.default_session_id = None
    driver.latest_session_id = None
    driver.sessions = {}
    driver.results = {}
    driver.acks = {}
    driver.ext_clients = {}
    driver.client_last_seen = {}
    return driver


def test_remote_scoped_execute_resolves_canonical_session_and_disables_daemon_rebind(
    monkeypatch,
):
    driver = _driver(remote=True)
    calls = []
    guarded = []

    def remote(command, timeout=30):
        calls.append(command)
        if command["cmd"] == "resolve_session":
            return {
                "r": {
                    "session_id": "chrome-a:9",
                    "rebound_from": "chrome-a:7",
                }
            }
        return {"r": {"data": 3, "tabId": 9}}

    driver._remote_cmd = remote
    monkeypatch.setattr(T, "guard_targets", lambda namespace, targets: guarded.append(
        (namespace, targets),
    ))

    with command_scope():
        result = driver.execute_js("return 3", timeout=2, session_id="chrome-a:7")

    assert result["executed_tab_id"] == 9
    assert [item["cmd"] for item in calls] == ["resolve_session", "execute_js"]
    payload = calls[-1]
    assert payload["sessionId"] == "chrome-a:9"
    assert payload["allowFailover"] == "0"
    assert payload["allowRebind"] == "0"
    assert guarded == [("127.0.0.1:18765", ["chrome-a:9"])]


def test_ensure_sessions_does_not_cross_browser_when_default_tab_dies(monkeypatch):
    driver = _driver()
    driver.default_session_id = "chrome-a:1"
    sessions = [{"id": "edge-b:2", "browser": "edge", "url": "https://other.test"}]

    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(driver, "resolve_session_target", lambda _sid: None)

    assert S.ensure_sessions() == sessions
    # Keep the dead client prefix as the routing boundary. The next implicit
    # tab operation must report that chrome-a has no tabs instead of using edge.
    assert driver.default_session_id == "chrome-a:1"


def test_prune_stale_default_reselects_only_the_same_browser_client(monkeypatch):
    driver = _driver()
    driver.default_session_id = "chrome-a:1"
    sessions = [
        {"id": "edge-b:3", "browser": "edge", "url": "https://edge.test"},
        {"id": "chrome-a:2", "browser": "chrome", "url": "https://chrome.test"},
    ]

    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(driver, "resolve_session_target", lambda _sid: None)

    assert S.prune_stale_default() == "chrome-a:2"
    assert driver.default_session_id == "chrome-a:2"


def test_local_default_failover_stays_inside_the_original_client():
    driver = _driver()
    dead = SimpleNamespace(
        id="chrome-a:1",
        disconnect_at=time.time() - 1,
        is_active=lambda: False,
    )
    same_client = SimpleNamespace(
        id="chrome-a:2",
        disconnect_at=None,
        is_active=lambda: True,
        info={"url": "https://chrome.test"},
    )
    other_client = SimpleNamespace(
        id="edge-b:3",
        disconnect_at=None,
        is_active=lambda: True,
        info={"url": "https://edge.test"},
    )
    driver.sessions = {item.id: item for item in (dead, same_client, other_client)}
    driver.default_session_id = dead.id
    driver.latest_session_id = other_client.id

    assert driver._live_default_session_id() == same_client.id
    assert driver.default_session_id == same_client.id
