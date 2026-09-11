"""Focused offline branch coverage for server orchestration helpers."""

from __future__ import annotations

import base64
import json
import logging
import runpy
import socket
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


@pytest.mark.parametrize("value", [None, "not-a-number", {}])
def test_invalid_timeout_conversion_names_the_parameter(value):
    with pytest.raises(ValueError, match="body_timeout must be a finite number greater than zero"):
        S._positive_timeout(value, name="body_timeout")


def test_invalid_bridge_port_fails_lazily_without_caching_the_bad_value(monkeypatch):
    monkeypatch.setattr(S, "_DRIVER_PORT", None)
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "invalid")
    with pytest.raises(ValueError, match="BROWSERTAP_BRIDGE_PORT must be an integer"):
        S._get_driver_port()
    assert S._DRIVER_PORT is None
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "19000")
    assert S._get_driver_port() == 19000


def test_stdio_logging_configuration_is_idempotent(monkeypatch):
    logger = logging.getLogger("browsertap_mcp")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.NOTSET)
    monkeypatch.setattr(logger, "propagate", True)
    S.configure_stdio_logging()
    handler = logger.handlers[0]
    S.configure_stdio_logging()
    assert logger.handlers == [handler]
    assert handler.stream is S.sys.stderr
    assert logger.propagate is False


def test_server_module_initializes_a_driver_before_running_stdio(monkeypatch):
    from browsertap_mcp import browser_bridge

    calls = []
    driver = SimpleNamespace()
    logger = logging.getLogger("browsertap_mcp")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    monkeypatch.setenv("BROWSERTAP_NO_SPAWN", "1")
    monkeypatch.setenv("BROWSERTAP_BRIDGE_HOST", "127.0.0.8")
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "19000")
    monkeypatch.setattr(browser_bridge, "BrowserBridge", lambda **kwargs: calls.append(("driver", kwargs)) or driver)
    monkeypatch.setattr(S.FastMCP, "run", lambda self, **kwargs: calls.append(("run", kwargs)))
    monkeypatch.delitem(S.sys.modules, S.__name__)

    namespace = runpy.run_module(S.__name__, run_name="__main__", alter_sys=True)

    assert calls == [
        ("driver", {"host": "127.0.0.8", "port": 19000}),
        ("run", {"transport": "stdio"}),
    ]
    assert namespace["_driver"] is driver
    assert logger.handlers[0].stream is S.sys.stderr


@pytest.mark.parametrize("payload", [None, [1, 2], "plain result"])
def test_result_envelope_preserves_non_object_values(payload):
    result = S._result_envelope("probe", payload)
    assert result["ok"] is True
    assert result["data"] == payload
    assert result["target"] is None


def test_stale_target_error_survives_failure_to_refresh_tabs(monkeypatch):
    def unavailable(**kwargs):
        raise OSError("bridge unavailable")

    monkeypatch.setattr(S, "active_sessions", unavailable)
    error = S._session_target_not_found("client:old")
    assert error.diagnostics["stale_session_id"] == "client:old"
    assert error.diagnostics["replacement_candidates"] == []


def test_exception_target_survives_an_unbindable_callable_signature():
    assert S._call_target(lambda: None, (), {"session_id": "client:7"}) == {
        "session_id": "client:7", "client_id": "client", "tab_id": 7,
    }


def test_legacy_result_without_a_status_does_not_hide_an_explicit_error():
    assert S._legacy_failure({"error": "command failed"}) == (
        "operation_failed", "command failed", False,
    )
    assert S._extension_data(None) == {}


def test_dialog_normalization_keeps_the_error_and_last_dialog_without_a_history():
    error = {"code": "dialog_blocked", "message": "waiting for confirmation"}
    dialog = {"type": "confirm", "message": "Continue?"}
    wrapped = {
        "__btap_dialog_result": True, "status": "blocked_by_dialog", "value": None,
        "error": error, "dialog": dialog, "pending_execution": True,
    }
    result = S._normalize_execute_js_dialog_result({
        "status": "success", "js_return": wrapped, "operation_id": "dialog-operation",
    })

    assert result["status"] == "blocked_by_dialog"
    assert result["error"] == error and result["error"] is not error
    assert result["dialog"] == dialog and result["dialog"] is not dialog
    assert result["reservation_held"] is True
    assert result["poll_with"] == "get_execute_js_result"
    assert result["js_return"] is None


def test_extension_failure_derives_retryability_from_undelivered_evidence():
    result = S._extension_operation_result({"data": {
        "status": "failed", "error": "route unavailable",
        "diagnostics": {"retry_safe": True, "delivery_state": "undelivered"},
    }}, operation="capture")

    assert result["status"] == "failed"
    assert result["retryable"] is True
    assert result["error"] == "route unavailable"
    assert result["operation"] == "capture"


@pytest.mark.parametrize("session_id", [":7", "client:invalid"])
def test_composite_target_requires_a_client_and_numeric_tab_id(session_id):
    with pytest.raises(ValueError, match="invalid composite session id"):
        S._split_session_target(session_id)


@pytest.mark.parametrize("tool, kwargs, message", [
    ("create_bookmark", {"title": "folder", "url": "  "}, "url must not be empty"),
    ("create_bookmark", {"title": "folder", "parent_id": "  "}, "parent_id must not be empty"),
    ("call_extension", {"extension_id": "  ", "message_json": "{}"}, "extension_id must not be empty"),
    ("network_capture_start", {"max_body_bytes": 1023}, "max_body_bytes"),
    ("network_capture_start", {"max_body_bytes": 2097153}, "max_body_bytes"),
    ("network_capture_start", {"body_timeout": 0.09}, "body_timeout"),
    ("network_capture_start", {"body_timeout": 10.1}, "body_timeout"),
    ("network_capture_stop", {"status_max": 600}, "status_max"),
    ("network_capture_stop", {"status_min": 400, "status_max": 200}, "status_min must not exceed"),
    ("get_console_messages", {"max_items": 0}, "max_items"),
    ("get_console_messages", {"max_items": 1001}, "max_items"),
    ("get_console_messages", {"filter": "errors"}, "filter must be"),
    ("page_click", {"selector": {"x": 1, "y": 2}, "x": 3, "y": 4}, "top-level coordinates"),
    ("page_click", {"selector": {"x": 1, "y": 2}, "offset_x": 3}, "cannot use offset"),
])
def test_invalid_tool_inputs_are_rejected_before_accessing_the_browser(monkeypatch, tool, kwargs, message):
    monkeypatch.setattr(S, "require_driver", lambda: pytest.fail("invalid input must not access the bridge"))
    with pytest.raises(ValueError, match=message):
        getattr(S, tool)(**kwargs)


def test_bookmark_without_a_url_creates_a_folder(monkeypatch):
    calls = []
    driver = SimpleNamespace(ext_cmd=lambda payload, **kwargs: calls.append((payload, kwargs)) or {
        "data": {"ok": True, "data": {"id": "folder-1"}},
    })
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    result = S.create_bookmark(" Folder ", session_id="client:7")
    assert result["data"]["id"] == "folder-1"
    assert calls[0][0] == {"cmd": "bookmarks", "method": "create", "node": {"title": "Folder"}}
    assert calls[0][1]["client_id"] == "client"


def test_console_user_filter_uses_and_retains_the_implicit_selected_tab(monkeypatch):
    driver = _install_page_driver(monkeypatch)
    calls = []
    driver.ext_cmd = lambda payload, **kwargs: calls.append((payload, kwargs)) or {"data": {"messages": []}}
    result = S.get_console_messages(filter=" USER ")
    assert result["messages"] == []
    assert calls[0][0]["filter"] == "user"
    assert calls[0][0]["tabId"] == 1
    assert driver.default_session_id == "client:1"


@pytest.mark.parametrize("tab_id", [None, 8])
def test_cookie_read_preserves_an_explicit_tab_override_and_browser_failure(monkeypatch, tab_id):
    calls = []
    driver = SimpleNamespace(ext_cmd=lambda payload, **kwargs: calls.append((payload, kwargs)) or {
        "data": {"ok": False, "error": "cookies permission denied"},
    })
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    with pytest.raises(RuntimeError, match="cookies permission denied"):
        S.get_cookies(session_id="client:7", tab_id=tab_id)
    assert calls[0][0]["tabId"] == (7 if tab_id is None else tab_id)
    assert calls[0][1]["client_id"] == "client"


