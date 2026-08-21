from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from browsertap_mcp import __version__
from scripts.versioning import (
    VersionError,
    bump_version,
    latest_release_tag,
    read_source_version,
    sync_versions,
    validate_version_bump,
    validate_version_increment,
    validate_versions,
)

ROOT = Path(__file__).resolve().parents[1]


def test_all_runtime_and_manifest_versions_are_identical():
    versions = validate_versions(ROOT)

    assert set(versions) == {
        "source",
        "package",
        "manifest",
        "readme",
        "readme_zh",
        "changelog",
    }
    assert len(set(versions.values())) == 1
    assert versions["source"] == __version__


@pytest.mark.parametrize(
    ("version", "part", "expected"),
    [
        ("0.3.0", "patch", "0.3.1"),
        ("0.3.9", "minor", "0.4.0"),
        ("1.9.9", "patch", "1.9.10"),
    ],
)
def test_patch_and_minor_bumps_are_deterministic(version, part, expected):
    assert bump_version(version, part) == expected


@pytest.mark.parametrize("version", ["", "v1.2.3", "1.2", "1.2.3.4", "1.2.beta"])
def test_invalid_versions_are_rejected(version):
    with pytest.raises(VersionError):
        bump_version(version, "patch")


def test_sync_versions_updates_source_and_manifest_atomically(tmp_path):
    package = tmp_path / "src" / "browsertap_mcp"
    extension = package / "chrome_extension"
    extension.mkdir(parents=True)
    (package / "_version.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": "0.1.0"}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='browsertap-mcp'\ndynamic=['version']\n"
        "[tool.setuptools.dynamic]\n"
        "version={attr='browsertap_mcp.__version__'}\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "Current release: unified Python package, bridge, and unpacked Chrome extension **0.1.0**.\n",
        encoding="utf-8",
    )
    (tmp_path / "README.zh-CN.md").write_text(
        "当前版本:Python 包、bridge 与 Chrome unpacked 扩展统一为 **0.1.0**。\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Changed\n\n- Pending change.\n\n"
        "## [0.1.0] - 2026-01-01\n\n- Initial release.\n\n"
        "[Unreleased]: https://github.com/LinVireo/browsertap-mcp/compare/v0.1.0...HEAD\n"
        "[0.1.0]: https://github.com/LinVireo/browsertap-mcp/releases/tag/v0.1.0\n",
        encoding="utf-8",
    )

    changed = sync_versions(tmp_path, "0.3.0")

    assert {path.name for path in changed} == {
        "_version.py",
        "manifest.json",
        "README.md",
        "README.zh-CN.md",
        "CHANGELOG.md",
    }
    assert read_source_version(tmp_path) == "0.3.0"
    assert (
        json.loads((extension / "manifest.json").read_text(encoding="utf-8"))["version"] == "0.3.0"
    )
    assert validate_versions(tmp_path) == {
        "source": "0.3.0",
        "package": "0.3.0",
        "manifest": "0.3.0",
        "readme": "0.3.0",
        "readme_zh": "0.3.0",
        "changelog": "0.3.0",
    }
    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]\n\n## [0.3.0] - " in changelog
    assert "### Changed\n\n- Pending change." in changelog
    assert (
        "[Unreleased]: https://github.com/LinVireo/browsertap-mcp/compare/v0.3.0...HEAD"
        in changelog
    )
    assert (
        "[0.3.0]: https://github.com/LinVireo/browsertap-mcp/compare/v0.1.0...v0.3.0" in changelog
    )


def test_static_project_version_is_rejected(tmp_path):
    package = tmp_path / "src" / "browsertap_mcp"
    extension = package / "chrome_extension"
    extension.mkdir(parents=True)
    (package / "_version.py").write_text('__version__ = "0.3.0"\n', encoding="utf-8")
    (extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": "0.3.0"}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='browsertap-mcp'\nversion='0.3.0'\n",
        encoding="utf-8",
    )

    with pytest.raises(VersionError, match="dynamic version"):
        validate_versions(tmp_path)


def test_production_changes_require_a_strict_version_increment():
    assert validate_version_increment("0.3.0", "0.3.1", ["src/browsertap_mcp/server.py"]) is True
    assert (
        validate_version_increment("0.3.0", "0.3.0", ["README.md", "tests/test_versioning.py"])
        is False
    )
    with pytest.raises(VersionError, match="did not increase"):
        validate_version_increment("0.3.0", "0.3.0", ["scripts/finalize_change.py"])


