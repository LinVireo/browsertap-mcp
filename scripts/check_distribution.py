"""Reject machine-local browser data from Python distribution archives."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

# A licence obligation, not packaging tidiness: the wheel is the copy most people
# receive, and part of what it carries is upstream's code under upstream's MIT
# notice. A wheel without these two distributes that code with its notice
# stripped. Kept apart from REQUIRED_WHEEL_SUFFIXES because these are the only
# required members the build generates rather than copies out of the tree, so the
# `src/` + suffix mapping that checks the others against `git ls-files` does not
# apply -- their tree counterparts are `LICENSE` and `THIRD-PARTY-NOTICES.md`,
# which REQUIRED_SDIST_SUFFIXES already pins.
REQUIRED_WHEEL_METADATA_SUFFIXES = (
    "/licenses/LICENSE",
    "/licenses/THIRD-PARTY-NOTICES.md",
)
REQUIRED_WHEEL_SUFFIXES = (
    "/browsertap_mcp/browser_bridge.py",
    "/browsertap_mcp/chrome_extension/background.js",
    "/browsertap_mcp/chrome_extension/content.js",
    "/browsertap_mcp/chrome_extension/disable_dialogs.js",
    "/browsertap_mcp/chrome_extension/manifest.json",
    "/browsertap_mcp/chrome_extension/popup.html",
    "/browsertap_mcp/chrome_extension/popup.js",
    "/browsertap_mcp/chrome_extension/_locales/en/messages.json",
    "/browsertap_mcp/chrome_extension/_locales/zh_CN/messages.json",
    # Required, not merely permitted: `browsertap skill-path` points a
    # caller's skill manager at these files, so a wheel that drops them ships a
    # command that resolves to an empty directory.
    "/browsertap_mcp/skills/browsertap-default/SKILL.md",
    "/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md",
)
REQUIRED_SDIST_SUFFIXES = (
    "/.gitignore",
    "/LICENSE",
    "/THIRD-PARTY-NOTICES.md",
    "/CONTRIBUTING.zh-CN.md",
    "/src/browsertap_mcp/browser_bridge.py",
    "/src/browsertap_mcp/skills/browsertap-default/SKILL.md",
    "/src/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md",
    "/.github/workflows/live.yml",
    "/.github/workflows/release.yml",
    "/.github/workflows/supply-chain.yml",
    "/.github/workflows/test.yml",
    # Not packaging metadata: `scripts/versioning.py` and two test modules read
    # it, and all three ship, so leaving it out turns a gate into a traceback.
    "/server.json",
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
    """True for a shipped agent skill at ``browsertap_mcp/skills/<name>/SKILL.md``.

    Matched by shape rather than by name so a skill added later ships without
    editing this gate, while a copy dropped anywhere else -- a caller's own
    installed mirror, a stray root file -- is still refused.
    """
    marker = "/browsertap_mcp/skills/"
    index = path.find(marker)
    if index < 0:
        return False
    tail = path[index + len(marker):]
    return tail.count("/") == 1 and tail.endswith("/SKILL.md")


def _forbidden_reason(name: str) -> str | None:
    path = _normalise(name)
    basename = path.rsplit("/", 1)[-1].lower()
    if path.endswith("/browsertap_mcp/chrome_extension/config.js"):
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
    for segment in ("/.browsertap/", "/artifacts/", "/out/", "/screenshots/"):
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


def _metadata_headers(text: str) -> list[tuple[str, str]]:
    """Parse the header block of RFC822 core metadata into ``(key, value)``.

    Stops at the first blank line: everything after it is the long description,
    which is Markdown and routinely contains lines that look like headers.
    Continuation lines (a folded value) are appended to the previous field.
    """
    headers: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            break
        if line[:1] in (" ", "\t") and headers:
            key, value = headers[-1]
            headers[-1] = (key, value + " " + line.strip())
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers.append((key.strip().lower(), value.strip()))
    return headers


def _publishing_violations(text: str, member: str) -> list[str]:
    """Check the fields a public index needs to render and classify the release.

    None of these can be caught by installing the wheel, which is why they ship
    broken so easily: a missing ``Description-Content-Type`` renders the README
    as plain text, a missing ``Requires-Python`` offers the wheel to
    interpreters it cannot run on, and a version number can never be reused on
    PyPI, so noticing after the upload means burning it.
    """
    headers = _metadata_headers(text)
    values: dict[str, list[str]] = {}
    for key, value in headers:
        values.setdefault(key, []).append(value)
    violations = []
    for field in ("metadata-version", "name", "summary", "requires-python"):
        if not values.get(field):
            violations.append(f"{member}: core metadata has no {field} field")
    content_type = (values.get("description-content-type") or [""])[0]
    if not content_type.startswith("text/markdown"):
        violations.append(
            f"{member}: description-content-type is {content_type!r}, so the index "
            "would render README.md as plain text"
        )
    license_fields = values.get("license-expression") or values.get("license") or []
    if not license_fields:
        violations.append(f"{member}: core metadata declares no license")
    if not any(url.lower().startswith("homepage,") for url in values.get("project-url", [])):
        violations.append(f"{member}: core metadata has no Homepage project URL")
    classifiers = values.get("classifier", [])
    for prefix in (
        "Development Status ::",
        "Intended Audience ::",
        "Programming Language :: Python :: 3.10",
        "Operating System ::",
        "Topic ::",
    ):
        if not any(entry.startswith(prefix) for entry in classifiers):
            violations.append(f"{member}: no {prefix!r} classifier in core metadata")
    return violations


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
    """Check the archive's own core metadata against the filename and the index.

    Nothing else in the release chain reads the built metadata, so a stale
    ``dist-info`` (a rebuild that reused an old wheel, or a bumped source
    version that never made it into the archive) would otherwise ship silently:
    the filename would show the new version while installers report the old one.
    The publishing fields are checked from the same read -- see
    ``_publishing_violations`` for why they cannot wait until upload time.
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
    return _publishing_violations(text, member)


def validate_archive(path: Path) -> list[str]:
    names = archive_names(path)
    normalised_names = {_normalise(name) for name in names}
    violations = [
        f"{name}: {reason}" for name in names if (reason := _forbidden_reason(name)) is not None
    ]
    if path.suffix == ".whl":
        for suffix in (*REQUIRED_WHEEL_SUFFIXES, *REQUIRED_WHEEL_METADATA_SUFFIXES):
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
