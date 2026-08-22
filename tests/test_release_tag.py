"""The release-tag gate: `v<version>` has to name the tree a publish would ship.

The version comes from the source tree, so nothing about the ref that started a
publish proves the tag agrees with it. These tests pin the two claims the gate
makes -- the tag exists and points at HEAD, and no production file is sitting
uncommitted -- because both are silent failures otherwise.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_release_tag import (
    _porcelain_paths,
    is_production_path,
    validate_release_tag,
)
from scripts.versioning import VERSIONED_PATH_PREFIXES, VERSIONED_PATHS

ROOT = Path(__file__).resolve().parents[1]


def test_a_tagged_release_commit_is_aligned(tmp_path, tagged_repo):
    repo = tagged_repo(tmp_path, "0.3.0", "v0.3.0")

    report = validate_release_tag(tmp_path)

    assert report["ok"] is True
    assert report["aligned"] is True
    assert report["tag"] == "v0.3.0"
    assert report["tag_commit"] == repo.head()
    assert report["commits_ahead"] is None
    assert report["worktree_dirty"] is False
    assert report["problems"] == []


def test_a_commit_after_the_tag_is_reported_with_both_shas(tmp_path, tagged_repo):
    """The exact failure this gate exists for: the tag describes an older tree."""
    repo = tagged_repo(tmp_path, "0.3.0", "v0.3.0")
    tag_commit = repo.head()
    (tmp_path / "src" / "browsertap_mcp" / "server.py").write_text("VALUE = 2\n", encoding="utf-8")
    head_commit = repo.commit("later work")

    report = validate_release_tag(tmp_path)

    assert report["ok"] is False
    assert report["aligned"] is False
    assert report["commits_ahead"] == 1
    assert report["production_changes_since_tag"] == ["src/browsertap_mcp/server.py"]
    problem = "\n".join(report["problems"])
    assert tag_commit[:7] in problem and head_commit[:7] in problem
    assert "src/browsertap_mcp/server.py" in problem


def test_a_documentation_only_gap_says_re_tagging_that_commit_is_enough(tmp_path, tagged_repo):
    """A misaligned tag is still a failure, but the fix is not the same one.

    Nothing shipped changed, so the honest instruction is "move the tag", not
    "cut another version".
    """
    repo = tagged_repo(tmp_path, "0.3.0", "v0.3.0")
    (tmp_path / "README.md").write_text("# docs\n", encoding="utf-8")
    repo.commit("docs only")

    report = validate_release_tag(tmp_path)

    assert report["ok"] is False
    assert report["production_changes_since_tag"] == []
    assert "re-tagging that commit is enough" in "\n".join(report["problems"])


def test_a_missing_tag_fails_by_default_and_is_a_note_before_tagging(tmp_path, tagged_repo):
    repo = tagged_repo(tmp_path, "0.3.0", "v0.3.0")
    repo.run("tag", "-d", "v0.3.0")

    strict = validate_release_tag(tmp_path)
    assert strict["ok"] is False
    assert strict["tag_exists"] is False
    assert "version 0.3.0 has no tag v0.3.0" in "\n".join(strict["problems"])

    # The local finalizer runs before the release commit is tagged, and a gate
    # that cannot be run at that point is a gate nobody runs.
    lenient = validate_release_tag(tmp_path, allow_missing_tag=True)
    assert lenient["ok"] is True
    assert lenient["problems"] == []
    assert any("after tagging" in note for note in lenient["notes"])


def test_uncommitted_production_changes_cannot_be_covered_by_any_tag(tmp_path, tagged_repo):
    tagged_repo(tmp_path, "0.3.0", "v0.3.0")
    (tmp_path / "src" / "browsertap_mcp" / "server.py").write_text("VALUE = 3\n", encoding="utf-8")

    report = validate_release_tag(tmp_path)

    assert report["ok"] is False
    assert report["aligned"] is True  # the tag is fine; the worktree is not
    assert report["worktree_production_changes"] == ["src/browsertap_mcp/server.py"]
    assert "uncommitted production changes" in "\n".join(report["problems"])


def test_documentation_dirt_is_recorded_without_failing_the_gate(tmp_path, tagged_repo):
    """Blocking on a stray note would train people to skip the check."""
    tagged_repo(tmp_path, "0.3.0", "v0.3.0")
    (tmp_path / "NOTES.md").write_text("scratch\n", encoding="utf-8")

    report = validate_release_tag(tmp_path)

    assert report["ok"] is True
    assert report["worktree_dirty"] is True
    assert report["worktree_production_changes"] == []
    assert any("non-production" in note for note in report["notes"])


def test_porcelain_paths_reads_renames_untracked_and_quoted_entries():
    output = (
        "R  src/browsertap_mcp/a.py -> src/browsertap_mcp/b.py\n"
        '?? "docs/with space.md"\n'
        " M pyproject.toml\n"
    )

    assert _porcelain_paths(output) == [
        "docs/with space.md",
        "pyproject.toml",
        "src/browsertap_mcp/a.py",
        "src/browsertap_mcp/b.py",
    ]


def test_porcelain_paths_survive_output_that_lost_its_leading_status_column():
    """The bug this replaces: `git status` output was stripped before parsing.

    " M src/.../server.py" became "M src/.../server.py", the path was read from
    column 3, and "rc/browsertap_mcp/server.py" is not a production path -- so an
    unstaged edit to the shipped code passed the gate as documentation dirt.
    """
    assert _porcelain_paths(" M src/browsertap_mcp/server.py") == [
        "src/browsertap_mcp/server.py"
    ]
    assert _porcelain_paths("M src/browsertap_mcp/server.py") == [
        "src/browsertap_mcp/server.py"
    ]


def test_production_classification_is_the_version_gate_s_own_rule():
    """Two gates disagreeing about "production" is how one of them gets bypassed."""
    samples = [
        "src/browsertap_mcp/server.py",
        "scripts/check_release_tag.py",
        "pyproject.toml",
        "README.md",
        "tests/test_release_tag.py",
        "docs/USAGE.md",
    ]

    for path in samples:
        expected = path in VERSIONED_PATHS or path.startswith(VERSIONED_PATH_PREFIXES)
        assert is_production_path(path) is expected, path
    # Windows-style separators reach this from `git status` on some setups.
    assert is_production_path("src\\browsertap_mcp\\server.py") is True


def test_publish_workflow_checks_the_tag_before_it_installs_or_builds():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build_stage = workflow.split("publish:", 1)[0]

    assert "python -m scripts.check_release_tag" in build_stage
    # Cheapest failing step first: no dependency install, no build, no upload.
    assert build_stage.index("python -m scripts.check_release_tag") < build_stage.index(
        "python -m build --wheel --sdist"
    )
    assert build_stage.index("python -m scripts.check_release_tag") < build_stage.index(
        "Install package and development dependencies"
    )
    # A TestPyPI rehearsal is allowed to predate the tag; the irreversible path is not.
    assert "python -m scripts.check_release_tag --allow-missing-tag" in build_stage
    strict = build_stage.split("python -m scripts.check_release_tag --allow-missing-tag", 1)[1]
    assert "if: github.event_name == 'release' || inputs.index == 'pypi'" in strict
