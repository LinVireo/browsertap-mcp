from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from browsertap_mcp import bridge as daemon
from browsertap_mcp import browser_bridge as bridge
from browsertap_mcp import cli, paths, server

SOURCE_ROOT = Path(bridge.__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("BROWSERTAP_", "AGENT_BROWSER_")):
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BROWSERTAP_NO_SPAWN", "1")
    monkeypatch.setenv("PYTHONPATH", str(SOURCE_ROOT))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("BTAP_TEST_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "_DRIVER_PORT", None)
    monkeypatch.setattr(server, "_DRIVER_HOST", None)
    monkeypatch.setattr(server, "_driver", None)
    return home


@pytest.mark.parametrize(
    "state_override,token_override,kind",
    [
        ("relative-state", None, "env"),
        ("absolute", "relative-token", "env"),
        ("relative-state", "relative-token", "env"),
        ("~/relative-state", "~/relative-token", "env"),
        (None, None, "default"),
        (None, None, "legacy"),
        (None, "relative-token", "default"),
        ("  ", None, "default"),
    ],
)
def test_spawn_preserves_effective_paths_across_cwd(
    monkeypatch, tmp_path, isolated_config, state_override, token_override, kind
):
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.delenv("BROWSERTAP_STATE_DIR")
    if state_override is not None:
        value = str(tmp_path / "absolute-state") if state_override == "absolute" else state_override
        monkeypatch.setenv("BROWSERTAP_STATE_DIR", value)
    if token_override is not None:
        monkeypatch.setenv("BROWSERTAP_BRIDGE_TOKEN_FILE", token_override)
    if kind == "legacy":
        (isolated_config / paths.LEGACY_STATE_DIR_NAME).mkdir()
    before = dict(os.environ)
    captured = {}

    def capture_spawn(command, **kwargs):
        captured.update(kwargs)
        return object()

    probes = iter([False, True])
    with monkeypatch.context() as spawn:
        spawn.setattr(server, "_port_open", lambda *args: next(probes))
        spawn.setattr(server.subprocess, "Popen", capture_spawn)
        assert server._spawn_bridge_daemon_locked()

    child_code = """
import json, os
from pathlib import Path
from browsertap_mcp import browser_bridge as bridge, paths
root = Path(os.environ['BTAP_TEST_ROOT'])
assert paths.state_dir().resolve().is_relative_to(root)
assert bridge.bridge_token_path().resolve().is_relative_to(root)
token = bridge.bridge_token()
pid_file = paths.state_dir(create=True) / 'bridge.pid'
pid_file.write_text(json.dumps({'pid': os.getpid()}), encoding='utf-8')
print(json.dumps({'paths': bridge.state_paths_report(enforced_token=token), 'pid': os.getpid()}))
"""
    child = subprocess.run(
        [sys.executable, "-B", "-c", child_code],
        cwd=captured["cwd"],
        env=captured.get("env", before),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    result = json.loads(child.stdout)
    parent = bridge.state_paths_report()
    assert Path(captured["cwd"]) == isolated_config
    assert dict(os.environ) == before
    for field in ("state_dir", "token_file", "token_fingerprint", "state_dir_kind"):
        assert parent[field] == result["paths"][field], field
    assert parent["state_dir_kind"] == kind
    assert Path(parent["state_dir"]).is_absolute()
    assert Path(parent["token_file"]).is_absolute()
    pid = json.loads((paths.state_dir() / "bridge.pid").read_text(encoding="utf-8"))
    assert pid["pid"] == result["pid"]
    child_env = captured["env"]
    if not state_override or not state_override.strip():
        assert child_env.get("BROWSERTAP_STATE_DIR") == before.get("BROWSERTAP_STATE_DIR")


@pytest.mark.parametrize("new_name_wins", [False, True])
def test_spawn_keeps_adopted_alias_precedence(monkeypatch, tmp_path, new_name_wins):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BROWSERTAP_STATE_DIR")
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", "old-state")
    monkeypatch.setenv("AGENT_BROWSER_BRIDGE_TOKEN_FILE", "old-token")
    # Track the canonical variable that adopt_legacy_env will populate, so it
    # cannot leak a relative token path into later tests after monkeypatch undo.
    monkeypatch.setenv("BROWSERTAP_BRIDGE_TOKEN_FILE", "")
    if new_name_wins:
        monkeypatch.setenv("BROWSERTAP_STATE_DIR", "new-state")
        monkeypatch.setenv("BROWSERTAP_BRIDGE_TOKEN_FILE", "new-token")
    paths.adopt_legacy_env()
    before = dict(os.environ)
    captured = {}
    probes = iter([False, True])
    monkeypatch.setattr(server, "_port_open", lambda *args: next(probes))
    monkeypatch.setattr(server.subprocess, "Popen", lambda command, **kw: captured.update(kw))
    assert server._spawn_bridge_daemon_locked()
    prefix = "new" if new_name_wins else "old"
    assert Path(captured["env"]["BROWSERTAP_STATE_DIR"]) == tmp_path / f"{prefix}-state"
    assert Path(captured["env"]["BROWSERTAP_BRIDGE_TOKEN_FILE"]) == tmp_path / f"{prefix}-token"
    assert dict(os.environ) == before


@pytest.mark.parametrize("probe_error", [socket.gaierror("name unavailable"), OSError("probe failed")])
@pytest.mark.parametrize("status,action", [("healthy", "none"), ("stale_extension", "reload_extension")])
def test_doctor_keeps_setup_json_when_a_later_probe_fails(
    monkeypatch, capsys, probe_error, status, action
):
    diagnosis = {"cause": "healthy", "ok": True, "advice": "ready"}
    setup = {
        "status": status,
        "action": action,
        "tabs": [{"id": "fixture:1"}],
        "diagnosis": diagnosis,
        "state_paths": {"state_dir_kind": "env"},
    }
    driver = SimpleNamespace(host="fixture.invalid", port=21000, is_remote=True)
    monkeypatch.setattr(cli, "get_driver", lambda: driver)
    monkeypatch.setattr(cli, "get_setup_status", lambda: dict(setup))

    def probe(host, port):
        if port == driver.port:
            raise probe_error
        return True

    monkeypatch.setattr(cli, "_port_open", probe)
    assert cli.cmd_doctor() == 1
    payload = json.loads(capsys.readouterr().out)
    for field in ("status", "action", "tabs", "diagnosis", "state_paths"):
        assert payload[field] == setup[field]
    assert payload["ws_port_open"] is None
    assert payload["http_port_open"] is True
    assert payload["port_probe_errors"]["ws_port_open"]["error_type"] == type(probe_error).__name__


@pytest.mark.parametrize("status,label", [
    ("bridge_unreachable", "bridge_unreachable"),
    ("stale_bridge", "stale_bridge"),
    (None, "bridge_unreachable"),
    ("unknown", "bridge_unreachable"),
])
def test_doctor_restart_label_uses_actual_status(monkeypatch, capsys, status, label):
    driver = SimpleNamespace(host="127.0.0.1", port=21000, is_remote=True)
    monkeypatch.setattr(cli, "get_driver", lambda: driver)
    monkeypatch.setattr(cli, "get_setup_status", lambda: {
        "status": status, "action": "restart_bridge", "tabs": [],
        "diagnosis": {"cause": "registering", "ok": False},
    })
    monkeypatch.setattr(cli, "_port_open", lambda *args: True)
    assert cli.cmd_doctor() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == status
    assert f"[!!] {label}:" in captured.err
    if label == "bridge_unreachable":
        assert "stale_bridge" not in captured.err


@pytest.mark.parametrize("connect_result", [0, errno.ECONNREFUSED])
def test_probe_tries_the_other_address_family_and_closes_the_socket(monkeypatch, connect_result):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 21000, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 21000)),
    ])
    closed = []
    attempted = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def settimeout(self, timeout):
            assert timeout == 1

        def connect_ex(self, address):
            assert address == ("127.0.0.1", 21000)
            return connect_result

    def socket_factory(family, kind, protocol):
        attempted.append(family)
        if family == socket.AF_INET6:
            raise OSError("IPv6 sockets unavailable")
        return Probe()

    monkeypatch.setattr(socket, "socket", socket_factory)
    assert cli._port_open("fixture.invalid", 21000) is (connect_result == 0)
    assert attempted == [socket.AF_INET6, socket.AF_INET]
    assert closed == [True]


