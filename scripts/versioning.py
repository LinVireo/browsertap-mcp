from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
CHANGELOG_RELEASE_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", re.MULTILINE)
CHANGELOG_UNRELEASED_RE = re.compile(r"^## \[Unreleased\]\s*$", re.MULTILINE)
CHANGELOG_REPOSITORY = "https://github.com/LinVireo/browsertap-mcp"
VersionPart = Literal["patch", "minor"]
README_VERSION_PATTERNS = {
    "README.md": re.compile(
        r"^(Current release: unified Python package, bridge, and unpacked Chrome extension \*\*)"
        r"(\d+\.\d+\.\d+)(\*\*\.)$",
        re.MULTILINE,
    ),
    "README.zh-CN.md": re.compile(
        r"^(当前版本:Python 包、bridge 与 Chrome unpacked 扩展统一为 \*\*)"
        r"(\d+\.\d+\.\d+)(\*\*。)$",
        re.MULTILINE,
    ),
}
VERSIONED_PATH_PREFIXES = ("src/", "scripts/")
# `server.json` is the MCP Registry listing and states the version twice, so an
# edit to it changes what a stranger installs. That makes it a production path
# like `src/` and `scripts/`, not documentation.
VERSIONED_PATHS = {"pyproject.toml", "server.json"}


class VersionError(RuntimeError):
    pass


