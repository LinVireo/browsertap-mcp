"""Prove local MCP requester exit through an OS lock held for its lifetime."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
from pathlib import Path
from typing import Literal

from .paths import state_dir

Liveness = Literal["alive", "dead", "unknown"]
_LOCAL_PREFIX = "mcp-local-v1:"
_IDENTITY_LOCK = threading.Lock()
_IDENTITIES: dict[tuple[int, str], tuple[str, int | None]] = {}


def _directory() -> Path:
    return state_dir() / "requester-locks"


def _identity_path(directory: Path, requester_id: str) -> Path:
    digest = hashlib.sha256(requester_id.encode("ascii")).hexdigest()
    return directory / f"{digest}.lock"


def _try_lock(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def process_requester_id() -> str:
    """Reuse one identity per process/state directory and retain its descriptor.

    The record is published only after acquiring the lock. An incomplete record
    is never evidence of death, and a published identity is never reused.
    """
    directory = _directory()
    key = (os.getpid(), str(directory.absolute()))
    with _IDENTITY_LOCK:
        existing = _IDENTITIES.get(key)
        if existing is not None:
            return existing[0]
        requester_id = _LOCAL_PREFIX + secrets.token_urlsafe(24)
        fd = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = _identity_path(directory, requester_id)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.ftruncate(fd, 1)
            if not _try_lock(fd):
                raise OSError("new requester identity lock could not be acquired")
            record = json.dumps({
                "format": 1,
                "requester_sha256": path.stem,
                "pid": os.getpid(),
            }, separators=(",", ":")).encode("ascii")
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = memoryview(record)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("requester identity record could not be written")
                remaining = remaining[written:]
            os.fsync(fd)
        except OSError:
            if fd is not None:
                os.close(fd)
            fd = None
            requester_id = "mcp-untracked-v1:" + secrets.token_urlsafe(24)
        _IDENTITIES[key] = (requester_id, fd)
        return requester_id


def requester_liveness(requester_id: str | None) -> Liveness:
    """Return dead only for a complete local identity whose OS lock is free."""
    if not isinstance(requester_id, str) or not re.fullmatch(
        r"mcp-local-v1:[A-Za-z0-9_-]{32}", requester_id, flags=re.ASCII
    ):
        return "unknown"
    with _IDENTITY_LOCK:
        if any(identity == requester_id and fd is not None for identity, fd in _IDENTITIES.values()):
            return "alive"
    path = _identity_path(_directory(), requester_id)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return "unknown"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 1024:
            return "unknown"
        if not _try_lock(fd):
            return "alive"
        os.lseek(fd, 0, os.SEEK_SET)
        record = json.loads(os.read(fd, 1024))
        if not isinstance(record, dict):
            return "unknown"
        if record != {
            "format": 1,
            "requester_sha256": path.stem,
            "pid": record.get("pid"),
        }:
            return "unknown"
        if type(record["format"]) is not int or type(record["pid"]) is not int or record["pid"] <= 0:
            return "unknown"
        return "dead"
    except (OSError, ValueError):
        return "unknown"
    finally:
        os.close(fd)


def _after_fork() -> None:
    global _IDENTITY_LOCK
    # An inherited descriptor must not keep the parent's identity alive after
    # the parent exits. A child mints its own identity on its first request.
    for _, fd in _IDENTITIES.values():
        if fd is not None:
            os.close(fd)
    _IDENTITIES.clear()
    _IDENTITY_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
