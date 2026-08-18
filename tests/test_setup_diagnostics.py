from __future__ import annotations

from agent_browser_mcp import __version__
from agent_browser_mcp import server as S


class _Driver:
    def __init__(self, diagnosis, runtime=None):
        self.default_session_id = "chrome:test:7"
        self.is_remote = True
        self._diagnosis = diagnosis
        self._runtime = runtime

    def diagnose(self, timeout=None):
        return dict(self._diagnosis)

    def ext_cmd(self, payload, timeout=15.0, **kwargs):
        assert payload == {"cmd": "bridge_status"}
        if isinstance(self._runtime, Exception):
            raise self._runtime
        return {"data": dict(self._runtime or {})}

    def get_all_sessions(self, timeout=None):
        return [{"id": self.default_session_id, "url": "https://example.test/"}]


def _status(monkeypatch, diagnosis, runtime=None):
    driver = _Driver(diagnosis, runtime)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "compact_tabs",
        lambda timeout=None, fresh=False: [
            {"id": "chrome:test:7", "url": "https://example.test/"}
        ],
    )
    return S.get_setup_status()


def test_setup_status_reports_all_equal_components_as_healthy(monkeypatch):
    result = _status(
        monkeypatch,
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": __version__,
            "extension_version": __version__,
            "protocol_version": 3,
            "extension_capabilities": {"content_command_channel_removed": True},
        },
    )

    assert result["status"] == "healthy"
    assert result["action"] == "none"
    assert result["extension_name"] == "Agent Browser MCP Bridge"
    assert result["restart_bridge_required"] is False
    assert result["reload_extension_required"] is False


def test_setup_status_classifies_old_bridge_before_extension(monkeypatch):
    result = _status(
        monkeypatch,
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": "0.2.9",
            "extension_version": __version__,
            "protocol_version": 3,
            "extension_capabilities": {"content_command_channel_removed": True},
        },
    )

    assert result["status"] == "stale_bridge"
    assert result["action"] == "restart_bridge"
    assert result["restart_bridge_required"] is True
    assert result["reload_extension_required"] is False


def test_setup_status_classifies_old_extension_runtime(monkeypatch):
    result = _status(
        monkeypatch,
        {"cause": "healthy", "ok": True, "bridge_version": __version__},
        {"extension_version": "0.2.9", "protocol_version": 2},
    )

    assert result["status"] == "stale_extension"
    assert result["action"] == "reload_extension"
    assert result["restart_bridge_required"] is False
    assert result["reload_extension_required"] is True


def test_setup_status_treats_missing_extension_capability_as_stale(monkeypatch):
    result = _status(
        monkeypatch,
        {"cause": "healthy", "ok": True, "bridge_version": __version__},
        {},
    )

    assert result["status"] == "stale_extension"
    assert result["extension_version"] is None
    assert result["protocol_version"] is None


def test_setup_status_requires_removed_content_command_channel(monkeypatch):
    result = _status(
        monkeypatch,
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": __version__,
            "extension_version": __version__,
            "protocol_version": 3,
            "extension_capabilities": {},
        },
    )

    assert result["status"] == "stale_extension"
    assert result["reload_extension_required"] is True
    assert result["missing_extension_capabilities"] == [
        "content_command_channel_removed"
    ]


def test_setup_status_preserves_unreachable_bridge_as_primary_action(monkeypatch):
    driver = _Driver(
        {"cause": "bridge_unreachable", "ok": False, "error": "refused"},
        RuntimeError("refused"),
    )
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "compact_tabs",
        lambda timeout=None, fresh=False: (_ for _ in ()).throw(RuntimeError("refused")),
    )

    result = S.get_setup_status()

    assert result["status"] == "bridge_unreachable"
    assert result["action"] == "restart_bridge"
    assert result["bridge_error"] == "refused"


def test_setup_status_resurrects_cached_remote_bridge(monkeypatch):
    driver = _Driver(
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": __version__,
            "extension_version": __version__,
            "protocol_version": 3,
            "extension_capabilities": {"content_command_channel_removed": True},
        }
    )
    spawned = []
    monkeypatch.setattr(S, "_driver", driver)
    monkeypatch.setattr(S, "_sessions_cache", None)
    monkeypatch.setattr(S, "_port_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        S,
        "spawn_bridge_daemon",
        lambda: spawned.append("spawned") or True,
    )

    result = S.get_setup_status()

    assert spawned == ["spawned"]
    assert result["status"] == "healthy"
    assert result["connected_tabs"] == 1