@pytest.mark.parametrize("marker", [b"\xff\x01", b"\xff\xd0", b"\xff\xd7"])
def test_jpeg_header_skips_standalone_markers_before_the_frame(marker):
    frame = b"\xff\xc2\x00\x11\x08\x00\x11\x00\x23\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    assert S._parse_image_header(b"\xff\xd8" + marker + frame + b"\xff\xd9") == (35, 17)


def test_jpeg_header_rejects_a_broken_marker_chain():
    assert S._parse_image_header(b"\xff\xd8BAD!\x00\x00") is None


def test_extended_webp_header_decodes_twenty_four_bit_canvas_dimensions():
    header = (b"RIFF" + (22).to_bytes(4, "little") + b"WEBPVP8X"
              + (10).to_bytes(4, "little") + b"\x00" * 4
              + (65536).to_bytes(3, "little") + (512).to_bytes(3, "little"))
    assert S._parse_image_header(header) == (65537, 513)


def test_switch_session_rejects_ambiguous_url_matches(monkeypatch):
    driver = SimpleNamespace(default_session_id="client:old")
    sessions = [
        {"id": "chrome:a:1", "browser": "chrome", "url": "https://example.test/one"},
        {"id": "chrome:a:2", "browser": "chrome", "url": "https://example.test/two"},
    ]
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda *args, **kwargs: sessions)
    driver.set_session = lambda pattern: (_ for _ in ()).throw(
        ValueError(
            f"URL pattern {pattern!r} matched 2 sessions: chrome:a:1, chrome:a:2. "
            "Pass the full session_id to select one."
        )
    )

    with pytest.raises(ValueError, match="matched 2 sessions"):
        S.switch_session(url_pattern="example.test")
    with pytest.raises(RuntimeError, match="matched 2 tabs") as exc:
        S.switch_session(browser="chrome", url_pattern="example.test")
    assert "chrome:a:1" in str(exc.value)
    assert "full session_id" in str(exc.value)
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize("directed", [False, True])
def test_switch_session_selects_a_unique_match_or_the_only_available_browser(monkeypatch, directed):
    driver = SimpleNamespace(default_session_id=None)
    sessions = [{"id": "chrome:7", "browser": "chrome", "url": "https://example.test/one"}]
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda **kwargs: sessions)
    monkeypatch.setattr(S, "ensure_sessions", lambda **kwargs: sessions)
    monkeypatch.setenv("BROWSERTAP_PREFERRED_BROWSER", "edge")

    target = S.switch_session(**({"browser": "chrome", "url_pattern": "/one"} if directed else {}))

    assert target == "chrome:7"
    assert driver.default_session_id == "chrome:7"


def test_switch_session_accepts_bridge_confirmed_replacement(monkeypatch):
    driver = SimpleNamespace(default_session_id="client:old")
    driver.resolve_session_target = lambda sid: {
        "session_id": "client:new",
        "rebound_from": sid,
        "replacement_session_id": "client:new",
        "tab_identity": "tab-a",
        "reason": "chrome.tabs.onReplaced",
    } if sid == "client:old" else None
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda *args, **kwargs: [{"id": "client:new", "tab_identity": "tab-a"}],
    )

    assert S.switch_session(session_id="client:old") == "client:new"
    assert driver.default_session_id == "client:new"


def test_execute_js_uses_replacement_for_policy_but_reports_rebound(monkeypatch):
    driver = SimpleNamespace(default_session_id="client:11", calls=[])
    old_sid = "client:11"
    new_sid = "client:12"
    driver.resolve_session_target = lambda sid: {
        "session_id": new_sid,
        "rebound_from": old_sid,
        "replacement_session_id": new_sid,
        "tab_identity": "tab-a",
        "reason": "chrome.tabs.onReplaced",
    } if sid == old_sid else {"session_id": str(sid)}

    def ext_cmd(payload, *, client_id=None, timeout=15.0):
        driver.calls.append((payload, client_id))
        if payload["cmd"] == "set_dialog_policy":
            return {"data": {"token": "scope-ready"}}
        return {"data": {"ok": True}}

    driver.ext_cmd = ext_cmd
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "ensure_sessions",
        lambda *args, **kwargs: [{"id": new_sid, "url": "https://example.test/"}],
    )
    execute_calls = []

    def execute(*args, **kwargs):
        execute_calls.append(kwargs)
        return {
            "status": "success",
            "js_return": 2,
            "tab_id": 12,
            "rebound_from": old_sid,
            "replacement_session_id": new_sid,
            "tab_identity": "tab-a",
            "rebind_reason": "chrome.tabs.onReplaced",
        }

    monkeypatch.setattr(S.simphtml, "execute_js_rich", execute)

    result = S.execute_js("1 + 1", session_id=old_sid, no_monitor=True)

    assert result["status"] == "success"
    assert [call[0]["cmd"] for call in driver.calls] == [
        "set_dialog_policy",
        "clear_dialog_policy",
    ]
    assert [call[0]["tabId"] for call in driver.calls] == [12, 12]
    assert execute_calls[0]["session_id"] == old_sid
    assert result["tab_id"] == 12
    assert result["rebound_from"] == old_sid


def test_switch_session_browser_url_pattern_requires_a_match(monkeypatch):
    driver = SimpleNamespace(default_session_id="client:old")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda *args, **kwargs: [
            {"id": "edge:a:1", "browser": "edge", "url": "https://other.test/"}
        ],
    )

    with pytest.raises(RuntimeError, match="No connected tab.*matches URL pattern"):
        S.switch_session(browser="edge", url_pattern="wanted.test")
    assert driver.default_session_id == "client:old"


def _install_page_driver(monkeypatch, *, default_session_id="client:old"):
    driver = SimpleNamespace(default_session_id=default_session_id)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "ensure_sessions",
        lambda *args, **kwargs: [{"id": "client:1", "url": "https://example.test/"}],
    )

    def switch_session(session_id=None, **_kwargs):
        selected = str(session_id) if session_id is not None else "client:1"
        driver.default_session_id = selected
        return selected

    monkeypatch.setattr(S, "switch_session", switch_session)
    monkeypatch.setattr(S, "compact_tabs", lambda *args, **kwargs: [{"id": "client:1"}])
    return driver


def _monotonic(monkeypatch, values):
    timeline = iter(values)
    monkeypatch.setattr(S.time, "monotonic", lambda: next(timeline, values[-1]))


@pytest.mark.parametrize("result", [0, 10061])
def test_port_probe_closes_its_socket_and_reports_connection_status(monkeypatch, result):
    calls = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def settimeout(self, timeout):
            calls.append(timeout)

        def connect_ex(self, address):
            calls.append(address)
            return result

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: Probe())
    assert S._port_open("127.0.0.8", 19000) is (result == 0)
    assert calls == [1, ("127.0.0.8", 19000), "closed"]


def test_server_skill_path_resolves_both_packaged_calling_guides():
    for name in ("browsertap-default", "browsertap-bridge-recovery"):
        assert (S.agent_skills_dir() / name / "SKILL.md").is_file()


@pytest.mark.parametrize("rotation_fails", [False, True])
def test_spawn_log_rotation_preserves_the_old_log_when_rename_fails(monkeypatch, tmp_path, rotation_fails):
    log = tmp_path / "bridge.log"
    log.write_bytes(b"old trace")
    monkeypatch.setattr(S, "state_dir", lambda **kwargs: tmp_path)
    monkeypatch.setattr(S.bridge_module, "LOG_MAX_BYTES", 4)
    if rotation_fails:
        def fail_replace(*args):
            raise PermissionError("log still open")

        monkeypatch.setattr(S.Path, "replace", fail_replace)
    assert S._bridge_log_path() == log
    assert (log if rotation_fails else log.with_suffix(".log.old")).read_bytes() == b"old trace"
    assert log.exists() is rotation_fails


