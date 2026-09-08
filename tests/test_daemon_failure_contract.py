"""Process identity and teardown failures must not orphan or kill other owners."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from browsertap_mcp import bridge as B


class NativeCall:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


def test_missing_windows_api_does_not_prove_process_absence(monkeypatch):
    monkeypatch.delattr(B.ctypes, "windll", raising=False)
    with pytest.raises(B.ProcessIdentityUnavailable, match="APIs are unavailable"):
        B._windows_process_identity(999)


@pytest.mark.parametrize("failure", [None, "missing", "open", "times", "image"])
def test_windows_process_identity_distinguishes_absence_and_unqueryable(monkeypatch, failure):
    from ctypes import wintypes

    handle = 123456
    def times(actual, created, *unused):
        assert actual == handle
        created._obj.dwHighDateTime = 2
        created._obj.dwLowDateTime = 7
        return failure != "times"
    def image(actual, flags, buffer, size):
        assert actual == handle
        buffer.value = "C:/test/python.exe"
        return failure != "image"
    native = SimpleNamespace(
        OpenProcess=NativeCall(lambda *a: 0 if failure in {"open", "missing"} else handle),
        GetLastError=NativeCall(lambda: 87 if failure == "missing" else 5),
        GetProcessTimes=NativeCall(times), QueryFullProcessImageNameW=NativeCall(image),
        CloseHandle=NativeCall(lambda value: True),
    )
    monkeypatch.setattr(B.ctypes, "windll", SimpleNamespace(kernel32=native), raising=False)
    if failure == "missing":
        assert B._windows_process_identity(999) is None
    elif failure:
        with pytest.raises(B.ProcessIdentityUnavailable):
            B._windows_process_identity(999)
    else:
        assert B._windows_process_identity(999) == {
            "pid": 999, "creation_ticks": (2 << 32) | 7,
            "executable": os.path.normcase(os.path.abspath("C:/test/python.exe")),
        }
    assert native.OpenProcess.restype is wintypes.HANDLE
    assert native.CloseHandle.calls == ([] if failure in {"open", "missing"} else [(handle,)])


@pytest.mark.parametrize("opened,terminated,wait,expected", [
    (False, True, 0, False), (True, False, 0, False),
    (True, True, 258, False), (True, True, 0, True),
])
def test_windows_stop_requires_positive_completion(monkeypatch, opened, terminated, wait, expected):
    native = SimpleNamespace(
        OpenProcess=NativeCall(lambda *a: 44 if opened else 0),
        TerminateProcess=NativeCall(lambda *a: terminated),
        WaitForSingleObject=NativeCall(lambda *a: wait),
        CloseHandle=NativeCall(lambda *a: True),
    )
    monkeypatch.setattr(B.sys, "platform", "win32")
    monkeypatch.setattr(B.ctypes, "windll", SimpleNamespace(kernel32=native), raising=False)
    monkeypatch.setattr(B, "_process_is_gone", lambda pid: False)
    assert B._terminate_process(999, 1) is expected
    assert native.CloseHandle.calls == ([(44,)] if opened else [])
    assert len(native.WaitForSingleObject.calls) == int(opened and terminated)


@pytest.mark.parametrize("failure,expected", [(ProcessLookupError(), True), (PermissionError(), False)])
def test_posix_stop_does_not_treat_permission_denial_as_success(monkeypatch, failure, expected):
    monkeypatch.setattr(B.sys, "platform", "linux")
    def failed(*a):
        raise failure
    monkeypatch.setattr(B.os, "kill", failed)
    assert B._terminate_process(999, 1) is expected


@pytest.mark.parametrize("gone", [True, False])
def test_posix_stop_waits_for_exit_and_obeys_its_deadline(monkeypatch, gone):
    monkeypatch.setattr(B.sys, "platform", "linux")
    sent, sleeps = [], []
    monkeypatch.setattr(B.os, "kill", lambda *a: sent.append(a))
    monkeypatch.setattr(B, "_process_is_gone", lambda pid: gone)
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(B.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(B.time, "sleep", sleeps.append)
    assert B._terminate_process(999, 1) is gone
    assert sent == [(999, B.signal.SIGTERM)]
    assert sleeps == ([] if gone else [0.05])


@pytest.mark.parametrize("platform,backend", [
    ("win32", "_windows_process_identity"), ("darwin", "_darwin_process_identity"),
    ("linux", "_posix_process_identity"),
])
def test_process_identity_uses_the_platform_backend(monkeypatch, platform, backend):
    monkeypatch.setattr(B.sys, "platform", platform)
    expected = {"pid": 999, "creation_ticks": 123}
    monkeypatch.setattr(B, backend, lambda pid: expected)
    assert B.process_identity(999) is expected


@pytest.mark.parametrize("failure", ["read", "parse", "exited"])
def test_procfs_identity_preserves_the_distinction_between_unknown_and_gone(monkeypatch, failure):
    class Proc:
        def __truediv__(self, name):
            return self
        def read_text(self, **kwargs):
            if failure == "read":
                raise PermissionError("denied")
            return "broken" if failure == "parse" else "999 (python) S " + "0 " * 18 + "777"
        def resolve(self, **kwargs):
            raise FileNotFoundError("exited during inspection")
    monkeypatch.setattr(B, "Path", lambda *args: Proc())
    if failure == "exited":
        assert B._posix_process_identity(999) is None
    else:
        with pytest.raises(B.ProcessIdentityUnavailable):
            B._posix_process_identity(999)


def test_darwin_identity_rejects_unparseable_start_time(monkeypatch):
    monkeypatch.setattr(B.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="bad bad bad bad bad /usr/bin/python",
    ))
    with pytest.raises(B.ProcessIdentityUnavailable, match="unparsable"):
        B._darwin_process_identity(999)


@pytest.mark.parametrize("payload", [[], {}, {"pid": "bad", "creation_ticks": 1,
                                           "executable": "python", "instance_id": "i"}])
def test_invalid_daemon_records_cannot_authorize_a_stop(monkeypatch, tmp_path, payload):
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path))
    B.bridge_pid_path().write_text(json.dumps(payload), encoding="utf-8")
    assert B.read_bridge_record() is None


@pytest.mark.parametrize("exists", [False, True])
def test_daemon_stop_preserves_a_failed_stop_record(monkeypatch, tmp_path, exists):
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path))
    record = {"pid": 999, "creation_ticks": 7, "executable": "python", "instance_id": "i"}
    B.bridge_pid_path().write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(B, "process_identity", lambda pid: record if exists else None)
    monkeypatch.setattr(B, "_terminate_process", lambda *args: False)
    result = B.stop_bridge_daemon()
    assert result["stopped"] is False
    assert result["status"] == ("stop_failed" if exists else "not_running")
    assert B.bridge_pid_path().exists() is exists


def test_record_write_without_an_identity_leaves_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(B, "process_identity", lambda pid: None)
    with pytest.raises(RuntimeError, match="unmanaged startup"):
        B._write_bridge_record(instance_id="i", host="127.0.0.1", port=18765)
    assert not B.bridge_pid_path().exists()


def test_rotating_a_closed_or_missing_stream_is_a_noop(monkeypatch):
    monkeypatch.setattr(B.sys, "stderr", None)
    assert B.rotate_own_log() is False
    def failed():
        raise OSError("closed stream")
    monkeypatch.setattr(B.sys, "stderr", SimpleNamespace(fileno=lambda: 77, flush=failed))
    assert B.rotate_own_log() is False


def test_rotation_truncate_failure_preserves_the_current_log(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path))
    path = B.bridge_log_path()
    path.write_bytes(b"old log")
    def failed(*a):
        raise OSError("truncate denied")
    with path.open("ab") as stream:
        monkeypatch.setattr(B.sys, "stderr", stream)
        monkeypatch.setattr(B.os, "ftruncate", failed)
        assert B.rotate_own_log(max_bytes=1) is False
    assert path.read_bytes() == path.with_suffix(".log.old").read_bytes() == b"old log"


def test_darwin_ps_timeout_is_not_a_missing_process(monkeypatch):
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("ps", 10)
    monkeypatch.setattr(B.subprocess, "run", timeout)
    with pytest.raises(B.ProcessIdentityUnavailable):
        B._darwin_process_identity(999)
