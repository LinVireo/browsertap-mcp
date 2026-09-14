"""Startup recovery uses synthetic daemons and isolated lock files only."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def startup(monkeypatch, tmp_path):
    """Run the real spawn control flow without opening sockets or processes."""
    clock = _Clock()
    state = SimpleNamespace(
        clock=clock, port_up=False, records=[], live=set(), record_delay=0.0,
        lock=tmp_path / "spawn.lock",
    )
    monkeypatch.setattr(S, "time", SimpleNamespace(
        monotonic=clock.monotonic, sleep=clock.sleep, time=time.time,
    ))
    monkeypatch.setattr(S, "_spawn_lock_path", lambda: state.lock)
    monkeypatch.setattr(S, "_bridge_log_path", lambda: tmp_path / "bridge.log")
    monkeypatch.setattr(S, "_get_driver_host", lambda: "127.0.0.1")
    monkeypatch.setattr(S, "_get_driver_port", lambda: 43101)
    monkeypatch.setattr(S, "_port_open", lambda *_args: state.port_up)
    monkeypatch.setattr(S, "_pid_alive", lambda pid: pid == os.getpid() or pid in state.live)

    def popen(command, **_kwargs):
        instance_id = next(part.split("=", 1)[1] for part in command
                           if part.startswith("--instance-id="))
        pid = 700001 + len(state.records)
        state.records.append({
            "pid": pid, "creation_ticks": pid * 10,
            "executable": str(tmp_path / "daemon-python"),
            "instance_id": instance_id, "host": "127.0.0.1",
            "ws_port": 43101, "http_port": 43102,
        })
        state.live.add(pid)
        state.port_up = True
        state.record_ready_at = clock.now + state.record_delay
        # The launcher PID is deliberately different (Windows venv stub).
        return SimpleNamespace(pid=pid + 1000)

    def read_record():
        if state.records and clock.now >= state.record_ready_at:
            return dict(state.records[-1])
        return None

    def process_identity(pid):
        if pid not in state.live:
            return None
        record = next(record for record in state.records if record["pid"] == pid)
        return {key: record[key] for key in ("pid", "creation_ticks", "executable")}

    monkeypatch.setattr(S.subprocess, "Popen", popen)
    monkeypatch.setattr(S.bridge_module, "read_bridge_record", read_record)
    monkeypatch.setattr(S.bridge_module, "process_identity", process_identity)
    return state


def test_same_mcp_recovers_a_daemon_that_dies_before_the_spawn_lock_expires(startup):
    assert S.spawn_bridge_daemon()
    first = startup.records[-1]
    startup.live.remove(first["pid"])
    startup.port_up = False

    assert S.spawn_bridge_daemon(), "a live MCP PID must not prevent daemon recovery"
    assert len(startup.records) == 2
    assert startup.records[-1]["instance_id"] != first["instance_id"]
    assert int(startup.lock.read_text()) == startup.records[-1]["pid"]
    assert startup.clock.now < S._SPAWN_LOCK_STALE


def test_http_readiness_waits_for_the_spawned_daemons_identity_record(startup):
    startup.record_delay = 0.75

    assert S.spawn_bridge_daemon()

    assert startup.clock.now >= startup.record_ready_at
    assert int(startup.lock.read_text()) == startup.records[-1]["pid"]
    assert startup.lock.exists(), "successful startup must retain its duplicate-spawn protection"


@pytest.mark.parametrize("field,value", [
    ("host", "different.invalid"), ("ws_port", 43111), ("http_port", 43112),
    ("instance_id", "previous-daemon"), ("pid", 700099),
    ("creation_ticks", -1), ("executable", "different-interpreter"),
])
def test_handoff_rejects_a_record_for_another_endpoint_or_process(
    startup, monkeypatch, field, value,
):
    original_read = S.bridge_module.read_bridge_record

    def wrong_record():
        record = original_read()
        if record is not None:
            record[field] = value
        return record

    monkeypatch.setattr(S.bridge_module, "read_bridge_record", wrong_record)

    assert S.spawn_bridge_daemon()
    assert startup.clock.now == 8
    assert startup.lock.read_text() == str(os.getpid())
    assert S._acquire_spawn_lock() is None


@pytest.mark.parametrize("unavailable", ["record", "identity"])
def test_unverifiable_handoff_keeps_the_original_claim_and_bounded_wait(
    startup, monkeypatch, unavailable,
):
    if unavailable == "record":
        monkeypatch.setattr(S.bridge_module, "read_bridge_record", lambda: None)
    else:
        def unknown_identity(_pid):
            raise S.bridge_module.ProcessIdentityUnavailable("synthetic access denied")

        monkeypatch.setattr(S.bridge_module, "process_identity", unknown_identity)

    assert S.spawn_bridge_daemon()
    assert startup.clock.now == 8
    assert startup.lock.read_text() == str(os.getpid())
    assert S._acquire_spawn_lock() is None


def test_failed_atomic_handoff_preserves_the_original_owner(startup, monkeypatch):
    original_replace = S.os.replace
    failed_writes = []

    def fail_lock_replace(source, destination):
        if Path(destination) == startup.lock:
            failed_writes.append(Path(source))
            raise PermissionError("synthetic locked destination")
        return original_replace(source, destination)

    monkeypatch.setattr(S.os, "replace", fail_lock_replace)

    assert S.spawn_bridge_daemon()
    assert failed_writes
    assert startup.clock.now == 8
    assert startup.lock.read_text() == str(os.getpid())
    assert all(not path.exists() for path in failed_writes)


def test_failed_launch_cannot_release_a_successors_claim(startup, monkeypatch):
    replacement = []

    def failed_launch(**_kwargs):
        claim = S._acquire_spawn_lock(reset=True)
        assert claim is not None
        replacement.append(claim)
        return False

    monkeypatch.setattr(S, "_spawn_bridge_daemon_locked", failed_launch)

    assert not S.spawn_bridge_daemon()
    assert startup.lock.exists()
    assert S._spawn_lock_fingerprint(startup.lock) == replacement[0].fingerprint


def test_handoff_cannot_overwrite_a_successors_claim(startup, monkeypatch):
    original_read = S.bridge_module.read_bridge_record
    replacement = []

    def superseded_record():
        if not replacement:
            claim = S._acquire_spawn_lock(reset=True)
            assert claim is not None
            replacement.append(claim)
        return original_read()

    monkeypatch.setattr(S.bridge_module, "read_bridge_record", superseded_record)

    assert S.spawn_bridge_daemon()
    assert startup.lock.read_text() == str(os.getpid())
    assert S._spawn_lock_fingerprint(startup.lock) == replacement[0].fingerprint


@pytest.mark.parametrize("prior_start", [False, True])
def test_concurrent_startup_and_recovery_each_spawn_one_daemon(
    startup, monkeypatch, prior_start,
):
    # Real elapsed time is needed for the waiting threads; every daemon and
    # socket remains synthetic. Exercise the real locked helper and Popen site.
    monkeypatch.setattr(S, "time", time)
    original_popen = S.subprocess.Popen

    def delayed_popen(command, **kwargs):
        time.sleep(0.1)
        return original_popen(command, **kwargs)

    monkeypatch.setattr(S.subprocess, "Popen", delayed_popen)
    if prior_start:
        assert S.spawn_bridge_daemon()
        startup.live.clear()
        startup.port_up = False
    starting = threading.Barrier(12)
    results, errors = [], []

    def start():
        try:
            starting.wait(timeout=5)
            results.append(S.spawn_bridge_daemon())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(12)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert len(results) == 12 and all(results)
    assert S.spawn_bridge_daemon(), "a caller arriving after success reuses the daemon"
    assert len(startup.records) == (2 if prior_start else 1)
    assert int(startup.lock.read_text()) == startup.records[-1]["pid"]


@pytest.mark.parametrize("owner", ["", "not-a-pid", "0", "-1"])
def test_unknown_recent_owners_keep_their_startup_protection(startup, owner):
    startup.lock.write_text(owner, encoding="utf-8")

    assert S._acquire_spawn_lock() is None
    assert startup.lock.read_text() == owner


def test_concurrent_dead_owner_reclamation_keeps_the_winning_claim(monkeypatch, tmp_path):
    lock = tmp_path / "spawn.lock"
    dead_pid = 700009
    lock.write_text(str(dead_pid), encoding="utf-8")
    monkeypatch.setattr(S, "_spawn_lock_path", lambda: lock)
    first_observed = threading.Event()
    resume_first = threading.Event()
    first_finished = threading.Event()
    second_progressed = threading.Event()
    results = {}
    errors = []

    def pid_alive(pid):
        if pid != dead_pid:
            return True
        if threading.current_thread().name == "first-reclaimer":
            first_observed.set()
            assert resume_first.wait(3)
        else:
            # The old implementation can read the dead owner while A is still
            # checking it, then delete A's replacement after A has returned.
            second_progressed.set()
            assert first_finished.wait(3)
        return False

    def acquire(name):
        try:
            results[name] = S._acquire_spawn_lock()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if name == "first":
                first_finished.set()
            else:
                second_progressed.set()

    monkeypatch.setattr(S, "_pid_alive", pid_alive)
    first = threading.Thread(target=acquire, args=("first",), name="first-reclaimer")
    second = threading.Thread(target=acquire, args=("second",), name="second-reclaimer")
    try:
        first.start()
        assert first_observed.wait(3)
        second.start()
        assert second_progressed.wait(3)
    finally:
        resume_first.set()
        first.join(4)
        first_finished.set()
        if second.ident is not None:
            second.join(4)

    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert results["first"] is not None
    assert results["second"] is None, "a stale observer must not delete the newly acquired claim"
    assert lock.read_text() == str(os.getpid())