def _ipv6_listener():
    listener = None
    try:
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listener.bind(("::1", 0))
        listener.listen(5)
        return listener
    except OSError as exc:
        if listener is not None:
            listener.close()
        if exc.errno in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT, 10047, 10049}:
            pytest.skip("numeric IPv6 loopback is unavailable on this host")
        raise


@pytest.mark.parametrize("entry", ["cli", "server", "stop"])
def test_numeric_ipv6_port_probes(monkeypatch, entry):
    with _ipv6_listener() as listener:
        port = listener.getsockname()[1]
        if entry == "stop":
            monkeypatch.setenv("BROWSERTAP_BRIDGE_HOST", "::1")
            monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", str(port - 1))
            assert daemon._configured_bridge_port_open()
        else:
            probe = cli._port_open if entry == "cli" else server._port_open
            assert probe("::1", port)


def test_numeric_ipv6_remote_constructor_uses_bracketed_url(monkeypatch):
    with _ipv6_listener() as listener:
        port = listener.getsockname()[1]
        monkeypatch.setattr(
            bridge.BrowserBridge, "_acquire_host_lock", lambda self: pytest.fail("listener exists")
        )
        driver = bridge.BrowserBridge(host="::1", port=port - 1)
        try:
            assert driver.is_remote
            assert driver.remote == f"http://[::1]:{port}/link"
        finally:
            driver._http.close()


