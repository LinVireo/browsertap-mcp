from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from browsertap_mcp.browser_bridge import BrowserBridge
from browsertap_mcp.command_scope import (
    TargetBusyError,
    command_scope,
    guard_targets,
    mark_dispatched,
)
from browsertap_mcp.paths import STATE_DIR_ENV

NAMESPACE = "127.0.0.1:18765"
TARGET = "chrome:scope-a:42"
OTHER_TARGET = "chrome:scope-a:43"
_WORKER_SCRIPT = """
import json
import os
import sys

from browsertap_mcp.command_scope import TargetBusyError, command_scope, guard_targets

request = json.loads(sys.argv[1])
hold = request['mode'] == 'hold'

def emit(value):
    print(json.dumps(value), flush=True)

try:
    with command_scope():
        guard_targets(request['namespace'], request['targets'])
        emit({'status': 'acquired'})
        if hold:
            action = sys.stdin.readline().strip()
            if action == 'exit':
                os._exit(29)
            if action == 'raise':
                raise RuntimeError('test scope failure')
except TargetBusyError as exc:
    emit({
        'status': 'busy', 'error_code': exc.error_code,
        'retry_safe': exc.retry_safe, 'delivery_state': exc.delivery_state,
        'diagnostics': exc.diagnostics,
    })
except RuntimeError as exc:
    if not hold or str(exc) != 'test scope failure':
        raise
    emit({'status': 'released', 'via': 'exception'})
else:
    if hold:
        emit({'status': 'released', 'via': 'normal'})

if hold:
    sys.stdin.readline()
"""


@pytest.fixture
def lock_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source, environment.get("PYTHONPATH")) if value
    )
    return environment


def _worker_arguments(mode, targets, namespace):
    request = {"mode": mode, "targets": targets, "namespace": namespace}
    return [sys.executable, "-u", "-c", _WORKER_SCRIPT, json.dumps(request)]


