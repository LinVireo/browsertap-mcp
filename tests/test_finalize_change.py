from __future__ import annotations

import sys

import pytest

from scripts import finalize_change as F


def test_archive_previous_outputs_moves_them_recoverably(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "ROOT", tmp_path)
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    (artifacts / "coverage-a85.json").write_text("old", encoding="utf-8")
    (artifacts / "offline-junit.xml").write_text("old junit", encoding="utf-8")
    (artifacts / "fable-review-prompt-0.3.5.md").write_text("old", encoding="utf-8")
    (dist / "browsertap_mcp-0.3.5-py3-none-any.whl").write_bytes(b"old wheel")

    destination = F._archive_previous_outputs()

    assert destination is not None
    assert not (artifacts / "coverage-a85.json").exists()
    assert not (artifacts / "offline-junit.xml").exists()
    assert not (dist / "browsertap_mcp-0.3.5-py3-none-any.whl").exists()
    assert (destination / "coverage-a85.json").read_text(encoding="utf-8") == "old"
    assert (destination / "offline-junit.xml").read_text(encoding="utf-8") == "old junit"
    assert (destination / "dist" / "browsertap_mcp-0.3.5-py3-none-any.whl").read_bytes() == b"old wheel"


def test_archive_previous_outputs_sweeps_unenumerated_names_and_stale_dist_dirs(
    monkeypatch, tmp_path
):
    """Anything left under artifacts/ is stale, whatever it is called.

    An enumerated pattern list silently kept files whose names nobody thought
    of, so a reader could see a previous round's report next to a fresh seal
    that does not cover it.
    """
    monkeypatch.setattr(F, "ROOT", tmp_path)
    artifacts = tmp_path / "artifacts"
    stale_dist = artifacts / "dist-0.3.11"
    stale_dist.mkdir(parents=True)
    (artifacts / "coverage.xml").write_text("bare xml", encoding="utf-8")
    (artifacts / "gates-prepush-0.3.11.md").write_text("prepush", encoding="utf-8")
    (artifacts / "some-unplanned-output.txt").write_text("unplanned", encoding="utf-8")
    (stale_dist / "browsertap_mcp-0.3.11.tar.gz").write_bytes(b"old sdist")
    kept = artifacts / "archive" / "20260101T000000000000Z"
    kept.mkdir(parents=True)
    (kept / "coverage.json").write_text("earlier round", encoding="utf-8")

    destination = F._archive_previous_outputs()

    assert destination is not None
    assert not (artifacts / "coverage.xml").exists()
    assert not (artifacts / "gates-prepush-0.3.11.md").exists()
    assert not (artifacts / "some-unplanned-output.txt").exists()
    assert not stale_dist.exists()
    assert (destination / "coverage.xml").read_text(encoding="utf-8") == "bare xml"
    assert (destination / "gates-prepush-0.3.11.md").read_text(encoding="utf-8") == "prepush"
    assert (destination / "some-unplanned-output.txt").read_text(encoding="utf-8") == "unplanned"
    assert (
        destination / "dist-0.3.11" / "browsertap_mcp-0.3.11.tar.gz"
    ).read_bytes() == b"old sdist"
    # Earlier archived rounds are history, not stale output: they stay put.
    assert (kept / "coverage.json").read_text(encoding="utf-8") == "earlier round"


@pytest.fixture
def finalizer_calls(monkeypatch, tmp_path):
    """Exercise the real finalizer ordering without launching build or browser work."""
    calls = []
    monkeypatch.setattr(F, "ROOT", tmp_path)
    monkeypatch.setattr(F, "read_source_version", lambda _root: "0.5.0")
    monkeypatch.setattr(F, "sync_versions", lambda _root, _target: [])
    monkeypatch.setattr(F, "validate_versions", lambda _root: None)
    monkeypatch.setattr(F, "_check_version_bump", lambda: calls.append(("version-bump",)))
    monkeypatch.setattr(F, "_require_module", lambda _module: None)
    monkeypatch.setattr(F, "_run", lambda *args: calls.append(args))
    monkeypatch.setattr(F, "write_manifest", lambda **kwargs: calls.append(("seal", kwargs)))
    return calls


@pytest.mark.parametrize("skip_live", [False, True])
def test_finalizer_uses_the_complete_runner_and_independent_report_check(finalizer_calls, skip_live):
    calls = finalizer_calls
    argv = ["--bump", "none", *(["--skip-live"] if skip_live else [])]
    assert F.main(argv) == 0
    offline = (sys.executable, "-m", "scripts.test_run_evidence", "--mode", "offline")
    live = (sys.executable, "-m", "scripts.test_run_evidence", "--mode", "live")
    version = (sys.executable, "-m", "pytest", "tests/test_versioning.py", "-q")
    seal = ("seal", {"include_live": not skip_live})
    report = (sys.executable, "-m", "scripts.acceptance_report")
    check = (*report, "--check")

    assert calls.count(offline) == 1
    assert calls.index(("version-bump",)) < calls.index(offline) < calls.index(version)
    assert calls.index(version) < calls.index(seal)
    assert (live in calls) is not skip_live
    assert (report in calls) is not skip_live
    assert (check in calls) is not skip_live
    if not skip_live:
        assert calls.index(offline) < calls.index(live) < calls.index(version)
        assert calls.index(seal) < calls.index(report) < calls.index(check)
    for command in (
        ("compileall", "-q", "src"), ("scripts.lint_report",), ("scripts.check_tool_docs",),
        ("build", "--wheel", "--sdist", "--outdir", "artifacts/dist"),
        ("scripts.check_distribution", "artifacts/dist"),
        ("scripts.check_install", "artifacts/dist", "--no-deps"), ("pip", "check"),
    ):
        assert (sys.executable, "-m", *command) in calls


def test_a_failed_independent_report_check_stops_finalization(finalizer_calls, monkeypatch, capsys):
    def run(*args):
        finalizer_calls.append(args)
        if args[-2:] == ("scripts.acceptance_report", "--check"):
            raise SystemExit(1)

    monkeypatch.setattr(F, "_run", run)
    with pytest.raises(SystemExit, match="1"):
        F.main(["--bump", "none"])
    assert finalizer_calls[-1] == (sys.executable, "-m", "scripts.acceptance_report", "--check")
    assert "finalized" not in capsys.readouterr().out
