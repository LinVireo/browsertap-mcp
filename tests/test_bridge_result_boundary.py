"""Bridge recovery preserves execution outcomes and browser identity."""

from __future__ import annotations

import errno
import os
from types import SimpleNamespace

import pytest

from browsertap_mcp import browser_bridge as B
from browsertap_mcp import requester_liveness as L
from browsertap_mcp.pending_operations import PendingOperations
from tests.test_browser_bridge_coverage import driver_stub


@pytest.mark.parametrize("timeout", [-1, 121, float("nan"), float("inf")])
def test_bridge_result_query_rejects_invalid_budget(timeout):
    with pytest.raises(ValueError):
        driver_stub().get_execute_js_result("op", timeout, requester_id="owner")


def test_bridge_result_query_rejects_empty_handle():
    with pytest.raises(ValueError):
        driver_stub().get_execute_js_result(" ", requester_id="owner")


@pytest.mark.parametrize("payload", [None, [], "not-a-result"])
def test_remote_result_query_rejects_malformed_envelopes(payload):
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *a, **kw: {"r": payload}
    with pytest.raises(RuntimeError, match="malformed"):
        driver.get_execute_js_result("op", requester_id="owner")


@pytest.mark.parametrize("success", [True, False])
def test_retained_results_preserve_errors_and_new_tab_metadata(success):
    driver = driver_stub()
    operations = driver._operation_state()
    operations.reserve("op", ["browser:7"], "owner")
    wire = {
        "success": success, "data": 7 if success else {"message": "failed in page"},
        "tabId": 7, "newTabs": [{"id": 9, "url": "https://example.test/", "ts": 123}],
    }
    operations.complete("op", wire)
    first = driver.get_execute_js_result("op", requester_id="owner")
    assert first == driver.get_execute_js_result("op", requester_id="owner")
    assert first["reservation_held"] is False
    if success:
        assert first["data"] == 7 and first["executed_tab_id"] == 7
        assert first["newTabs"] == [{"id": 9, "url": "https://example.test/"}]
        assert wire["newTabs"][0]["ts"] == 123
    else:
        assert first["status"] == "failed"
        assert "failed in page" in first["error"]


def test_pending_unacknowledged_result_is_not_retryable():
    driver = driver_stub()
    driver._operation_state().reserve("op", ["browser:7"], "owner")
    result = driver.get_execute_js_result("op", requester_id="owner")
    assert result["status"] == "in_progress"
    assert result["delivery_state"] == "sent_unconfirmed"
    assert result["retry_safe"] is False
    assert result["reservation_held"] is True


@pytest.mark.parametrize("remote", [False, True])
def test_browser_selection_refuses_missing_or_ambiguous_clients(remote):
    driver = driver_stub(remote=remote)
    driver._remote_cmd = lambda *a, **kw: {"r": []}
    with pytest.raises(B.ExtensionNotConnectedError):
        driver.select_client_id()
    driver.ext_clients = {"first": {}, "second": {}}
    driver._remote_cmd = lambda *a, **kw: {"r": [{"client_id": "first"}, {"client_id": "second"}]}
    with pytest.raises(B.AmbiguousBrowserError):
        driver.select_client_id()
    assert driver.select_client_id("explicit") == "explicit"


def test_remote_default_selection_prefers_a_scriptable_active_tab():
    driver = driver_stub(remote=True)
    driver.select_client_id = lambda: "browser"
    driver.get_all_sessions = lambda: [
        {"id": "browser:1", "url": "chrome://extensions", "active": True},
        {"id": "browser:7", "url": "https://example.test/", "active": True},
        {"id": "other:8", "url": "https://other.test/"},
    ]
    assert driver._live_default_session_id() == "browser:7"
    driver.default_session_id = None
    driver.get_all_sessions = lambda: []
    assert driver._live_default_session_id() is None


@pytest.mark.parametrize("value", [10**400, -(10**400), float("nan"), True, "1"])
def test_unusable_peer_timestamps_never_refresh_liveness(value):
    assert B._timestamp(value) is None