def _probe(environment, targets, *, namespace=NAMESPACE):
    completed = subprocess.run(
        _worker_arguments("probe", targets, namespace),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


class _LockHolder:
    def __init__(self, environment, targets, namespace):
        self.process = subprocess.Popen(
            _worker_arguments("hold", targets, namespace),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
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
            line = self.events.get(timeout=10)
        except queue.Empty:
            pytest.fail("lock-holder process did not report within 10 seconds")
        if line is None:
            self.process.wait(timeout=5)
            pytest.fail(f"lock-holder process exited early: {self.process.stderr.read()}")
        return json.loads(line)

    def send(self, action):
        self.process.stdin.write(action + "\n")
        self.process.stdin.flush()

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.send("release\nfinish")
                    self.process.wait(timeout=5)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            self.reader.join(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()


@contextmanager
def _held(environment, targets=None, *, namespace=NAMESPACE):
    holder = _LockHolder(environment, targets or [TARGET], namespace)
    try:
        assert holder.receive() == {"status": "acquired"}
        yield holder
    finally:
        holder.close()


def _assert_busy(error, *, target, dispatched):
    assert error.error_code == "target_busy"
    assert error.retry_safe is not dispatched
    assert error.delivery_state == ("delivered_partial" if dispatched else "undelivered")
    assert error.diagnostics == {"busy_target": target, "may_have_executed": dispatched}


def test_same_target_is_busy_until_the_other_process_exits_its_scope(lock_environment):
    with _held(lock_environment) as holder:
        with command_scope(), pytest.raises(TargetBusyError) as raised:
            guard_targets(NAMESPACE, [TARGET])
        _assert_busy(raised.value, target=TARGET, dispatched=False)

        holder.send("release")
        assert holder.receive() == {"status": "released", "via": "normal"}
        assert holder.process.poll() is None
        with command_scope():
            guard_targets(NAMESPACE, [TARGET])


@pytest.mark.parametrize(
    ("namespace", "target"),
    [
        (NAMESPACE, OTHER_TARGET),
        (NAMESPACE, "chrome:scope-b:42"),
        ("127.0.0.1:28765", TARGET),
    ],
)
def test_distinct_tabs_clients_and_bridge_namespaces_can_run_concurrently(
    lock_environment,
    namespace,
    target,
):
    with _held(lock_environment), command_scope():
        guard_targets(namespace, [target])


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "::1", "[::1]"])
def test_loopback_aliases_compete_for_the_same_target_lock(lock_environment, host):
    with _held(lock_environment, namespace=f"{host}:18765"):
        with command_scope(), pytest.raises(TargetBusyError) as raised:
            guard_targets(NAMESPACE, [TARGET])
        _assert_busy(raised.value, target=TARGET, dispatched=False)


def test_loopback_aliases_share_one_lock_within_a_nested_scope(lock_environment):
    with command_scope():
        guard_targets("localhost:18765", [TARGET])
        with command_scope():
            guard_targets("[::1]:18765", [TARGET])
        assert _probe(lock_environment, [TARGET])["status"] == "busy"
    assert _probe(lock_environment, [TARGET]) == {"status": "acquired"}


def test_an_exception_releases_the_lock_while_the_owner_process_stays_alive(lock_environment):
    with _held(lock_environment) as holder:
        holder.send("raise")
        assert holder.receive() == {"status": "released", "via": "exception"}
        assert holder.process.poll() is None
        with command_scope():
            guard_targets(NAMESPACE, [TARGET])


def test_process_exit_releases_the_os_lock_without_running_scope_cleanup(lock_environment):
    with _held(lock_environment) as holder:
        holder.send("exit")
        assert holder.process.wait(timeout=5) == 29
        with command_scope():
            guard_targets(NAMESPACE, [TARGET])


def test_nested_scopes_keep_the_lock_until_the_outer_scope_finishes(lock_environment):
    with command_scope():
        guard_targets(NAMESPACE, [TARGET])
        with pytest.raises(ValueError, match="inner failure"), command_scope():
            guard_targets(NAMESPACE, [TARGET, TARGET])
            raise ValueError("inner failure")
        assert _probe(lock_environment, [TARGET])["status"] == "busy"
        guard_targets(NAMESPACE, [TARGET])
    assert _probe(lock_environment, [TARGET]) == {"status": "acquired"}


def test_partial_multi_target_acquisition_is_released_when_the_scope_unwinds(lock_environment):
    first_target = "chrome:scope-a:41"
    with _held(lock_environment):
        with pytest.raises(TargetBusyError), command_scope():
            guard_targets(NAMESPACE, [TARGET, first_target])
        assert _probe(lock_environment, [first_target]) == {"status": "acquired"}


def test_direct_bridge_traffic_outside_a_scope_does_not_acquire_or_mark_a_scope(lock_environment):
    with _held(lock_environment):
        guard_targets(NAMESPACE, [TARGET])
        mark_dispatched()
        with command_scope(), pytest.raises(TargetBusyError) as raised:
            guard_targets(NAMESPACE, [TARGET])
        _assert_busy(raised.value, target=TARGET, dispatched=False)


def _remote_bridge(monkeypatch, *, on_dispatch=None):
    driver = BrowserBridge.__new__(BrowserBridge)
    driver.host, driver.port = "127.0.0.1", 18765
    driver.is_remote = True
    driver.default_session_id = TARGET
    calls = []

    def remote(command, timeout=30):
        if command["cmd"] == "resolve_session":
            return {"r": {"session_id": command["sessionId"]}}
        calls.append(command)
        if on_dispatch is not None:
            on_dispatch(command)
        return {"r": {"data": {}, "client_id": command.get("clientId")}}

    monkeypatch.setattr(driver, "_remote_cmd", remote)
    return driver, calls


def _dispatch(driver, route, target):
    if route == "execute_js":
        return driver.execute_js("globalThis.__scope_test = 1", session_id=target)
    client_id, tab_id = target.rsplit(":", 1)
    return driver.ext_cmd(
        {"cmd": "batch", "tabId": int(tab_id), "commands": []},
        client_id=client_id,
    )


@pytest.mark.parametrize("route", ["execute_js", "ext_cmd"])
def test_bridge_refuses_a_busy_target_before_remote_dispatch(lock_environment, monkeypatch, route):
    driver, calls = _remote_bridge(monkeypatch)
    with _held(lock_environment):
        with command_scope(), pytest.raises(TargetBusyError) as raised:
            _dispatch(driver, route, TARGET)
        _assert_busy(raised.value, target=TARGET, dispatched=False)
    assert calls == []


def test_bridge_keeps_one_target_locked_across_javascript_and_cdp_roundtrips(
    lock_environment,
    monkeypatch,
):
    during_dispatch = []
    driver, calls = _remote_bridge(
        monkeypatch,
        on_dispatch=lambda _command: during_dispatch.append(
            _probe(lock_environment, [TARGET])["status"],
        ),
    )
    with command_scope():
        _dispatch(driver, "execute_js", TARGET)
        assert _probe(lock_environment, [TARGET])["status"] == "busy"
        _dispatch(driver, "ext_cmd", TARGET)
        assert _probe(lock_environment, [TARGET])["status"] == "busy"
        assert _probe(lock_environment, [OTHER_TARGET]) == {"status": "acquired"}
    assert [command["cmd"] for command in calls] == ["execute_js", "ext_cmd"]
    assert during_dispatch == ["busy", "busy"]
    assert _probe(lock_environment, [TARGET]) == {"status": "acquired"}


@pytest.mark.parametrize("route", ["execute_js", "ext_cmd"])
def test_busy_after_an_earlier_dispatch_is_not_safe_to_retry(lock_environment, monkeypatch, route):
    driver, calls = _remote_bridge(monkeypatch)
    with _held(lock_environment, [OTHER_TARGET]):
        with command_scope():
            _dispatch(driver, route, TARGET)
            with pytest.raises(TargetBusyError) as raised:
                _dispatch(driver, route, OTHER_TARGET)
            _assert_busy(raised.value, target=OTHER_TARGET, dispatched=True)
        assert _probe(lock_environment, [TARGET]) == {"status": "acquired"}
    assert len(calls) == 1


@pytest.mark.parametrize("route", ["execute_js", "ext_cmd"])
def test_remote_dispatch_failure_releases_the_scopes_target(lock_environment, monkeypatch, route):
    def fail_dispatch(_command):
        raise TimeoutError("test transport failure")

    driver, calls = _remote_bridge(monkeypatch, on_dispatch=fail_dispatch)
    with pytest.raises(TimeoutError, match="test transport failure"), command_scope():
        _dispatch(driver, route, TARGET)
    assert len(calls) == 1
    assert _probe(lock_environment, [TARGET]) == {"status": "acquired"}


def test_multi_tab_extension_command_refuses_all_dispatch_if_one_target_is_busy(
    lock_environment,
    monkeypatch,
):
    driver, calls = _remote_bridge(monkeypatch)
    with _held(lock_environment):
        with pytest.raises(TargetBusyError), command_scope():
            driver.ext_cmd(
                {"cmd": "tabs", "method": "remove", "tabId": [41, 42]}, client_id="chrome:scope-a"
            )
        assert _probe(lock_environment, ["chrome:scope-a:41"]) == {"status": "acquired"}
    assert calls == []


def test_local_failover_locks_the_final_target_before_dispatch(lock_environment, monkeypatch):
    driver, calls = _remote_bridge(monkeypatch)
    driver.is_remote = False
    driver.latest_session_id = TARGET
    sent = []
    live = SimpleNamespace(
        id=TARGET,
        type="ext_ws",
        info={"tab_id": 42, "url": "https://scope.test/"},
        is_active=lambda: True,
        ws_client=SimpleNamespace(send_message=sent.append),
    )
    driver.sessions = {TARGET: live}
    monkeypatch.setattr(driver, "resolve_session_target", lambda _session: None)
    monkeypatch.setattr(driver, "_activity_snapshot", lambda: 0)
    clock = [0.0]

    def advance(_serial, delay):
        clock[0] += delay
        return 0

    monkeypatch.setattr(driver, "_wait_for_activity", advance)
    with _held(lock_environment):
        monkeypatch.setattr("browsertap_mcp.browser_bridge.time.monotonic", lambda: clock[0])
        with command_scope(), pytest.raises(TargetBusyError) as raised:
            driver.execute_js("globalThis.__scope_test = 1", session_id=OTHER_TARGET,
                              allow_failover=True)
        _assert_busy(raised.value, target=TARGET, dispatched=False)
    assert sent == calls == []