def test_numeric_ipv6_host_lock_is_exclusive():
    with _ipv6_listener() as reservation:
        lock_port = reservation.getsockname()[1]
    driver = bridge.BrowserBridge.__new__(bridge.BrowserBridge)
    driver.host, driver.port = "::1", lock_port - 2
    lock = driver._acquire_host_lock()
    assert lock is not None
    try:
        assert lock.family == socket.AF_INET6
        assert lock.getsockname()[1] == lock_port
        assert driver._acquire_host_lock() is None
    finally:
        lock.close()


def test_numeric_ipv6_http_listener_without_reverse_dns(monkeypatch):
    with _ipv6_listener() as reservation:
        http_port = reservation.getsockname()[1]
    driver = bridge.BrowserBridge.__new__(bridge.BrowserBridge)
    driver.host, driver.port = "::1", http_port - 1
    driver.get_all_sessions = lambda: []
    monkeypatch.setattr(socket, "getfqdn", lambda *args: pytest.fail("reverse DNS must not run"))
    driver.start_http_server()
    try:
        deadline = time.monotonic() + 2
        while getattr(driver, "http_server", None) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert getattr(driver, "http_server", None) is not None
        connection = HTTPConnection("::1", http_port, timeout=2)
        try:
            connection.request("POST", "/link", json.dumps({"cmd": "get_all_sessions"}), {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bridge.bridge_token()}",
            })
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"r": []}
        finally:
            connection.close()
    finally:
        driver.stop_http_server()


@pytest.mark.parametrize("invalid", ["bad", "", "0", "-1", "65534", "65535", "65536", "1.5"])
def test_invalid_port_is_not_cached(monkeypatch, invalid):
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", invalid)
    with pytest.raises(ValueError, match="BROWSERTAP_BRIDGE_PORT"):
        server._get_driver_port()
    assert server._DRIVER_PORT is None
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "65533")
    assert server._get_driver_port() == 65533


@pytest.mark.parametrize("port", [0, -1, 65534, 65535, True, 1.5])
def test_constructor_rejects_invalid_base_port_before_network(monkeypatch, port):
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("network before validation"))
    with pytest.raises(ValueError, match="BROWSERTAP_BRIDGE_PORT"):
        bridge.BrowserBridge(port=port)


def test_spawn_validates_port_before_creating_a_lock(monkeypatch):
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "65534")
    monkeypatch.setattr(server, "_acquire_spawn_lock", lambda: pytest.fail("lock before validation"))
    with pytest.raises(ValueError, match="BROWSERTAP_BRIDGE_PORT"):
        server.spawn_bridge_daemon()


@pytest.mark.parametrize("entry", ["doctor", "foreground", "stop", "restart", "daemon"])
def test_invalid_port_entry_points_return_json(monkeypatch, capsys, entry):
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "65534")
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("network before validation"))
    if entry == "doctor":
        result = cli.cmd_doctor()
    elif entry == "daemon":
        result = daemon.main([])
    else:
        result = cli.cmd_bridge(stop=entry == "stop", restart=entry == "restart")
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "initialization_failed"
    assert payload["action"] == "check_config"
    assert payload["error_type"] == "ValueError"
    assert "BROWSERTAP_BRIDGE_PORT" in payload["error"]


