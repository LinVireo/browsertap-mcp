from __future__ import annotations

from scripts import finalize_change as F


def test_archive_previous_outputs_moves_them_recoverably(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "ROOT", tmp_path)
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    (artifacts / "coverage-a85.json").write_text("old", encoding="utf-8")
    (artifacts / "offline-junit.xml").write_text("old junit", encoding="utf-8")
    (artifacts / "fable-review-prompt-0.3.5.md").write_text("old", encoding="utf-8")
    (dist / "agent_browser_mcp-0.3.5-py3-none-any.whl").write_bytes(b"old wheel")

    destination = F._archive_previous_outputs()

    assert destination is not None
    assert not (artifacts / "coverage-a85.json").exists()
    assert not (artifacts / "offline-junit.xml").exists()
    assert not (dist / "agent_browser_mcp-0.3.5-py3-none-any.whl").exists()
    assert (destination / "coverage-a85.json").read_text(encoding="utf-8") == "old"
    assert (destination / "offline-junit.xml").read_text(encoding="utf-8") == "old junit"
    assert (destination / "dist" / "agent_browser_mcp-0.3.5-py3-none-any.whl").read_bytes() == b"old wheel"


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
    (stale_dist / "agent_browser_mcp-0.3.11.tar.gz").write_bytes(b"old sdist")
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
        destination / "dist-0.3.11" / "agent_browser_mcp-0.3.11.tar.gz"
    ).read_bytes() == b"old sdist"
    # Earlier archived rounds are history, not stale output: they stay put.
    assert (kept / "coverage.json").read_text(encoding="utf-8") == "earlier round"
