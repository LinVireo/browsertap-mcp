from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_browser_mcp import __version__
from scripts.versioning import (
    VersionError,
    bump_version,
    read_source_version,
    sync_versions,
    validate_versions,
)


ROOT = Path(__file__).resolve().parents[1]


def test_all_runtime_and_manifest_versions_are_identical():
    versions = validate_versions(ROOT)

    assert set(versions) == {"source", "package", "manifest"}
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
    package = tmp_path / "src" / "agent_browser_mcp"
    extension = package / "chrome_extension"
    extension.mkdir(parents=True)
    (package / "_version.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": "0.1.0"}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='agent-browser-mcp'\ndynamic=['version']\n"
        "[tool.setuptools.dynamic]\n"
        "version={attr='agent_browser_mcp.__version__'}\n",
        encoding="utf-8",
    )

    changed = sync_versions(tmp_path, "0.3.0")

    assert {path.name for path in changed} == {"_version.py", "manifest.json"}
    assert read_source_version(tmp_path) == "0.3.0"
    assert json.loads((extension / "manifest.json").read_text(encoding="utf-8"))["version"] == "0.3.0"
    assert validate_versions(tmp_path) == {
        "source": "0.3.0",
        "package": "0.3.0",
        "manifest": "0.3.0",
    }


def test_static_project_version_is_rejected(tmp_path):
    package = tmp_path / "src" / "agent_browser_mcp"
    extension = package / "chrome_extension"
    extension.mkdir(parents=True)
    (package / "_version.py").write_text('__version__ = "0.3.0"\n', encoding="utf-8")
    (extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": "0.3.0"}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='agent-browser-mcp'\nversion='0.3.0'\n",
        encoding="utf-8",
    )

    with pytest.raises(VersionError, match="dynamic version"):
        validate_versions(tmp_path)
