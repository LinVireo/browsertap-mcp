from __future__ import annotations

import json

import pytest

from browsertap_mcp import cli, native_host, native_installer


@pytest.mark.parametrize("argv,expected", [
    (["install-native-host"], ("install", None)),
    (["install-native-host", "--extension-id", "a" * 32], ("install", "a" * 32)),
    (["uninstall-native-host"], ("uninstall", None)),
])
def test_native_installer_commands_dispatch_and_print_json(monkeypatch, capsys, argv, expected):
    calls = []
    monkeypatch.setattr(native_installer, "install_native_host",
                        lambda extension_id=None: calls.append(("install", extension_id)) or {"status": "installed"})
    monkeypatch.setattr(native_installer, "uninstall_native_host",
                        lambda: calls.append(("uninstall", None)) or {"status": "removed"})
    monkeypatch.setattr(cli, "get_driver", lambda: pytest.fail("installer started a bridge"))
    assert cli.main(argv) == 0
    assert calls == [expected]
    assert json.loads(capsys.readouterr().out)["status"] in {"installed", "removed"}


@pytest.mark.parametrize("uninstall", [False, True])
def test_native_installer_failures_are_structured(monkeypatch, capsys, uninstall):
    def fail(**kwargs):
        raise native_installer.NativeInstallError("foreign_registration", "Another installation owns the registration")

    monkeypatch.setattr(native_installer, "install_native_host", fail)
    monkeypatch.setattr(native_installer, "uninstall_native_host", fail)
    assert cli.cmd_native_install(uninstall=uninstall) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "foreign_registration"
    assert payload["status"] == ("uninstall_failed" if uninstall else "install_failed")


def test_native_installer_unexpected_io_does_not_log_raw_data(monkeypatch, capsys):
    def fail(**kwargs):
        raise OSError("synthetic-private-data")

    monkeypatch.setattr(native_installer, "install_native_host", fail)
    assert cli.cmd_native_install() == 1
    captured = capsys.readouterr()
    assert "synthetic-private-data" not in captured.out + captured.err


def test_bridge_native_mode_runs_host_without_starting_foreground_daemon(monkeypatch):
    calls = []
    monkeypatch.setattr(native_host, "main", lambda argv: calls.append(argv) or 17)
    assert cli.main(["bridge", "--mode", "native"]) == 17
    assert calls == [[]]


@pytest.mark.parametrize("flag", ["--stop", "--restart"])
def test_native_mode_rejects_daemon_lifecycle_flags(monkeypatch, capsys, flag):
    monkeypatch.setattr(native_host, "main", lambda argv: pytest.fail("invalid mode launched host"))
    assert cli.main(["bridge", "--mode", "native", flag]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "cannot be combined" in captured.err


def test_doctor_reports_native_installation_without_registering(monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setattr(native_installer, "native_host_status", lambda: {"status": "installed", "installed": True})
    monkeypatch.setattr(cli, "get_driver", lambda: SimpleNamespace(host="127.0.0.1", port=23111))
    monkeypatch.setattr(cli, "get_setup_status", lambda: {
        "status": "healthy", "action": "none", "tabs": [], "diagnosis": {"ok": True},
    })
    monkeypatch.setattr(cli, "_port_open", lambda *args: True)
    assert cli.cmd_doctor() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["native_host"]["status"] == "installed"
    assert payload["next_steps"] == []


@pytest.mark.parametrize("action, guidance", [
    ("restart_bridge", "browsertap bridge --restart"),
    ("reload_extension", "chrome://extensions"),
    ("restart_mcp_session", "Restart the MCP session/client"),
    ("wait_for_extension", "Wait for the extension handshake"),
    ("check_extension_connection", "check its connection status"),
])
def test_doctor_next_steps_follow_the_diagnosed_action(monkeypatch, capsys, action, guidance):
    from types import SimpleNamespace
    monkeypatch.setattr(native_installer, "native_host_status", lambda: {"status": "missing", "installed": False})
    monkeypatch.setattr(cli, "get_driver", lambda: SimpleNamespace(host="127.0.0.1", port=23111))
    monkeypatch.setattr(cli, "get_setup_status", lambda: {
        "status": "unavailable", "action": action, "tabs": [], "diagnosis": {},
    })
    monkeypatch.setattr(cli, "_port_open", lambda *args: True)
    assert cli.cmd_doctor() == 1
    steps = json.loads(capsys.readouterr().out)["next_steps"]
    assert len(steps) == 1 and guidance in steps[0]
    assert "Load the unpacked extension" not in steps[0]
