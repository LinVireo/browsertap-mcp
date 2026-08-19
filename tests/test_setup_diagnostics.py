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


def test_setup_status_blames_this_process_when_components_are_newer(monkeypatch):
    """A bare `!=` sent the user round a loop they could not exit.

    Upgrading the package while an MCP session is live leaves this process on
    the old `__version__` while the bridge and extension on disk are already
    new. The old code answered `stale_bridge` / `restart_bridge`, but a restart
    re-reads the same new files and reports the same mismatch, and the same held
    for `reload_extension` after the user had just reloaded it. The stale build
    is this process, so that is what the verdict has to name.
    """
    result = _status(
        monkeypatch,
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": "99.0.0",
            "extension_version": "99.0.0",
            "protocol_version": 3,
            "extension_capabilities": {"content_command_channel_removed": True},
        },
    )

    assert result["status"] == "stale_package"
    assert result["action"] == "restart_mcp_session"
    assert result["restart_mcp_session_required"] is True
    # Neither of these can clear the mismatch, so neither may ask for it.
    assert result["restart_bridge_required"] is False
    assert result["reload_extension_required"] is False
    assert "Restart the MCP session or client" in result["notes"][0]


def test_setup_status_still_blames_an_older_component_not_this_process(monkeypatch):
    """The common direction must keep its existing verdict."""
    result = _status(
        monkeypatch,
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": "0.0.1",
            "extension_version": __version__,
            "protocol_version": 3,
            "extension_capabilities": {"content_command_channel_removed": True},
        },
    )

    assert result["status"] == "stale_bridge"
    assert result["action"] == "restart_bridge"
    assert result["restart_mcp_session_required"] is False


def test_setup_status_blames_this_process_for_a_newer_protocol(monkeypatch):
    """Protocol skew has the same two directions as a version string."""
    result = _status(
        monkeypatch,
        {"cause": "healthy", "ok": True, "bridge_version": __version__},
        {
            "extension_version": __version__,
            "protocol_version": S._EXTENSION_PROTOCOL_VERSION + 1,
            "capabilities": {"content_command_channel_removed": True},
        },
    )

    assert result["status"] == "stale_package"
    assert result["action"] == "restart_mcp_session"
    assert result["reload_extension_required"] is False


def test_setup_status_falls_back_to_inequality_for_unorderable_versions(monkeypatch):
    """An unparseable version cannot be given a direction, so it must not be
    guessed into `stale_package`: the conservative answer is the old one."""
    result = _status(
        monkeypatch,
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": "not-a-version",
            "extension_version": __version__,
            "protocol_version": 3,
            "extension_capabilities": {"content_command_channel_removed": True},
        },
    )

    assert result["status"] == "stale_bridge"
    assert result["restart_bridge_required"] is True
    assert result["restart_mcp_session_required"] is False


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