def test_spawn_lock_stands_down_when_a_stale_lock_cannot_be_removed(monkeypatch, tmp_path):
    lock = tmp_path / "spawn.lock"
    lock.write_text("123", encoding="utf-8")
    monkeypatch.setattr(S, "_spawn_lock_path", lambda: lock)
    monkeypatch.setattr(S.time, "time", lambda: lock.stat().st_mtime + S._SPAWN_LOCK_STALE + 1)

    def fail_unlink(*args, **kwargs):
        raise PermissionError("lock is busy")

    monkeypatch.setattr(S.Path, "unlink", fail_unlink)
    assert S._acquire_spawn_lock() is None
    assert lock.read_text() == "123"


def test_spawn_lock_write_failure_retains_the_exclusive_claim(monkeypatch, tmp_path):
    lock = tmp_path / "spawn.lock"
    monkeypatch.setattr(S, "_spawn_lock_path", lambda: lock)

    def fail_write(*args):
        raise OSError("write failed")

    monkeypatch.setattr(S.os, "write", fail_write)
    assert S._acquire_spawn_lock() == lock
    assert lock.read_bytes() == b""
    assert S._acquire_spawn_lock() is None


@pytest.mark.parametrize("reset", [False, True])
def test_spawn_lock_cleanup_failure_does_not_mask_the_failed_launch(monkeypatch, reset):
    cleanups = []

    def unlink(**kwargs):
        cleanups.append(kwargs)
        raise PermissionError("lock is busy")

    lock = SimpleNamespace(unlink=unlink)
    monkeypatch.setattr(S, "_spawn_lock_path", lambda: lock)
    monkeypatch.setattr(S, "_acquire_spawn_lock", lambda: lock)
    monkeypatch.setattr(S, "_spawn_bridge_daemon_locked", lambda: False)
    assert S.spawn_bridge_daemon(reset_spawn_lock=reset) is False
    assert cleanups == [{"missing_ok": True}] * (2 if reset else 1)


def test_spawn_rechecks_the_port_after_winning_the_lock(monkeypatch):
    monkeypatch.setattr(S, "_port_open", lambda *args: True)
    monkeypatch.setattr(S.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("already running"))
    assert S._spawn_bridge_daemon_locked() is True


@pytest.mark.parametrize("platform, gui, ready", [
    ("linux", False, True), ("win32", False, True), ("win32", True, False),
])
def test_detached_spawn_uses_platform_flags_and_a_bounded_readiness_wait(
    monkeypatch, tmp_path, platform, gui, ready,
):
    executable = tmp_path / "python.exe"
    if gui:
        (tmp_path / "pythonw.exe").touch()
    calls, sleeps = [], []
    checks = iter([False, False, ready])
    monkeypatch.setattr(S.sys, "platform", platform)
    monkeypatch.setattr(S.sys, "executable", str(executable))
    monkeypatch.setattr(S, "_port_open", lambda *args: next(checks, ready))
    monkeypatch.setattr(S, "_bridge_log_path", lambda: tmp_path / "bridge.log")
    monkeypatch.setattr(S.subprocess, "Popen", lambda cmd, **kwargs: calls.append((cmd, kwargs)))
    monkeypatch.setattr(S.time, "sleep", sleeps.append)
    _monotonic(monkeypatch, [0, 0, 1, 9])
    assert S._spawn_bridge_daemon_locked() is ready
    command, options = calls[0]
    assert command[0] == str(tmp_path / ("pythonw.exe" if gui else "python.exe"))
    assert options["close_fds"] is True
    if platform == "win32":
        assert options["creationflags"] & 0x08000000
    else:
        assert options["start_new_session"] is True
        assert "creationflags" not in options
    assert sleeps == [0.25] * (1 if ready else 2)


def test_driver_initialization_reuses_the_instance_published_while_waiting_for_the_lock(monkeypatch):
    winner = object()

    class InitializationLock:
        def __enter__(self):
            S._driver = winner

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(S, "_driver", None)
    monkeypatch.setattr(S, "_DRIVER_LOCK", InitializationLock())
    monkeypatch.setattr(S, "BrowserBridge", lambda **kwargs: pytest.fail("must reuse the winner"))
    assert S.get_driver() is winner


def test_ensure_sessions_does_not_prune_the_default_when_no_tabs_are_connected(monkeypatch):
    monkeypatch.setattr(S, "active_sessions", lambda **kwargs: [])
    monkeypatch.setattr(S, "prune_stale_default", lambda: pytest.fail("no target to prune"))
    with pytest.raises(RuntimeError, match="No connected browser tabs"):
        S.ensure_sessions()


def test_exec_js_returns_a_successful_reply_without_replaying_or_changing_the_default(monkeypatch):
    calls = []
    driver = SimpleNamespace(default_session_id="client:old", execute_js=lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"data": 7}
    ))
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    assert S.exec_js("return 7", session_id="client:1") == {"data": 7}
    assert len(calls) == 1
    assert calls[0][1]["session_id"] == "client:1"
    assert driver.default_session_id == "client:old"


def test_exec_js_exhausted_deadline_prevents_dispatch(monkeypatch):
    driver = SimpleNamespace(execute_js=lambda *args, **kwargs: pytest.fail("deadline expired"))
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    _monotonic(monkeypatch, [0, 2])
    with pytest.raises(TimeoutError, match="deadline exhausted before dispatch"):
        S.exec_js("return 7", timeout=1)


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("read-only")])
def test_spawn_lock_permission_and_os_errors_stand_down(monkeypatch, tmp_path, error):
    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        S.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    assert S._acquire_spawn_lock() is None


def test_spawn_bridge_waiter_times_out_without_starting_another_daemon(monkeypatch):
    sleeps = []
    monkeypatch.setattr(S, "_acquire_spawn_lock", lambda: None)
    monkeypatch.setattr(S, "_port_open", lambda *_args: False)
    monkeypatch.setattr(S.time, "sleep", sleeps.append)
    _monotonic(monkeypatch, [0.0, 0.0, 11.0])

    assert S.spawn_bridge_daemon() is False
    assert sleeps == [0.25]


def test_spawn_bridge_failed_daemon_start_releases_lock(monkeypatch, tmp_path):
    lock = tmp_path / "spawn.lock"
    lock.write_text("owner", encoding="utf-8")
    monkeypatch.setattr(S, "_acquire_spawn_lock", lambda: lock)
    monkeypatch.setattr(S, "_spawn_bridge_daemon_locked", lambda: False)

    assert S.spawn_bridge_daemon() is False
    assert not lock.exists()


def test_spawn_bridge_locked_maps_process_start_oserror(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_port_open", lambda *_args: False)
    monkeypatch.setattr(S, "_bridge_log_path", lambda: tmp_path / "bridge.log")
    monkeypatch.setattr(
        S.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("blocked")),
    )

    assert S._spawn_bridge_daemon_locked() is False


def test_spawn_bridge_quotes_dash_prefixed_instance_id(monkeypatch, tmp_path):
    commands = []
    port_checks = iter([False, True])
    monkeypatch.setattr(S, "_port_open", lambda *_args: next(port_checks))
    monkeypatch.setattr(S, "_bridge_log_path", lambda: tmp_path / "bridge.log")
    monkeypatch.setattr(S.secrets, "token_urlsafe", lambda _size: "-leading-token")
    monkeypatch.setattr(
        S.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(),
    )

    assert S._spawn_bridge_daemon_locked() is True
    assert "--instance-id=-leading-token" in commands[0]
    assert "--instance-id" not in commands[0]


@pytest.mark.parametrize(
    ("no_spawn", "port_open", "expected_spawns"),
    [(None, False, 1), ("1", False, 0), (None, True, 0)],
)
def test_get_driver_bootstrap_and_cache_branches(
    monkeypatch, no_spawn, port_open, expected_spawns
):
    calls = []

    class FakeDriver:
        def __init__(self, *, host, port):
            calls.append(("driver", host, port))

    monkeypatch.setattr(S, "_driver", None)
    monkeypatch.setattr(S, "_port_open", lambda *_args: port_open)
    monkeypatch.setattr(
        S,
        "spawn_bridge_daemon",
        lambda: calls.append(("spawn",)) or False,
    )
    monkeypatch.setattr(S, "BrowserBridge", FakeDriver)
    if no_spawn is None:
        monkeypatch.delenv("BROWSERTAP_NO_SPAWN", raising=False)
    else:
        monkeypatch.setenv("BROWSERTAP_NO_SPAWN", no_spawn)

    first = S.get_driver()
    second = S.get_driver()

    assert first is second
    assert len([call for call in calls if call[0] == "driver"]) == 1
    assert calls.count(("spawn",)) == expected_spawns


