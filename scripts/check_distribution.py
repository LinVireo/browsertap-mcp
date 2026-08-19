"""Reject machine-local browser data from Python distribution archives."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

REQUIRED_WHEEL_SUFFIXES = (
    "/agent_browser_mcp/browser_bridge.py",
    "/agent_browser_mcp/tmwebdriver.py",
    "/agent_browser_mcp/chrome_extension/background.js",
    "/agent_browser_mcp/chrome_extension/content.js",
    "/agent_browser_mcp/chrome_extension/disable_dialogs.js",
    "/agent_browser_mcp/chrome_extension/manifest.json",
    "/agent_browser_mcp/chrome_extension/popup.html",
    "/agent_browser_mcp/chrome_extension/popup.js",
    "/agent_browser_mcp/chrome_extension/_locales/en/messages.json",
    "/agent_browser_mcp/chrome_extension/_locales/zh_CN/messages.json",
    # Required, not merely permitted: `agent-browser-mcp skill-path` points a
    # caller's skill manager at these files, so a wheel that drops them ships a
    # command that resolves to an empty directory.
    "/agent_browser_mcp/skills/browser-mcp-default/SKILL.md",
    "/agent_browser_mcp/skills/abm-bridge-recovery/SKILL.md",
)
REQUIRED_SDIST_SUFFIXES = (
    "/.gitignore",
    "/CONTRIBUTING.zh-CN.md",
    "/src/agent_browser_mcp/browser_bridge.py",
    "/src/agent_browser_mcp/tmwebdriver.py",
    "/src/agent_browser_mcp/skills/browser-mcp-default/SKILL.md",
    "/src/agent_browser_mcp/skills/abm-bridge-recovery/SKILL.md",
    "/.github/workflows/live.yml",
    "/.github/workflows/test.yml",
    "/examples/claude-desktop-config.json",
    "/examples/cursor-mcp.json",
    "/examples/hermes-config.yaml",
    "/scripts/check_distribution.py",
    "/scripts/check_tool_docs.py",
    "/scripts/tool_coverage_report.py",
    "/tests/conftest.py",
    "/tests/tool_coverage_manifest.py",
)


def _normalise(name: str) -> str:
    normalised = name.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return "/" + normalised.lstrip("/")


def _is_packaged_skill(path: str) -> bool:
    """True for a shipped agent skill at ``agent_browser_mcp/skills/<name>/SKILL.md``.

    Matched by shape rather than by name so a skill added later ships without
    editing this gate, while a copy dropped anywhere else -- a caller's own
    installed mirror, a stray root file -- is still refused.
    """
    marker = "/agent_browser_mcp/skills/"
    index = path.find(marker)
    if index < 0:
        return False
    tail = path[index + len(marker):]
    return tail.count("/") == 1 and tail.endswith("/SKILL.md")


def _forbidden_reason(name: str) -> str | None:
    path = _normalise(name)
    basename = path.rsplit("/", 1)[-1].lower()
    if path.endswith("/agent_browser_mcp/chrome_extension/config.js"):
        return "generated extension config"
    if basename == "bridge-token":
        return "bridge authentication token"
    if basename == ".env" or (basename.startswith(".env.") and basename != ".env.example"):
        return "environment secrets file"
    if basename.endswith(".har"):
        return "browser network capture"
    if (basename == "skill.md" or basename.endswith(".skill.md")) and not _is_packaged_skill(path):
        return "agent skill outside the packaged skills directory"
    if basename.startswith("cookie") and basename.endswith(".json"):
        return "cookie export"
    for segment in ("/.agent-browser-mcp/", "/artifacts/", "/out/", "/screenshots/"):
        if segment in path.lower():
            return f"local runtime directory {segment.strip('/')}"
    return None


def archive_names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            return archive.getnames()
    raise ValueError(f"unsupported distribution archive: {path}")


def filename_version(path: Path) -> str | None:
    """Read the version encoded in a distribution filename.

    Wheels are ``name-version-python-abi-platform.whl``; source archives are
    ``name-version.tar.gz``. Both put the version in the second dash-separated
    field of the stem.
    """
    if path.suffix == ".whl":
        parts = path.stem.split("-")
        return parts[1] if len(parts) >= 2 else None
    name = path.name
    for suffix in (".tar.gz", ".tgz"):
        if name.endswith(suffix):
            parts = name[: -len(suffix)].split("-")
            return parts[1] if len(parts) >= 2 else None
    return None


def _metadata_version(text: str) -> str | None:
    """Read ``Version:`` from RFC822 core metadata, stopping at the body."""
    for line in text.splitlines():
        if not line.strip():
            break
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def _read_metadata(path: Path) -> tuple[str, str] | None:
    """Return ``(member_name, text)`` for an archive's core metadata file."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if _normalise(name).endswith(".dist-info/METADATA")
            ]
            if len(candidates) != 1:
                return None
            return candidates[0], archive.read(candidates[0]).decode("utf-8", "replace")
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            candidates = [
                name
                for name in archive.getnames()
                if _normalise(name).count("/") == 2 and _normalise(name).endswith("/PKG-INFO")
            ]
            if len(candidates) != 1:
                return None
            handle = archive.extractfile(candidates[0])
            if handle is None:
                return None
            with handle:
                return candidates[0], handle.read().decode("utf-8", "replace")
    return None


