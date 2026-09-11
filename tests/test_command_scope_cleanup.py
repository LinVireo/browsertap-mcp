from __future__ import annotations

import errno
import os

import pytest

import browsertap_mcp.command_scope as C
from browsertap_mcp.paths import STATE_DIR_ENV

NAMESPACE = "scope-cleanup.invalid:18765"


class _OSProxy:
    def __init__(self, close):
        self.close = close

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.fixture(autouse=True)
def _isolated_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _close_owned(descriptors):
    for fd, identity in descriptors.items():
        try:
            if _identity(fd) == identity:
                os.close(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


def _assert_closed(fd):
    with pytest.raises(OSError) as raised:
        os.fstat(fd)
    assert raised.value.errno == errno.EBADF


def _lock_identities(tmp_path):
    result = {}
    for path in (tmp_path / "state" / "command-locks").iterdir():
        info = path.stat()
        result[path.name] = info.st_dev, info.st_ino, info.st_size
    return result


@pytest.mark.parametrize(
    ("failed_positions", "error_type"),
    [
        ((0,), OSError),
        ((1,), OSError),
        ((0, 2), OSError),
        ((0,), KeyboardInterrupt),
    ],
    ids=["first-close", "middle-close", "multiple-errors", "interrupted-close"],
)
def test_close_attempts_every_descriptor_and_preserves_the_first_error(
    tmp_path, monkeypatch, failed_positions, error_type,
):
    scope = C._Scope(persist_defaults=False)
    targets = ["tab-a", "tab-b", "tab-c"]
    scope.acquire(NAMESPACE, targets)
    descriptors = {fd: _identity(fd) for fd in scope.locks.values()}
    fds = list(descriptors)
    files_before = _lock_identities(tmp_path)
    errors = {fds[index]: error_type(f"synthetic close failure {index}") for index in failed_positions}
    calls = []

    def close_then_report(fd):
        calls.append(fd)
        os.close(fd)
        if fd in errors:
            raise errors[fd]

    try:
        with monkeypatch.context() as patch:
            patch.setattr(C, "os", _OSProxy(close_then_report))
            with pytest.raises(error_type) as raised:
                scope.close()
        assert raised.value is errors[fds[failed_positions[0]]]
        assert calls == fds
        for fd in fds:
            _assert_closed(fd)
        for target in targets:
            with C.command_scope():
                C.guard_targets(NAMESPACE, [target])
        assert _lock_identities(tmp_path) == files_before
    finally:
        _close_owned(descriptors)
        scope.locks.clear()


def test_repeated_cleanup_does_not_close_a_reused_descriptor(tmp_path, monkeypatch):
    scope = C._Scope(persist_defaults=False)
    scope.acquire(NAMESPACE, ["tab-reuse"])
    fd = next(iter(scope.locks.values()))
    descriptors = {fd: _identity(fd)}
    failure = OSError(errno.EIO, "synthetic close result is unknown")
    replacements = {}

    def close_then_report(closing_fd):
        os.close(closing_fd)
        raise failure

    try:
        with monkeypatch.context() as patch:
            patch.setattr(C, "os", _OSProxy(close_then_report))
            with pytest.raises(OSError) as raised:
                scope.close()
        assert raised.value is failure
        _assert_closed(fd)
        # Allocate real descriptors until the released number is reused. Never
        # dup2 over a descriptor that another component might already own.
        for index in range(64):
            replacement = os.open(
                tmp_path / f"unrelated-{index}.bin", os.O_CREAT | os.O_RDWR, 0o600,
            )
            replacements[replacement] = _identity(replacement)
            if replacement == fd:
                break
        assert fd in replacements, "the real released fd number must be reused"
        scope.close()
        assert _identity(fd) == replacements[fd]
        os.write(fd, b"the unrelated descriptor remains usable")
    finally:
        _close_owned(replacements)
        _close_owned(descriptors)
        scope.locks.clear()


def test_unknown_close_result_is_not_retried_and_other_targets_are_released(monkeypatch):
    scope = C._Scope(persist_defaults=False)
    scope.acquire(NAMESPACE, ["tab-unknown-a", "tab-unknown-b"])
    descriptors = {fd: _identity(fd) for fd in scope.locks.values()}
    first, second = descriptors
    failure = OSError(errno.EINTR, "synthetic close error before descriptor release")
    calls = []

    def report_before_close(fd):
        calls.append(fd)
        if fd == first:
            raise failure
        os.close(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(C, "os", _OSProxy(report_before_close))
            with pytest.raises(OSError) as raised:
                scope.close()
            assert raised.value is failure
            assert calls == [first, second]
            scope.close()
            assert calls == [first, second]
        assert _identity(first) == descriptors[first]
        _assert_closed(second)
        with C.command_scope():
            C.guard_targets(NAMESPACE, ["tab-unknown-b"])
    finally:
        # The injected pre-close failure deliberately left this test-owned fd
        # open. Production cannot infer that outcome and must not replay close.
        _close_owned(descriptors)
        scope.locks.clear()


def test_default_publication_error_still_drains_locks_after_a_close_error(monkeypatch):
    scope = C._Scope(persist_defaults=True)
    scope.acquire(NAMESPACE, ["tab-publish-a", "tab-publish-b"])
    descriptors = {fd: _identity(fd) for fd in scope.locks.values()}
    first, second = descriptors
    publication_error = ValueError("synthetic default publication failure")
    close_error = OSError(errno.EIO, "synthetic close result is unknown")
    calls = []

    class Owner:
        def publish_default_session_id(self, current, *, expected, expected_revision):
            raise publication_error

    scope.default_sessions[Owner()] = ("before", "after", 1)

    def close_then_report(fd):
        calls.append(fd)
        os.close(fd)
        if fd == first:
            raise close_error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(C, "os", _OSProxy(close_then_report))
            with pytest.raises(OSError) as raised:
                scope.close()
        assert raised.value is close_error
        assert raised.value.__context__ is publication_error
        assert calls == [first, second]
        _assert_closed(first)
        _assert_closed(second)
        with C.command_scope():
            C.guard_targets(NAMESPACE, ["tab-publish-b"])
    finally:
        _close_owned(descriptors)
        scope.locks.clear()