@pytest.mark.parametrize(
    ("is_remote", "no_spawn", "port_open", "expected_spawns"),
    [
        (True, None, False, 1),
        (False, None, False, 0),
        (True, "1", False, 0),
        (True, None, True, 0),
    ],
)
def test_require_driver_recovery_branches(
    monkeypatch, is_remote, no_spawn, port_open, expected_spawns
):
    driver = SimpleNamespace(is_remote=is_remote)
    calls = []
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "_port_open", lambda *_args: port_open)
    monkeypatch.setattr(
        S,
        "spawn_bridge_daemon",
        lambda: calls.append("spawn") or False,
    )
    if no_spawn is None:
        monkeypatch.delenv("BROWSERTAP_NO_SPAWN", raising=False)
    else:
        monkeypatch.setenv("BROWSERTAP_NO_SPAWN", no_spawn)

    assert S.require_driver() is driver
    assert calls == ["spawn"] * expected_spawns


def test_scan_page_returns_links_and_background_visibility_hint(monkeypatch):
    driver = _install_page_driver(monkeypatch)

    def get_html(_driver, **kwargs):
        kwargs["link_refs"]["https://example.test/long/path"] = "r1"
        return "<main>ok</main><!--btap-offscreen:4 scrollY:0 viewH:0 docH:9000-->"

    monkeypatch.setattr(S.simphtml, "get_html", get_html)

    result = S.scan_page(session_id="client:1")

    assert result["status"] == "success"
    assert result["links"] == {"r1": "https://example.test/long/path"}
    assert result["offscreen"]["viewport_height"] == 0
    assert "text_only=true" in result["hint"]
    assert "execute_js" in result["hint"]
    assert "Only call activate_tab when" in result["hint"]
    assert driver.default_session_id == "client:old"


def test_scan_page_text_only_pins_implicit_session_and_reports_scrolling_hint(monkeypatch):
    driver = _install_page_driver(monkeypatch, default_session_id=None)
    seen = {}

    def get_html(_driver, **kwargs):
        seen.update(kwargs)
        return "text<!--btap-offscreen:3 scrollY:500 viewH:700 docH:9000-->"

    monkeypatch.setattr(S.simphtml, "get_html", get_html)

    result = S.scan_page(text_only=True)

    assert result["active_session_id"] == "client:1"
    assert "scroll_page" in result["hint"]
    assert "links" not in result
    assert seen["link_refs"] is None
    assert driver.default_session_id == "client:1"


@pytest.mark.parametrize(
    ("probe", "expected_state", "expected_ready"),
    [
        ({"ready_state": "complete", "has_body": True, "text_chars": 0, "html_chars": 317000, "loading": False}, "shell_only", False),
        ({"ready_state": "complete", "has_body": True, "text_chars": 8, "html_chars": 317000, "loading": False}, "shell_only", False),
        ({"ready_state": "complete", "has_body": True, "text_chars": 4, "html_chars": 20, "loading": True}, "hydrating", False),
        ({"ready_state": "complete", "has_body": True, "text_chars": 42, "html_chars": 120, "loading": False}, "content", True),
    ],
)
def test_scan_page_reports_spa_render_readiness(monkeypatch, probe, expected_state, expected_ready):
    driver = _install_page_driver(monkeypatch)
    monkeypatch.setattr(S.simphtml, "get_html", lambda *_args, **_kwargs: "<main></main>")
    driver.execute_js = lambda *_args, **_kwargs: {"data": probe}

    result = S.scan_page(session_id="client:1")

    assert result["render_state"] == expected_state
    assert result["content_ready"] is expected_ready
    assert result["render"]["html_chars"] == probe["html_chars"]
    if not expected_ready:
        assert "Retry scan_page" in result["hint"]


def test_scan_page_classifies_page_unavailable_and_restores_target(monkeypatch):
    driver = _install_page_driver(monkeypatch)
    monkeypatch.setattr(
        S.simphtml,
        "get_html",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(S.simphtml.PageUnavailable("page slept")),
    )

    result = S.scan_page(session_id="client:1")

    assert result["status"] == "no_response"
    assert result["error"] == "page slept"
    assert result["active_session_id"] == "client:1"
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize(
    ("condition", "gone", "needle"),
    [
        ({"text": "ready"}, False, "innerText"),
        ({"selector": "#gone"}, True, "!("),
        ({"url_pattern": "example"}, False, "RegExp"),
        ({"js": "window.ready"}, False, "window.ready"),
    ],
)
def test_wait_for_condition_success_paths(monkeypatch, condition, gone, needle):
    driver = _install_page_driver(monkeypatch)
    scripts = []
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.1])

    def exec_js(script, **kwargs):
        scripts.append((script, kwargs))
        return {"data": json.dumps({"met": True, "url": "https://example.test/", "title": "Done"})}

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for(**condition, gone=gone, timeout=1, session_id="client:1")

    assert result["status"] == "success"
    assert result["condition"].endswith(" gone") is gone
    assert needle in scripts[0][0]
    assert scripts[0][1]["session_id"] == "client:1"
    assert driver.default_session_id == "client:old"


def test_wait_for_structured_locator_preserves_timeout_details(monkeypatch):
    _install_page_driver(monkeypatch)
    monkeypatch.setattr(S, "normalize_locator", lambda _value: {"role": "button"})
    monkeypatch.setattr(S, "locator_query_script", lambda _value: "LOCATE_BUTTON()")
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0])
    scripts = []

    def exec_js(script, **_kwargs):
        scripts.append(script)
        return {
            "data": json.dumps(
                {
                    "met": False,
                    "url": "https://example.test/",
                    "title": "Waiting",
                    "locator_status": "ambiguous",
                    "matches": 2,
                    "stage": "role",
                    "error": "two matches",
                }
            )
        }

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for(selector={"role": "button"}, gone=True, timeout=1)

    assert result["status"] == "timeout"
    assert result["locator_status"] == "ambiguous"
    assert result["matches"] == 2
    assert result["stage"] == "role"
    assert result["error"] == "two matches"
    assert "located.status === 'not_found'" in scripts[0]


def test_wait_for_retries_page_unload_then_succeeds(monkeypatch):
    driver = _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.2, 0.3, 0.3])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    responses = iter([RuntimeError("page unloaded"), {"data": {"met": True, "url": "u"}}])
    sessions = []

    def exec_js(*_args, **kwargs):
        sessions.append(kwargs["session_id"])
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(S, "exec_js", exec_js)

    assert S.wait_for(text="ready", timeout=1, session_id="client:1")["status"] == "success"
    assert sessions == ["client:1", "client:1"]
    assert driver.default_session_id == "client:old"


def test_wait_for_timeout_reports_repeated_page_failure(monkeypatch):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("page unavailable")),
    )

    result = S.wait_for(text="ready", timeout=1)

    assert result["status"] == "timeout"
    assert "page unavailable" in result["error"]
    assert "hint" in result


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_wait_for_rejects_non_positive_or_non_finite_timeout(timeout):
    with pytest.raises(ValueError, match="finite.*greater than zero"):
        S.wait_for(text="ready", timeout=timeout)


def test_wait_for_retry_sleep_and_bridge_budget_stay_inside_total_deadline(monkeypatch):
    _install_page_driver(monkeypatch)
    clock = SimpleNamespace(now=0.0)
    calls = []
    sleeps = []

    monkeypatch.setattr(S.time, "monotonic", lambda: clock.now)

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    def exec_js(_script, **kwargs):
        calls.append(kwargs["timeout"])
        clock.now += 0.8
        raise RuntimeError("page unavailable")

    monkeypatch.setattr(S.time, "sleep", sleep)
    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for(text="ready", timeout=1.0)

    assert result["status"] == "timeout"
    assert calls == [pytest.approx(1.0)]
    assert sleeps == [pytest.approx(0.2)]
    assert clock.now == pytest.approx(1.0)