def _version_violations(path: Path) -> list[str]:
    """Cross-check the filename version against the archive's own metadata.

    Nothing else in the release chain reads the built metadata, so a stale
    ``dist-info`` (a rebuild that reused an old wheel, or a bumped source
    version that never made it into the archive) would otherwise ship silently:
    the filename would show the new version while installers report the old one.
    """
    expected = filename_version(path)
    if expected is None:
        return [f"cannot read a version from the distribution filename: {path.name}"]
    try:
        found = _read_metadata(path)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return [f"distribution metadata unreadable: {type(exc).__name__}"]
    if found is None:
        return ["distribution does not contain exactly one core metadata file"]
    member, text = found
    declared = _metadata_version(text)
    if declared is None:
        return [f"{member}: no Version field in core metadata"]
    if declared != expected:
        return [
            f"{member}: metadata version {declared!r} does not match "
            f"filename version {expected!r}"
        ]
    return []


def validate_archive(path: Path) -> list[str]:
    names = archive_names(path)
    normalised_names = {_normalise(name) for name in names}
    violations = [
        f"{name}: {reason}" for name in names if (reason := _forbidden_reason(name)) is not None
    ]
    if path.suffix == ".whl":
        for suffix in REQUIRED_WHEEL_SUFFIXES:
            if not any(name.endswith(suffix) for name in normalised_names):
                violations.append(f"missing required wheel file: {suffix.lstrip('/')}")
    if path.name.endswith((".tar.gz", ".tgz")):
        for suffix in REQUIRED_SDIST_SUFFIXES:
            if not any(name.endswith(suffix) for name in normalised_names):
                violations.append(f"missing required source file: {suffix.lstrip('/')}")
    violations.extend(_version_violations(path))
    return violations


def validate_dist_dir(dist_dir: Path) -> tuple[list[Path], dict[Path, list[str]]]:
    archives = sorted((*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz")))
    if not any(path.suffix == ".whl" for path in archives):
        raise ValueError(f"no wheel found in {dist_dir}")
    if not any(path.name.endswith(".tar.gz") for path in archives):
        raise ValueError(f"no source distribution found in {dist_dir}")
    failures = {path: issues for path in archives if (issues := validate_archive(path))}
    return archives, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check release archives for machine-local browser data"
    )
    parser.add_argument("dist_dir", nargs="?", type=Path, default=Path("artifacts/dist"))
    args = parser.parse_args(argv)
    try:
        archives, failures = validate_dist_dir(args.dist_dir)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"distribution check failed: {exc}")
        return 1
    if failures:
        for path, issues in failures.items():
            for issue in issues:
                print(f"{path}: {issue}")
        return 1
    print(f"distribution_ok=True archives_checked={len(archives)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
