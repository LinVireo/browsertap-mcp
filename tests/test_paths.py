"""Every file BTAP writes outside the package must resolve through paths.state_dir.

The literal ``.browsertap`` used to be repeated at five call sites and
only the pid file consulted ``BROWSERTAP_STATE_DIR``, so a redirected state
directory took the pid file with it and silently left the token, the log and both
lock files in the real home directory. These tests pin the whole set, because a
new writer that rebuilds the path by hand reintroduces exactly that split
without failing anything else.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from browsertap_mcp import bridge, browser_bridge, paths, physical_input, server


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

    assert paths.state_dir() == tmp_path / ".browsertap"


def test_state_dir_lookup_alone_creates_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "absent"))

    assert not paths.state_dir().exists()
    assert paths.state_dir(create=True).is_dir()


def test_blank_override_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.STATE_DIR_ENV, "   ")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert paths.state_dir() == tmp_path / ".browsertap"


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


def test_legacy_state_dir_is_kept_when_it_is_the_only_one(monkeypatch, tmp_path):
    """A pre-0.4.0 install keeps its bridge token instead of being handed a new one."""
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    legacy = tmp_path / paths.LEGACY_STATE_DIR_NAME
    legacy.mkdir()

    assert paths.state_dir() == legacy


def test_new_state_dir_wins_once_it_exists(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / paths.LEGACY_STATE_DIR_NAME).mkdir()
    (tmp_path / paths.DEFAULT_STATE_DIR_NAME).mkdir()

    assert paths.state_dir() == tmp_path / paths.DEFAULT_STATE_DIR_NAME


def test_legacy_state_dir_is_ignored_when_the_override_is_set(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / paths.LEGACY_STATE_DIR_NAME).mkdir()
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "redirected"))

    assert paths.state_dir() == tmp_path / "redirected"


def test_legacy_state_dir_must_be_a_directory(monkeypatch, tmp_path):
    """A stray file with the old name is not a state directory to adopt."""
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / paths.LEGACY_STATE_DIR_NAME).write_text("not a directory", encoding="utf-8")

    assert paths.state_dir() == tmp_path / paths.DEFAULT_STATE_DIR_NAME


def test_legacy_env_names_fill_in_their_replacements():
    env = {"AGENT_BROWSER_MODE": "lab", "AGENT_BROWSER_NO_SPAWN": "1"}

    adopted = paths.adopt_legacy_env(env)

    assert env["BROWSERTAP_MODE"] == "lab"
    assert env["BROWSERTAP_NO_SPAWN"] == "1"
    assert sorted(adopted) == ["BROWSERTAP_MODE", "BROWSERTAP_NO_SPAWN"]


def test_renamed_env_names_are_not_a_prefix_swap():
    """The bridge host and port lost the `TMWD` of the class 0.4.0 deleted."""
    env = {"AGENT_BROWSER_TMWD_HOST": "10.0.0.2", "AGENT_BROWSER_TMWD_PORT": "19765"}

    paths.adopt_legacy_env(env)

    assert env["BROWSERTAP_BRIDGE_HOST"] == "10.0.0.2"
    assert env["BROWSERTAP_BRIDGE_PORT"] == "19765"
    assert "BROWSERTAP_TMWD_HOST" not in env
    assert "BROWSERTAP_TMWD_PORT" not in env


def test_new_env_name_wins_over_the_legacy_one():
    env = {"AGENT_BROWSER_MODE": "lab", "BROWSERTAP_MODE": "default"}

    assert paths.adopt_legacy_env(env) == []
    assert env["BROWSERTAP_MODE"] == "default"


def test_adopting_legacy_env_twice_changes_nothing():
    env = {"AGENT_BROWSER_MODE": "lab"}

    assert paths.adopt_legacy_env(env) == ["BROWSERTAP_MODE"]
    assert paths.adopt_legacy_env(env) == []
    assert env["BROWSERTAP_MODE"] == "lab"
