from __future__ import annotations

import errno
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from browsertap_mcp import requester_liveness as L
from browsertap_mcp.paths import STATE_DIR_ENV


def test_fork_hook_registration_releases_inherited_identity_descriptors(monkeypatch, tmp_path):
    hooks = []
    monkeypatch.setattr(os, "register_at_fork", lambda **kwargs: hooks.append(kwargs), raising=False)
    spec = importlib.util.spec_from_file_location("browsertap_mcp._fork_hook_test", L.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(hooks) == 1
    assert set(hooks[0]) == {"after_in_child"}

    path = tmp_path / "inherited.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    module._IDENTITIES[(123, str(tmp_path))] = ("parent", fd)
    inherited_lock = module._IDENTITY_LOCK
    try:
        hooks[0]["after_in_child"]()
        assert module._IDENTITIES == {}
        assert module._IDENTITY_LOCK is not inherited_lock
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        if module._IDENTITIES:
            os.close(fd)

_HOLDER = """
import os
import sys
from browsertap_mcp.requester_liveness import process_requester_id
print(process_requester_id(), flush=True)
if sys.stdin.readline().strip() == 'crash':
    os._exit(29)
"""


@pytest.fixture
def requester_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source, environment.get("PYTHONPATH")) if value
    )
    return environment


@contextmanager
def _holder(environment):
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", _HOLDER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    events = queue.Queue()
    reader = threading.Thread(target=lambda: events.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        identity = events.get(timeout=10).strip()
        assert identity.startswith("mcp-local-v1:")
        yield process, identity
    finally:
        if process.poll() is None:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def _stop(process, *, abrupt=False):
    process.stdin.write("crash\n" if abrupt else "exit\n")
    process.stdin.flush()
    assert process.wait(timeout=5) == (29 if abrupt else 0)


def _probe(environment, identity):
    completed = subprocess.run(
        [sys.executable, "-c", (
            "import sys; from browsertap_mcp.requester_liveness import requester_liveness; "
            "print(requester_liveness(sys.argv[1]))"
        ), identity],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.stdout.strip()


def test_concurrent_callers_reuse_the_process_identity(requester_environment):
    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = list(executor.map(lambda _: L.process_requester_id(), range(16)))
    assert len(set(identities)) == 1
    identity = identities[0]
    assert L.requester_liveness(identity) == "alive"
    assert _probe(requester_environment, identity) == "alive"
    files = list(L._directory().glob("*.lock"))
    assert len(files) == 1


@pytest.mark.parametrize("exit_mode", ["normal", "abrupt"])
def test_only_owner_process_exit_releases_its_identity(requester_environment, exit_mode):
    with _holder(requester_environment) as (process, identity):
        assert L.requester_liveness(identity) == "alive"
        assert _probe(requester_environment, identity) == "alive"
        _stop(process, abrupt=exit_mode == "abrupt")
        assert L.requester_liveness(identity) == "dead"
        assert _probe(requester_environment, identity) == "dead"
        assert L.requester_liveness(identity) == "dead"


@pytest.mark.parametrize("identity", [None, "", "legacy-owner", "mcp-local-v1:../bad", 42])
def test_unverifiable_requesters_never_read_as_dead(requester_environment, identity):
    assert L.requester_liveness(identity) == "unknown"


def test_missing_local_identity_record_is_unknown(requester_environment):
    assert L.requester_liveness("mcp-local-v1:" + "a" * 32) == "unknown"


@pytest.mark.parametrize(
    "content",
    [b"", b"{", b"null", b"[]", b'"text"', b"{}", b"\xff", b'{"format":2}'],
)
def test_incomplete_or_invalid_records_do_not_prove_owner_exit(
    requester_environment, content,
):
    with _holder(requester_environment) as (process, identity):
        _stop(process, abrupt=True)
        path = L._identity_path(L._directory(), identity)
        path.write_bytes(content)
        assert L.requester_liveness(identity) == "unknown"


def test_unreadable_identity_is_unknown(requester_environment, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "test read denied")

    monkeypatch.setattr(L.os, "open", denied)
    assert L.requester_liveness("mcp-local-v1:" + "a" * 32) == "unknown"


@pytest.mark.parametrize("field,value", [
    ("format", True), ("pid", False), ("pid", -1), ("requester_sha256", "another-owner"),
])
def test_mismatched_identity_metadata_does_not_prove_exit(requester_environment, field, value):
    with _holder(requester_environment) as (process, identity):
        _stop(process)
        path = L._identity_path(L._directory(), identity)
        record = json.loads(path.read_bytes())
        record[field] = value
        path.write_text(json.dumps(record), encoding="ascii")
        assert L.requester_liveness(identity) == "unknown"


def test_truncated_probe_cannot_accept_an_oversized_identity_record(requester_environment):
    with _holder(requester_environment) as (process, identity):
        _stop(process)
        path = L._identity_path(L._directory(), identity)
        path.write_bytes(path.read_bytes() + b" " * 1024 + b"invalid trailing data")
        assert L.requester_liveness(identity) == "unknown"


def test_identity_storage_failure_keeps_an_untracked_requester(requester_environment, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "test write denied")

    monkeypatch.setattr(L.os, "open", denied)
    identity = L.process_requester_id()
    assert identity.startswith("mcp-untracked-v1:")
    assert L.process_requester_id() == identity
    assert L.requester_liveness(identity) == "unknown"
