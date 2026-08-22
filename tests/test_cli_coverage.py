from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from browsertap_mcp import cli


class FakeDriver:
    host = "127.0.0.9"
    port = 19000
    is_remote = True

    def __init__(self, sessions=None, diagnosis=None):
        self.sessions = sessions if sessions is not None else [{"id": "chrome:test:7"}]
        self.diagnosis = diagnosis or {
            "cause": "healthy",
            "ok": True,
            "advice": "ready",
        }

    def get_all_sessions(self):
        if isinstance(self.sessions, BaseException):
            raise self.sessions
        return self.sessions

    def diagnose(self):
        if isinstance(self.diagnosis, BaseException):
            raise self.diagnosis
        return self.diagnosis


def _install_doctor_fakes(monkeypatch, driver, payload):
    monkeypatch.setattr(cli, "get_driver", lambda: driver)
    monkeypatch.setattr(cli, "get_setup_status", lambda: dict(payload))
    monkeypatch.setattr(cli, "_port_open", lambda host, port: port == driver.port)


def test_extension_path_prints_path_without_writing_package_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "chrome_extension_dir", lambda: tmp_path)

    assert cli.cmd_extension_path() == 0
    assert capsys.readouterr().out.strip() == str(tmp_path)


def test_print_hermes_config(capsys):
    assert cli.cmd_print_hermes_config() == 0
    output = capsys.readouterr().out
    assert "browsertap:" in output
    assert "command: browsertap" in output
    assert "connect_timeout: 60" in output


def test_version_flag_reports_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"browsertap {cli.__version__}"


@pytest.mark.parametrize("connect_result, expected", [(0, True), (10061, False)])
def test_port_open_closes_socket(monkeypatch, connect_result, expected):
    class FakeSocket:
        def __init__(self):
            self.timeout = None
            self.address = None
            self.closed = False

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect_ex(self, address):
            self.address = address
            return connect_result

        def close(self):
            self.closed = True

    sock = FakeSocket()
    monkeypatch.setattr(cli.socket, "socket", lambda: sock)

    assert cli._port_open("127.0.0.2", 12345) is expected
    assert (sock.timeout, sock.address, sock.closed) == (
        1,
        ("127.0.0.2", 12345),
        True,
    )


def test_doctor_healthy_returns_zero_and_prints_details(monkeypatch, capsys):
    driver = FakeDriver()
    _install_doctor_fakes(
        monkeypatch,
        driver,
        {
            "status": "healthy",
            "action": "none",
            "diagnosis": driver.diagnosis,
        },
    )

    assert cli.cmd_doctor() == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["connected_tabs"] == 1
    assert payload["remote_mode"] is True
    assert payload["bridge_http_port"] == 19001
    assert payload["ws_port_open"] is True
    assert payload["http_port_open"] is False
    assert "[OK] healthy: ready" in captured.err


def test_doctor_passes_the_resolved_state_paths_through(monkeypatch, capsys):
    # cmd_doctor rewrites parts of the payload; the state directory and token
    # file have to survive that, because "which file did each side read?" is the
    # question a 401 leaves unanswered and doctor is where a reader looks.
    driver = FakeDriver()
    state_paths = {
        "state_dir": "/home/u/.agent-browser-mcp",
        "state_dir_kind": "legacy",
        "token_file": "/home/u/.agent-browser-mcp/bridge-token",
        "auth_enabled": True,
        "token_fingerprint": "sha256:deadbeef",
    }
    _install_doctor_fakes(
        monkeypatch,
        driver,
        {
            "status": "healthy",
            "action": "none",
            "diagnosis": driver.diagnosis,
            "state_paths": state_paths,
        },
    )

    assert cli.cmd_doctor() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state_paths"] == state_paths


def test_doctor_reload_extension_returns_nonzero(monkeypatch, capsys):
    driver = FakeDriver(diagnosis={"cause": "healthy", "ok": True})
    _install_doctor_fakes(
        monkeypatch,
        driver,
        {
            "status": "stale_extension",
            "action": "reload_extension",
            "diagnosis": driver.diagnosis,
        },
    )

    assert cli.cmd_doctor() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "stale_extension"
    assert "stale_extension" in captured.err
    assert "Reload BrowserTap Bridge" in captured.err


