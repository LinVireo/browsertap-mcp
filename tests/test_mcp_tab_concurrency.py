"""Drive registered MCP runners with a real driver and an offline transport."""

from __future__ import annotations

import threading

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ElicitRequestURLParams

from browsertap_mcp import server as S
from browsertap_mcp.browser_bridge import BrowserBridge
from browsertap_mcp.command_scope import command_scope, guard_targets
from browsertap_mcp.paths import STATE_DIR_ENV

DEFAULT = "chrome:concurrency:40"
FIRST = "chrome:concurrency:41"
SECOND = "chrome:concurrency:42"
NAMESPACE = "127.0.0.1:18765"


@pytest.fixture
def remote_driver(monkeypatch, tmp_path):
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    driver = BrowserBridge.__new__(BrowserBridge)
    driver.host, driver.port = "127.0.0.1", 18765
    driver.is_remote = True
    driver.default_session_id = DEFAULT
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    return driver


def _transport(driver, monkeypatch, on_dispatch):
    def remote(command, timeout=30):
        if command["cmd"] == "resolve_session":
            return {"r": {"session_id": command["sessionId"]}}
        assert command["cmd"] == "execute_js"
        on_dispatch(command)
        return {"r": {"data": command["sessionId"]}}

    monkeypatch.setattr(driver, "_remote_cmd", remote)


def _register(monkeypatch, fn, *, serialize=False):
    mcp = FastMCP("tab-concurrency")
    monkeypatch.setattr(S, "_mcp_tool", mcp.tool)
    S._threaded_tool(serialize=serialize)(fn)
    return mcp._tool_manager.get_tool(fn.__name__).fn


@pytest.mark.anyio
async def test_registered_sync_calls_on_different_tabs_run_together(remote_driver, monkeypatch):
    driver = remote_driver
    rendezvous = threading.Barrier(2, timeout=3)
    observed = []

    def dispatch(command):
        rendezvous.wait()
        observed.append((command["sessionId"], driver.default_session_id))

    _transport(driver, monkeypatch, dispatch)

    def probe(session_id: str) -> dict:
        previous = driver.default_session_id
        driver.default_session_id = session_id
        try:
            return driver.execute_js("1")
        finally:
            assert driver.default_session_id == session_id
            driver.default_session_id = previous

    registered = _register(monkeypatch, probe)
    results = []

    async def run(target):
        results.append(await registered(target))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run, FIRST)
        tasks.start_soon(run, SECOND)

    assert all(result["ok"] for result in results)
    assert sorted(observed) == [(FIRST, FIRST), (SECOND, SECOND)]
    assert driver.default_session_id == DEFAULT


@pytest.mark.anyio
async def test_registered_async_call_keeps_its_target_across_worker_offloads(
    remote_driver, monkeypatch,
):
    driver = remote_driver
    rendezvous = threading.Barrier(2, timeout=3)
    observed = []

    def dispatch(command):
        rendezvous.wait()
        observed.append((command["sessionId"], driver.default_session_id))

    _transport(driver, monkeypatch, dispatch)

    async def probe(session_id: str) -> dict:
        previous = driver.default_session_id
        await anyio.to_thread.run_sync(setattr, driver, "default_session_id", session_id)
        try:
            assert driver.default_session_id == session_id
            return await anyio.to_thread.run_sync(driver.execute_js, "1")
        finally:
            assert driver.default_session_id == session_id
            driver.default_session_id = previous

    registered = _register(monkeypatch, probe)
    results = []

    async def run(target):
        results.append(await registered(target))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run, FIRST)
        tasks.start_soon(run, SECOND)

    assert all(result["ok"] for result in results)
    assert sorted(observed) == [(FIRST, FIRST), (SECOND, SECOND)]
    assert driver.default_session_id == DEFAULT


