"""Bounded local snapshots required before removing browser bookmarks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .command_scope import command_scope, guard_targets
from .paths import state_dir

MAX_BACKUP_BYTES = 16 * 1024 * 1024
MAX_STORE_BYTES = 64 * 1024 * 1024
MAX_BACKUPS = 100
RETENTION_SECONDS = 30 * 24 * 60 * 60
_BACKUP_NAME = re.compile(r"btap-bookmarks-[0-9a-f]{32}\.json\Z")
_TEMP_NAME = re.compile(r"\.btap-bookmarks-[0-9a-f]{32}\.tmp\Z")


def bookmark_subtree(tree: Any, bookmark_id: str) -> dict[str, Any]:
    """Find one unambiguous subtree in the browser's complete tree reply."""
    if not isinstance(tree, list):
        raise ValueError("browser did not return a bookmark tree")
    pending = list(tree)
    found = None
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise ValueError("browser returned a malformed bookmark node")
        if node["id"] == bookmark_id:
            if found is not None:
                raise ValueError("browser returned duplicate bookmark ids")
            found = node
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError("browser returned malformed bookmark children")
        pending.extend(children)
    if found is None:
        raise ValueError("bookmark is absent from the backup snapshot; nothing was removed")
    return found


def _make_room(directory: Path, incoming_bytes: int, now: float) -> None:
    """Expire only this store's regular files while its stable lock is held."""
    backups = []
    for path in directory.iterdir():
        if not (_BACKUP_NAME.fullmatch(path.name) or _TEMP_NAME.fullmatch(path.name)):
            continue
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if _TEMP_NAME.fullmatch(path.name):
            # A process crash can leave an unpublished write. No writer can be
            # active here because all writes hold the same process-safe lock.
            path.unlink()
        elif now - metadata.st_mtime >= RETENTION_SECONDS:
            path.unlink()
        else:
            backups.append((metadata.st_mtime_ns, path, metadata.st_size))
    total = sum(size for _, _, size in backups)
    remaining = len(backups)
    for _, path, size in sorted(backups):
        if remaining < MAX_BACKUPS and total + incoming_bytes <= MAX_STORE_BYTES:
            break
        path.unlink()
        remaining -= 1
        total -= size
    if remaining >= MAX_BACKUPS or total + incoming_bytes > MAX_STORE_BYTES:
        raise OSError("bookmark backup storage limit exceeded")


def save_bookmark_backup(
    subtree: dict[str, Any], *, client_id: str, recursive: bool,
) -> dict[str, Any]:
    """Fsync and atomically publish the recovery snapshot before deletion.

    The caller keeps its command_scope open through the browser removal, so a
    concurrent call cannot evict this backup between publication and dispatch.
    """
    captured_at = time.time()
    payload = {
        "schema": "btap.bookmark-backup.v1",
        "captured_at": captured_at,
        "client_id": client_id,
        "bookmark_id": subtree["id"],
        "recursive": recursive,
        "subtree": subtree,
    }
    # JSON can carry unpaired UTF-16 surrogates from JS. Escape only those code
    # points, keeping valid Unicode's UTF-8 bytes and byte quotas unchanged.
    content = (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode(
        "utf-8", errors="backslashreplace",
    )
    if len(content) > min(MAX_BACKUP_BYTES, MAX_STORE_BYTES):
        raise ValueError("bookmark subtree exceeds the local backup size limit; nothing was removed")
    with command_scope():
        guard_targets("local-storage", ["bookmark-backups"])
        directory = state_dir(create=True) / "bookmark-backups"
        directory.mkdir(exist_ok=True)
        # The configured state root may intentionally resolve links. Check this
        # managed child itself before retention or writes can follow a redirect.
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError(
                "bookmark backup path must be an ordinary directory (no symlink or reparse point)"
            )
        _make_room(directory, len(content), captured_at)
        identity = uuid.uuid4().hex
        destination = directory / f"btap-bookmarks-{identity}.json"
        temporary = directory / f".btap-bookmarks-{identity}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if destination.exists():
                raise FileExistsError("bookmark backup identifier already exists")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "backup_path": str(destination.resolve()),
        "backup_sha256": hashlib.sha256(content).hexdigest(),
        "backup_bytes": len(content),
        "backup_captured_at": captured_at,
        "backup_expires_at": captured_at + RETENTION_SECONDS,
    }