def _parse_version(version: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(str(version).strip())
    if match is None:
        raise VersionError("version must use MAJOR.MINOR.PATCH with numeric components")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def bump_version(version: str, part: VersionPart) -> str:
    major, minor, patch = _parse_version(version)
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        raise VersionError("part must be 'patch' or 'minor'")
    return f"{major}.{minor}.{patch}"


def _version_path(root: Path) -> Path:
    return root / "src" / "browsertap_mcp" / "_version.py"


def _manifest_path(root: Path) -> Path:
    return root / "src" / "browsertap_mcp" / "chrome_extension" / "manifest.json"


def _server_manifest_path(root: Path) -> Path:
    return root / "server.json"


def read_source_version(root: Path = ROOT) -> str:
    path = _version_path(Path(root))
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise VersionError(f"cannot read version source: {exc}") from exc
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            version = node.value.value
            _parse_version(version)
            return version
    raise VersionError(f"{path} must assign a string to __version__")


def _read_package_version(root: Path, source_version: str) -> str:
    """Assert that the build reads its version from the one source attribute.

    This is a structural check, not a sixth independent value: `pyproject.toml`
    carries no literal version to compare, so the only thing worth verifying is
    that it still delegates. The return is `source_version` unchanged, so a
    caller must not present `package` as a value that could disagree with
    `source`. The built archive's own metadata is cross-checked against the
    filename by `scripts/check_distribution.py`.
    """
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VersionError(f"cannot read pyproject.toml: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise VersionError("pyproject.toml is missing [project]")
    if "version" in project:
        raise VersionError("pyproject.toml must use dynamic version, not project.version")
    dynamic = project.get("dynamic")
    if not isinstance(dynamic, list) or "version" not in dynamic:
        raise VersionError("pyproject.toml must declare project.dynamic = ['version']")
    setuptools = data.get("tool", {}).get("setuptools", {})
    version_config = setuptools.get("dynamic", {}).get("version")
    if version_config != {"attr": "browsertap_mcp.__version__"}:
        raise VersionError(
            "tool.setuptools.dynamic.version must read browsertap_mcp.__version__"
        )
    return source_version


def _read_manifest_version(root: Path) -> str:
    path = _manifest_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VersionError(f"cannot read extension manifest: {exc}") from exc
    version = data.get("version")
    if not isinstance(version, str):
        raise VersionError("extension manifest version must be a string")
    _parse_version(version)
    return version


def _read_distribution_name(root: Path) -> str:
    """The distribution name the index will serve, read from pyproject.

    Hardcoding it here would add a second place to rename the package, which is
    the failure the registry manifest check below exists to catch.
    """
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VersionError(f"cannot read pyproject.toml: {exc}") from exc
    name = data.get("project", {}).get("name")
    if not isinstance(name, str) or not name:
        raise VersionError("pyproject.toml is missing project.name")
    return name


def _read_server_manifest_version(root: Path) -> str:
    """The version the MCP Registry listing claims, proved to be stated once.

    `server.json` carries the version twice: at the top level, which is what the
    registry displays, and inside every `packages[]` entry, which is what a
    client actually installs. Reading only the first would let a bump move the
    label while the install stayed pinned to an older release -- a listing that
    names one version and hands over another, which nobody downstream can
    detect. So every copy is read and all of them have to agree.

    The entry also has to describe *this* package. A merely stale manifest is
    caught by the version comparison, but one pointing at another identifier or
    another index would publish someone else's package under this repository's
    namespace, and nothing looks at it again once the listing is accepted.
    """
    path = _server_manifest_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VersionError(f"cannot read server.json: {exc}") from exc
    version = data.get("version")
    if not isinstance(version, str):
        raise VersionError("server.json version must be a string")
    _parse_version(version)
    repository = data.get("repository")
    if not isinstance(repository, dict) or repository.get("url") != CHANGELOG_REPOSITORY:
        raise VersionError(f"server.json repository.url must be {CHANGELOG_REPOSITORY}")
    packages = data.get("packages")
    if not isinstance(packages, list) or not packages:
        raise VersionError("server.json must list at least one package")
    expected_name = _read_distribution_name(root)
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            raise VersionError(f"server.json packages[{index}] must be an object")
        if package.get("registryType") != "pypi":
            raise VersionError(f"server.json packages[{index}].registryType must be pypi")
        if package.get("identifier") != expected_name:
            raise VersionError(
                f"server.json packages[{index}].identifier must be {expected_name}"
            )
        if package.get("version") != version:
            raise VersionError(
                f"server.json packages[{index}].version is {package.get('version')!r} "
                f"but the manifest names {version!r}"
            )
    return version


def _read_readme_version(root: Path, name: str) -> str:
    path = root / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionError(f"cannot read {name}: {exc}") from exc
    pattern = README_VERSION_PATTERNS[name]
    match = pattern.search(text)
    if match is None:
        raise VersionError(f"{name} is missing its canonical release version line")
    version = match.group(2)
    _parse_version(version)
    return version


def _read_changelog_version(root: Path) -> str:
    path = root / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VersionError(f"cannot read CHANGELOG.md: {exc}") from exc
    if CHANGELOG_UNRELEASED_RE.search(text) is None:
        raise VersionError("CHANGELOG.md is missing an [Unreleased] section")
    match = CHANGELOG_RELEASE_RE.search(text)
    if match is None:
        raise VersionError("CHANGELOG.md is missing a dated release section")
    version = match.group(1)
    _parse_version(version)
    for label in ("Unreleased", version):
        if re.search(rf"^\[{re.escape(label)}\]:\s+\S+$", text, re.MULTILINE) is None:
            raise VersionError(f"CHANGELOG.md is missing the [{label}] comparison link")
    return version


def validate_versions(root: Path = ROOT) -> dict[str, str]:
    root = Path(root).resolve()
    source = read_source_version(root)
    versions = {
        "source": source,
        "package": _read_package_version(root, source),
        "manifest": _read_manifest_version(root),
        "server_manifest": _read_server_manifest_version(root),
        "readme": _read_readme_version(root, "README.md"),
        "readme_zh": _read_readme_version(root, "README.zh-CN.md"),
        "changelog": _read_changelog_version(root),
    }
    if len(set(versions.values())) != 1:
        rendered = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise VersionError(f"BTAP versions are inconsistent: {rendered}")
    return versions


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_changelog(text: str, target: str) -> str:
    unreleased = CHANGELOG_UNRELEASED_RE.search(text)
    latest = CHANGELOG_RELEASE_RE.search(text)
    if unreleased is None or latest is None or latest.start() < unreleased.end():
        raise VersionError("CHANGELOG.md must put [Unreleased] before dated releases")
    previous = latest.group(1)
    if target == previous:
        return text
    if _parse_version(target) <= _parse_version(previous):
        raise VersionError(
            f"cannot synchronize CHANGELOG.md backwards: latest={previous}, target={target}"
        )

    pending = text[unreleased.end() : latest.start()].strip()
    prefix = text[: unreleased.start()]
    suffix = text[latest.start() :]
    release = f"## [{target}] - {date.today().isoformat()}"
    if pending:
        release += f"\n\n{pending}"
    updated = f"{prefix}## [Unreleased]\n\n{release}\n\n{suffix}"

    unreleased_link = f"[Unreleased]: {CHANGELOG_REPOSITORY}/compare/v{target}...HEAD"
    target_link = f"[{target}]: {CHANGELOG_REPOSITORY}/compare/v{previous}...v{target}"
    updated, replaced = re.subn(
        r"^\[Unreleased\]:\s+\S+$",
        lambda _match: f"{unreleased_link}\n{target_link}",
        updated,
        count=1,
        flags=re.MULTILINE,
    )
    if replaced != 1:
        raise VersionError("CHANGELOG.md is missing the [Unreleased] comparison link")
    return updated


def sync_versions(root: Path, version: str) -> list[Path]:
    root = Path(root).resolve()
    _parse_version(version)
    source_path = _version_path(root)
    manifest_path = _manifest_path(root)
    server_manifest_path = _server_manifest_path(root)
    changelog_path = root / "CHANGELOG.md"
    readme_paths = [root / name for name in README_VERSION_PATTERNS]

    current_source = source_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    server_manifest = json.loads(server_manifest_path.read_text(encoding="utf-8"))
    current_changelog = changelog_path.read_text(encoding="utf-8")
    _read_package_version(root, version)
    manifest["version"] = version
    # Both copies, for the reason `_read_server_manifest_version` documents: the
    # top-level one is the label, the per-package one is what gets installed.
    server_manifest["version"] = version
    for package in server_manifest.get("packages", []):
        if isinstance(package, dict):
            package["version"] = version
    readme_desired: dict[Path, str] = {}
    for path in readme_paths:
        pattern = README_VERSION_PATTERNS[path.name]
        current = path.read_text(encoding="utf-8")
        desired, replacements = pattern.subn(
            lambda match: f"{match.group(1)}{version}{match.group(3)}",
            current,
            count=1,
        )
        if replacements != 1:
            raise VersionError(f"{path.name} is missing its canonical release version line")
        readme_desired[path] = desired
    desired = {
        source_path: f'__version__ = "{version}"\n',
        manifest_path: json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        server_manifest_path: json.dumps(server_manifest, ensure_ascii=False, indent=2) + "\n",
        changelog_path: _sync_changelog(current_changelog, version),
        **readme_desired,
    }
    originals = {
        source_path: current_source,
        manifest_path: manifest_path.read_text(encoding="utf-8"),
        server_manifest_path: server_manifest_path.read_text(encoding="utf-8"),
        changelog_path: current_changelog,
        **{path: path.read_text(encoding="utf-8") for path in readme_paths},
    }
    changed = [path for path, content in desired.items() if originals[path] != content]
    written: list[Path] = []
    try:
        for path in changed:
            _atomic_write(path, desired[path])
            written.append(path)
    except BaseException:
        for path in reversed(written):
            _atomic_write(path, originals[path])
        raise
    validate_versions(root)
    return changed


def validate_version_increment(
    base_version: str,
    current_version: str,
    changed_paths: list[str] | tuple[str, ...],
) -> bool:
    production_changed = any(
        path.replace("\\", "/") in VERSIONED_PATHS
        or path.replace("\\", "/").startswith(VERSIONED_PATH_PREFIXES)
        for path in changed_paths
    )
    if production_changed and _parse_version(current_version) <= _parse_version(base_version):
        raise VersionError(
            "production files changed but the unified version did not increase: "
            f"base={base_version}, current={current_version}"
        )
    return production_changed


def latest_release_tag(root: Path = ROOT) -> str | None:
    """The newest tag reachable from HEAD, or None when the repository has none.

    The local finalizer has no equivalent of a CI push event to compare against,
    so it uses the last release as the baseline. A repository with no tag yet has
    nothing to compare against, which is not a failure.
    """
    completed = subprocess.run(
        ("git", "describe", "--tags", "--abbrev=0"),
        cwd=Path(root),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


#: Package directories the version source has lived in, newest first. The 0.4.0
#: rename moved it, and a baseline from before that commit still has to be
#: readable -- otherwise the gate crashes on exactly the change set it exists to
#: validate, and the release that renames the package is the one that skips the
#: check.
VERSION_SOURCE_REFS = (
    "src/browsertap_mcp/_version.py",
    "src/agent_browser_mcp/_version.py",
)


def _version_source_at_ref(root: Path, ref: str) -> str:
    details: list[str] = []
    for candidate in VERSION_SOURCE_REFS:
        completed = subprocess.run(
            ("git", "show", f"{ref}:{candidate}"),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout
        details.append(completed.stderr.strip() or completed.stdout.strip())
    raise VersionError(f"cannot compare version against {ref!r}: {'; '.join(details)}")


def _version_at_ref(root: Path, ref: str) -> str:
    source = _version_source_at_ref(root, ref)
    try:
        module = ast.parse(source, filename=f"{ref}:_version.py")
    except SyntaxError as exc:
        raise VersionError(f"base version source is invalid: {exc}") from exc
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise VersionError("base version source does not assign __version__")


def _changed_paths_since(root: Path, ref: str, *, include_worktree: bool) -> list[str]:
    """Paths that differ from ``ref``.

    ``include_worktree`` compares the *working tree* rather than ``HEAD``, which
    is what the local finalizer needs: it runs before the change set is
    committed, and a commit-range diff would report a production change set of
    nothing and let the version stand still.
    """
    revisions = (ref,) if include_worktree else (f"{ref}...HEAD",)
    commands = [("git", "diff", "--name-only", *revisions)]
    if include_worktree:
        commands.append(("git", "ls-files", "--others", "--exclude-standard"))
    paths: set[str] = set()
    for command in commands:
        try:
            output = subprocess.run(
                command,
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise VersionError(f"cannot compare version against {ref!r}: {detail}") from exc
        paths.update(line.strip() for line in output.splitlines() if line.strip())
    return sorted(paths)


def validate_version_bump(
    root: Path, base_ref: str, *, include_worktree: bool = False
) -> dict[str, object]:
    root = Path(root).resolve()
    base_version = _version_at_ref(root, base_ref)
    current_version = read_source_version(root)
    changed_paths = _changed_paths_since(root, base_ref, include_worktree=include_worktree)
    production_changed = validate_version_increment(base_version, current_version, changed_paths)
    return {
        "base": base_version,
        "current": current_version,
        "production_changed": production_changed,
        "changed_paths": changed_paths,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or update BTAP component versions")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    bump = subparsers.add_parser("bump")
    bump.add_argument("part", choices=("patch", "minor"))
    sync = subparsers.add_parser("sync")
    sync.add_argument("version")
    check_bump = subparsers.add_parser("check-bump")
    check_bump.add_argument("--base", required=True)
    check_bump.add_argument(
        "--worktree",
        action="store_true",
        help="compare the working tree instead of HEAD, for a check that runs before the commit",
    )
    args = parser.parse_args(argv)

    if args.command == "check":
        print(json.dumps(validate_versions(ROOT), sort_keys=True))
        return 0
    if args.command == "check-bump":
        result = validate_version_bump(ROOT, args.base, include_worktree=args.worktree)
        print(json.dumps(result, sort_keys=True))
        return 0
    current = read_source_version(ROOT)
    target = bump_version(current, args.part) if args.command == "bump" else args.version
    changed = sync_versions(ROOT, target)
    print(json.dumps({"version": target, "changed": [str(path) for path in changed]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
