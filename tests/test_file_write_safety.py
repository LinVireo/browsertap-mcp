"""File failures must preserve existing data and other requests' descriptors."""

from __future__ import annotations

import base64
import errno
import hashlib
import os
from contextlib import contextmanager

import pytest

from browsertap_mcp import server as S


def test_atomic_write_completes_partial_writes(monkeypatch, tmp_path):
    original_write = os.write
    monkeypatch.setattr(S.os, "write", lambda descriptor, data: original_write(descriptor, data[:2]))
    target = tmp_path / "capture.png"
    S._atomic_write_bytes(target, b"complete result")
    assert target.read_bytes() == b"complete result"
    assert list(tmp_path.iterdir()) == [target]


def test_failed_replace_keeps_a_reused_descriptor_open(monkeypatch, tmp_path):
    target = tmp_path / "capture.png"
    target.write_bytes(b"original")
    unrelated = None

    def failed_replace(source, destination):
        nonlocal unrelated
        unrelated = os.open(tmp_path / "other-request", os.O_CREAT | os.O_WRONLY, 0o600)
        raise PermissionError(errno.EACCES, "replace refused")

    monkeypatch.setattr(S.os, "replace", failed_replace)
    try:
        with pytest.raises(RuntimeError, match="permission denied"):
            S._atomic_write_bytes(target, b"replacement")
        assert os.write(unrelated, b"still open") == 10
    finally:
        if unrelated is not None:
            try:
                os.close(unrelated)
            except OSError:
                pass
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".tmp_*"))


def test_payload_failure_keeps_a_reused_descriptor_open(monkeypatch, tmp_path):
    original_fdopen = os.fdopen
    unrelated = None
    monkeypatch.setattr(S.tempfile, "tempdir", str(tmp_path))

    @contextmanager
    def failing_stream(descriptor, mode):
        nonlocal unrelated
        try:
            with original_fdopen(descriptor, mode) as stream:
                yield stream
                raise OSError(errno.ENOSPC, "flush failed")
        finally:
            unrelated = os.open(tmp_path / "other-request", os.O_CREAT | os.O_WRONLY, 0o600)

    monkeypatch.setattr(S.os, "fdopen", failing_stream)
    try:
        with pytest.raises(OSError, match="flush failed"):
            S._write_execute_js_payload(b'{"result":42}')
        assert os.write(unrelated, b"still open") == 10
    finally:
        if unrelated is not None:
            try:
                os.close(unrelated)
            except OSError:
                pass
    assert not list(tmp_path.glob("browsertap-execute-js-*.json"))


def test_payload_open_failure_releases_descriptor_and_partial_file(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "tempdir", str(tmp_path))
    descriptors = []

    def failed_open(descriptor, mode):
        descriptors.append(descriptor)
        raise OSError(errno.ENOSPC, "open failed")

    monkeypatch.setattr(S.os, "fdopen", failed_open)
    with pytest.raises(OSError, match="open failed"):
        S._write_execute_js_payload(b"{}")
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cleanup", ["close", "unlink"])
def test_payload_cleanup_failure_preserves_the_original_open_error(monkeypatch, tmp_path, cleanup):
    monkeypatch.setattr(S.tempfile, "tempdir", str(tmp_path))
    real_close, real_unlink = os.close, S.Path.unlink
    cleanup_calls = []

    def failed_open(descriptor, mode):
        raise OSError(errno.ENOSPC, "payload open failed")

    def failed_close(descriptor):
        real_close(descriptor)
        cleanup_calls.append("close")
        raise OSError("close failed")

    def failed_unlink(path, *args, **kwargs):
        real_unlink(path, *args, **kwargs)
        cleanup_calls.append("unlink")
        raise OSError("unlink failed")

    monkeypatch.setattr(S.os, "fdopen", failed_open)
    if cleanup == "close":
        monkeypatch.setattr(S.os, "close", failed_close)
    else:
        monkeypatch.setattr(S.Path, "unlink", failed_unlink)
    with pytest.raises(OSError, match="payload open failed"):
        S._write_execute_js_payload(b"{}")
    assert cleanup_calls == [cleanup]
    assert not list(tmp_path.iterdir())


def test_atomic_write_close_failure_does_not_mask_the_disk_error(monkeypatch, tmp_path):
    target = tmp_path / "capture.png"
    target.write_bytes(b"original")
    real_close = os.close
    closed = []

    def failed_write(descriptor, data):
        raise OSError(errno.ENOSPC, "disk full")

    def failed_close(descriptor):
        real_close(descriptor)
        closed.append(descriptor)
        raise OSError("close failed")

    monkeypatch.setattr(S.os, "write", failed_write)
    monkeypatch.setattr(S.os, "close", failed_close)
    with pytest.raises(RuntimeError, match="disk full"):
        S._atomic_write_bytes(target, b"replacement")
    assert len(closed) == 1
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


def test_payload_file_is_complete_and_matches_its_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "tempdir", str(tmp_path))
    payload = b'{"result":42}'
    path, size, digest = S._write_execute_js_payload(payload)
    assert path.parent == tmp_path
    assert path.read_bytes() == payload
    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("failure", [OSError(errno.ENOSPC, "disk full"), KeyboardInterrupt()])
def test_atomic_write_failure_preserves_destination_and_cleans_up(
    monkeypatch, tmp_path, failure,
):
    target = tmp_path / "capture.png"
    target.write_bytes(b"original")
    descriptors = []

    def failed_write(descriptor, data):
        descriptors.append(descriptor)
        raise failure

    monkeypatch.setattr(S.os, "write", failed_write)
    with pytest.raises(RuntimeError if isinstance(failure, OSError) else KeyboardInterrupt):
        S._atomic_write_bytes(target, b"replacement")
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".tmp_*"))


@pytest.mark.parametrize("path", [None, "", "   ", "../escape.pdf", "/absolute.pdf"])
def test_pdf_rejects_invalid_paths_before_printing(monkeypatch, tmp_path, path):
    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    calls = []
    monkeypatch.setattr(S, "cdp_command", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(ValueError):
        S.save_pdf(path)
    assert calls == []
    assert not list(tmp_path.iterdir())


def test_pdf_sync_failure_preserves_destination_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    target = tmp_path / "Downloads" / "browsertap" / "page.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    monkeypatch.setattr(S, "cdp_command", lambda *args, **kwargs: {
        "data": {"data": base64.b64encode(b"%PDF-1.7\nbody\n%%EOF\n").decode("ascii")},
    })

    def failed_sync(descriptor):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(S.os, "fsync", failed_sync)
    with pytest.raises(RuntimeError, match="disk full"):
        S.save_pdf("page.pdf")
    assert target.read_bytes() == b"original"
    assert list(target.parent.iterdir()) == [target]