@pytest.mark.anyio
async def test_same_tab_refuses_while_other_tab_dispatches(remote_driver, monkeypatch):
    driver = remote_driver
    entered, release = threading.Event(), threading.Event()
    dispatched = []

    def dispatch(command):
        dispatched.append(command["sessionId"])
        if command["sessionId"] == FIRST:
            entered.set()
            assert release.wait(3)

    _transport(driver, monkeypatch, dispatch)

    def probe(session_id: str) -> dict:
        return driver.execute_js("1", session_id=session_id)

    registered = _register(monkeypatch, probe)
    results = []

    async def hold():
        results.append(await registered(FIRST))

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(hold)
            assert await anyio.to_thread.run_sync(entered.wait, 3)
            busy = await registered(FIRST)
            other = await registered(SECOND)
            release.set()
    finally:
        release.set()

    assert isinstance(busy, CallToolResult)
    assert busy.isError is True
    assert busy.structuredContent["error_code"] == "target_busy"
    assert busy.structuredContent["diagnostics"]["delivery_state"] == "undelivered"
    assert busy.structuredContent["retryable"] is True
    assert other["ok"] is True
    assert results[0]["ok"] is True
    assert dispatched == [FIRST, SECOND]


@pytest.mark.anyio
async def test_cancelled_async_call_releases_its_tab_scope(remote_driver, monkeypatch):
    driver = remote_driver
    entered, finished = anyio.Event(), anyio.Event()
    scopes = []
    _transport(driver, monkeypatch, lambda command: None)

    async def holder(session_id: str) -> dict:
        await anyio.to_thread.run_sync(driver.execute_js, "1", 1, session_id)
        entered.set()
        await anyio.sleep_forever()

    registered_holder = _register(monkeypatch, holder)

    def probe(session_id: str) -> dict:
        return driver.execute_js("1", session_id=session_id)

    registered_probe = _register(monkeypatch, probe)

    async def run():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await registered_holder(FIRST)
        finished.set()

    with anyio.fail_after(3):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run)
            await entered.wait()
            busy = await registered_probe(FIRST)
            assert isinstance(busy, CallToolResult)
            assert busy.structuredContent["error_code"] == "target_busy"
            assert (await registered_probe(SECOND))["ok"] is True
            scopes[0].cancel()
            await finished.wait()
            assert (await registered_probe(FIRST))["ok"] is True

    assert driver.default_session_id == DEFAULT


@pytest.mark.anyio
@pytest.mark.parametrize("selected", [DEFAULT, SECOND])
async def test_explicit_switch_survives_an_older_implicit_failover(
    remote_driver, monkeypatch, selected,
):
    driver = remote_driver
    entered, release = anyio.Event(), anyio.Event()
    _transport(driver, monkeypatch, lambda command: None)
    monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: [])

    async def probe() -> dict:
        assert driver.default_session_id == DEFAULT
        driver.default_session_id = FIRST
        entered.set()
        await release.wait()
        assert driver.default_session_id == FIRST
        return {"status": "ok"}

    registered = _register(monkeypatch, probe)
    switch = S.mcp._tool_manager.get_tool("switch_tab").fn
    results = []

    async def run():
        results.append(await registered())

    with anyio.fail_after(3):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run)
            await entered.wait()
            assert driver.default_session_id == DEFAULT
            assert (await switch(session_id=selected))["ok"] is True
            assert driver.default_session_id == selected
            release.set()

    assert results[0]["ok"] is True
    assert driver.default_session_id == selected


@pytest.mark.anyio
@pytest.mark.parametrize("explicit", [False, True])
async def test_only_implicit_default_selection_is_persisted(remote_driver, monkeypatch, explicit):
    driver = remote_driver

    def probe(session_id: str | None = None) -> dict:
        driver.default_session_id = FIRST
        with command_scope(persist_defaults=True):
            assert driver.default_session_id == FIRST
        return {"status": "ok"}

    registered = _register(monkeypatch, probe)
    result = await registered(**({"session_id": FIRST} if explicit else {}))

    assert result["ok"] is True
    assert driver.default_session_id == (DEFAULT if explicit else FIRST)


