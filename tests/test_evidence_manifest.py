from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from scripts import evidence_manifest as E


def test_validate_manifest_accepts_matching_source_and_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.setattr(E, "DEFAULT_OUTPUT", tmp_path / "artifacts" / "evidence-manifest.json")
    monkeypatch.setattr(
        E,
        "source_identity",
        lambda: {"git_head": "abc", "git_dirty": False, "content_sha256": "tree", "file_count": 2},
    )
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    files = {
        artifacts / "coverage.json": b"coverage",
        artifacts / "offline-junit.xml": b"junit",
        artifacts / "tool-coverage-offline.json": b"tools",
        dist / "package.whl": b"wheel",
        dist / "package.tar.gz": b"sdist",
    }
    for path, content in files.items():
        path.write_bytes(content)
    manifest = {
        "schema_version": E.SCHEMA_VERSION,
        "generated": "2026-08-16T00:00:00+00:00",
        "include_live": False,
        "source": E.source_identity(),
        "artifacts": {
            path.relative_to(tmp_path).as_posix(): {
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
            for path, content in files.items()
        },
    }
    manifest_path = artifacts / "evidence-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded, problems = E.validate_manifest(manifest_path)

    assert loaded == manifest
    assert problems == []


def test_validate_manifest_rejects_an_older_schema_by_version(monkeypatch, tmp_path):
    """A previous-schema seal must fail on its version, not on content.

    The source record changed shape, so a version-1 manifest can never compare
    equal to a version-2 fingerprint. Reporting that as "source tree no longer
    matches" would send the reader looking for a source change that never
    happened.
    """
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.setattr(E, "source_identity", lambda: {})
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manifest_path = artifacts / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "include_live": False, "source": {}, "artifacts": {}}),
        encoding="utf-8",
    )

    _loaded, problems = E.validate_manifest(manifest_path)

    assert any(problem.startswith("unsupported evidence manifest schema: 1") for problem in problems)
    assert "source tree no longer matches the evidence manifest" not in problems


def test_validate_manifest_rejects_changed_source_and_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "ROOT", tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    coverage = artifacts / "coverage.json"
    tools = artifacts / "tool-coverage-offline.json"
    junit = artifacts / "offline-junit.xml"
    coverage.write_text("new", encoding="utf-8")
    tools.write_text("tools", encoding="utf-8")
    junit.write_text("junit", encoding="utf-8")
    manifest = {
        "schema_version": E.SCHEMA_VERSION,
        "include_live": False,
        "source": {"git_head": "old"},
        "artifacts": {
            "artifacts/coverage.json": {"sha256": "old"},
            "artifacts/tool-coverage-offline.json": {
                "sha256": hashlib.sha256(b"tools").hexdigest()
            },
            "artifacts/offline-junit.xml": {
                "sha256": hashlib.sha256(b"junit").hexdigest()
            },
        },
    }
    manifest_path = artifacts / "evidence-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(E, "source_identity", lambda: {"git_head": "new"})

    _loaded, problems = E.validate_manifest(manifest_path)

    assert "source tree no longer matches the evidence manifest" in problems
    assert "artifact hash mismatch: artifacts/coverage.json" in problems


def test_validate_manifest_requires_live_binding(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "ROOT", tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manifest_path = artifacts / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": E.SCHEMA_VERSION, "include_live": False, "source": {}, "artifacts": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(E, "source_identity", lambda: {})

    _loaded, problems = E.validate_manifest(manifest_path, require_live=True)

    assert "evidence manifest does not include live results" in problems
    assert any(problem.startswith("artifact records missing:") for problem in problems)


def test_validate_manifest_rejects_extra_records_and_noncanonical_distribution_sets(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.setattr(E, "source_identity", lambda: {})
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    for relative, content in {
        "coverage.json": b"coverage",
        "offline-junit.xml": b"junit",
        "tool-coverage-offline.json": b"tools",
        "extra.txt": b"extra",
        "dist/a.whl": b"wheel",
        "dist/b.tar.gz": b"sdist",
        "dist/c.whl": b"extra wheel",
    }.items():
        path = artifacts / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    records = {}
    for path in artifacts.rglob("*"):
        if path.is_file() and path.name != "evidence-manifest.json":
            records[path.relative_to(tmp_path).as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
    manifest_path = artifacts / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": E.SCHEMA_VERSION,
                "include_live": False,
                "source": {},
                "artifacts": records,
            }
        ),
        encoding="utf-8",
    )

    _loaded, problems = E.validate_manifest(manifest_path)

    assert any(problem.startswith("unexpected artifact records:") for problem in problems)
    assert any(
        problem.startswith("distribution records must contain exactly one")
        for problem in problems
    )


def _init_repo(root: Path, *files: str) -> None:
    """Create a throwaway repository with one commit."""
    def run(*args: str) -> None:
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)

    root.mkdir(parents=True, exist_ok=True)
    run("init", "-q")
    run("config", "user.email", "btap-test@example.invalid")
    run("config", "user.name", "BTAP Test")
    run("config", "commit.gpgsign", "false")
    for name in files:
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")


def test_source_identity_hashes_real_worktree_content(monkeypatch, tmp_path):
    """Cover the real fingerprint, not a stub of it.

    Every other test in this file replaces `source_identity`, so nothing
    exercised the digest itself: an implementation that hashed nothing would
    still let the whole file pass.
    """
    monkeypatch.setattr(E, "ROOT", tmp_path)
    _init_repo(tmp_path, "kept.py", "removed.py")

    baseline = E.source_identity()
    assert baseline["git_dirty"] is False
    assert baseline["file_count"] == 2
    assert baseline["missing_file_count"] == 0
    assert len(baseline["content_sha256"]) == 64

    # Rewriting the same bytes must not move the fingerprint: content, not mtime.
    (tmp_path / "kept.py").write_text("kept.py\n", encoding="utf-8")
    assert E.source_identity()["content_sha256"] == baseline["content_sha256"]

    (tmp_path / "kept.py").write_text("changed\n", encoding="utf-8")
    changed = E.source_identity()
    assert changed["content_sha256"] != baseline["content_sha256"]
    assert changed["git_dirty"] is True
    assert changed["file_count"] == 2


def test_source_identity_separates_a_deleted_tracked_file_from_one_never_tracked(
    monkeypatch, tmp_path
):
    """Deleting a release file must not fingerprint as "never had it".

    Absent paths used to be dropped, so a tree whose tracked `removed.py` had
    been deleted produced exactly the same `content_sha256` and `file_count` as
    a tree that never contained that file, and nothing in the record showed that
    Git still tracked it.
    """
    deleted = tmp_path / "deleted"
    never = tmp_path / "never"
    _init_repo(deleted, "kept.py", "removed.py")
    _init_repo(never, "kept.py")
    (deleted / "removed.py").unlink()

    monkeypatch.setattr(E, "ROOT", deleted)
    after_delete = E.source_identity()
    monkeypatch.setattr(E, "ROOT", never)
    never_tracked = E.source_identity()

    assert after_delete["content_sha256"] != never_tracked["content_sha256"]
    assert after_delete["file_count"] == never_tracked["file_count"] == 1
    assert after_delete["missing_file_count"] == 1
    assert never_tracked["missing_file_count"] == 0
    assert after_delete["git_dirty"] is True
    assert never_tracked["git_dirty"] is False
