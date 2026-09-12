"""Concurrent POSIX token publication with only synthetic, temporary state."""
from __future__ import annotations

import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest

from browsertap_mcp import _token_file as F
from browsertap_mcp import browser_bridge as B


@pytest.fixture
def token_path(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "_WINDOWS", False)
    path = tmp_path / "token"
    monkeypatch.setenv(B.TOKEN_FILE_ENV, str(path))
    monkeypatch.setenv(B.TOKEN_AUTH_ENV, "1")
    monkeypatch.setenv(B.TOKEN_ENV, "synthetic-concurrent-reader")
    return path


def test_concurrent_bootstrap_never_observes_a_short_write_prefix(monkeypatch, token_path):
    partial, release = threading.Event(), threading.Event()
    original_write = os.write
    first_candidate = "synthetic-original-writer"

    def short_write(fd, payload):
        if not partial.is_set():
            count = original_write(fd, payload[:1])
            partial.set()
            assert release.wait(5)
            return count
        return original_write(fd, payload)

    monkeypatch.setattr(os, "write", short_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(B._persist_token, token_path, first_candidate)
        try:
            assert partial.wait(5)
            reader = pool.submit(B.bridge_token)
            observed = reader.result(timeout=5)
        finally:
            release.set()
        written = writer.result(timeout=5)
    final = token_path.read_text(encoding="utf-8").strip()
    assert observed == written == final
    assert final in {first_candidate, "synthetic-concurrent-reader"}
    assert list(token_path.parent.iterdir()) == [token_path]


@pytest.mark.parametrize("raw", [b"x", b"legacy-without-newline", "旧 token".encode(), b"old-token\n"])
def test_existing_tokens_keep_their_bytes_and_win_over_new_candidates(monkeypatch, token_path, raw):
    token_path.write_bytes(raw)
    expected = raw.decode("utf-8").strip()
    assert B.bridge_token() == expected
    assert B._persist_token(token_path, "synthetic-losing-candidate") == expected
    assert token_path.read_bytes() == raw
    assert list(token_path.parent.iterdir()) == [token_path]


@pytest.mark.parametrize("failure", ["write", "zero_progress", "encoding"])
def test_failed_candidate_never_becomes_the_persistent_token(monkeypatch, token_path, failure):
    original_write = os.write
    attempted = False
    private_error = "synthetic-private-write-failure"

    def failed_write(fd, payload):
        nonlocal attempted
        if not attempted:
            attempted = True
            return original_write(fd, payload[:1])
        if failure == "zero_progress":
            return 0
        raise OSError(private_error)

    monkeypatch.setattr(os, "write", failed_write)
    candidate = "synthetic-input-\ud800" if failure == "encoding" else "synthetic-input"
    with pytest.raises(RuntimeError) as caught:
        B._persist_token(token_path, candidate)
    assert private_error not in "".join(traceback.format_exception(caught.value))
    assert not token_path.exists()
    assert list(token_path.parent.iterdir()) == []


@pytest.mark.parametrize("unreadable", [False, True])
def test_a_competitor_publishing_first_is_preserved(monkeypatch, token_path, unreadable):
    original_link, original_read = os.link, F.read_text
    winner = b"synthetic-racing-winner-without-newline"

    def competing_link(source, destination):
        assert destination == token_path
        with token_path.open("xb") as stream:
            stream.write(winner)
        return original_link(source, destination)

    def read(path):
        if unreadable and path == token_path:
            raise PermissionError("synthetic-private-read-error")
        return original_read(path)

    monkeypatch.setattr(os, "link", competing_link)
    monkeypatch.setattr(F, "read_text", read)
    if unreadable:
        with pytest.raises(RuntimeError, match="unreadable") as caught:
            B._persist_token(token_path, "synthetic-losing-candidate")
        assert "synthetic-private-read-error" not in "".join(traceback.format_exception(caught.value))
    else:
        assert B._persist_token(token_path, "synthetic-losing-candidate") == winner.decode()
    assert token_path.read_bytes() == winner
    assert list(token_path.parent.iterdir()) == [token_path]


@pytest.mark.parametrize("stage", ["sync", "publish"])
def test_storage_failure_removes_the_candidate_and_keeps_the_target_absent(monkeypatch, token_path, stage):
    private_error = "synthetic-private-publication-error"

    def denied(*args):
        raise PermissionError(private_error)

    monkeypatch.setattr(os, "fsync" if stage == "sync" else "link", denied)
    with pytest.raises(RuntimeError, match="cannot persist") as caught:
        B._persist_token(token_path, "synthetic-input")
    assert private_error not in "".join(traceback.format_exception(caught.value))
    assert not token_path.exists()
    assert list(token_path.parent.iterdir()) == []


@pytest.mark.parametrize("raw, reason", [(b"", "empty"), (b"\xff", "invalid_encoding")])
def test_invalid_existing_token_is_not_replaced_or_removed(monkeypatch, token_path, raw, reason):
    token_path.write_bytes(raw)
    monkeypatch.setattr(B.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match=reason):
        B._persist_token(token_path, "synthetic-losing-candidate")
    assert token_path.read_bytes() == raw
    assert list(token_path.parent.iterdir()) == [token_path]