@pytest.mark.parametrize("args", [["--help"], ["--version"], ["extension-path"], ["skill-path"]])
def test_non_network_cli_commands_stay_lazy_with_bad_port(monkeypatch, args):
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "bad")
    result = subprocess.run(
        [sys.executable, "-B", "-m", "browsertap_mcp.cli", *args],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    assert not paths.state_dir().exists()


@pytest.mark.parametrize("status,content", [
    ("missing", None), ("empty", b" \n"), ("ready", b"synthetic-token\n"),
    ("invalid_encoding", b"\xffsynthetic-private-bytes"),
])
def test_token_diagnostics_report_physical_state_without_disclosing_content(status, content):
    token_path = bridge.bridge_token_path()
    if content is not None:
        token_path.parent.mkdir(parents=True)
        token_path.write_bytes(content)
    report = bridge.state_paths_report()
    assert report["token_file_status"] == status
    assert report["token_file_exists"] is (content is not None)
    assert (report["token_fingerprint"] is not None) is (status == "ready")
    rendered = json.dumps(report)
    assert "synthetic-token" not in rendered
    assert "synthetic-private-bytes" not in rendered
    if content is None:
        assert not token_path.parent.exists()
    else:
        assert token_path.read_bytes() == content


@pytest.mark.parametrize("status", ["unreadable", "invalid_encoding"])
def test_unreadable_token_fails_without_retry_or_overwrite(monkeypatch, status):
    token_path = bridge.bridge_token_path()
    token_path.parent.mkdir(parents=True)
    original = b"\xffprivate-fixture" if status == "invalid_encoding" else b"private-fixture"
    token_path.write_bytes(original)
    if status == "unreadable":
        original_read = bridge._token_file.read_text

        def denied(path, *args, **kwargs):
            if path == token_path:
                raise PermissionError("private-fixture")
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(bridge._token_file, "read_text", denied)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: pytest.fail("not an empty-file race"))
    monkeypatch.setattr(
        bridge.secrets, "token_urlsafe", lambda *a: pytest.fail("must not generate a replacement")
    )
    report = bridge.state_paths_report()
    assert report["token_file_exists"] is True
    assert report["token_file_status"] == status
    assert bridge._read_token_file(token_path) == ""
    with pytest.raises(RuntimeError, match=status) as failure:
        bridge.bridge_token()
    assert "private-fixture" not in str(failure.value)
    assert "private-fixture" not in json.dumps(report)
    assert token_path.read_bytes() == original


def test_empty_token_waits_for_atomic_creator(monkeypatch):
    token_path = bridge.bridge_token_path()
    token_path.parent.mkdir(parents=True)
    token_path.touch()
    sleeps = []

    def complete_write(delay):
        sleeps.append(delay)
        token_path.write_text("winning-synthetic-token\n", encoding="utf-8")

    monkeypatch.setattr(bridge.time, "sleep", complete_write)
    assert bridge._persist_token(token_path, "losing-synthetic-token") == "winning-synthetic-token"
    assert sleeps == [0.01]
    assert token_path.read_text(encoding="utf-8") == "winning-synthetic-token\n"


def test_token_diagnostics_leave_existence_unknown_when_metadata_is_unreadable(monkeypatch):
    token_path = bridge.bridge_token_path()
    original_read, original_stat = bridge._token_file.read_text, Path.stat

    def denied_read(path, *args, **kwargs):
        if path == token_path:
            raise PermissionError("private-fixture")
        return original_read(path, *args, **kwargs)

    def denied_stat(path, *args, **kwargs):
        if path == token_path:
            raise PermissionError("private-fixture")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(bridge._token_file, "read_text", denied_read)
    monkeypatch.setattr(Path, "stat", denied_stat)
    report = bridge.state_paths_report()
    assert report["token_file_status"] == "unreadable"
    assert report["token_file_exists"] is None
    assert report["token_file_error"] == "permission_denied"
    assert "private-fixture" not in json.dumps(report)


@pytest.mark.parametrize("error_type,reason", [
    (PermissionError, "permission_denied"), (OSError, "io_error"),
])
@pytest.mark.parametrize("selection", ["env", "default"])
def test_token_report_survives_unreadable_state_directory(monkeypatch, error_type, reason, selection):
    if selection == "default":
        monkeypatch.delenv(paths.STATE_DIR_ENV)
        (Path.home() / paths.DEFAULT_STATE_DIR_NAME).mkdir()
        (Path.home() / paths.LEGACY_STATE_DIR_NAME).mkdir()
    directory = paths.state_dir()
    token_path = bridge.bridge_token_path()
    original_read, original_stat = bridge._token_file.read_text, Path.stat

    def denied_read(path, *args, **kwargs):
        if path == token_path:
            raise error_type("private-fixture")
        return original_read(path, *args, **kwargs)

    def denied_stat(path, *args, **kwargs):
        if path in (directory, token_path):
            raise error_type("private-fixture")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(bridge._token_file, "read_text", denied_read)
    monkeypatch.setattr(Path, "stat", denied_stat)

    report = bridge.state_paths_report()

    assert report["state_dir"] == str(directory)
    assert report["state_dir_kind"] == selection
    assert report["token_file"] == str(token_path)
    assert report["state_dir_exists"] is None
    assert report["token_file_status"] == "unreadable"
    assert report["token_file_exists"] is None
    assert report["token_file_error"] == reason
    assert report["token_fingerprint"] is None
    assert "private-fixture" not in json.dumps(report)
