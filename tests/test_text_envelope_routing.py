"""Command envelopes travel on `ext_cmd`, never as a text script.

Since the extension's cmd/code split (82aabc9) anything that arrives in `code`
is evaluated as page JavaScript. Four server call sites kept sending a JSON
envelope that way and died live with `SyntaxError: Unexpected token ':'`:
`set_cookies` and `delete_cookies` silently fell back to `document.cookie`
(HttpOnly dropped, `status: ok`), `cdp_batch` and `upload_files` failed
outright. These tests pin the route, not just the payload.
"""
from __future__ import annotations

import pytest

from browsertap_mcp import server as S


class _Driver:
    default_session_id = "chrome:7"

    def __init__(self, reply):
        self.reply = reply
        self.ext_calls = []

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.ext_calls.append((payload, client_id, timeout))
        return self.reply

    def execute_js(self, *args, **kwargs):  # pragma: no cover - the wrong route
        raise AssertionError("a command envelope must not be sent as a text script")


@pytest.fixture
def driver(monkeypatch):
    driver = _Driver({"data": {}, "client_id": "chrome"})
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: "chrome:7")
    monkeypatch.setattr(S, "exec_js", driver.execute_js)
    return driver


def test_set_cookies_writes_through_ext_cmd_with_cdp_method(driver):
    out = S.set_cookies(
        {"name": "k", "value": "v", "url": "https://x.test/", "httpOnly": True},
        session_id="chrome:7",
    )
    assert out["status"] == "ok"
    assert out["results"][0]["method"] == "cdp"
    assert "cdp_error" not in out["results"][0]
    payload, client_id, _timeout = driver.ext_calls[0]
    assert payload["cmd"] == "cdp"
    assert payload["method"] == "Network.setCookie"
    assert payload["tabId"] == 7
    assert payload["params"]["httpOnly"] is True
    assert client_id == "chrome"


def test_delete_cookies_deletes_through_ext_cmd(driver):
    out = S.delete_cookies("k", url="https://x.test/", session_id="chrome:7")
    assert out["status"] == "ok" and out["method"] == "cdp"
    payload, _client_id, _timeout = driver.ext_calls[0]
    assert payload["cmd"] == "cdp" and payload["method"] == "Network.deleteCookies"


def test_cdp_helper_honours_an_explicit_tab_id_over_the_session_tab(driver):
    S._cdp("Network.deleteCookies", {"name": "k"}, "chrome:7", 9, 5.0)
    payload, _client_id, _timeout = driver.ext_calls[0]
    assert payload["tabId"] == 9


def test_cdp_helper_reports_a_cdp_refusal_as_an_error(driver):
    driver.reply = {"data": {"ok": False, "code": "cdp_error", "error": "nope"}}
    with pytest.raises(RuntimeError, match="cdp_error: nope"):
        S._cdp("Network.setCookie", {"name": "k"}, "chrome:7", None, 5.0)


def test_cdp_batch_rejects_a_non_object_document(driver):
    with pytest.raises(ValueError, match="JSON object"):
        S.cdp_batch("[1, 2]")


def test_extension_batch_falls_back_to_text_only_for_an_unknown_command(monkeypatch):
    """Only an explicit old-router refusal proves the batch did not run."""
    sent = []

    class OldRouter(_Driver):
        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            raise RuntimeError("Unknown cmd: batch")

    driver = OldRouter(None)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: "chrome:7")
    monkeypatch.setattr(
        S, "exec_js",
        lambda script, session_id=None, timeout=15.0: sent.append((script, session_id)) or {"data": []},
    )
    out = S._extension_batch({"cmd": "batch", "commands": []}, session_id="chrome:7", timeout=5.0)
    assert out == {"data": []}
    assert sent and sent[0][1] == "chrome:7"


def test_extension_batch_never_replays_after_a_timeout(monkeypatch):
    class Slow(_Driver):
        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            raise TimeoutError("extension did not respond")

    driver = Slow(None)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: "chrome:7")
    monkeypatch.setattr(S, "exec_js", driver.execute_js)
    with pytest.raises(TimeoutError):
        S._extension_batch({"cmd": "batch", "commands": []}, session_id="chrome:7", timeout=5.0)