def test_wait_for_caps_a_lost_page_chunk_and_retries_within_the_total_deadline(monkeypatch):
    _install_page_driver(monkeypatch)
    clock = SimpleNamespace(now=0.0)
    calls = []
    scripts = []

    monkeypatch.setattr(S.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(S.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))

    def exec_js(script, **kwargs):
        scripts.append(script)
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            clock.now += kwargs["timeout"]
            raise RuntimeError("page unloaded after ACK")
        return {"data": {"met": True, "url": "https://example.test/"}}

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for(text="ready", timeout=30)

    assert result["status"] == "success"
    assert calls == [pytest.approx(6.0), pytest.approx(6.0)]
    assert "new Promise" not in scripts[0]
    assert clock.now == pytest.approx(6.3)


def test_wait_for_url_rejects_empty_pattern():
    with pytest.raises(ValueError, match="url_pattern"):
        S.wait_for_url("")


def test_wait_for_url_keeps_javascript_regex_syntax(monkeypatch):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.1])
    scripts = []

    def exec_js(script, **_kwargs):
        scripts.append(script)
        return {
            "data": json.dumps(
                {"met": True, "url": "https://example.test/done", "title": "Done", "ready": "complete"}
            )
        }

    monkeypatch.setattr(S, "exec_js", exec_js)
    result = S.wait_for_url(r"(?<segment>done)", timeout=1)

    assert result["status"] == "success"
    assert "(?<segment>done)" in scripts[0]
    assert "catch (_) { return location.href.includes(pattern); }" in scripts[0]


@pytest.mark.parametrize("wait_ready", [True, False])
def test_wait_for_url_success_and_ready_policy(monkeypatch, wait_ready):
    driver = _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.1])
    scripts = []

    def exec_js(script, **kwargs):
        scripts.append((script, kwargs))
        return {
            "data": json.dumps(
                {"met": True, "url": "https://example.test/done", "title": "Done", "ready": "complete"}
            )
        }

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for_url("example.test/done", timeout=1, session_id="client:1", wait_ready=wait_ready)

    assert result["status"] == "success"
    assert result["waited_for_ready"] is wait_ready
    assert ("document.readyState === 'complete'" in scripts[0][0]) is wait_ready
    assert scripts[0][1]["session_id"] == "client:1"
    assert driver.default_session_id == "client:old"


def test_wait_for_url_retries_page_unload_on_the_explicit_session(monkeypatch):
    driver = _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 0.2, 0.3, 0.3])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    responses = iter(
        [
            RuntimeError("page unloaded"),
            {"data": {"met": True, "url": "https://example.test/done", "ready": "complete"}},
        ]
    )
    sessions = []

    def exec_js(*_args, **kwargs):
        sessions.append(kwargs["session_id"])
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for_url("example.test/done", timeout=1, session_id="client:1")

    assert result["status"] == "success"
    assert sessions == ["client:1", "client:1"]
    assert driver.default_session_id == "client:old"


def test_wait_for_url_caps_a_lost_page_chunk_and_retries_within_the_total_deadline(monkeypatch):
    _install_page_driver(monkeypatch)
    clock = SimpleNamespace(now=0.0)
    calls = []
    scripts = []

    monkeypatch.setattr(S.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(S.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds))

    def exec_js(script, **kwargs):
        scripts.append(script)
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            clock.now += kwargs["timeout"]
            raise RuntimeError("page unloaded after ACK")
        return {
            "data": {
                "met": True,
                "url": "https://example.test/done",
                "ready": "complete",
            }
        }

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for_url("example.test/done", timeout=30)

    assert result["status"] == "success"
    assert calls == [pytest.approx(6.0), pytest.approx(6.0)]
    assert "new Promise" not in scripts[0]
    assert clock.now == pytest.approx(6.3)


@pytest.mark.parametrize(
    ("info", "expected_hint"),
    [
        ({"met": False, "url": "https://elsewhere.test/", "ready": "interactive", "error": "bad regex"}, "current URL"),
        ({"met": False}, "could not read the current URL"),
    ],
)
def test_wait_for_url_timeout_hints(monkeypatch, info, expected_hint):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": info})

    result = S.wait_for_url("target", timeout=1)

    assert result["status"] == "timeout"
    assert expected_hint in result["hint"]
    if info.get("error"):
        assert result["error"] == info["error"]


def test_wait_for_url_retries_unload_and_reports_last_error(monkeypatch):
    _install_page_driver(monkeypatch)
    _monotonic(monkeypatch, [0.0, 0.0, 0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("navigation blink")),
    )

    result = S.wait_for_url("target", timeout=1)

    assert result["status"] == "timeout"
    assert "navigation blink" in result["error"]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_wait_for_url_rejects_non_positive_or_non_finite_timeout(timeout):
    with pytest.raises(ValueError, match="finite.*greater than zero"):
        S.wait_for_url("target", timeout=timeout)


def test_wait_for_url_retry_sleep_and_bridge_budget_stay_inside_total_deadline(monkeypatch):
    _install_page_driver(monkeypatch)
    clock = SimpleNamespace(now=0.0)
    calls = []
    sleeps = []

    monkeypatch.setattr(S.time, "monotonic", lambda: clock.now)

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    def exec_js(_script, **kwargs):
        calls.append(kwargs["timeout"])
        clock.now += 0.8
        raise RuntimeError("navigation blink")

    monkeypatch.setattr(S.time, "sleep", sleep)
    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.wait_for_url("target", timeout=1.0)

    assert result["status"] == "timeout"
    assert calls == [pytest.approx(1.0)]
    assert sleeps == [pytest.approx(0.2)]
    assert clock.now == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("target", "script_marker"),
    [
        ("bottom", "scrollHeight"),
        ("top", "scrollTo(0, 0)"),
        ("125.5", "scrollTo(0, 125.5)"),
        ("#submit", "scrollIntoView"),
    ],
)
def test_scroll_page_target_modes(monkeypatch, target, script_marker):
    driver = _install_page_driver(monkeypatch)
    scripts = []

    def exec_js(script, **kwargs):
        scripts.append((script, kwargs))
        return {"data": json.dumps({"before": 1, "after": 2, "viewH": 600, "docH": 900, "atBottom": False})}

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.scroll_page(target, session_id="client:1")

    assert result == {
        "status": "success",
        "scrolled_from": 1,
        "scroll_y": 2,
        "viewport_height": 600,
        "doc_height": 900,
        "at_bottom": False,
        "moved": True,
    }
    assert script_marker in scripts[0][0]
    assert scripts[0][1]["session_id"] is None
    assert driver.default_session_id == "client:old"


def test_scroll_page_selector_not_found_and_exception_restore(monkeypatch):
    driver = _install_page_driver(monkeypatch)
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": {"__not_found": True}})

    result = S.scroll_page("#missing", session_id="client:1")
    assert result["status"] == "not_found"
    assert result["selector"] == "#missing"
    assert driver.default_session_id == "client:old"

    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bridge failed")),
    )
    with pytest.raises(RuntimeError, match="bridge failed"):
        S.scroll_page("top", session_id="client:1")
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize("value", ["", "{broken", [], 1])
def test_parse_cookies_rejects_empty_or_invalid_inputs(value):
    with pytest.raises(ValueError, match="cookies"):
        S._parse_cookies_arg(value)


def test_parse_and_normalize_cookie_accepts_aliases_and_scopes():
    assert S._parse_cookies_arg('{"name":"sid"}') == [{"name": "sid"}]
    assert S._parse_cookies_arg({"name": "sid"}) == [{"name": "sid"}]

    normalized = S._normalize_cookie(
        {
            "name": "sid",
            "value": "value",
            "domain": ".example.test",
            "path": "/app",
            "http_only": 1,
            "secure": True,
            "expirationDate": "123.5",
            "same_site": "no_restriction",
        },
        0,
    )

    assert normalized == {
        "name": "sid",
        "value": "value",
        "domain": ".example.test",
        "path": "/app",
        "httpOnly": True,
        "secure": True,
        "expires": 123.5,
        "sameSite": "None",
    }


@pytest.mark.parametrize(
    ("cookie", "message"),
    [
        ("not-an-object", "must be an object"),
        ({}, "missing name"),
        ({"name": "bad name"}, "invalid separator"),
        ({"name": "sid", "value": "bad;value"}, "value"),
        ({"name": "sid", "expires": "later"}, "expires"),
        ({"name": "sid", "sameSite": "sometimes"}, "sameSite"),
        ({"name": "sid", "sameSite": "None"}, "secure=true"),
    ],
)
def test_normalize_cookie_rejects_unsafe_values(cookie, message):
    with pytest.raises(ValueError, match=message):
        S._normalize_cookie(cookie, 3)


