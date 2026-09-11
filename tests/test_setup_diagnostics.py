from __future__ import annotations

import pytest

from browsertap_mcp import __version__
from browsertap_mcp import server as S
from browsertap_mcp.extension_build import STAMP_LENGTH, stamp_line, write_extension_stamp


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
            "extension_capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
        },
    )

    assert result["status"] == "healthy"
    assert result["action"] == "none"
    assert result["extension_name"] == "BrowserTap Bridge"
    assert result["restart_bridge_required"] is False
    assert result["reload_extension_required"] is False
    registry = result["capability_registry"]
    assert registry["complete"] is True
    assert set(registry["groups"]) == {"page", "browser", "desktop"}
    assert registry["tool_count"] == registry["declared_tool_count"] == 49
    assert registry["groups"]["desktop"] == []


@pytest.mark.parametrize(
    ("cause", "expected_status", "expected_action"),
    [
        ("starting", "starting", "wait_for_extension"),
        ("ext_never_registered", "extension_unavailable", "check_extension_connection"),
        ("sw_slept_or_dropped", "extension_unavailable", "check_extension_connection"),
        ("healthy", "extension_unavailable", "check_extension_connection"),
    ],
)
def test_missing_runtime_status_does_not_request_reload(
    monkeypatch, cause, expected_status, expected_action
):
    result = _status(
        monkeypatch,
        {
            "cause": cause,
            "ok": False,
            "bridge_version": __version__,
            "ever_registered": False,
            "bridge_uptime_seconds": 2.0 if cause == "starting" else 30.0,
            "startup_grace_seconds": 10.0,
        },
        RuntimeError("Extension not connected"),
    )

    assert result["status"] == expected_status
    assert result["action"] == expected_action
    assert result["reload_extension_required"] is False
    assert result["extension_status_available"] is False
    assert result["missing_extension_capabilities"] == []
    assert result["extension_build_verdict"] == "unverifiable"
    assert result["extension_build_enforced"] is False
    assert not any("Reload the unpacked extension" in note for note in result["notes"])


@pytest.mark.parametrize(
    ("version", "expected_status", "expected_action"),
    [
        ("0.0.1", "stale_bridge", "restart_bridge"),
        ("99.0.0", "stale_package", "restart_mcp_session"),
    ],
)
def test_missing_handshake_does_not_hide_a_known_stale_component(
    monkeypatch, version, expected_status, expected_action
):
    result = _status(
        monkeypatch,
        {"cause": "starting", "ok": False, "bridge_version": version},
        RuntimeError("Extension not connected"),
    )

    assert result["status"] == expected_status
    assert result["action"] == expected_action
    assert result["reload_extension_required"] is False


def test_extension_connecting_during_the_fallback_probe_completes_startup(
    monkeypatch, tmp_path
):
    directory, stamp = _extension_tree(tmp_path, "connected-during-probe")
    result = _build_status(
        monkeypatch,
        directory,
        {"cause": "starting", "ok": False, "bridge_version": __version__},
        runtime={
            "extension_version": __version__,
            "protocol_version": 3,
            "capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
            "build_stamp": stamp,
        },
    )

    assert result["status"] == "healthy"
    assert result["action"] == "none"
    assert result["extension_status_available"] is True
    assert result["reload_extension_required"] is False
    assert result["extension_build_verdict"] == "matches_tree"


