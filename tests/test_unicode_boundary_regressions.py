"""Unicode output and identity boundaries, exercised without live transports."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import socket
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from browsertap_mcp import bridge, cli, native_installer
from browsertap_mcp import browser_bridge as B
from browsertap_mcp.command_scope import command_scope, guard_targets
from browsertap_mcp.extension_build import compute_extension_stamp
from browsertap_mcp.pending_operations import OperationAccessError
from tests.test_browser_bridge_coverage import driver_stub, wsgi_post

TEXT = '中文😀 high:\ud800 low:\udfff literal:\\ud800 "\n\t'
VALUE = {"key-\ud800": TEXT, "nested": [None, True, "中文😀", {"low-\udfff": TEXT}]}
PATH_PARTS = [pytest.param("目录😀", id="scalar")]
# Only Windows permits these unpaired UTF-16 filesystem names. Keep portable
# cases collected elsewhere without turning inapplicable paths into skips.
if sys.platform == "win32":
    PATH_PARTS.extend([
        pytest.param("目录-\ud800", id="high"),
        pytest.param("目录-\udfff", id="low"),
    ])


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch, tmp_path):
    import wsgiref.simple_server

    def forbidden(*args, **kwargs):
        pytest.fail("this regression must not use a real transport or token file")

    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "18765")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(B.threading.Thread, "start", forbidden)
    monkeypatch.setattr(B, "bridge_token", lambda: "")
    monkeypatch.setattr(B, "bridge_token_path", forbidden)
    monkeypatch.setattr(B, "_persist_token", forbidden)
    monkeypatch.setattr(wsgiref.simple_server, "make_server", forbidden)


@contextmanager
def output_streams(monkeypatch, *, strict_stdout=True):
    stdout = io.BytesIO() if strict_stdout else io.StringIO()
    stderr = io.BytesIO()
    out_writer = io.TextIOWrapper(stdout, encoding="utf-8", errors="strict", write_through=True) if strict_stdout else stdout
    err_writer = io.TextIOWrapper(stderr, encoding="utf-8", errors="strict", write_through=True)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdout", out_writer)
            patch.setattr(sys, "stderr", err_writer)
            yield stdout, stderr
    finally:
        if strict_stdout:
            out_writer.detach()
        err_writer.detach()


def install_doctor(monkeypatch, payload):
    driver = SimpleNamespace(host="127.0.0.1", port=18765, is_remote=True)
    monkeypatch.setattr(cli, "get_driver", Mock(return_value=driver))
    monkeypatch.setattr(cli, "get_setup_status", lambda: copy.deepcopy(payload))
    monkeypatch.setattr(cli, "_port_open", lambda *args: True)


@pytest.mark.parametrize("title", ["中文😀", "\ud800", "\udfff"], ids=["scalar", "high", "low"])
def test_doctor_browser_strings_reach_real_utf8_stdout(monkeypatch, title):
    payload = {"status": "healthy", "action": "none", "tabs": [
        {"id": "synthetic:7", "title": title, "url": "https://synthetic.example/"}],
        "diagnosis": {"ok": True, "cause": "healthy"}, "details": {"nested": [None, True, title]}}
    original = copy.deepcopy(payload)
    install_doctor(monkeypatch, payload)
    with output_streams(monkeypatch) as (stdout, stderr):
        status = cli.cmd_doctor()
    decoded = json.loads(stdout.getvalue())
    assert status == 0
    assert decoded["tabs"] == original["tabs"] and decoded["details"] == original["details"]
    assert decoded["status"] == "healthy" and decoded["action"] == "none"
    assert payload == original and stderr.getvalue() == b""
    cli.get_driver.assert_called_once_with()


@pytest.mark.parametrize("part", PATH_PARTS)
def test_doctor_initialization_json_preserves_an_existing_unicode_path(monkeypatch, tmp_path, part):
    directory = tmp_path / part
    directory.mkdir()
    assert directory.is_dir()
    monkeypatch.setattr(cli, "chrome_extension_dir", lambda: directory)
    monkeypatch.setattr(cli, "get_driver", Mock(side_effect=RuntimeError("synthetic initialization failure")))
    native_status = {"status": "missing", "installed": False, "manifest_path": str(directory / "native.json")}
    monkeypatch.setattr(native_installer, "native_host_status", lambda: native_status)
    with output_streams(monkeypatch) as (stdout, stderr):
        status = cli.cmd_doctor()
    assert status == 1
    assert json.loads(stdout.getvalue()) == {
        "status": "initialization_failed", "action": "check_config",
        "extension_path": str(directory), "error": "synthetic initialization failure",
        "error_type": "RuntimeError", "native_host": native_status,
    }
    assert b"initialization_failed" in stderr.getvalue()


@pytest.mark.parametrize("branch", ["initialization", "advice"])
def test_doctor_dynamic_stderr_is_safe_after_json_exists(monkeypatch, tmp_path, branch):
    # These two synthetic producer values isolate the stderr sinks. They do not
    # establish another product ingress beyond the title/path failures above.
    if branch == "initialization":
        monkeypatch.setattr(cli, "chrome_extension_dir", lambda: tmp_path)
        monkeypatch.setattr(cli, "get_driver", Mock(side_effect=RuntimeError(TEXT)))
        expected_status, expected_field = 1, "error"
    else:
        install_doctor(monkeypatch, {
            "status": "healthy", "action": "none", "tabs": [],
            "diagnosis": {"ok": True, "cause": "synthetic", "advice": TEXT},
        })
        expected_status, expected_field = 0, "diagnosis"
    # StringIO deliberately lets the original JSON succeed, so a red run
    # reaches the independent strict stderr encoding boundary.
    with output_streams(monkeypatch, strict_stdout=False) as (stdout, stderr):
        status = cli.cmd_doctor()
    decoded = json.loads(stdout.getvalue())
    assert status == expected_status
    if expected_field == "error":
        assert decoded["error"] == TEXT and decoded["action"] == "check_config"
    else:
        assert decoded["diagnosis"]["advice"] == TEXT and decoded["action"] == "none"
    assert stderr.getvalue().isascii()
    assert b"\\ud800" in stderr.getvalue() and b"\\udfff" in stderr.getvalue()


@pytest.mark.parametrize("branch", ["initialization", "advice"])
def test_doctor_does_not_swallow_a_real_stderr_write_error(monkeypatch, tmp_path, branch):
    class BrokenStderr:
        def write(self, text):
            raise OSError("synthetic stderr I/O failure")

        def flush(self):
            pass

    if branch == "initialization":
        monkeypatch.setattr(cli, "chrome_extension_dir", lambda: tmp_path)
        monkeypatch.setattr(cli, "get_driver", Mock(side_effect=RuntimeError("synthetic")))
    else:
        install_doctor(monkeypatch, {
            "status": "healthy", "action": "none", "tabs": [],
            "diagnosis": {"ok": True, "cause": "synthetic", "advice": "inspect configuration"},
        })
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", BrokenStderr())
    with pytest.raises(OSError, match="synthetic stderr I/O failure"):
        cli.cmd_doctor()
    assert json.loads(stdout.getvalue())["status"] in {"healthy", "initialization_failed"}


@pytest.mark.parametrize("mode", ["invalid_config", "stop", "restart_refused", "restart_started", "restart_failed"])
def test_all_bridge_json_outputs_preserve_synthetic_unicode_fields(monkeypatch, mode):
    # Management producers are synthetic here to cover every touched JSON
    # output contract; this is not an additional real-world ingress finding.
    stopped = {"status": "not_running", "stopped": False, "diagnostics": VALUE}
    stop = Mock(return_value=stopped)
    spawn = Mock(return_value=mode == "restart_started")
    monkeypatch.setattr(bridge, "stop_bridge_daemon", stop)
    monkeypatch.setattr(cli, "spawn_bridge_daemon", spawn)
    if mode == "invalid_config":
        monkeypatch.setattr(cli, "configured_bridge_port", Mock(side_effect=ValueError(TEXT)))
        expected = {"status": "initialization_failed", "action": "check_config", "error": TEXT, "error_type": "ValueError"}
        expected_status = 1
    elif mode == "stop":
        expected, expected_status = stopped, 0
    else:
        if mode == "restart_refused":
            stopped["status"] = "identity_unavailable"
        started = mode == "restart_started"
        expected = {"status": "restarted" if started else "restart_failed", "stop": stopped, "started": started}
        expected_status = 0 if started else 1
    with output_streams(monkeypatch) as (stdout, _stderr):
        status = cli.cmd_bridge(stop=mode in {"invalid_config", "stop"}, restart=mode.startswith("restart"))
    assert status == expected_status and json.loads(stdout.getvalue()) == expected
    assert stdout.getvalue().isascii()
    if mode == "invalid_config":
        stop.assert_not_called()
    else:
        stop.assert_called_once_with()
    if mode in {"restart_started", "restart_failed"}:
        spawn.assert_called_once_with(reset_spawn_lock=True)
    else:
        spawn.assert_not_called()


@pytest.mark.parametrize("session_id", ["中文😀:7", "synthetic-\ud800:7", "synthetic-\udfff:7"],
                         ids=["scalar", "high", "low"])
def test_registered_http_unicode_target_keeps_lock_delivery_and_receipt(monkeypatch, session_id):
    starts = []

    class DormantThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            starts.append(True)

    driver = driver_stub()
    monkeypatch.setattr(B.threading, "Thread", DormantThread)
    monkeypatch.setattr(B, "HTTP_POLL_SECONDS", 0.001)
    driver.start_http_server()
    identity = {"sessionId": session_id, "url": "https://synthetic.example/", "title": "synthetic"}
    registration = wsgi_post(driver.app, "/api/longpoll", identity)
    assert registration["status"] == 200 and registration["unread_body_bytes"] == 0
    assert B._valid_protocol_id(session_id) and session_id in driver.sessions
    deliveries = []

    def poll_during_wait(serial, delay):
        deliveries.append(wsgi_post(driver.app, "/api/longpoll", identity))
        return driver._activity_snapshot()

    monkeypatch.setattr(driver, "_wait_for_activity", poll_during_wait)
    with command_scope() as scope:
        pending = driver.execute_js("/* synthetic inert code */", session_id=session_id,
                                    requester_id="synthetic-owner", wait=False, timeout=1)
        assert scope.dispatched and len(scope.locks) == 1
        assert len(deliveries) == 1 and deliveries[0]["status"] == 200
        command = json.loads(deliveries[0]["body"])
        assert command["id"] == pending["operation_id"]
        assert command["code"] == "/* synthetic inert code */"
        reply = wsgi_post(driver.app, "/api/result", {
            "type": "result", "id": command["id"], "sessionId": session_id, "result": VALUE,
        })
        assert reply["status"] == 200 and reply["body"] == "ok"
        result = driver.get_execute_js_result(command["id"], requester_id="synthetic-owner")
        assert result["status"] == "success" and result["data"] == VALUE
        with pytest.raises(OperationAccessError) as mismatch:
            driver.get_execute_js_result(command["id"], requester_id="other")
        assert mismatch.value.error_code == "operation_owner_mismatch"
        assert driver.get_execute_js_result(command["id"], requester_id="synthetic-owner") == result
    assert scope.locks == {} and starts == [True] and driver.http_server is None


def test_ordinary_target_lock_filenames_keep_the_legacy_digest(tmp_path):
    targets = ["plain:7", "中文😀:7"]
    expected = {hashlib.sha256(f"127.0.0.1:18765/{target}".encode("utf-8")).hexdigest() + ".lock"
                for target in targets}
    with command_scope():
        guard_targets("localhost:18765", targets)
        assert {path.name for path in (tmp_path / "state/command-locks").iterdir()} == expected


@pytest.mark.parametrize("part", PATH_PARTS)
def test_extension_stamp_preserves_real_unicode_relative_paths(tmp_path, part):
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        path = root / (part + ".bin")
        path.write_bytes(b"synthetic payload")
        assert path.is_file() and path.name == part + ".bin"
    stamp = compute_extension_stamp(first)
    assert len(stamp) == 16
    assert compute_extension_stamp(second) == stamp
    (second / (part + ".bin")).write_bytes(b"changed payload")
    assert compute_extension_stamp(second) != stamp


if sys.platform == "win32":
    def test_distinct_real_surrogate_filenames_do_not_collapse(tmp_path):
        stamps = []
        for index, name in enumerate(["asset-\ud800.bin", "asset-\udfff.bin", "asset-\ufffd.bin"]):
            root = tmp_path / str(index)
            root.mkdir()
            (root / name).write_bytes(b"same payload")
            stamps.append(compute_extension_stamp(root))
        assert len(set(stamps)) == len(stamps)


def test_ordinary_unicode_filename_keeps_legacy_stamp_bytes(tmp_path):
    name, payload = "普通😀.bin", b"synthetic payload"
    (tmp_path / name).write_bytes(payload)
    legacy = hashlib.sha256(f"{name}\0{len(payload)}\0".encode("utf-8") + payload).hexdigest()[:16]
    assert compute_extension_stamp(tmp_path) == legacy