def _tagged_repo(root: Path, version: str, tag: str) -> None:
    """A throwaway repository whose single commit is a tagged release."""

    def run(*args: str) -> None:
        subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)

    package = root / "src" / "browsertap_mcp"
    package.mkdir(parents=True, exist_ok=True)
    (package / "_version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (package / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
    run("init", "-q")
    run("config", "user.email", "btap-test@example.invalid")
    run("config", "user.name", "BTAP Test")
    run("config", "commit.gpgsign", "false")
    run("add", "-A")
    run("commit", "-q", "-m", "release")
    run("tag", tag)


def test_uncommitted_production_changes_still_require_a_version_increment(tmp_path):
    """The finalizer runs before the commit, so the check must see the worktree.

    A commit-range comparison reports an empty change set for a round that is
    still uncommitted, which is how a release kept the previous version number
    while carrying real behaviour changes: the only wiring for this check was a
    CI push event, and nothing had been pushed.
    """
    _tagged_repo(tmp_path, "0.3.0", "v0.3.0")
    (tmp_path / "src" / "browsertap_mcp" / "server.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )

    assert latest_release_tag(tmp_path) == "v0.3.0"

    committed_only = validate_version_bump(tmp_path, "v0.3.0")
    assert committed_only["production_changed"] is False
    assert committed_only["changed_paths"] == []

    with pytest.raises(VersionError, match="did not increase"):
        validate_version_bump(tmp_path, "v0.3.0", include_worktree=True)


def test_base_version_is_readable_from_the_pre_rename_package_path(tmp_path):
    """The 0.4.0 rename moved the version source, and the baseline predates it.

    Resolving only the current path made the gate raise `path exists on disk, but
    not in <ref>` for every baseline older than the rename commit -- so the one
    release whose change set is largest would have been the release that skipped
    the check.
    """
    def run(*args: str) -> None:
        subprocess.run(("git", *args), cwd=tmp_path, check=True, capture_output=True, text=True)

    legacy = tmp_path / "src" / "agent_browser_mcp"
    legacy.mkdir(parents=True)
    (legacy / "_version.py").write_text('__version__ = "0.3.13"\n', encoding="utf-8")
    run("init", "-q")
    run("config", "user.email", "btap-test@example.invalid")
    run("config", "user.name", "BTAP Test")
    run("config", "commit.gpgsign", "false")
    run("add", "-A")
    run("commit", "-q", "-m", "pre-rename")
    run("tag", "v0.3.13")

    renamed = tmp_path / "src" / "browsertap_mcp"
    legacy.rename(renamed)
    (renamed / "_version.py").write_text('__version__ = "0.4.0"\n', encoding="utf-8")

    result = validate_version_bump(tmp_path, "v0.3.13", include_worktree=True)

    assert result["base"] == "0.3.13"
    assert result["current"] == "0.4.0"
    assert result["production_changed"] is True


def test_unknown_base_ref_still_reports_a_usable_error(tmp_path):
    _tagged_repo(tmp_path, "0.3.0", "v0.3.0")

    with pytest.raises(VersionError, match="cannot compare version against"):
        validate_version_bump(tmp_path, "v9.9.9")


def test_worktree_comparison_counts_untracked_production_files(tmp_path):
    _tagged_repo(tmp_path, "0.3.0", "v0.3.0")
    (tmp_path / "src" / "browsertap_mcp" / "added.py").write_text("X = 1\n", encoding="utf-8")

    with pytest.raises(VersionError, match="did not increase"):
        validate_version_bump(tmp_path, "v0.3.0", include_worktree=True)

    (tmp_path / "src" / "browsertap_mcp" / "_version.py").write_text(
        '__version__ = "0.3.1"\n', encoding="utf-8"
    )
    bumped = validate_version_bump(tmp_path, "v0.3.0", include_worktree=True)

    assert bumped["base"] == "0.3.0"
    assert bumped["current"] == "0.3.1"
    assert "src/browsertap_mcp/added.py" in bumped["changed_paths"]


def test_untagged_repository_has_no_baseline_to_compare_against(tmp_path):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True, capture_output=True)

    assert latest_release_tag(tmp_path) is None


def test_finalizer_checks_the_version_increment_before_running_gates():
    module = ast.parse((ROOT / "scripts" / "finalize_change.py").read_text(encoding="utf-8"))
    main = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = [
        node.func.id
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert calls.index("sync_versions") < calls.index("_check_version_bump")
    assert calls.index("_check_version_bump") < calls.index("_run_gates")


def test_finalizer_synchronizes_version_before_running_gates():
    module = ast.parse((ROOT / "scripts" / "finalize_change.py").read_text(encoding="utf-8"))
    main = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = [
        node.func.id
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert calls.index("sync_versions") < calls.index("_run_gates")


def test_offline_workflow_has_explicit_quality_gates():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")

    assert "--cov-fail-under=85" in workflow
    assert "--junitxml=artifacts/offline-junit.xml" in workflow
    assert "python -m ruff check src tests scripts" in workflow
    assert "refs/heads/release/" in workflow
    assert (
        "pull_request"
        not in workflow.split("Check SemVer bump on release branches", 1)[1].split("- name:", 1)[0]
    )