def test_default_publish_failure_still_releases_tab_locks(remote_driver, monkeypatch):
    driver = remote_driver

    def fail_publish(*args, **kwargs):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(driver, "publish_default_session_id", fail_publish)
    with pytest.raises(RuntimeError, match="publish failed"):
        with command_scope(persist_defaults=True):
            driver.default_session_id = FIRST
            guard_targets(NAMESPACE, [FIRST])

    with command_scope():
        guard_targets(NAMESPACE, [FIRST])
    assert driver.default_session_id == DEFAULT


def test_unchanged_scoped_default_is_not_published(remote_driver, monkeypatch):
    driver = remote_driver
    monkeypatch.setattr(
        driver, "publish_default_session_id",
        lambda *args, **kwargs: pytest.fail("an unchanged default must not be republished"),
    )

    with command_scope(persist_defaults=True):
        assert driver.default_session_id == DEFAULT
        driver.default_session_id = DEFAULT

    assert driver.default_session_id == DEFAULT


def test_session_cache_invalidation_cannot_clear_a_snapshot_mid_read(monkeypatch):
    sessions = [{"id": FIRST}]
    monkeypatch.setattr(S, "_sessions_cache", (0.0, sessions))

    def invalidate_during_clock_read():
        S.invalidate_sessions_cache()
        return 1.0

    monkeypatch.setattr(S.time, "monotonic", invalidate_during_clock_read)
    monkeypatch.setattr(S, "require_driver", lambda: pytest.fail("the cache was fresh"))

    assert S.active_sessions() is sessions


@pytest.mark.anyio
@pytest.mark.parametrize("serialize", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_url_elicitation_keeps_its_protocol_error_and_releases_the_lock(
    monkeypatch, serialize, asynchronous,
):
    lock = threading.Lock()
    monkeypatch.setattr(S, "_TOOL_LOCK", lock)
    request = ElicitRequestURLParams(
        mode="url", message="Complete authorization", url="https://example.test/authorize",
        elicitationId="test-authorization",
    )
    failure = S.UrlElicitationRequiredError([request])

    if asynchronous:
        async def probe() -> dict:
            raise failure
    else:
        def probe() -> dict:
            raise failure

    registered = _register(monkeypatch, probe, serialize=serialize)

    with pytest.raises(S.UrlElicitationRequiredError) as raised:
        await registered()

    assert raised.value is failure
    assert raised.value.elicitations == [request]
    assert not lock.locked()


@pytest.mark.anyio
async def test_serialized_async_success_releases_the_lock_and_restores_the_target(
    remote_driver, monkeypatch,
):
    lock = threading.Lock()
    monkeypatch.setattr(S, "_TOOL_LOCK", lock)

    async def probe(session_id: str) -> dict:
        assert lock.locked()
        remote_driver.default_session_id = session_id
        await anyio.sleep(0)
        return {"status": "ok", "session_id": remote_driver.default_session_id}

    registered = _register(monkeypatch, probe, serialize=True)
    result = await registered(FIRST)

    assert result["ok"] is True
    assert remote_driver.default_session_id == DEFAULT
    assert not lock.locked()


@pytest.mark.anyio
@pytest.mark.parametrize("acquired_before_cancellation", [False, True])
async def test_cancellation_at_the_worker_boundary_cannot_leak_the_tool_lock(
    monkeypatch, acquired_before_cancellation,
):
    lock = threading.Lock()
    monkeypatch.setattr(S, "_TOOL_LOCK", lock)

    async def cancelled_worker(function):
        if acquired_before_cancellation:
            function()
        scope.cancel()
        await anyio.lowlevel.checkpoint()

    monkeypatch.setattr(S.anyio.to_thread, "run_sync", cancelled_worker)

    with anyio.CancelScope() as scope:
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await S._acquire_tool_lock()

    assert not lock.locked()