def test_page_location_and_document_cookie_parse_string_or_object(monkeypatch):
    responses = iter(
        [
            {"data": json.dumps({"url": "https://example.test/", "host": "example.test"})},
            {"data": {"ok": True}},
            {"data": "[]"},
        ]
    )
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: next(responses))

    assert S._page_location()["host"] == "example.test"
    assert S._cookie_via_document({"name": "sid", "value": "1"}, None, 2) == {"ok": True}
    assert S._cookie_via_document({"name": "sid", "value": "1"}, None, 2) == {}


@pytest.mark.parametrize("session_id,client_id,tab_id", [
    ("chrome:profile:9", "chrome:profile", 9),
    (None, None, None),
])
def test_get_cookies_uses_extension_command_channel(monkeypatch, session_id, client_id, tab_id):
    calls = []
    driver = SimpleNamespace(default_session_id="chrome:old:7")

    def ext_cmd(payload, **kwargs):
        calls.append((payload, kwargs))
        return {"data": {"ok": True, "data": [{"name": "sid", "value": "v1"}]}}

    driver.ext_cmd = ext_cmd
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    result = S.get_cookies(session_id=session_id)

    assert result["ok"] is True
    assert result["data"] == [{"name": "sid", "value": "v1"}]
    assert calls == [
        (
            {"cmd": "cookies", **({"tabId": tab_id} if tab_id is not None else {})},
            {"client_id": client_id, "timeout": 15.0},
        )
    ]


def test_set_cookies_success_partial_and_page_scope(monkeypatch):
    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {"url": "https://example.test/app"})
    replies = iter([{}, {"success": False}])
    calls = []

    def cdp(method, params, session_id, tab_id, timeout):
        calls.append((method, params, session_id, tab_id, timeout))
        return next(replies)

    monkeypatch.setattr(S, "_cdp", cdp)

    result = S.set_cookies(
        [
            {"name": "page", "value": "1"},
            {"name": "domain", "value": "2", "domain": ".example.test"},
        ],
        session_id="client:1",
    )

    assert result["status"] == "partial"
    assert result["set"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["scoped_to"] == "https://example.test/app"
    assert "hint" in result
    assert calls[0][1]["url"] == "https://example.test/app"


def test_set_cookies_requires_page_url_for_implicit_scope(monkeypatch):
    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {})

    with pytest.raises(RuntimeError, match="URL"):
        S.set_cookies({"name": "sid"})


@pytest.mark.parametrize(
    ("fallback", "expected_status", "expect_note"),
    [
        ({"ok": True}, "ok", True),
        ({"ok": False, "error": "blocked"}, "failed", False),
        (RuntimeError("fallback failed"), "failed", False),
    ],
)
@pytest.mark.parametrize("http_only", [False, True])
def test_set_cookies_cdp_fallbacks_are_explicit(monkeypatch, fallback, expected_status, expect_note, http_only):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))

    def document_cookie(*_args, **_kwargs):
        if isinstance(fallback, Exception):
            raise fallback
        return fallback

    monkeypatch.setattr(S, "_cookie_via_document", document_cookie)

    result = S.set_cookies(
        {"name": "sid", "httpOnly": http_only, "url": "https://example.test/"}
    )

    entry = result["results"][0]
    assert entry["status"] == expected_status
    assert entry["method"] == "document.cookie"
    if http_only:
        assert entry["httpOnly_dropped"] is True
    else:
        assert "httpOnly_dropped" not in entry
    assert ("note" in entry) is (expect_note and http_only)


def test_set_cookies_does_not_fallback_into_wrong_named_tab(monkeypatch):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))
    monkeypatch.setattr(
        S,
        "_cookie_via_document",
        lambda *_args, **_kwargs: pytest.fail("must not write in the default tab"),
    )

    result = S.set_cookies({"name": "sid", "url": "https://example.test/"}, tab_id=7)

    assert result["status"] == "failed"
    assert "tab_id=7" in result["results"][0]["error"]


def test_delete_cookies_success_and_page_scope(monkeypatch):
    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {"url": "https://example.test/app"})
    calls = []
    monkeypatch.setattr(S, "_cdp", lambda *args: calls.append(args) or {})

    result = S.delete_cookies(" sid ", path="/app")

    assert result["status"] == "ok"
    assert result["scope"] == {"path": "/app", "url": "https://example.test/app"}
    assert calls[0][1]["name"] == "sid"


def test_delete_cookies_with_domain_keeps_the_requested_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_cdp", lambda *args: calls.append(args) or {})
    monkeypatch.setattr(S, "_page_location", lambda **kwargs: pytest.fail("scope is already explicit"))

    result = S.delete_cookies("sid", domain=".example.test", path="/app", session_id="chrome:7")

    assert result["status"] == "ok"
    assert result["scope"] == {"domain": ".example.test", "path": "/app"}
    assert calls == [(
        "Network.deleteCookies", {"name": "sid", "domain": ".example.test", "path": "/app"},
        "chrome:7", None, 20.0,
    )]


def test_delete_cookies_validation_and_missing_page_url(monkeypatch):
    with pytest.raises(ValueError, match="name"):
        S.delete_cookies("")

    monkeypatch.setattr(S, "_page_location", lambda **_kwargs: {})
    with pytest.raises(RuntimeError, match="URL"):
        S.delete_cookies("sid")


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [({"gone": True}, "ok"), ({"gone": False}, "failed"), (RuntimeError("js failed"), "failed")],
)
def test_delete_cookies_document_fallback(monkeypatch, response, expected_status):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))

    def exec_js(*_args, **_kwargs):
        if isinstance(response, Exception):
            raise response
        return {"data": json.dumps(response)}

    monkeypatch.setattr(S, "exec_js", exec_js)

    result = S.delete_cookies("sid", url="https://example.test/")

    assert result["status"] == expected_status
    assert result["method"] == "document.cookie"


def test_delete_cookies_named_tab_refuses_document_fallback(monkeypatch):
    monkeypatch.setattr(S, "_cdp", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp busy")))
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: pytest.fail("must not run in default tab"))

    result = S.delete_cookies("sid", url="https://example.test/", tab_id=9)

    assert result["status"] == "failed"
    assert "tab_id=9" in result["error"]


@pytest.mark.parametrize("area", ["", "indexeddb"])
def test_storage_area_rejects_unknown_values(area):
    with pytest.raises(ValueError, match="area"):
        S._storage_area(area)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"offset": True},
        {"offset": -1},
        {"max_items": 0},
        {"max_items": True},
        {"max_bytes": 0},
        {"max_bytes": True},
    ],
)
def test_storage_get_validates_bounds(kwargs):
    with pytest.raises(ValueError):
        S.storage_get(**kwargs)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": True, "found": True, "value": "v"}, {"status": "success", "found": True, "value": "v"}),
        ({"ok": True, "found": False, "value": None}, {"status": "success", "found": False, "note": True}),
        ({"ok": False, "error": "blocked"}, {"status": "error", "error_code": "storage_unavailable"}),
        ([], {"status": "error", "error_code": "invalid_response"}),
    ],
)
def test_storage_get_key_results(monkeypatch, payload, expected):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": json.dumps(payload)})

    result = S.storage_get("key", area="session")

    for key, value in expected.items():
        if key == "note":
            assert "note" in result
        else:
            assert result[key] == value


@pytest.mark.parametrize("truncated", [True, False])
def test_storage_get_dump_pagination(monkeypatch, truncated):
    payload = {
        "ok": True,
        "items": {"a": "1"},
        "total_keys": 3,
        "bytes": 2,
        "truncated": truncated,
        "next_offset": 1,
    }
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": payload})

    result = S.storage_get(offset=0, max_items=1, max_bytes=10)

    assert result["status"] == "success"
    assert result["truncated"] is truncated
    assert ("next_offset" in result) is truncated


