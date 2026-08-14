from __future__ import annotations

import argparse
import ast
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VersionPart = Literal["patch", "minor"]


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
    return root / "src" / "agent_browser_mcp" / "_version.py"


def _manifest_path(root: Path) -> Path:
    return root / "src" / "agent_browser_mcp" / "chrome_extension" / "manifest.json"


def read_source_version(root: Path = ROOT) -> str:
    path = _version_path(Path(root))
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise VersionError(f"cannot read version source: {exc}") from exc
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            version = node.value.value
            _parse_version(version)
            return version
    raise VersionError(f"{path} must assign a string to __version__")


def _read_package_version(root: Path, source_version: str) -> str:
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
    if version_config != {"attr": "agent_browser_mcp.__version__"}:
        raise VersionError(
            "tool.setuptools.dynamic.version must read agent_browser_mcp.__version__"
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


def validate_versions(root: Path = ROOT) -> dict[str, str]:
    root = Path(root).resolve()
    source = read_source_version(root)
    versions = {
        "source": source,
        "package": _read_package_version(root, source),
        "manifest": _read_manifest_version(root),
    }
    if len(set(versions.values())) != 1:
        rendered = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise VersionError(f"ABM versions are inconsistent: {rendered}")
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


def sync_versions(root: Path, version: str) -> list[Path]:
    root = Path(root).resolve()
    _parse_version(version)
    source_path = _version_path(root)
    manifest_path = _manifest_path(root)

    current_source = source_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _read_package_version(root, version)
    manifest["version"] = version
    desired = {
        source_path: f'__version__ = "{version}"\n',
        manifest_path: json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    }
    originals = {
        source_path: current_source,
        manifest_path: manifest_path.read_text(encoding="utf-8"),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or update ABM component versions")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    bump = subparsers.add_parser("bump")
    bump.add_argument("part", choices=("patch", "minor"))
    sync = subparsers.add_parser("sync")
    sync.add_argument("version")
    args = parser.parse_args(argv)

    if args.command == "check":
        print(json.dumps(validate_versions(ROOT), sort_keys=True))
        return 0
    current = read_source_version(ROOT)
    target = bump_version(current, args.part) if args.command == "bump" else args.version
    changed = sync_versions(ROOT, target)
    print(json.dumps({"version": target, "changed": [str(path) for path in changed]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
