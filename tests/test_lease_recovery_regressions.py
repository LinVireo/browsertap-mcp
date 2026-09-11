"""Physical lease crash recovery using real file locks and stubbed input."""

from __future__ import annotations

import errno
import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from browsertap_mcp import physical_input as P

_CRASH_AFTER_CREATE = """
import os
import sys
from pathlib import Path
from browsertap_mcp import physical_input as P

path = Path(sys.argv[1])
real_open = P.os.open

def crash_after_create(target, flags, mode=0o777):
    fd = real_open(target, flags, mode)
    if Path(target) == path and flags & os.O_EXCL:
        os._exit(29)
    return fd

P.os.open = crash_after_create
with P.PhysicalInputLease(path=path):
    raise AssertionError('the process must exit before writing metadata')
"""

_CONTENDER = """
import json
import sys
from browsertap_mcp import physical_input as P

print(json.dumps({'status': 'ready'}), flush=True)
if sys.stdin.readline().strip() != 'go':
    raise SystemExit(0)
try:
    with P.PhysicalInputLease(path=sys.argv[1]) as lease:
        print(json.dumps({'status': 'acquired', 'token': lease.owner_token}), flush=True)
        sys.stdin.readline()
except P.PhysicalInputBusy:
    print(json.dumps({'status': 'busy'}), flush=True)
"""


def _child_environment():
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source, environment.get("PYTHONPATH")) if value
    )
    return environment


