from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_browser_mcp import bridge


class FakeDriver:
    def __init__(self, *, remote=False):
        self.is_remote = remote
        self.closed = False

    def close(self):
        self.closed = True


def test_remote_bridge_exits_without_sleeping(monkeypatch, capsys):
    driver = FakeDriver(remote=True)
    calls = []
    monkeypatch.setattr(bridge, "TMWebDriver", lambda **kwargs: driver)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: calls.append(seconds))
    monkeypatch.setenv("AGENT_BROWSER_TMWD_HOST", "127.0.0.8")
    monkeypatch.setenv("AGENT_BROWSER_TMWD_PORT", "19876")

    assert bridge.main() == 0
    assert calls == []
    assert driver.closed is True
    assert "bridge already running at 127.0.0.8:19876" in capsys.readouterr().out


def test_local_bridge_handles_interrupt_and_closes_driver(monkeypatch, capsys):
    driver = FakeDriver()
    monkeypatch.setattr(bridge, "TMWebDriver", lambda **kwargs: driver)
    monkeypatch.setattr(
        bridge.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(bridge.os, "getpid", lambda: 4321)

    assert bridge.main() == 0
    assert driver.closed is True
    output = capsys.readouterr().out
    assert "bridge started: ws=18765 http=18766 pid=4321" in output
    assert "bridge stopped" in output


def test_local_bridge_runtime_failure_propagates_but_closes(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(bridge, "TMWebDriver", lambda **kwargs: driver)
    monkeypatch.setattr(
        bridge.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(RuntimeError("loop failed")),
    )

    with pytest.raises(RuntimeError, match="loop failed"):
        bridge.main()
    assert driver.closed is True


def test_bridge_constructor_failure_propagates(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "TMWebDriver",
        lambda **kwargs: (_ for _ in ()).throw(OSError("bind failed")),
    )

    with pytest.raises(OSError, match="bind failed"):
        bridge.main()


def test_bridge_rejects_non_numeric_port_before_constructing(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_TMWD_PORT", "not-a-port")
    monkeypatch.setattr(
        bridge,
        "TMWebDriver",
        lambda **kwargs: pytest.fail("driver must not be constructed"),
    )

    with pytest.raises(ValueError, match="invalid literal"):
        bridge.main()


def test_remote_driver_without_close_is_supported(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "TMWebDriver",
        lambda **kwargs: SimpleNamespace(is_remote=True),
    )

    assert bridge.main() == 0
