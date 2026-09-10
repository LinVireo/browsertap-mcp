"""Isolate default targets and hold tab locks for one MCP tool invocation."""

from __future__ import annotations

import errno
import hashlib
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .paths import state_dir


def _normalize_namespace(namespace: str) -> str:
    host, separator, port = namespace.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if separator and port and host.casefold() in {"localhost", "127.0.0.1", "::1"}:
        return f"127.0.0.1:{port}"
    return namespace


class TargetBusyError(RuntimeError):
    error_code = "target_busy"

    def __init__(self, target: str, *, dispatched: bool) -> None:
        super().__init__(
            f"Target {target} is in use by another MCP call; check its result before retrying."
        )
        self.retry_safe = not dispatched
        self.delivery_state = "delivered_partial" if dispatched else "undelivered"
        self.diagnostics = {"busy_target": target, "may_have_executed": dispatched}


class _Scope:
    def __init__(self, *, persist_defaults: bool) -> None:
        self.locks: dict[str, int] = {}
        self.dispatched = False
        self.persist_defaults = persist_defaults
        self.default_sessions: dict[Any, tuple[str | None, str | None, int]] = {}

    def acquire(self, namespace: str, targets: list[str]) -> None:
        namespace = _normalize_namespace(namespace)
        directory = state_dir(create=True) / "command-locks"
        directory.mkdir(exist_ok=True)
        for target in sorted(set(targets)):
            key = f"{namespace}/{target}"
            if key in self.locks:
                continue
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            fd = os.open(directory / f"{digest}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                # Keep the inode and lock byte stable across owners; closing the
                # descriptor (including process death) releases the OS lock.
                if os.fstat(fd).st_size == 0:
                    os.ftruncate(fd, 1)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException as exc:
                os.close(fd)
                if isinstance(exc, OSError) and exc.errno in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                }:
                    raise TargetBusyError(target, dispatched=self.dispatched) from None
                raise
            self.locks[key] = fd

    def close(self) -> None:
        try:
            if self.persist_defaults:
                for owner, (initial, current, revision) in self.default_sessions.items():
                    if current != initial:
                        # An implicit failover may stick, but cannot overwrite a
                        # concurrent explicit switch made after this call began.
                        owner.publish_default_session_id(
                            current, expected=initial, expected_revision=revision
                        )
        finally:
            for fd in self.locks.values():
                os.close(fd)
            self.locks.clear()


_CURRENT: ContextVar[_Scope | None] = ContextVar("btap_command_scope", default=None)


def has_command_scope() -> bool:
    return _CURRENT.get() is not None


def get_scoped_default_session(
    owner: object, fallback: str | None, *, revision: int = 0
) -> str | None:
    scope = _CURRENT.get()
    if scope is None:
        return fallback
    return scope.default_sessions.setdefault(owner, (fallback, fallback, revision))[1]


def set_scoped_default_session(
    owner: object, value: str | None, fallback: str | None, *, revision: int = 0
) -> bool:
    scope = _CURRENT.get()
    if scope is None:
        return False
    initial, _, initial_revision = scope.default_sessions.setdefault(
        owner, (fallback, fallback, revision)
    )
    scope.default_sessions[owner] = (initial, value, initial_revision)
    return True


@contextmanager
def command_scope(*, persist_defaults: bool = False):
    current = _CURRENT.get()
    if current is not None:
        yield current
        return
    scope = _Scope(persist_defaults=persist_defaults)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)
        scope.close()


def guard_targets(namespace: str, targets: list[str]) -> None:
    """Acquire before dispatch; direct bridge traffic has no MCP scope."""
    scope = _CURRENT.get()
    if scope is not None:
        scope.acquire(namespace, targets)


def mark_dispatched() -> None:
    scope = _CURRENT.get()
    if scope is not None:
        scope.dispatched = True