class _Contender:
    def __init__(self, path):
        # Only standard-library lease code is needed. Bypass the Windows venv
        # launcher so bounded process cleanup reaches the actual lock owner.
        self.process = subprocess.Popen(
            [sys._base_executable, "-B", "-u", "-c", _CONTENDER, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_child_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.events = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self):
        for line in self.process.stdout:
            self.events.put(line)
        self.events.put(None)

    def receive(self):
        try:
            line = self.events.get(timeout=15)
        except queue.Empty:
            pytest.fail("lease contender did not report within 15 seconds")
        if line is None:
            self.process.wait(timeout=5)
            pytest.fail(f"lease contender exited early: {self.process.stderr.read()}")
        return json.loads(line)

    def send(self, value):
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            self.reader.join(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()


@contextmanager
def _contender(path):
    contender = _Contender(path)
    try:
        assert contender.receive() == {"status": "ready"}
        yield contender
    finally:
        contender.close()


@pytest.fixture(autouse=True)
def stub_physical_input(monkeypatch):
    # Every dispatched action below is a list append; even quiet-window
    # sampling must not reach the user's mouse or keyboard.
    monkeypatch.setattr(P, "wait_for_quiet", lambda _seconds: {"test_stub": True})


def test_crash_between_exclusive_create_and_first_write_is_recoverable(tmp_path):
    path = tmp_path / "physical.lock"
    completed = subprocess.run(
        [sys._base_executable, "-B", "-c", _CRASH_AFTER_CREATE, str(path)],
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 29, completed.stderr
    assert path.read_bytes() == b""
    actions = []

    P.run_physical_action("recovered action", lambda: actions.append("once"), lock_path=path)

    assert actions == ["once"]
    assert not path.exists()


@pytest.mark.parametrize(
    "payload",
    [b"", b"{", b"not-json", b"\xff", b"[]", b"null", b"{}", b'{"expires_at":0}'],
)
def test_orphaned_invalid_metadata_is_recovered(payload, tmp_path):
    path = tmp_path / "physical.lock"
    path.write_bytes(payload)
    with P.PhysicalInputLease(path=path) as lease:
        assert json.loads(path.read_bytes())["owner_token"] == lease.owner_token
    assert not path.exists()


@pytest.mark.parametrize("payload", [b"", b"not-json", b"{}"])
def test_corrupt_metadata_cannot_steal_a_live_process_guard(payload, tmp_path):
    path = tmp_path / "physical.lock"
    actions = []
    with _contender(path) as holder:
        holder.send("go")
        assert holder.receive()["status"] == "acquired"
        path.write_bytes(payload)
        with pytest.raises(P.PhysicalInputBusy):
            P.run_physical_action("blocked", lambda: actions.append("input"), lock_path=path)
        assert path.read_bytes() == payload
        assert holder.process.poll() is None

    assert actions == []
    with P.PhysicalInputLease(path=path):
        assert path.exists()


@pytest.mark.parametrize("token", [None, "synthetic-live-owner"])
def test_live_owner_metadata_is_preserved_even_without_a_guard(token, tmp_path):
    path = tmp_path / "physical.lock"
    record = {"pid": os.getpid(), "expires_at": time.time() + 60}
    if token is not None:
        record["owner_token"] = token
    payload = json.dumps(record).encode("utf-8")
    path.write_bytes(payload)

    with pytest.raises(P.PhysicalInputBusy), P.PhysicalInputLease(path=path):
        pytest.fail("live metadata must not authorize input")

    assert path.read_bytes() == payload


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
@pytest.mark.parametrize("failed_read", [1, 2])
def test_unreadable_metadata_is_not_treated_as_corrupt(
    monkeypatch, tmp_path, error_number, failed_read,
):
    path = tmp_path / "physical.lock"
    path.write_bytes(b"")
    real_read_text = Path.read_text
    error = OSError(error_number, "synthetic metadata read failure")
    reads = 0

    def read_text(target, *args, **kwargs):
        nonlocal reads
        if target == path:
            reads += 1
            if reads == failed_read:
                raise error
        return real_read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    actions = []
    with pytest.raises(OSError) as raised:
        P.run_physical_action("blocked", lambda: actions.append("input"), lock_path=path)

    assert raised.value is error
    assert path.read_bytes() == b""
    assert actions == []
    with P._ArbitrationGuard(path):
        pass  # The failed attempt must release the guard.


@pytest.mark.parametrize("payload", [b"", b"not-json", b"{}"])
def test_simultaneous_orphan_recovery_has_only_one_owner(payload, tmp_path):
    path = tmp_path / "physical.lock"
    path.write_bytes(payload)
    with ExitStack() as stack:
        contenders = [stack.enter_context(_contender(path)) for _ in range(2)]
        for contender in contenders:
            contender.send("go")
        outcomes = [contender.receive() for contender in contenders]
        assert sorted(outcome["status"] for outcome in outcomes) == ["acquired", "busy"]
        token = next(outcome["token"] for outcome in outcomes if outcome["status"] == "acquired")
        assert json.loads(path.read_bytes())["owner_token"] == token
        with pytest.raises(P.PhysicalInputBusy), P.PhysicalInputLease(path=path):
            pytest.fail("the successful child still owns the lease")

    assert not path.exists()


def test_guard_initialization_race_rechecks_a_byte_filled_by_another_owner(monkeypatch, tmp_path):
    path = tmp_path / "physical.lock"
    holder = P._ArbitrationGuard(path)
    real_ftruncate = P.os.ftruncate

    def filled_then_locked(fd, size):
        # A competitor can initialize and lock the byte after this caller's
        # fstat returned size zero. Windows then denies this caller's truncate.
        real_ftruncate(fd, size)
        holder.__enter__()
        raise PermissionError(errno.EACCES, "synthetic concurrent guard initialization")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(P.os, "ftruncate", filled_then_locked)
            with pytest.raises(P.PhysicalInputBusy), P._ArbitrationGuard(path):
                pytest.fail("the competing holder owns the initialized guard")
        assert holder._fd is not None
    finally:
        holder.__exit__(None, None, None)

    with P.PhysicalInputLease(path=path):
        pass


def test_guard_initialization_permission_failure_is_preserved(monkeypatch, tmp_path):
    path = tmp_path / "physical.lock"
    error = PermissionError(errno.EACCES, "synthetic guard storage failure")

    def denied(_fd, _size):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(P.os, "ftruncate", denied)
        with pytest.raises(PermissionError) as raised, P._ArbitrationGuard(path):
            pytest.fail("uninitialized storage must not authorize input")

    assert raised.value is error
    assert Path(f"{path}.guard").read_bytes() == b""
    with P._ArbitrationGuard(path):
        pass


@pytest.mark.parametrize("record_kind", ["malformed", "stale"])
@pytest.mark.parametrize("replacement_kind", ["inode", "in_place"])
@pytest.mark.parametrize("replace_after_read", [1, 2])
def test_recovery_preserves_replaced_metadata(
    monkeypatch, tmp_path, record_kind, replacement_kind, replace_after_read,
):
    path = tmp_path / "physical.lock"
    record = {"owner_token": "synthetic-stale-token", "expires_at": 0}
    payload = b"broken-json" if record_kind == "malformed" else json.dumps(record).encode()
    replacement_payload = payload
    if replacement_kind == "in_place":
        record["expires_at"] = time.time() + 60
        replacement_payload = (
            b"changed-json" if record_kind == "malformed" else json.dumps(record).encode()
        )
    path.write_bytes(payload)
    replacement = tmp_path / "replacement.lock"
    replacement.write_bytes(replacement_payload)
    real_read_record = P._read_record
    reads = 0

    def replace_after_validation(target):
        nonlocal reads
        current = real_read_record(target)
        reads += 1
        if reads == replace_after_read:
            if replacement_kind == "inode":
                replacement.replace(path)
            else:
                before = path.stat()
                path.write_bytes(replacement_payload)
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000))
        return current

    monkeypatch.setattr(P, "_read_record", replace_after_validation)
    with pytest.raises(P.PhysicalInputBusy), P.PhysicalInputLease(path=path):
        pytest.fail("a replaced record must be left to its owner")

    assert reads >= replace_after_read
    assert path.read_bytes() == replacement_payload