def test_unparseable_url_and_non_string_script_intent_do_not_leak_content():
    assert B.redact_url("https://[broken:password@host/") == "<unparsable url>"
    assert B._page_script_intent(None) is None
    message, fields = B._page_error_source({"error": {"code": "blocked"}, "message": "outer error"})
    assert message == "outer error" and fields == {"code": "blocked"}


def test_navigation_access_failure_provides_an_actionable_non_retryable_result():
    code, retry, diagnostics = B._page_execution_metadata(
        {"error": "Cannot access contents of the page", "csp": True},
        script="window.open('https://example.test/')",
    )
    assert code == "page_access_denied" and retry is False
    assert diagnostics["next_action"] == "open_new_tab_or_choose_scriptable_tab"
    assert diagnostics["csp"] is True


def test_failed_liveness_probe_cannot_authorize_recovery():
    def failed(owner):
        raise OSError("liveness unavailable")
    operations = PendingOperations(requester_liveness=failed)
    assert operations._owner_is_dead(None) is False
    assert operations._owner_is_dead("owner") is False


def test_reply_without_websocket_owner_is_never_accepted():
    operations = PendingOperations()
    operations.reserve("op", ["browser:7"], "owner", reply_transport="ws")
    assert operations.accepts_reply("op", "ws", object()) is False
    assert operations.complete("missing", {"success": True, "data": 1}) is False
    operations.acknowledge("missing")
    operations.forget("missing")
    assert operations.pending_ids() == ["op"]


@pytest.mark.parametrize("stage", ["lock", "write", "sync"])
def test_failed_requester_publication_keeps_identity_untracked_and_releases_fd(monkeypatch, tmp_path, stage):
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(L, "_IDENTITIES", {})
    real_open = os.open
    opened = []
    def tracked_open(*a, **kw):
        descriptor = real_open(*a, **kw)
        opened.append(descriptor)
        return descriptor
    monkeypatch.setattr(L.os, "open", tracked_open)
    if stage == "lock":
        monkeypatch.setattr(L, "_try_lock", lambda descriptor: False)
    elif stage == "write":
        monkeypatch.setattr(L.os, "write", lambda *a: 0)
    else:
        def failed(*a):
            raise OSError(errno.EIO, "sync failed")
        monkeypatch.setattr(L.os, "fsync", failed)
    identity = L.process_requester_id()
    assert identity.startswith("mcp-untracked-v1:")
    assert L.requester_liveness(identity) == "unknown"
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_after_fork_drops_inherited_identity_descriptors(monkeypatch, tmp_path):
    descriptor = os.open(tmp_path / "inherited", os.O_CREAT | os.O_RDWR, 0o600)
    identities = {(1, "state"): ("parent", descriptor), (2, "other"): ("untracked", None)}
    monkeypatch.setattr(L, "_IDENTITIES", identities)
    lock = L._IDENTITY_LOCK
    L._after_fork()
    assert identities == {}
    assert L._IDENTITY_LOCK is not lock
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("error", [None, errno.EACCES, errno.EIO])
def test_lock_failure_distinguishes_contention_from_io_failure(monkeypatch, tmp_path, platform, error):
    descriptor = os.open(tmp_path / "locked", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        def failed(*args):
            if error is not None:
                raise OSError(error, "lock storage failed")
        monkeypatch.setattr(L.sys, "platform", platform)
        monkeypatch.setitem(L.sys.modules, "msvcrt", SimpleNamespace(locking=failed, LK_NBLCK=1))
        monkeypatch.setitem(L.sys.modules, "fcntl", SimpleNamespace(flock=failed, LOCK_EX=2, LOCK_NB=4))
        if error == errno.EIO:
            with pytest.raises(OSError, match="lock storage failed"):
                L._try_lock(descriptor)
        else:
            assert L._try_lock(descriptor) is (error is None)
    finally:
        os.close(descriptor)