@pytest.mark.parametrize(
    ("error", "code", "delivery_state", "retry_safe"),
    [
        (
            S.BridgeNoResponseError(
                "bridge did not answer",
                delivery_state="delivered_no_result",
                retry_safe=False,
            ),
            "no_response",
            "delivered_no_result",
            True,
        ),
        (TimeoutError("late"), "timeout", "unknown", True),
        (RuntimeError("socket closed"), "bridge_error", "unknown", True),
    ],
)
def test_storage_get_classifies_transport_errors(
    monkeypatch, error, code, delivery_state, retry_safe
):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = S.storage_get("key")

    assert result["status"] == "error"
    assert result["error_code"] == code
    assert result["delivery_state"] == delivery_state
    assert result["retry_safe"] is retry_safe
    assert result["retryable"] is retry_safe


def test_storage_set_success_serializes_non_string(monkeypatch):
    monkeypatch.setattr(
        S,
        "exec_js",
        lambda *_args, **_kwargs: {"data": {"ok": True, "existed": True, "keys": 4}},
    )

    result = S.storage_set("settings", {"dark": True}, area="localstorage")

    assert result["status"] == "success"
    assert result["replaced"] is True
    assert result["total_keys"] == 4
    assert "JSON" in result["note"]


@pytest.mark.parametrize("key", ["", 1])
def test_storage_set_requires_nonempty_string_key(key):
    with pytest.raises(ValueError, match="key"):
        S.storage_set(key, "value")


@pytest.mark.parametrize("payload", [{"ok": False, "error": "quota"}, [], None])
def test_storage_set_reports_unconfirmed_write(monkeypatch, payload):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": payload})

    result = S.storage_set("key", "value")

    assert result["status"] == "failed"
    assert "hint" in result


@pytest.mark.parametrize(
    ("error", "code", "delivery_state", "retry_safe"),
    [
        (
            S.BridgeNoResponseError(
                "bridge did not answer",
                delivery_state="undelivered",
                retry_safe=True,
            ),
            "no_response",
            "undelivered",
            True,
        ),
        (
            S.BridgeNoResponseError(
                "bridge did not answer",
                delivery_state="delivered_no_result",
                retry_safe=False,
            ),
            "no_response",
            "delivered_no_result",
            False,
        ),
        (TimeoutError("late"), "timeout", "unknown", False),
        (RuntimeError("bridge failed"), "bridge_error", "unknown", False),
    ],
)
def test_storage_set_classifies_transport_errors(
    monkeypatch, error, code, delivery_state, retry_safe
):
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = S.storage_set("key", "value")

    assert result["status"] == "error"
    assert result["error_code"] == code
    assert result["delivery_state"] == delivery_state
    assert result["retry_safe"] is retry_safe
    assert result["retryable"] is retry_safe


@pytest.mark.parametrize(
    "kwargs",
    [
        {"format": "gif"},
        {"format": "png", "quality": 80},
        {"format": "jpeg", "quality": True},
        {"format": "jpeg", "quality": 101},
        {"full_page": True, "clip": {"x": 0, "y": 0, "width": 1, "height": 1}},
        {"clip": "bad"},
        {"clip": {"x": 0, "y": 0, "width": 1, "height": 1, "extra": 1}},
        {"clip": {"x": 0, "y": 0, "width": 1}},
        {"clip": {"x": 0, "y": 0, "width": 0, "height": 1}},
        {"clip": {"x": 0, "y": 0, "width": 1, "height": 1, "scale": 0}},
        {"clip": {"x": float("nan"), "y": 0, "width": 1, "height": 1}},
        {"clip": {"x": 0, "y": float("inf"), "width": 1, "height": 1}},
        {"clip": {"x": 0, "y": 0, "width": float("-inf"), "height": 1}},
        {"clip": {"x": "bad", "y": 0, "width": 1, "height": 1}},
        {"clip": {"x": None, "y": 0, "width": 1, "height": 1}},
    ],
)
def test_capture_page_screenshot_validates_options(kwargs):
    with pytest.raises(ValueError):
        S.capture_page_screenshot(**kwargs)


class _ScreenshotDriver:
    def __init__(self, response, default_session_id="client:old"):
        self.response = response
        self.default_session_id = default_session_id
        self.calls = []

    def ext_cmd(self, payload, client_id=None, timeout=20.0):
        self.calls.append((payload, client_id, timeout))
        return self.response


def _install_screenshot(monkeypatch, response):
    driver = _ScreenshotDriver(response)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": "client:7"}])

    def switch_session(session_id=None, **_kwargs):
        sid = str(session_id) if session_id is not None else "client:7"
        driver.default_session_id = sid
        return sid

    monkeypatch.setattr(S, "switch_session", switch_session)
    return driver


@pytest.mark.parametrize("wrapping_depth", [2, 3])
def test_capture_page_screenshot_jpeg_clip_and_nested_payload(monkeypatch, wrapping_depth):
    raw = b"small image"
    b64 = base64.b64encode(raw).decode("ascii")
    response = b64
    for _ in range(wrapping_depth):
        response = {"data": response}
    driver = _install_screenshot(monkeypatch, response)

    result = S.capture_page_screenshot(
        session_id="client:7",
        format="jpg",
        clip={"x": 1, "y": 2, "width": 3, "height": 4},
        quality=80,
        return_base64=True,
    )

    assert result.structuredContent["format"] == "jpeg"
    assert result.structuredContent["base64"] == b64
    params = driver.calls[0][0]["params"]
    assert params["quality"] == 80
    assert params["clip"]["scale"] == 1.0
    assert driver.default_session_id == "client:old"


@pytest.mark.parametrize("payload", [None, "", {"data": ""}, "not-base64!"])
def test_capture_page_screenshot_rejects_missing_or_invalid_image(monkeypatch, payload):
    _install_screenshot(monkeypatch, {"data": payload})

    with pytest.raises(RuntimeError, match="Screenshot failed"):
        S.capture_page_screenshot()


def test_capture_page_screenshot_full_page_and_session_mismatch(monkeypatch):
    b64 = base64.b64encode(b"image").decode("ascii")
    driver = _install_screenshot(monkeypatch, {"data": b64})

    result = S.capture_page_screenshot(full_page=True, format="webp")
    assert result.structuredContent["full_page"] is True
    assert driver.calls[0][0]["fullPage"] is True

    with pytest.raises(ValueError, match="does not match"):
        S.capture_page_screenshot(session_id="client:7", tab_id=8)
    assert driver.default_session_id == "client:7"


@pytest.mark.parametrize("reply,message", [
    ({"ok": False, "error": "debugger detached"}, "debugger detached"),
    (None, "incomplete batch result"),
    ([], "incomplete batch result"),
    ([{}, None], "incomplete batch result"),
])
def test_upload_files_does_not_replay_an_unconfirmed_batch(monkeypatch, tmp_path, reply, message):
    upload = tmp_path / "input.txt"
    upload.write_text("fixture", encoding="utf-8")
    calls = []

    def batch(payload, **kwargs):
        calls.append((payload, kwargs))
        return {"data": reply}

    monkeypatch.setattr(S, "_extension_batch", batch)

    with pytest.raises(RuntimeError, match=message):
        S.upload_files("#file", [str(upload)], session_id="chrome:7")

    assert len(calls) == 1
    assert calls[0][0]["commands"][-1]["params"]["files"] == [str(upload.resolve())]
    assert calls[0][1]["session_id"] == "chrome:7"
    assert upload.read_text(encoding="utf-8") == "fixture"


def test_extension_batch_supports_an_embedded_driver_without_a_command_router(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace())
    monkeypatch.setattr(S, "exec_js", lambda *args, **kwargs: calls.append((args, kwargs)) or {"data": []})
    payload = {"cmd": "batch", "commands": [{"cmd": "cdp", "method": "DOM.getDocument"}]}

    assert S._extension_batch(payload, session_id="chrome:7", timeout=2) == {"data": []}
    assert json.loads(calls[0][0][0]) == payload
    assert calls[0][1] == {"session_id": "chrome:7", "timeout": 2}


def test_extension_batch_does_not_replay_an_arbitrary_transport_error(monkeypatch):
    failure = OSError("socket closed after send")
    calls = []

    def ext_cmd(*args, **kwargs):
        calls.append((args, kwargs))
        raise failure

    monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace(ext_cmd=ext_cmd))
    monkeypatch.setattr(S, "_resolve_cdp_target", lambda *args: ("chrome:7", "chrome", 7))
    monkeypatch.setattr(S, "exec_js", lambda *args, **kwargs: pytest.fail("must not resend the batch"))

    with pytest.raises(OSError) as raised:
        S._extension_batch({"cmd": "batch", "commands": []}, session_id="chrome:7", timeout=2)

    assert raised.value is failure
    assert len(calls) == 1