def test_doctor_reports_bridge_failures_and_restart_action(monkeypatch, tmp_path, capsys):
    driver = FakeDriver(
        sessions=RuntimeError("tabs unavailable"),
        diagnosis=RuntimeError("diagnose unavailable"),
    )
    monkeypatch.setattr(cli, "get_driver", lambda: driver)
    monkeypatch.setattr(
        cli,
        "get_setup_status",
        lambda: (_ for _ in ()).throw(RuntimeError("setup unavailable")),
    )
    monkeypatch.setattr(cli, "chrome_extension_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_port_open", lambda *args: False)

    assert cli.cmd_doctor() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "bridge_unreachable"
    assert payload["connected_tabs"] == 0
    assert payload["error"] == "tabs unavailable"
    assert payload["setup_error"] == "setup unavailable"
    assert payload["diagnosis"]["cause"] == "diagnose_failed"
    assert "browsertap-mcp" in payload["next_steps"][-1]
    assert "hermes" not in payload["next_steps"][-1].lower()
    assert "stale_bridge" in captured.err


def test_doctor_prints_nonhealthy_diagnosis_advice(monkeypatch, capsys):
    diagnosis = {"cause": "registering", "ok": False, "advice": "wait"}
    driver = FakeDriver(diagnosis=diagnosis)
    _install_doctor_fakes(
        monkeypatch,
        driver,
        {"status": "registering", "action": "none"},
    )

    assert cli.cmd_doctor() == 1
    assert "[!!] registering: wait" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command, target",
    [
        ("extension-path", "cmd_extension_path"),
        ("doctor", "cmd_doctor"),
        ("print-hermes-config", "cmd_print_hermes_config"),
    ],
)
def test_main_dispatches_simple_subcommands(monkeypatch, command, target):
    monkeypatch.setattr(cli, target, lambda: 17)
    assert cli.main([command]) == 17


def test_main_dispatches_bridge(monkeypatch):
    monkeypatch.setattr(cli, "cmd_bridge", lambda **kwargs: 23)
    assert cli.main(["bridge"]) == 23


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["bridge", "--stop"], {"stop": True, "restart": False}),
        (["bridge", "--restart"], {"stop": False, "restart": True}),
    ],
)
def test_main_dispatches_bridge_management(monkeypatch, args, expected):
    calls = []
    monkeypatch.setattr(cli, "cmd_bridge", lambda **kwargs: calls.append(kwargs) or 19)
    assert cli.main(args) == 19
    assert calls == [expected]


def test_cmd_bridge_stop_and_restart(monkeypatch, capsys):
    from browsertap_mcp import bridge

    monkeypatch.setattr(
        bridge, "stop_bridge_daemon", lambda: {"status": "stopped", "stopped": True}
    )
    monkeypatch.setattr(
        cli, "spawn_bridge_daemon", lambda **kwargs: kwargs == {"reset_spawn_lock": True}
    )
    assert cli.cmd_bridge(stop=True) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
    assert cli.cmd_bridge(restart=True) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "restarted"


@pytest.mark.parametrize("stop_status", ["identity_mismatch", "unmanaged_running"])
def test_cmd_bridge_refuses_restart_after_unverified_stop(monkeypatch, capsys, stop_status):
    from browsertap_mcp import bridge

    monkeypatch.setattr(
        bridge,
        "stop_bridge_daemon",
        lambda: {"status": stop_status, "stopped": False},
    )
    monkeypatch.setattr(
        cli,
        "spawn_bridge_daemon",
        lambda **kwargs: pytest.fail("must not spawn after an unverified stop"),
    )
    assert cli.cmd_bridge(restart=True) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "restart_failed"


def test_main_without_subcommand_starts_stdio_server(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "get_driver", lambda: calls.append("driver"))
    monkeypatch.setattr(
        cli,
        "mcp",
        SimpleNamespace(run=lambda **kwargs: calls.append(("run", kwargs))),
    )

    assert cli.main([]) == 0
    assert calls == ["driver", ("run", {"transport": "stdio"})]


def test_main_rejects_unknown_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main(["not-a-command"])
    assert exc.value.code == 2