def test_setup_status_classifies_old_bridge_before_extension(monkeypatch):
    result = _status(
        monkeypatch,
        {
            "cause": "healthy",
            "ok": True,
            "bridge_version": "0.2.9",
            "extension_version": __version__,
            "protocol_version": 3,
            "extension_capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
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
    assert result["extension_status_available"] is True


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
        "batch_result_guard",
        "content_command_channel_removed",
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
            "extension_capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
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
            "extension_capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
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
            "capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
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
            "extension_capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
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
            "extension_capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
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


# --- state directory and token file ------------------------------------------
# The bridge daemon and the MCP server resolve these independently, each from
# its own environment, and a mismatch shows up only as a 401 whose body names no
# path at all. get_setup_status therefore reports both answers and says which
# fields differ; `doctor` prints the same payload.
_LOCAL_PATHS = {
    "state_dir": "/home/u/.browsertap",
    "state_dir_exists": True,
    "state_dir_kind": "default",
    "state_dir_env": None,
    "default_state_dir_name": ".browsertap",
    "token_file": "/home/u/.browsertap/bridge-token",
    "token_file_exists": True,
    "token_file_from_env": False,
    "auth_enabled": True,
    "token_fingerprint": "sha256:aaaaaaaa",
}


def _healthy(**extra):
    diagnosis = {
        "cause": "healthy",
        "ok": True,
        "bridge_version": __version__,
        "extension_version": __version__,
        "protocol_version": 3,
        "extension_capabilities": {
            "content_command_channel_removed": True,
            "batch_result_guard": True,
        },
    }
    diagnosis.update(extra)
    return diagnosis


def _paths_status(monkeypatch, bridge_paths, local=None):
    monkeypatch.setattr(S, "state_paths_report", lambda: dict(local or _LOCAL_PATHS))
    diagnosis = _healthy() if bridge_paths is None else _healthy(state_paths=bridge_paths)
    return _status(monkeypatch, diagnosis)


def test_setup_status_reports_the_resolved_state_directory(monkeypatch):
    result = _paths_status(monkeypatch, dict(_LOCAL_PATHS))

    assert result["state_paths"]["state_dir"] == "/home/u/.browsertap"
    assert result["state_paths"]["token_file"].endswith("bridge-token")
    assert result["state_paths"]["auth_enabled"] is True
    # Agreement is silence: nothing to fix, nothing added to notes.
    assert "state_paths_disagreement" not in result


def test_setup_status_reports_the_bridge_answer_next_to_its_own(monkeypatch):
    bridge_paths = dict(_LOCAL_PATHS)
    result = _paths_status(monkeypatch, bridge_paths)

    assert result["diagnosis"]["state_paths"] == bridge_paths


def test_setup_status_flags_two_processes_on_different_state_directories(monkeypatch):
    # The upgrade case: the daemon started before the new directory existed, so
    # it is still serving out of the pre-0.4.0 one with a different token.
    bridge_paths = dict(
        _LOCAL_PATHS,
        state_dir="/home/u/.agent-browser-mcp",
        state_dir_kind="legacy",
        token_file="/home/u/.agent-browser-mcp/bridge-token",
        token_fingerprint="sha256:bbbbbbbb",
    )

    result = _paths_status(monkeypatch, bridge_paths)

    disagreement = result["state_paths_disagreement"]
    assert set(disagreement) == {"state_dir", "token_file", "token_fingerprint"}
    assert disagreement["token_file"] == {
        "this_process": "/home/u/.browsertap/bridge-token",
        "bridge": "/home/u/.agent-browser-mcp/bridge-token",
    }
    # Leading note, because every other field in the payload looks healthy here.
    assert "state_paths_disagreement" in result["notes"][0]
    assert "different environments" in result["notes"][0]


def test_setup_status_separates_a_stale_daemon_from_a_path_mismatch(monkeypatch):
    # Same paths, but the daemon locked its token in memory before the file was
    # replaced. This one a bridge restart does fix, and the note says so.
    bridge_paths = dict(_LOCAL_PATHS, token_matches_file=False)

    result = _paths_status(monkeypatch, bridge_paths)

    disagreement = result["state_paths_disagreement"]
    assert disagreement == {"bridge_token_is_from_before_the_file_changed": True}
    assert "browsertap bridge --restart" in result["notes"][0]


def test_setup_status_flags_auth_enabled_on_only_one_side(monkeypatch):
    bridge_paths = dict(_LOCAL_PATHS, auth_enabled=False, token_fingerprint=None)

    result = _paths_status(monkeypatch, bridge_paths)

    assert result["state_paths_disagreement"]["auth_enabled"] == {
        "this_process": True,
        "bridge": False,
    }


def test_setup_status_stays_quiet_when_the_bridge_predates_the_report(monkeypatch):
    # An older daemon does not send state_paths at all. Absence is not a
    # mismatch, or every upgrade would report a fault it cannot explain.
    result = _paths_status(monkeypatch, None)

    assert "state_paths_disagreement" not in result
    assert result["state_paths"] == _LOCAL_PATHS
    assert result["status"] == "healthy"


# --- The build stamp: the one question version equality cannot answer ---------
#
# Every test below holds all three version fields equal and every required
# capability present, which is the state the old logic called `healthy`. That is
# deliberate: the stamp exists because that state was measured twice here to be
# wrong in both directions, so a verdict that only fired when a version already
# disagreed would add nothing.


def _extension_tree(tmp_path, name, extra=None):
    """A minimal extension directory whose recorded stamp matches its sources.

    Returns `(directory, stamp)`. Built with the real writer rather than a
    hardcoded hash, so these tests keep meaning if the digest ever changes shape.
    """
    directory = tmp_path / name
    directory.mkdir(parents=True)
    (directory / "background.js").write_text(
        "// worker code\n" + stamp_line("0" * STAMP_LENGTH) + "\n",
        encoding="utf-8",
        newline="",
    )
    (directory / "manifest.json").write_text(
        '{\n  "version": "9.9.9"\n}\n', encoding="utf-8", newline=""
    )
    for relative, body in (extra or {}).items():
        (directory / relative).write_text(body, encoding="utf-8", newline="")
    stamp, _ = write_extension_stamp(directory)
    return directory, stamp


def _healthy_diagnosis(**overrides):
    base = {
        "cause": "healthy",
        "ok": True,
        "bridge_version": __version__,
        "extension_version": __version__,
        "protocol_version": 3,
        "extension_capabilities": {
            "content_command_channel_removed": True,
            "batch_result_guard": True,
        },
    }
    base.update(overrides)
    return base


def _build_status(monkeypatch, directory, diagnosis, runtime=None):
    monkeypatch.setattr(S, "chrome_extension_dir", lambda: directory)
    return _status(monkeypatch, diagnosis, runtime)


def test_a_worker_running_the_tree_is_reported_as_measured_and_matching(monkeypatch, tmp_path):
    """The healthy case, and the only one that may claim the worker was checked."""
    directory, stamp = _extension_tree(tmp_path, "matching")

    result = _build_status(
        monkeypatch, directory, _healthy_diagnosis(extension_build_stamp=stamp)
    )

    assert result["extension_build_verdict"] == "matches_tree"
    assert result["extension_build_enforced"] is True
    assert result["extension_build_stamp"] == stamp
    assert result["expected_extension_build_stamp"] == stamp
    assert result["reload_extension_required"] is False
    assert result["status"] == "healthy"
    assert result["action"] == "none"


def test_a_worker_on_other_code_is_caught_though_every_version_agrees(monkeypatch, tmp_path):
    """The whole reason the stamp exists.

    Measured on this project: the versions agreed, every advertised capability was
    present, and a reload was still needed. Nothing else in `get_setup_status`
    could see it, so the verdict here has to be the thing that names the fix.
    """
    directory, stamp = _extension_tree(tmp_path, "stale-worker")
    other = "f" * STAMP_LENGTH
    assert other != stamp

    result = _build_status(
        monkeypatch, directory, _healthy_diagnosis(extension_build_stamp=other)
    )

    assert result["extension_build_verdict"] == "stale_worker"
    assert result["extension_build_enforced"] is True
    assert result["extension_build_stamp"] == other
    assert result["expected_extension_build_stamp"] == stamp
    assert result["reload_extension_required"] is True
    assert result["status"] == "stale_extension"
    assert result["action"] == "reload_extension"


def test_a_worker_ahead_of_this_process_is_not_told_to_reload(monkeypatch, tmp_path):
    """Direction matters, and the stamp comparison cannot establish it.

    A worker whose version is newer than this process is compared against *this
    process's* copy of the tree, so a mismatching stamp means the server is behind,
    not the browser. Reloading the extension cannot fix that, so the verdict stays
    honest about what it saw while the action names the only thing that can.
    """
    directory, stamp = _extension_tree(tmp_path, "newer-worker")
    other = "f" * STAMP_LENGTH
    assert other != stamp

    result = _build_status(
        monkeypatch,
        directory,
        _healthy_diagnosis(extension_version="99.0.0", extension_build_stamp=other),
    )

    assert result["extension_build_verdict"] == "stale_worker"
    assert result["extension_build_enforced"] is True
    assert result["reload_extension_required"] is False
    assert result["status"] == "stale_package"
    assert result["action"] == "restart_mcp_session"


def test_a_stamp_nobody_regenerated_refuses_to_judge_the_worker(monkeypatch, tmp_path):
    """A stale stamp makes a *fresh* worker report the old literal too.

    So the comparison proves nothing in either direction, and saying `stale_worker`
    here would name a browser reload as the fix for a problem a reload cannot
    touch. `enforced` is what keeps that from reading as a check that ran.
    """
    directory, stamp = _extension_tree(tmp_path, "unregenerated")
    # An edit to a file that is not the stamp's own: the sources now hash to
    # something else while background.js still carries the old value.
    (directory / "content.js").write_text(
        "// added later\n", encoding="utf-8", newline=""
    )

    result = _build_status(
        monkeypatch, directory, _healthy_diagnosis(extension_build_stamp=stamp)
    )

    assert result["extension_build_verdict"] == "stamp_not_regenerated"
    assert result["extension_build_enforced"] is False
    # Not driven by the stamp at all: the three version fields agree, so nothing
    # else asks for a reload either.
    assert result["reload_extension_required"] is False
    assert result["status"] == "healthy"
    note = result["notes"][0]
    assert "scripts.extension_stamp --write" in note
    # Both numbers, because the developer's next move is to see which is which.
    assert stamp in note and result["expected_extension_build_stamp"] in note
    assert result["expected_extension_build_stamp"] != stamp


def test_a_worker_that_reports_no_stamp_says_so_instead_of_guessing(monkeypatch, tmp_path):
    """An extension built before the stamp existed. Absence is not a mismatch.

    Treating a missing value as unequal would report every pre-stamp install as a
    stale worker -- a gate crying wolf at exactly the people upgrading.
    """
    directory, stamp = _extension_tree(tmp_path, "no-stamp")

    result = _build_status(
        monkeypatch, directory, _healthy_diagnosis(), runtime={"protocol_version": 3}
    )

    assert result["extension_build_verdict"] == "unverifiable"
    assert result["extension_build_enforced"] is False
    assert result["extension_build_stamp"] is None
    # Still reported, so a reader can see what the comparison would have been.
    assert result["expected_extension_build_stamp"] == stamp
    assert result["reload_extension_required"] is False
    assert result["status"] == "healthy"
    # `healthy` is correct and is also the problem: this is the exact shape measured
    # live on this project -- every version equal, protocol matched, every capability
    # present, `action: none` -- with the browser running code from before the stamp.
    # Without the note the skipped check is invisible to anyone not reading
    # `enforced`, so the disclosure is what stops `healthy` from speaking for it.
    assert result["action"] == "none"
    note = result["notes"][0]
    assert "extension_build_enforced is false" in note
    # Both causes, because they need different fixes and leave identical evidence.
    assert "Reload the unpacked extension" in note
    assert "browsertap bridge --restart" in note


def test_the_fallback_probe_fills_a_stamp_an_older_bridge_did_not_forward(monkeypatch, tmp_path):
    """The stamp joins the probe condition, or it is missing exactly where it helps.

    A bridge that predates the stamp still forwards both versions, so a probe
    keyed only on those would never run on the installs whose stamp is absent.
    """
    directory, stamp = _extension_tree(tmp_path, "probed")

    result = _build_status(
        monkeypatch,
        directory,
        _healthy_diagnosis(),
        runtime={
            "extension_version": __version__,
            "protocol_version": 3,
            "capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
            "build_stamp": stamp,
        },
    )

    assert result["extension_build_stamp"] == stamp
    assert result["extension_build_verdict"] == "matches_tree"
    assert result["status"] == "healthy"


def test_the_probe_fills_gaps_without_discarding_what_the_bridge_sent(monkeypatch, tmp_path):
    """Widening the probe's condition made an old assignment dangerous.

    It used to run only when a version was already None, so overwriting could not
    lose anything. It now fires with all three version fields in hand, and a
    `bridge_status` reply that omits `capabilities` would empty a populated set --
    demanding a reload of an extension that was fine, which is the cry-wolf shape
    this repository has already had to undo twice.
    """
    directory, stamp = _extension_tree(tmp_path, "partial-probe")

    result = _build_status(
        monkeypatch,
        directory,
        _healthy_diagnosis(),
        # A truthful but partial answer: the stamp only, which is what the probe
        # was reached for.
        runtime={"build_stamp": stamp},
    )

    assert result["extension_build_stamp"] == stamp
    assert result["extension_version"] == __version__
    assert result["protocol_version"] == 3
    assert result["missing_extension_capabilities"] == []
    assert result["reload_extension_required"] is False
    assert result["status"] == "healthy"


def test_an_unreadable_recorded_stamp_is_disclosed_without_losing_the_verdict(
    monkeypatch, tmp_path
):
    """Two questions, and only one of them is unanswerable here.

    An install whose `background.js` carries no stamp line is what a partial
    checkout or a hand-edited file produces. That breaks the *developer's*
    question -- did someone edit an extension file without regenerating the stamp
    -- because there is no recorded value to compare the sources against, and it
    is surfaced as `extension_build_error` naming the command that fixes it.

    It does not break the *worker's* question. Code with no stamp constant cannot
    report a stamp, so a worker that reports one is provably running something
    else, and the tree hash is still a real measurement to say so against. Hence
    `stale_worker` with `enforced: True`: the comparison happened. Downgrading it
    to `unverifiable` would throw away the one answer still available and leave a
    genuinely stale worker looking unexamined.
    """
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "background.js").write_text(
        "// no stamp line\n", encoding="utf-8", newline=""
    )

    result = _build_status(
        monkeypatch, directory, _healthy_diagnosis(extension_build_stamp="a" * STAMP_LENGTH)
    )

    assert "extension_build_error" in result
    assert "extension_stamp --write" in result["extension_build_error"]
    assert result["extension_build_verdict"] == "stale_worker"
    assert result["extension_build_enforced"] is True
    assert result["reload_extension_required"] is True
    # And with no stamp reported either, there is nothing left to measure, so the
    # verdict stops claiming one.
    quiet = _build_status(monkeypatch, directory, _healthy_diagnosis())
    assert quiet["extension_build_verdict"] == "unverifiable"
    assert quiet["extension_build_enforced"] is False
    assert "extension_build_error" in quiet
    # And it does *not* also get the note about a worker reporting no stamp. That
    # note tells the reader a verifiable tree went unverified, which would be a
    # false claim here: this tree has no recorded stamp to verify against, and the
    # error already names the command that fixes it. Two messages for one fact is
    # how a gate starts getting skimmed.
    assert not any("reported none" in n for n in quiet["notes"])


def test_a_release_bump_alone_does_not_demand_a_reload(monkeypatch, tmp_path):
    """The stamp overrules the version number, which is the weakest of the four.

    Measured on this project at 0.4.18: the bridge had been restarted, the worker's
    stamp matched the tree, protocol matched, every capability was present, and
    `doctor` still exited 1 asking for a reload -- because `versioning bump` had
    rewritten `manifest.json` and Chrome only parses that at load time. So the gap
    could not close on its own, and the reload it demanded would have changed
    exactly one thing: the number this gate was complaining about.

    That is not a cosmetic annoyance. `tests/live_preflight.py` reads
    `reload_extension_required` rather than the verdict and has no override, so a
    human click stood between every release bump and any live evidence at all.
    """
    directory, stamp = _extension_tree(tmp_path, "bumped")
    older = "0.0.1"
    assert older != __version__

    result = _build_status(
        monkeypatch,
        directory,
        _healthy_diagnosis(extension_version=older, extension_build_stamp=stamp),
    )

    assert result["extension_build_verdict"] == "matches_tree"
    assert result["extension_build_enforced"] is True
    assert result["reload_extension_required"] is False
    assert result["status"] == "healthy"
    assert result["action"] == "none"
    # The version gap is still published, and explained. `healthy` sitting next to
    # two different version numbers is otherwise a reader's dead end: nothing else
    # in the payload says which one this process believed, and the true answer --
    # neither, it compared the code -- is not derivable from the other fields.
    assert result["extension_version"] == older
    assert result["package_version"] == __version__
    note = result["notes"][0]
    assert older in note and __version__ in note
    assert "matches_tree" in note


def test_the_version_number_still_decides_when_the_stamp_cannot(monkeypatch, tmp_path):
    """A weak signal beats none, so the fallback is not removed -- only demoted.

    An extension built before the stamp existed reports none, which is the exact
    population that most needs the version comparison: it is the only evidence
    left. Both cases here are `enforced: False`, so neither may claim the worker
    was checked, and the version number is what names the fix.
    """
    directory, _ = _extension_tree(tmp_path, "no-stamp-old-version")
    older = "0.0.1"

    unverifiable = _build_status(
        monkeypatch,
        directory,
        _healthy_diagnosis(extension_version=older),
        runtime={"protocol_version": 3},
    )

    assert unverifiable["extension_build_verdict"] == "unverifiable"
    assert unverifiable["extension_build_enforced"] is False
    assert unverifiable["reload_extension_required"] is True
    assert unverifiable["status"] == "stale_extension"
    assert unverifiable["action"] == "reload_extension"

    # And the other unenforced verdict, which a developer's own edit produces. The
    # stamp is equally unable to judge, so the version number decides here too --
    # and the reload it asks for is real, because the sources moved.
    edited, stamp = _extension_tree(tmp_path, "unregenerated-old-version")
    (edited / "content.js").write_text("// added later\n", encoding="utf-8", newline="")

    result = _build_status(
        monkeypatch,
        edited,
        _healthy_diagnosis(extension_version=older, extension_build_stamp=stamp),
    )

    assert result["extension_build_verdict"] == "stamp_not_regenerated"
    assert result["extension_build_enforced"] is False
    assert result["reload_extension_required"] is True
    assert result["action"] == "reload_extension"


def test_a_matching_stamp_does_not_excuse_a_protocol_or_capability_gap(monkeypatch, tmp_path):
    """Only the version number yields to the stamp. The other two are not opinions.

    A worker whose JavaScript hashes to this tree while speaking a different
    protocol, or while failing to advertise a capability this tree's code requires,
    is not a release-number gap -- it is a contradiction, and the two possible
    causes (a hand-modified install, a bridge misreporting the runtime) both need a
    human. Folding these into the same yield would turn the strongest signal into a
    blanket excuse, which is worse than the cry-wolf it was fixing.
    """
    directory, stamp = _extension_tree(tmp_path, "matching-but-wrong")

    protocol = _build_status(
        monkeypatch,
        directory,
        _healthy_diagnosis(
            extension_version="0.0.1", protocol_version=2, extension_build_stamp=stamp
        ),
    )

    assert protocol["extension_build_verdict"] == "matches_tree"
    assert protocol["reload_extension_required"] is True
    assert protocol["action"] == "reload_extension"

    capability = _build_status(
        monkeypatch,
        directory,
        _healthy_diagnosis(
            extension_version="0.0.1",
            extension_capabilities={},
            extension_build_stamp=stamp,
        ),
    )

    assert capability["extension_build_verdict"] == "matches_tree"
    assert capability["missing_extension_capabilities"] != []
    assert capability["reload_extension_required"] is True
    assert capability["action"] == "reload_extension"
