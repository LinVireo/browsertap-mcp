"""Compare package-import source snapshots with the package currently on disk.

This is a source manifest, not a hash of executed code objects. It covers every
``.py`` and the JavaScript assets cached by MCP imports, without needing Git.
Imports must use stable files: bytecode/custom loaders, runtime monkeypatches
and same-user ABA edits cannot be certified by this on-disk snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SOURCE_IDENTITY_SCHEMA = "btap.package-source.v2"
_SOURCE_SCOPE = "package_python_sources_and_imported_javascript"
_PACKAGE_DIRECTORY = Path(__file__).parent
# These assets are cached by server.py and simphtml.py at import time. Keep an
# explicit required roster: discovering only existing .js files would silently
# certify a partial installation when one of these files is missing.
_IMPORTED_JAVASCRIPT = (
    "chrome_extension/result_serialization.js",
    "chrome_extension/guarded_eval.js",
    "chrome_extension/disable_dialogs.js",
    "page_scripts/page_outline.js",
    "page_scripts/list_groups.js",
)


class _SourceChangedError(Exception):
    pass


@dataclass(frozen=True)
class SourceIdentity:
    manifest: tuple[tuple[str, str], ...]
    sha256: str | None
    error: str | None = None
    schema: str = SOURCE_IDENTITY_SCHEMA
    scope: str = _SOURCE_SCOPE

    def as_dict(self) -> dict[str, Any]:
        # No installation paths or exception text: this goes into public live
        # evidence as well as local diagnostics. Return a copy of immutable state.
        return {
            "schema": self.schema,
            "scope": self.scope,
            "sha256": self.sha256,
            "file_count": len(self.manifest),
            "complete": self.sha256 is not None and self.error is None,
            "error": self.error,
        }


def _raise_walk_error(error: OSError) -> None:
    raise error


def _python_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(directory, onerror=_raise_walk_error):
        # os.walk skips directory symlinks by default. Silently omitting a
        # package behind one would let a partial manifest claim to be complete.
        if any((Path(root) / name).is_symlink() for name in dirs):
            raise OSError("source directory symlink cannot be fully inspected")
        files.extend(Path(root) / name for name in names if name.endswith(".py"))
    return sorted(files, key=lambda path: path.relative_to(directory).as_posix())


def _file_state(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def capture_source_identity(directory: Path | None = None) -> SourceIdentity:
    """Hash Python and required import assets; missing/unstable input is unknown."""
    manifest: list[tuple[str, str]] = []
    states: list[tuple[Path, tuple[int, int, int, int]]] = []
    try:
        root = (directory if directory is not None else _PACKAGE_DIRECTORY).resolve(strict=True)
        python_files = _python_files(root)
        if not python_files or root / "__init__.py" not in python_files:
            return SourceIdentity((), None, "python_sources_unavailable")
        files = python_files + [root / name for name in _IMPORTED_JAVASCRIPT]
        for path in files:
            with path.open("rb") as source:
                before = os.fstat(source.fileno())
                contents = source.read()
                after = os.fstat(source.fileno())
            if (
                _file_state(before) != _file_state(after)
                or _file_state(after) != _file_state(path.stat())
            ):
                raise _SourceChangedError
            manifest.append(
                (path.relative_to(root).as_posix(), hashlib.sha256(contents).hexdigest())
            )
            states.append((path, _file_state(after)))
        if python_files != _python_files(root):
            raise _SourceChangedError
        # A file read near the start may change while a later file is hashed.
        # Per-file before/after checks alone would miss that window.
        if any(_file_state(path.stat()) != state for path, state in states):
            raise _SourceChangedError
    except OSError:
        return SourceIdentity(tuple(manifest), None, "python_sources_unreadable")
    except _SourceChangedError:
        return SourceIdentity(tuple(manifest), None, "python_sources_changed_during_read")
    payload = json.dumps(
        [SOURCE_IDENTITY_SCHEMA, manifest], ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return SourceIdentity(tuple(manifest), hashlib.sha256(payload).hexdigest())


def loaded_source_identity() -> dict[str, Any]:
    # __init__ seals this before importing the version or any other submodule.
    # In particular this function must never re-read disk or lazily initialise
    # the snapshot during a diagnostic request.
    from . import _PYTHON_SOURCE_IDENTITY

    return _PYTHON_SOURCE_IDENTITY.as_dict()


def current_source_identity() -> dict[str, Any]:
    return capture_source_identity().as_dict()


def _verifiable_identity(identity: Any) -> bool:
    if not isinstance(identity, Mapping):
        return False
    digest = identity.get("sha256")
    count = identity.get("file_count")
    return (
        identity.get("schema") == SOURCE_IDENTITY_SCHEMA
        and identity.get("scope") == _SOURCE_SCOPE
        and identity.get("complete") is True
        and identity.get("error") is None
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and type(count) is int
        and count > 0
    )


def compare_source_identities(loaded: Any, expected: Any) -> str:
    """Only complete, same-schema source identities can prove a comparison."""
    if not _verifiable_identity(loaded) or not _verifiable_identity(expected):
        return "unverifiable"
    if loaded["sha256"] == expected["sha256"] and loaded["file_count"] == expected["file_count"]:
        return "matches_tree"
    return "stale_process"