def test_pyautogui_loader_configures_the_imported_backend_without_desktop_io(monkeypatch):
    backend = SimpleNamespace(FAILSAFE=True)
    monkeypatch.setitem(S.sys.modules, "pyautogui", backend)

    assert S._pyautogui() is backend
    assert backend.FAILSAFE is False


@pytest.mark.parametrize("collector_state", ["missing", "exception", "failed", "in_progress"])
def test_waiting_for_a_pending_probe_keeps_its_receipt_without_replaying(monkeypatch, collector_state):
    now = [0.0]
    dispatches, polls = [], []
    driver = SimpleNamespace()
    receipt = {
        "operation_id": "wait-operation", "delivery_state": "sent_unconfirmed",
        "reservation_held": True,
    }
    pending_error = TimeoutError("waiting for the page receipt")
    pending_error.diagnostics = receipt

    def execute(*args, **kwargs):
        dispatches.append((args, kwargs))
        assert len(dispatches) == 1
        raise pending_error

    def collect(operation_id, **kwargs):
        polls.append((operation_id, kwargs))
        now[0] = 2
        if collector_state == "exception":
            raise OSError("result lookup disconnected")
        if collector_state == "failed":
            return {"status": "failed", "error": "page unloaded"}
        return {"status": "in_progress"}

    if collector_state != "missing":
        driver.get_execute_js_result = collect
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "exec_js", execute)
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(S.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))

    info, error, pending, _ = S._poll_wait_condition("return true", "chrome:7", 2)

    assert info == {}
    assert len(dispatches) == 1
    assert len(polls) == (collector_state != "missing")
    if collector_state == "failed":
        assert error == "page unloaded"
        assert pending == {
            **receipt,
            "operation_status": "failed",
            "retry_safe": False,
            "poll_with": "get_execute_js_result",
        }
    else:
        assert error == (
            "result lookup disconnected" if collector_state == "exception" else str(pending_error)
        )
        assert pending["operation_id"] == "wait-operation"
        assert pending["reservation_held"] is True
        assert pending["retry_safe"] is False


@pytest.mark.parametrize("data", [None, [], json.dumps("not a condition snapshot")])
def test_wait_probe_does_not_accept_a_non_object_as_a_condition(monkeypatch, data):
    now = [0.0]

    def execute(*args, **kwargs):
        now[0] = 2
        return {"data": data}

    monkeypatch.setattr(S, "exec_js", execute)
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])

    info, error, pending, elapsed = S._poll_wait_condition("return true", "chrome:7", 2)

    assert info == {} and pending == {}
    assert error == "wait probe returned no condition snapshot"
    assert elapsed == 2000


def test_a_refused_tab_close_preserves_its_cleanup_capability(monkeypatch):
    ownership = S._TabOwnershipRegistry()
    ownership.register("client:1", "generation-1", owner_id="owner")
    before = ownership.outstanding()
    calls = []
    driver = SimpleNamespace(ext_cmd=lambda *args, **kwargs: calls.append((args, kwargs)) or {
        "data": {"ok": False, "error": "tab close refused"},
    })
    driver.resolve_session_target = lambda session_id: {"session_id": session_id}
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", ownership)
    monkeypatch.setattr(S, "_normalize_tab_targets", lambda *args, **kwargs: ([1], "client"))
    monkeypatch.setattr(S, "active_sessions", lambda **kwargs: [{
        "id": "client:1", "generation": "generation-1",
    }])

    with pytest.raises(RuntimeError, match="tab close refused"):
        S.close_tabs("client:1", session_id="client:1", owner_id="owner")

    assert len(calls) == 1
    assert ownership.outstanding() == before


def test_activate_tab_preserves_the_explicit_target_and_activation_evidence(monkeypatch):
    activations, pauses = [], []

    def activate(session_id):
        activations.append(session_id)
        return {"session_id": session_id, "on_screen": True}

    monkeypatch.setattr(S, "_activate", activate)
    monkeypatch.setattr(S.time, "sleep", pauses.append)

    assert S.activate_tab("chrome:7") == {
        "status": "ok", "session_id": "chrome:7", "on_screen": True,
    }
    assert activations == ["chrome:7"]
    assert pauses == [0.3]


@pytest.mark.parametrize("failed", [False, True])
def test_optional_activation_preserves_an_explicit_alias_or_a_skipped_reason(monkeypatch, failed):
    activations, pauses = [], []

    def activate(session_id):
        activations.append(session_id)
        if failed:
            raise RuntimeError("target is no longer connected")
        return {"session_id": session_id, "on_screen": True}

    monkeypatch.setattr(S, "_activate", activate)
    monkeypatch.setattr(S.time, "sleep", pauses.append)

    result = S._maybe_activate("chrome:7")

    assert activations == ["chrome:7"]
    assert result == (
        {"activation_skipped": "target is no longer connected"} if failed else
        {"session_id": "chrome:7", "on_screen": True}
    )
    assert pauses == ([] if failed else [0.3])


@pytest.mark.parametrize("reply", [None, {"data": None}, {"data": {"data": ""}}])
def test_save_pdf_requires_pdf_data_before_creating_a_file(monkeypatch, tmp_path, reply):
    destination = tmp_path / "page.pdf"
    monkeypatch.setattr(S, "_validate_safe_path", lambda *args, **kwargs: destination)
    monkeypatch.setattr(S, "cdp_command", lambda *args, **kwargs: reply)

    with pytest.raises(RuntimeError, match="returned no PDF data"):
        S.save_pdf(str(destination), session_id="chrome:7")

    assert not destination.exists()


def test_cookie_normalization_keeps_an_unspecified_samesite_policy():
    result = S._normalize_cookie({"name": "sid", "value": "v", "sameSite": "unspecified"}, 1)
    assert result["name"] == "sid"
    assert result["value"] == "v"
    assert "sameSite" not in result


def test_string_storage_values_are_written_without_a_serialization_note(monkeypatch):
    calls = []
    value = 'literal "quoted" value'
    monkeypatch.setattr(S, "exec_js", lambda *args, **kwargs: calls.append((args, kwargs)) or {
        "data": {"ok": True, "existed": True, "keys": 2},
    })

    result = S.storage_set("key", value, session_id="chrome:7")

    assert result["status"] == "success"
    assert result["replaced"] is True
    assert "note" not in result
    assert json.dumps(value) in calls[0][0][0]
    assert calls[0][1]["session_id"] == "chrome:7"


def test_scroll_without_an_explicit_target_keeps_the_existing_default(monkeypatch):
    driver = _install_page_driver(monkeypatch, default_session_id="chrome:7")
    monkeypatch.setattr(S, "switch_session", lambda **kwargs: pytest.fail("the implicit target is already selected"))
    monkeypatch.setattr(S, "exec_js", lambda *args, **kwargs: {"data": {"before": 0, "after": 500}})

    result = S.scroll_page("500")

    assert result["scroll_y"] == 500
    assert driver.default_session_id == "chrome:7"


def test_cdp_can_address_a_worker_without_an_extension_id(monkeypatch):
    calls = []
    driver = SimpleNamespace(ext_cmd=lambda *args, **kwargs: calls.append((args, kwargs)) or {"data": 7})
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "_implicit_client_id", lambda session_id=None: "chrome")

    assert S.cdp_command("Runtime.evaluate", target_id="worker", session_id="chrome:7") == {"data": 7}
    assert calls[0][0][0] == {
        "cmd": "cdp", "method": "Runtime.evaluate", "params": {}, "targetId": "worker",
    }
    assert calls[0][1]["client_id"] == "chrome"


@pytest.mark.parametrize("diagnosis", [None, [], "invalid diagnosis"])
def test_setup_status_tolerates_an_unstructured_diagnosis(monkeypatch, diagnosis):
    driver = SimpleNamespace(
        default_session_id=None, diagnose=lambda **kwargs: diagnosis,
        ext_cmd=lambda *args, **kwargs: {"data": {}}, is_remote=False,
    )
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: [])

    result = S.get_setup_status()

    assert result["diagnosis"] == {}
    assert result["tabs"] == []
