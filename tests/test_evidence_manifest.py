from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import evidence_manifest as E

ROOT = Path(__file__).resolve().parents[1]


def _offline_files(artifacts: Path) -> dict[Path, bytes]:
    """One file per canonical offline artifact, derived from the module's own list.

    Hand-listing them here passed for as long as the two copies happened to
    agree: adding `lint.json` to `OFFLINE_ARTIFACTS` left three fixtures writing
    a set the sealer no longer considers complete, and the resulting failures
    pointed at the sealer rather than at the fixtures. Reading the list means a
    new artifact is covered by these tests the moment it is required.
    """
    return {artifacts / Path(relative).name: relative.encode() for relative in E.OFFLINE_ARTIFACTS}


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
        **_offline_files(artifacts),
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


@pytest.mark.parametrize("mode", ["offline", "live"])
@pytest.mark.parametrize("phase", ["collection", "execution"])
def test_a_seal_requires_complete_suite_receipts(monkeypatch, tmp_path, mode, phase):
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.setattr(E, "source_identity", lambda: {})
    relative_paths = [*E.OFFLINE_ARTIFACTS, *E.LIVE_ARTIFACTS,
                      "artifacts/dist/package.whl", "artifacts/dist/package.tar.gz"]
    for relative in relative_paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    manifest = E.build_manifest(include_live=True)
    relative = f"artifacts/{mode}-{phase}.json"
    assert relative in manifest["artifacts"]
    (tmp_path / relative).unlink()
    with pytest.raises(FileNotFoundError, match=relative):
        E.build_manifest(include_live=True)
    manifest["artifacts"].pop(relative)
    path = tmp_path / "artifacts/evidence-manifest.json"
    path.write_bytes(json.dumps(manifest).encode())

    _loaded, problems = E.validate_manifest(path, require_live=True)
    assert f"artifact records missing: {relative}" in problems


def test_a_live_seal_binds_the_preflight_record_as_well_as_the_junit(monkeypatch, tmp_path):
    """A passing junit with an unbound preflight record is the stale-build hole.

    `live-preflight.json` is where the build of each of the three processes and
    the idle-browser verdict are written. It sat beside the junit and was
    uploaded with it, but nothing hashed it, so a seal could pair a passing
    suite with a preflight record left over from an older run -- or with none,
    which is what a run that never reached the session fixture leaves behind.
    """
    assert "artifacts/live-preflight.json" in E.LIVE_ARTIFACTS
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.setattr(E, "source_identity", lambda: {})
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    files = {
        **_offline_files(artifacts),
        **{
            artifacts / Path(relative).name: relative.encode()
            for relative in E.LIVE_ARTIFACTS if relative != "artifacts/live-preflight.json"
        },
        dist / "package.whl": b"wheel",
        dist / "package.tar.gz": b"sdist",
    }
    for path, content in files.items():
        path.write_bytes(content)

    # Sealing refuses outright rather than sealing the half that exists.
    with pytest.raises(FileNotFoundError) as sealing:
        E.build_manifest(include_live=True)
    assert "artifacts/live-preflight.json" in str(sealing.value)

    # And a manifest hand-built without the record fails validation, so an
    # older seal cannot be presented as a current one either.
    manifest_path = artifacts / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": E.SCHEMA_VERSION,
                "generated": "2026-08-23T00:00:00+00:00",
                "include_live": True,
                "source": {},
                "artifacts": {
                    path.relative_to(tmp_path).as_posix(): {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "bytes": len(content),
                    }
                    for path, content in files.items()
                },
            }
        ),
        encoding="utf-8",
    )

    _loaded, problems = E.validate_manifest(manifest_path, require_live=True)

    assert "artifact records missing: artifacts/live-preflight.json" in problems


def test_validate_manifest_rejects_extra_records_and_noncanonical_distribution_sets(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(E, "ROOT", tmp_path)
    monkeypatch.setattr(E, "source_identity", lambda: {})
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    for relative, content in {
        **{Path(name).name: name.encode() for name in E.OFFLINE_ARTIFACTS},
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


def _repo_tracked_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [name for name in completed.stdout.decode().split("\0") if name]


def test_every_tracked_path_has_its_line_endings_governed():
    """An ungoverned path makes `content_sha256` depend on who cloned it.

    The fingerprint above hashes raw working-tree bytes, and `core.autocrlf=true`
    -- Git for Windows' default -- rewrites the line endings of any path no
    attribute governs. So the seal recorded on one machine cannot be reproduced
    on another, including from a clone of the commit it names.

    `.gitattributes` used to list suffixes, and the list lost three times:
    `*.mjs`, `*.yml` and `*.toml` were appended late, `.yaml` was never covered,
    and an extensionless path cannot be covered by a suffix at all. Measured at
    0.4.15: five tracked files were ungoverned and a fresh clone differed from the
    working tree in three of them. Ask git which paths it governs rather than
    re-deriving the answer from a fourth list of extensions.
    """
    names = _repo_tracked_files()
    completed = subprocess.run(
        ["git", "check-attr", "eol", "-z", "--stdin"],
        cwd=ROOT,
        input="\0".join(names).encode(),
        capture_output=True,
        check=True,
    )
    fields = completed.stdout.decode().split("\0")
    governed = {
        fields[index]: fields[index + 2]
        for index in range(0, len(fields) - 2, 3)
        if fields[index + 1] == "eol"
    }
    assert set(governed) == set(names), "git did not answer for every tracked path"
    ungoverned = sorted(name for name, value in governed.items() if value != "lf")
    assert not ungoverned, f"no eol=lf attribute governs: {ungoverned}"


def test_no_tracked_text_file_holds_crlf_in_this_worktree():
    """The attribute above governs a checkout; a local tool can still undo it.

    `Path.write_text` translates on Windows, so a script that reads a file,
    edits one line and writes it back converts the whole file -- which
    `check_derived_notices.py --write` did to its own source, meaning the one
    command a maintainer runs to make the notice table honest silently made the
    release fingerprint unreproducible. A `ruff format` with `line-ending = auto`
    did it to 21 tracked files at once.

    Nothing in the sealed record mentions line endings, so this is the only place
    the damage is visible before a third party fails to reproduce the hash.
    """
    offenders = []
    for name in _repo_tracked_files():
        path = ROOT / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:8000]:  # git treats it as binary; so do we
            continue
        if b"\r\n" in raw:
            offenders.append(name)
    assert not offenders, f"CRLF in the working tree: {sorted(offenders)}"
