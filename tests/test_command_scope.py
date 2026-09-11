from __future__ import annotations

import errno
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

import browsertap_mcp.command_scope as scope_module
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


def test_non_loopback_namespace_is_not_rewritten_into_the_loopback_lock(
    lock_environment,
):
    with _held(lock_environment):
        with command_scope():
            guard_targets("browser.example:18765", [TARGET])


def test_posix_scope_uses_a_nonblocking_flock(monkeypatch, tmp_path):
    calls = []

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_NB = 2

        @staticmethod
        def flock(fd, operation):
            calls.append((fd, operation))

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(scope_module, "state_dir", lambda *, create=False: state)
    monkeypatch.setattr(scope_module.sys, "platform", "linux")
    monkeypatch.setitem(scope_module.sys.modules, "fcntl", FakeFcntl)

    with command_scope():
        guard_targets("posix.example:18765", [TARGET])

    assert len(calls) == 1
    assert calls[0][1] == FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB


def test_lock_storage_error_is_reraised_after_closing_the_descriptor(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(scope_module, "state_dir", lambda *, create=False: state)
    real_open = scope_module.os.open
    real_fstat = scope_module.os.fstat
    opened = []

    def tracked_open(path, flags, mode):
        fd = real_open(path, flags, mode)
        opened.append(fd)
        return fd

    def fail_fstat(_fd):
        raise OSError(errno.EIO, "lock storage failed")

    monkeypatch.setattr(scope_module.os, "open", tracked_open)
    monkeypatch.setattr(scope_module.os, "fstat", fail_fstat)

    with pytest.raises(OSError, match="lock storage failed"), command_scope():
        guard_targets("storage.example:18765", [TARGET])

    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])


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


@pytest.mark.parametrize("child_tab_id", [42, "00042", 42.0])
def test_batch_child_target_is_locked_before_any_remote_dispatch(
    lock_environment, monkeypatch, child_tab_id,
):
    driver, calls = _remote_bridge(monkeypatch)
    with _held(lock_environment):
        with command_scope(), pytest.raises(TargetBusyError) as raised:
            driver.ext_cmd({
                "cmd": "batch", "tabId": 43,
                "commands": [
                    {"cmd": "cdp", "method": "Runtime.evaluate"},
                    {"cmd": "cdp", "tabId": child_tab_id, "method": "Runtime.evaluate"},
                ],
            }, client_id="chrome:scope-a")
        _assert_busy(raised.value, target=TARGET, dispatched=False)
        assert _probe(lock_environment, [OTHER_TARGET]) == {"status": "acquired"}
    assert calls == []


@pytest.mark.parametrize("tab_id", ["00042", 42.0])
def test_extension_tab_alias_uses_the_existing_remote_target_lock(
    lock_environment, monkeypatch, tab_id,
):
    driver, calls = _remote_bridge(monkeypatch)
    with _held(lock_environment):
        with command_scope(), pytest.raises(TargetBusyError) as raised:
            driver.ext_cmd({"cmd": "cdp", "tabId": tab_id}, client_id="chrome:scope-a")
        _assert_busy(raised.value, target=TARGET, dispatched=False)
    assert calls == []


@pytest.mark.parametrize("use_default", [False, True])
def test_multi_tab_batch_holds_all_targets_and_preserves_the_callers_payload(
    lock_environment, monkeypatch, use_default,
):
    during_dispatch = []
    driver, calls = _remote_bridge(
        monkeypatch,
        on_dispatch=lambda _command: during_dispatch.append([
            _probe(lock_environment, [target])["status"] for target in (TARGET, OTHER_TARGET)
        ]),
    )
    payload = {"cmd": "batch", "commands": [
        {"cmd": "cdp", "tabId": "00042", "method": "Runtime.evaluate"},
        {"cmd": "cdp", "method": "Runtime.evaluate"},
    ]}
    if use_default:
        payload["tabId"] = "00043"
    else:
        payload["commands"][1]["tabId"] = "00043"
    original = json.loads(json.dumps(payload))

    with command_scope():
        driver.ext_cmd(payload, client_id="chrome:scope-a")
        assert during_dispatch == [["busy", "busy"]]
        assert _probe(lock_environment, [TARGET])["status"] == "busy"
        assert _probe(lock_environment, [OTHER_TARGET])["status"] == "busy"

    assert len(calls) == 1
    assert [child["tabId"] for child in calls[0]["payload"]["commands"]] == [42, 43]
    assert payload == original
    assert _probe(lock_environment, [TARGET, OTHER_TARGET]) == {"status": "acquired"}


@pytest.mark.parametrize("tab_id", [True, False, 1.5, -1, 2147483648, "bad", float("inf"), float("nan")])
@pytest.mark.parametrize("in_child", [False, True])
def test_invalid_batch_targets_are_rejected_before_remote_dispatch(monkeypatch, tab_id, in_child):
    driver, calls = _remote_bridge(monkeypatch)
    payload = {
        "cmd": "batch", "tabId": 43,
        "commands": [{"cmd": "cdp", "tabId": 42, "method": "Runtime.evaluate"}],
    }
    target = payload["commands"][0] if in_child else payload
    target["tabId"] = tab_id

    with pytest.raises(ValueError, match="tabId"), command_scope():
        driver.ext_cmd(payload, client_id="chrome:scope-a")
    assert calls == []


@pytest.mark.parametrize("outer,child", [
    ({}, {}), ({}, {"tabId": None}), ({}, {"tabId": [42]}),
    ({"tabId": 43}, {"tabId": 0}), ({"tabId": 0}, {}),
    ({"tabId": [42, 43]}, {}),
])
def test_batch_cdp_requires_a_resolved_scalar_target(monkeypatch, outer, child):
    driver, calls = _remote_bridge(monkeypatch)
    payload = {"cmd": "batch", **outer, "commands": [
        {"cmd": "cookies", "url": "https://example.test"},
        {"cmd": "cdp", "method": "Runtime.evaluate", **child},
    ]}

    with pytest.raises(ValueError, match="tabId"), command_scope():
        driver.ext_cmd(payload, client_id="chrome:scope-a")
    assert calls == []


@pytest.mark.parametrize("commands", [None, "cdp", [None]])
def test_invalid_batch_structure_is_rejected_before_remote_dispatch(monkeypatch, commands):
    driver, calls = _remote_bridge(monkeypatch)
    with pytest.raises(ValueError, match="batch commands"):
        driver.ext_cmd({"cmd": "batch", "commands": commands}, client_id="chrome:scope-a")
    assert calls == []


def test_batch_null_child_target_inherits_and_guards_its_canonical_target_once(monkeypatch):
    driver, calls = _remote_bridge(monkeypatch)
    target_sets = []
    monkeypatch.setattr(
        "browsertap_mcp.browser_bridge.guard_targets",
        lambda _namespace, targets: target_sets.append(targets),
    )
    payload = {"cmd": "batch", "tabId": "00042", "commands": [
        {"cmd": "cdp", "tabId": None, "method": "Runtime.evaluate"},
        {"cmd": "cdp", "tabId": 42.0, "method": "Runtime.evaluate"},
    ]}
    driver.ext_cmd(payload, client_id="chrome:scope-a")

    assert target_sets == [[TARGET]]
    assert [child["tabId"] for child in calls[0]["payload"]["commands"]] == [42, 42]
    assert payload["commands"][0]["tabId"] is None
    assert payload["tabId"] == "00042"


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
