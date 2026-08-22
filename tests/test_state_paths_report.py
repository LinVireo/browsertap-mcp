"""Which state directory and token file a process resolved must be observable.

`paths.state_dir` has two ways of answering ("BROWSERTAP_STATE_DIR", and the
pre-0.4.0 `~/.agent-browser-mcp` when it is the only directory that exists) and
`bridge_token_path` has a third override on top. Until this report existed,
nothing printed any of them: a 401 whose body says only "missing or bad bridge
token" left an operator guessing which of two identically-named files each
process had read. These tests pin the answer *and* pin that the token itself is
never part of it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from browsertap_mcp import browser_bridge, paths
from browsertap_mcp.browser_bridge import state_paths_report, token_fingerprint

TOKEN = "s3cret-token-value-do-not-print"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A clean home with no state directory and no overrides in the way."""
    monkeypatch.delenv(paths.STATE_DIR_ENV, raising=False)
    monkeypatch.delenv(browser_bridge.TOKEN_FILE_ENV, raising=False)
    monkeypatch.delenv(browser_bridge.TOKEN_AUTH_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _write_token(directory: Path, token: str = TOKEN) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bridge-token"
    path.write_text(token + "\n", encoding="utf-8")
    return path


def test_the_default_directory_is_reported_as_default(home):
    _write_token(home / ".browsertap")

    report = state_paths_report()

    assert report["state_dir"] == str(home / ".browsertap")
    assert report["state_dir_kind"] == "default"
    assert report["state_dir_exists"] is True
    assert report["state_dir_env"] is None
    assert report["token_file"] == str(home / ".browsertap" / "bridge-token")
    assert report["token_file_exists"] is True
    assert report["token_file_from_env"] is False


def test_a_pre_rename_install_says_it_is_on_the_legacy_directory(home):
    # The upgrade case this exists for: the old directory keeps being used, so
    # the report has to name it rather than the one the docs mention.
    _write_token(home / ".agent-browser-mcp")

    report = state_paths_report()

    assert report["state_dir"] == str(home / ".agent-browser-mcp")
    assert report["state_dir_kind"] == "legacy"
    assert report["default_state_dir_name"] == ".browsertap"


def test_the_new_directory_wins_once_it_exists(home):
    _write_token(home / ".agent-browser-mcp")
    _write_token(home / ".browsertap")

    assert state_paths_report()["state_dir_kind"] == "default"


def test_an_env_override_is_named_so_it_can_be_unset(home, monkeypatch, tmp_path):
    elsewhere = tmp_path / "scratch-state"
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(elsewhere))

    report = state_paths_report()

    assert report["state_dir"] == str(elsewhere)
    assert report["state_dir_kind"] == "env"
    assert report["state_dir_env"] == paths.STATE_DIR_ENV
    # Asking where a file would go must not create it (paths.state_dir docstring).
    assert report["state_dir_exists"] is False
    assert report["token_file_exists"] is False
    assert report["token_fingerprint"] is None


def test_an_overridden_token_file_is_flagged_separately(home, monkeypatch, tmp_path):
    # The two overrides are independent: a token file outside the state dir is a
    # supported configuration and the usual reason two processes disagree.
    path = _write_token(tmp_path / "custom")
    monkeypatch.setenv(browser_bridge.TOKEN_FILE_ENV, str(path))

    report = state_paths_report()

    assert report["token_file"] == str(path)
    assert report["token_file_from_env"] is True
    assert report["state_dir"] == str(home / ".browsertap")


def test_disabled_auth_is_reported_as_disabled(home, monkeypatch):
    _write_token(home / ".browsertap")
    monkeypatch.setenv(browser_bridge.TOKEN_AUTH_ENV, "off")

    assert state_paths_report()["auth_enabled"] is False
    # The file is still there and still readable; only enforcement is off.
    assert state_paths_report()["token_file_exists"] is True


def test_the_report_never_contains_the_token(home):
    _write_token(home / ".browsertap")

    report = state_paths_report(enforced_token=TOKEN)

    assert TOKEN not in repr(report)
    assert report["token_fingerprint"] == token_fingerprint(TOKEN)
    assert report["token_fingerprint"].startswith("sha256:")
    assert len(report["token_fingerprint"]) == len("sha256:") + 8


def test_a_daemon_running_from_before_the_file_changed_is_visible(home):
    # The documented 401 that survives every editor restart: the daemon locked
    # its token in memory at start-up, the file was replaced afterwards.
    _write_token(home / ".browsertap", "new-token")

    report = state_paths_report(enforced_token="token-from-the-old-daemon")

    assert report["token_matches_file"] is False
    assert report["enforced_token_fingerprint"] != report["token_fingerprint"]


def test_a_matching_daemon_token_says_so(home):
    _write_token(home / ".browsertap")

    report = state_paths_report(enforced_token=TOKEN)

    assert report["token_matches_file"] is True
    assert report["enforced_token_fingerprint"] == report["token_fingerprint"]


@pytest.mark.parametrize("enforced", [None, "", 0])
def test_the_comparison_fields_are_absent_when_no_token_is_enforced(home, enforced):
    # "" is what bridge_token() returns with authentication off, so it must not
    # read as a daemon whose token stopped matching the file.
    _write_token(home / ".browsertap")

    report = state_paths_report(enforced_token=enforced)

    assert "token_matches_file" not in report
    assert "enforced_token_fingerprint" not in report


def test_a_missing_file_cannot_be_reported_as_a_match(home):
    (home / ".browsertap").mkdir()

    report = state_paths_report(enforced_token=TOKEN)

    assert report["token_file_exists"] is False
    assert report["token_matches_file"] is False


@pytest.mark.parametrize("value", ["", None, 0, b"bytes"])
def test_a_fingerprint_of_nothing_is_none_not_a_hash_of_nothing(value):
    assert token_fingerprint(value) is None


def test_asking_for_the_report_does_not_create_the_token_file(home):
    (home / ".browsertap").mkdir()
    state_paths_report()

    # bridge_token() creates on demand; the report must only ever read, or
    # running `doctor` against a disabled install would silently arm auth.
    assert not (home / ".browsertap" / "bridge-token").exists()
