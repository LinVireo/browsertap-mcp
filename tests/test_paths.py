"""Every file ABM writes outside the package must resolve through paths.state_dir.

The literal ``.agent-browser-mcp`` used to be repeated at five call sites and
only the pid file consulted ``AGENT_BROWSER_STATE_DIR``, so a redirected state
directory took the pid file with it and silently left the token, the log and both
lock files in the real home directory. These tests pin the whole set, because a
new writer that rebuilds the path by hand reintroduces exactly that split
without failing anything else.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_browser_mcp import bridge, browser_bridge, paths, physical_input, server


def _all_state_paths() -> dict[str, Path]:
    return {
        "bridge.pid": bridge.bridge_pid_path(),
        "bridge-token": browser_bridge.bridge_token_path(),
        "physical-input.lock": physical_input._default_lock_path(),
        "bridge.log": server._bridge_log_path(),
        "spawn.lock": server._spawn_lock_path(),
    }


def test_state_dir_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert paths.state_dir() == tmp_path / ".agent-browser-mcp"


def test_state_dir_lookup_alone_creates_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "absent"))

    assert not paths.state_dir().exists()
    assert paths.state_dir(create=True).is_dir()


def test_blank_override_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.STATE_DIR_ENV, "   ")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert paths.state_dir() == tmp_path / ".agent-browser-mcp"


@pytest.mark.parametrize("name", sorted(_all_state_paths()))
def test_every_state_file_follows_the_override(monkeypatch, tmp_path, name):
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "redirected"))

    resolved = _all_state_paths()[name]

    assert resolved.parent == tmp_path / "redirected", (
        f"{name} ignored {paths.STATE_DIR_ENV} and would be written outside the "
        "configured state directory"
    )
    assert resolved.name == name


def test_token_file_override_still_wins_over_state_dir(monkeypatch, tmp_path):
    """The narrower override keeps precedence; only the default moved."""
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "redirected"))
    monkeypatch.setenv(browser_bridge.TOKEN_FILE_ENV, str(tmp_path / "explicit-token"))

    assert browser_bridge.bridge_token_path() == tmp_path / "explicit-token"
