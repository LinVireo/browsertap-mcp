from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_browser_mcp import bridge


class FakeDriver:
    def __init__(self, *, remote=False):
        self.is_remote = remote
        self.closed = False

    def close(self):
        self.closed = True


def test_remote_bridge_exits_without_sleeping(monkeypatch, caplog):
    caplog.set_level("INFO", logger="agent_browser_mcp.bridge")
    driver = FakeDriver(remote=True)
    calls = []
    monkeypatch.setattr(bridge, "BrowserBridge", lambda **kwargs: driver)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: calls.append(seconds))
    monkeypatch.setenv("AGENT_BROWSER_TMWD_HOST", "127.0.0.8")
    monkeypatch.setenv("AGENT_BROWSER_TMWD_PORT", "19876")

    assert bridge.main([]) == 0
    assert calls == []
    assert driver.closed is True
    assert "Bridge already running at 127.0.0.8:19876" in caplog.text


def test_local_bridge_handles_interrupt_and_closes_driver(monkeypatch, caplog):
    caplog.set_level("INFO", logger="agent_browser_mcp.bridge")
    driver = FakeDriver()
    monkeypatch.setattr(bridge, "BrowserBridge", lambda **kwargs: driver)
    monkeypatch.setattr(
        bridge.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(bridge.os, "getpid", lambda: 4321)
    monkeypatch.setattr(
        bridge,
        "_write_bridge_record",
        lambda **kwargs: {"instance_id": kwargs["instance_id"]},
    )
    monkeypatch.setattr(bridge, "_remove_record_if_owned", lambda record: None)

    assert bridge.main([]) == 0
    assert driver.closed is True
    output = caplog.text
    assert "Bridge started: ws=18765 http=18766 pid=4321" in output
    assert "Bridge stopped" in output


def test_local_bridge_runtime_failure_propagates_but_closes(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(bridge, "BrowserBridge", lambda **kwargs: driver)
    monkeypatch.setattr(
        bridge.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(RuntimeError("loop failed")),
    )
    monkeypatch.setattr(
        bridge,
        "_write_bridge_record",
        lambda **kwargs: {"instance_id": kwargs["instance_id"]},
    )
    monkeypatch.setattr(bridge, "_remove_record_if_owned", lambda record: None)

    with pytest.raises(RuntimeError, match="loop failed"):
        bridge.main([])
    assert driver.closed is True


def test_bridge_constructor_failure_propagates(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "BrowserBridge",
        lambda **kwargs: (_ for _ in ()).throw(OSError("bind failed")),
    )

    with pytest.raises(OSError, match="bind failed"):
        bridge.main([])


def test_bridge_rejects_non_numeric_port_before_constructing(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_TMWD_PORT", "not-a-port")
    monkeypatch.setattr(
        bridge,
        "BrowserBridge",
        lambda **kwargs: pytest.fail("driver must not be constructed"),
    )

    with pytest.raises(ValueError, match="invalid literal"):
        bridge.main([])


def test_remote_driver_without_close_is_supported(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "BrowserBridge",
        lambda **kwargs: SimpleNamespace(is_remote=True),
    )

    assert bridge.main([]) == 0


def test_bridge_record_is_atomic_and_private(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        bridge,
        "process_identity",
        lambda pid: {"pid": pid, "creation_ticks": 123, "executable": "python"},
    )
    monkeypatch.setattr(bridge.os, "getpid", lambda: 44)

    record = bridge._write_bridge_record(
        instance_id="instance-44", host="127.0.0.1", port=18765
    )

    assert bridge.read_bridge_record() == record
    assert json.loads(bridge.bridge_pid_path().read_text(encoding="utf-8"))["pid"] == 44


def test_stop_bridge_refuses_unmanaged_listener(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "_configured_bridge_port_open", lambda: True)
    monkeypatch.setattr(
        bridge,
        "_terminate_process",
        lambda *args: pytest.fail("an unmanaged listener must never be terminated"),
    )

    result = bridge.stop_bridge_daemon()

    assert result["status"] == "unmanaged_running"
    assert result["stopped"] is False
    assert "TROUBLESHOOTING.md" in result["error"]


def test_stop_bridge_reports_no_managed_or_listening_process(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "_configured_bridge_port_open", lambda: False)

    assert bridge.stop_bridge_daemon() == {"status": "not_running", "stopped": False}


def test_stop_bridge_refuses_reused_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", str(tmp_path))
    record = {
        "pid": 45,
        "creation_ticks": 100,
        "executable": "python",
        "instance_id": "old-instance",
    }
    bridge.bridge_pid_path().write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(
        bridge,
        "process_identity",
        lambda pid: {"pid": pid, "creation_ticks": 101, "executable": "python"},
    )
    monkeypatch.setattr(
        bridge,
        "_terminate_process",
        lambda *args: pytest.fail("a reused pid must never be terminated"),
    )

    result = bridge.stop_bridge_daemon()

    assert result["status"] == "identity_mismatch"
    assert not bridge.bridge_pid_path().exists()


def test_stop_bridge_terminates_only_matching_record(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", str(tmp_path))
    record = {
        "pid": 46,
        "creation_ticks": 200,
        "executable": "python",
        "instance_id": "current-instance",
    }
    bridge.bridge_pid_path().write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(bridge, "process_identity", lambda pid: dict(record))
    calls = []
    monkeypatch.setattr(
        bridge, "_terminate_process", lambda pid, timeout: calls.append((pid, timeout)) or True
    )

    result = bridge.stop_bridge_daemon(timeout=1.25)

    assert result == {"status": "stopped", "stopped": True, "pid": 46}
    assert calls == [(46, 1.25)]
    assert not bridge.bridge_pid_path().exists()


def test_stop_bridge_keeps_record_when_identity_is_unknowable(monkeypatch, tmp_path):
    """An unqueryable pid is not a dead pid.

    Folding ProcessIdentityUnavailable into "not running" deletes bridge.pid
    while the daemon is still holding the ports, so the next --restart spawns a
    rival that cannot bind and the record ends up pointing at the broken one.
    """
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", str(tmp_path))
    record = {
        "pid": 47,
        "creation_ticks": 300,
        "executable": "python",
        "instance_id": "elevated-instance",
    }
    bridge.bridge_pid_path().write_text(json.dumps(record), encoding="utf-8")

    def denied(pid):
        raise bridge.ProcessIdentityUnavailable(
            f"OpenProcess failed for pid {pid} with WinError 5"
        )

    monkeypatch.setattr(bridge, "process_identity", denied)
    monkeypatch.setattr(
        bridge,
        "_terminate_process",
        lambda *args: pytest.fail("an unidentifiable process must never be terminated"),
    )

    result = bridge.stop_bridge_daemon()

    assert result["status"] == "identity_unavailable"
    assert result["stopped"] is False
    assert result["pid"] == 47
    assert bridge.bridge_pid_path().exists()


def test_process_is_gone_requires_proof(monkeypatch):
    monkeypatch.setattr(bridge, "process_identity", lambda pid: None)
    assert bridge._process_is_gone(9) is True

    def denied(pid):
        raise bridge.ProcessIdentityUnavailable("access denied")

    monkeypatch.setattr(bridge, "process_identity", denied)
    assert bridge._process_is_gone(9) is False


def test_write_bridge_record_refuses_when_own_identity_is_unknowable(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BROWSER_STATE_DIR", str(tmp_path))

    def denied(pid):
        raise bridge.ProcessIdentityUnavailable("GetProcessTimes failed")

    monkeypatch.setattr(bridge, "process_identity", denied)

    with pytest.raises(RuntimeError, match="refusing unmanaged startup"):
        bridge._write_bridge_record(instance_id="i", host="127.0.0.1", port=18765)
    assert not bridge.bridge_pid_path().exists()


def test_process_identity_rejects_non_pids():
    assert bridge.process_identity(0) is None
    assert bridge.process_identity(-1) is None
    assert bridge.process_identity(True) is None  # noqa: E712 - bool is not a pid
    assert bridge.process_identity("7") is None


def test_darwin_process_identity_parses_ps(monkeypatch):
    """Darwin has no /proc; without this backend the daemon cannot start there.

    Verified against a stubbed `ps` only — the real command still needs one
    smoke test on macOS.
    """
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("env", {}).get("LC_ALL")))
        return SimpleNamespace(
            returncode=0,
            stdout="Mon Aug 18 03:14:07 2026 /usr/local/bin/python3.12\n",
            stderr="",
        )

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    identity = bridge._darwin_process_identity(4242)

    assert identity["pid"] == 4242
    assert identity["executable"].endswith("python3.12")
    assert identity["creation_ticks"] == int(
        bridge.time.mktime(bridge.time.strptime("Mon Aug 18 03:14:07 2026",
                                                "%a %b %d %H:%M:%S %Y"))
    )
    # The date format is only stable with the C locale pinned.
    assert calls[0][1] == "C"
    assert "-p" in calls[0][0] and "4242" in calls[0][0]


def test_darwin_process_identity_separates_missing_from_opaque(monkeypatch):
    monkeypatch.setattr(
        bridge.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    assert bridge._darwin_process_identity(4242) is None

    monkeypatch.setattr(
        bridge.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="garbage\n", stderr=""),
    )
    with pytest.raises(bridge.ProcessIdentityUnavailable):
        bridge._darwin_process_identity(4242)

    def boom(argv, **kwargs):
        raise OSError("ps missing")

    monkeypatch.setattr(bridge.subprocess, "run", boom)
    with pytest.raises(bridge.ProcessIdentityUnavailable):
        bridge._darwin_process_identity(4242)


def test_posix_process_identity_separates_missing_from_denied(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "Path", lambda *a: tmp_path / "no-proc")
    assert bridge._posix_process_identity(4242) is None

    # procfs fields after the final ')' start at field 3 (state); starttime is
    # field 22, i.e. index 19 of that slice.
    tail = " ".join(["S"] + [str(n) for n in range(1, 19)] + ["998877"])
    stat_line = f"4242 (py thon) {tail}"

    class FakeProc:
        """/proc/<pid> where stat is readable but exe is another user's."""

        def __truediv__(self, name):
            if str(name) == "stat":
                return SimpleNamespace(read_text=lambda encoding=None: stat_line)
            if str(name) == "exe":
                def resolve(strict=False):
                    raise PermissionError("operation not permitted")
                return SimpleNamespace(resolve=resolve)
            return self  # Path("/proc") / pid

    monkeypatch.setattr(bridge, "Path", lambda *a: FakeProc())
    with pytest.raises(bridge.ProcessIdentityUnavailable):
        bridge._posix_process_identity(4242)


def test_posix_process_identity_reads_starttime_past_a_spaced_comm(monkeypatch):
    tail = " ".join(["S"] + [str(n) for n in range(1, 19)] + ["998877"])
    stat_line = f"4242 (py thon) {tail}"

    class FakeProc:
        def __truediv__(self, name):
            if str(name) == "stat":
                return SimpleNamespace(read_text=lambda encoding=None: stat_line)
            if str(name) == "exe":
                return SimpleNamespace(resolve=lambda strict=False: "/usr/bin/python3")
            return self

    monkeypatch.setattr(bridge, "Path", lambda *a: FakeProc())
    identity = bridge._posix_process_identity(4242)

    assert identity == {
        "pid": 4242,
        "creation_ticks": 998877,
        "executable": "/usr/bin/python3",
    }
