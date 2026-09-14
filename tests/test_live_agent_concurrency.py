"""Exercise independent MCP processes against real browser tabs and a local page."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from contextlib import AsyncExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.live_preflight import resolve_remembered_tab

pytestmark = pytest.mark.live

_PAGE = b'''<!doctype html><html><head><title>BTAP concurrency fixture</title></head>
<body><h1>BTAP concurrency fixture</h1><label>Editable <input id="editable" value="seed"></label>
<input id="disabled" disabled><input id="readonly" readonly><input id="checked" type="checkbox" checked>
<textarea id="textarea">initial</textarea><button id="button">Button</button></body></html>'''


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        gate = self.server.gates.get(self.path)
        if gate is not None:
            gate[0].set()
            if not gate[1].wait(timeout=60):
                self.send_error(504, "test did not release its request gate")
                return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        try:
            self.wfile.write(_PAGE)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # The lifecycle recovery case closes a tab with a pending fetch.

    def log_message(self, _format, *_args):
        return


@contextmanager
def _page_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    server.gates = {}
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", server.gates
    finally:
        for _started, release in server.gates.values():
            release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


async def _client(stack):
    env = os.environ.copy()
    env["BROWSERTAP_NO_SPAWN"] = "1"
    env.pop("BROWSERTAP_PREFERRED_BROWSER", None)
    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
        command=sys.executable, args=["-u", "-m", "browsertap_mcp.server"], env=env,
    )))
    client = await stack.enter_async_context(ClientSession(*streams))
    await client.initialize()
    return client


async def _call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    payload = result.structuredContent
    if payload is None:
        payload = json.loads(next(item.text for item in result.content if item.type == "text"))
    return result, payload


async def _ok(client, name, **arguments):
    result, payload = await _call(client, name, **arguments)
    assert not result.isError, (name, payload)
    return payload.get("data", payload)


async def _owned_tab(client, url, browser_id, owned, *, registration_timeout=10.0):
    result, payload = await _call(client, "open_new_tab", url=url, client_id=browser_id)
    created = payload.get("legacy", payload.get("data", payload))
    # Retain recovery capabilities before asserting: create can succeed even
    # when its ACK is lost, and teardown still owes that tab a close.
    if created.get("owned") or (created.get("may_have_created") and created.get("owner_id")):
        owned.append((client, created))
    for _attempt in range(2):
        if created.get("owned") or not created.get("recovery"):
            break
        recovery = created["recovery"]
        result, payload = await _call(
            client, "open_new_tab", url=url, operation_id=recovery["operation_id"],
            client_id=recovery["client_id"], owner_id=recovery["owner_id"], timeout=15,
        )
        recovered = payload.get("legacy", payload.get("data", payload))
        created.update(recovered)
    assert created.get("owned") and created.get("session_id"), json.dumps(created)
    assert created["session_id"].startswith(browser_id + ":"), created
    assert created.get("generation"), "owned tab create returned no lifecycle generation"
    deadline = time.monotonic() + registration_timeout
    observed_generations = []
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            inventory = await asyncio.wait_for(_ok(client, "list_tabs"), timeout=remaining)
        except asyncio.TimeoutError:
            break
        observed_generations = [
            str(tab.get("generation") or "") for tab in inventory.get("tabs", [])
            if str(tab.get("id")) == created["session_id"]
        ]
        if str(created["generation"]) in observed_generations:
            return created
        remaining = max(0.0, deadline - time.monotonic())
        if remaining:
            await asyncio.sleep(min(0.1, remaining))
    raise AssertionError(
        "owned tab never registered its exact session/generation: "
        f"{created['session_id']} generation={created['generation']} "
        f"ready={created.get('ready')}, observed_generations={observed_generations}"
    )


async def _js(client, script, session_id=None, *, timeout=15):
    args = {"script": script, "no_monitor": True, "timeout": timeout}
    if session_id is not None:
        args["session_id"] = session_id
    result = await _ok(client, "execute_js", **args)
    return result["js_return"]


@contextmanager
def _request_gate(gates):
    path = f"/hold/{uuid.uuid4().hex}"
    started, release = threading.Event(), threading.Event()
    gates[path] = started, release
    try:
        yield path, started
    finally:
        release.set()


async def _started(event):
    assert await asyncio.to_thread(event.wait, 15), "browser script did not reach its HTTP gate"


def _held_script(path):
    return f"""
        window.__executions = (window.__executions || 0) + 1;
        return fetch({json.dumps(path)}).then(() => window.__executions);
    """


async def _busy(client, sid):
    result, payload = await _call(
        client, "execute_js", script="window.__wrong = true; return true",
        session_id=sid, no_monitor=True, timeout=5,
    )
    assert result.isError and payload["error_code"] == "target_busy", payload


async def _concurrent_calls(first, second, sid_a, sid_b, gates):
    for same_process in (False, True):
        peer = first if same_process else second
        with _request_gate(gates) as (path, started):
            pending = asyncio.create_task(_js(first, _held_script(path), sid_a, timeout=60))
            try:
                await _started(started)
                assert await _js(peer, "return window.__agent", sid_b) == "second"
                await _ok(
                    peer, "page_type", text="parallel-input", selector="#editable",
                    clear=True, session_id=sid_b,
                )
                assert await _js(peer, "return document.querySelector('#editable').value", sid_b) == "parallel-input"
                assert not pending.done(), "other tab waited for the blocked command"
                await _busy(peer, sid_a)
            finally:
                gates[path][1].set()
                await pending
    assert await _js(first, "return window.__wrong === undefined", sid_a) is True
    assert await _js(first, "return window.__agent") == "first"
    assert await _js(second, "return window.__agent") == "second"


async def _pending_operations(first, second, sid_a, sid_b, gates):
    await _js(first, "window.__executions = 0; return true", sid_a)
    with _request_gate(gates) as (path, started):
        pending = asyncio.create_task(_call(
            first, "execute_js", script=_held_script(path), session_id=sid_a,
            no_monitor=True, wait=False, timeout=15,
        ))
        try:
            await _started(started)
            result, payload = await pending
            raw = payload.get("legacy", payload.get("data", payload))
            assert not result.isError and raw["status"] == "in_progress", payload
            operation_id = raw["operation_id"]
            foreign_result, foreign_payload = await _call(
                second, "get_execute_js_result", operation_id=operation_id,
            )
            assert foreign_result.isError and foreign_payload["error_code"] == "operation_owner_mismatch", foreign_payload
            for peer in (first, second):
                await _busy(peer, sid_a)
            assert await _js(second, "return window.__agent", sid_b) == "second"
            progress = await _ok(first, "get_execute_js_result", operation_id=operation_id)
            assert progress["status"] == "in_progress", progress
        finally:
            gates[path][1].set()
            await pending
    finished = await _ok(first, "get_execute_js_result", operation_id=operation_id, timeout=15)
    assert finished["js_return"] == 1, finished
    repeated = await _ok(first, "get_execute_js_result", operation_id=operation_id)
    assert repeated["operation_id"] == finished["operation_id"] == operation_id
    assert repeated["status"] == finished["status"] == "success"
    assert repeated["js_return"] == finished["js_return"] == 1
    assert await _js(first, "return window.__executions", sid_a) == 1


async def _timed_out_execution(first, second, sid, other_sid, gates):
    # A local HTTP gate controls both the held Promise and independent proof
    # that the old script wrote to the page after the response deadline.
    with _request_gate(gates) as (path, started), _request_gate(gates) as (late_path, continued):
        gates[late_path][1].set()
        script = f"""
            return fetch({json.dumps(path)}).then(() => {{
                window.__lateEffect = 'after-timeout';
                return fetch({json.dumps(late_path)});
            }}).then(() => window.__lateEffect);
        """
        pending = asyncio.create_task(_call(
            first, "execute_js", script=script, session_id=sid,
            no_monitor=True, timeout=3,
        ))
        try:
            await _started(started)
            result, payload = await pending
            raw = payload.get("legacy", payload.get("data", payload))
            # The daemon deadline and the extension watchdog may answer in
            # either order. Neither reply proves that the script stopped.
            assert result.isError and raw["status"] in {"no_response", "failed"}, payload
            operation_id = raw["operation_id"]
            for _attempt in range(30):
                _result, receipt = await _call(first, "get_execute_js_result", operation_id=operation_id)
                progress = receipt.get("legacy", receipt.get("data", receipt))
                if progress.get("operation_status") == "outcome_unknown":
                    break
                assert progress["status"] == "in_progress", progress
                await asyncio.sleep(0.05)
            else:
                pytest.fail(f"executeScript watchdog did not retain an unknown outcome: {progress}")
            assert "exec_timeout" in str(progress), progress
            foreign_result, foreign_payload = await _call(
                second, "get_execute_js_result", operation_id=operation_id,
            )
            assert foreign_result.isError and foreign_payload["error_code"] == "operation_owner_mismatch", foreign_payload
            assert not continued.is_set()
            for release in (False, True):
                if release:
                    gates[path][1].set()
                    await _started(continued)
                _result, receipt = await _call(first, "get_execute_js_result", operation_id=operation_id)
                progress = receipt.get("legacy", receipt.get("data", receipt))
                assert progress["operation_id"] == operation_id
                assert progress["operation_status"] == "outcome_unknown", progress
                assert progress["reservation_held"] is True and progress["retry_safe"] is False, progress
                for peer in (first, second):
                    await _busy(peer, sid)
                assert await _js(second, "return window.__agent", other_sid) == "second"
            return operation_id
        finally:
            gates[path][1].set()
            await pending


async def _capture_ownership(first, second, sid_a, sid_b):
    for start, stop in (("console_capture_start", "console_capture_stop"),
                        ("network_capture_start", "network_capture_stop")):
        await _ok(first, start, session_id=sid_a)
        try:
            for command in (start, stop):
                result, payload = await _call(second, command, session_id=sid_a)
                assert result.isError and payload["error_code"] == "capture_busy", (command, payload)
            if start == "console_capture_start":
                result, payload = await _call(second, "get_console_messages", session_id=sid_a, clear=True)
                assert result.isError and payload["error_code"] == "capture_busy", payload
            await _ok(second, start, session_id=sid_b)
            await _ok(second, stop, session_id=sid_b)
            await _js(first, "console.log('btap-owner'); return true", sid_a)
        finally:
            await _ok(first, stop, session_id=sid_a)
        await _ok(second, start, session_id=sid_a)
        await _ok(second, stop, session_id=sid_a)


async def _abandoned_capture(recovering, sid):
    async with AsyncExitStack() as stack:
        abandoned = await _client(stack)
        await _ok(abandoned, "console_capture_start", session_id=sid)
        await _ok(abandoned, "network_capture_start", session_id=sid)
    for stop in ("console_capture_stop", "network_capture_stop"):
        await _ok(recovering, stop, session_id=sid)
    await _ok(recovering, "console_capture_start", session_id=sid)
    await _ok(recovering, "console_capture_stop", session_id=sid)


async def _abandoned_execution(recovering, other, created, other_sid, gates):
    sid = created["session_id"]
    with _request_gate(gates) as (path, started):
        async with AsyncExitStack() as stack:
            abandoned = await _client(stack)
            pending = await _ok(
                abandoned, "execute_js", script=_held_script(path), session_id=sid,
                no_monitor=True, wait=False, timeout=15,
            )
            assert pending["status"] == "in_progress", pending
            await _started(started)
        await _busy(recovering, sid)
        assert await _js(other, "return window.__agent", other_sid) == "second"
        await _ok(
            recovering, "close_tabs", tab_id=sid, session_id=sid,
            owner_id=created["owner_id"],
        )


async def _unknown_execution(client, operation_id):
    for _attempt in range(30):
        _result, payload = await _call(client, "get_execute_js_result", operation_id=operation_id)
        raw = payload.get("legacy", payload.get("data", payload))
        if raw.get("operation_status") == "outcome_unknown":
            assert raw["reservation_held"] is True and raw["retry_safe"] is False, raw
            return
        assert raw["status"] == "in_progress", payload
        await asyncio.sleep(0.05)
    pytest.fail(f"execution did not expose its unknown outcome: {payload}")


async def _uncertain_execution(first, second, sid, other_sid, gates, *, manual):
    if manual:
        _result, payload = await _call(
            first, "execute_js", script="confirm('BTAP concurrency fixture'); 7",
            session_id=sid, no_monitor=True, dialog_policy="manual", timeout=10,
        )
        raw = payload.get("legacy", payload.get("data", payload))
        assert raw["status"] == "blocked_by_dialog", payload
        operation_id = raw["operation_id"]
        result, refused = await _call(second, "handle_dialog", action="dismiss", session_id=sid)
        assert result.isError and refused["error_code"] == "target_busy", refused
        await _ok(first, "handle_dialog", action="dismiss", session_id=sid)
        for _attempt in range(30):
            observed = await _ok(first, "handle_dialog", action="manual", session_id=sid)
            if observed["pending_execution"] is False:
                assert observed["status"] == "no_dialog", observed
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the handled manual execution did not settle")
        await _unknown_execution(first, operation_id)
    else:
        with _request_gate(gates) as (path, started):
            pending = asyncio.create_task(_call(
                first, "cdp_command", method="Runtime.evaluate", session_id=sid,
                params_json=json.dumps({
                    "expression": f"fetch({json.dumps(path)}).then(r => r.status)",
                    "awaitPromise": True, "returnByValue": True,
                }), timeout=1,
            ))
            try:
                await _started(started)
                result, payload = await pending
                assert result.isError, payload
                operation_id = payload["diagnostics"]["operation_id"]
                assert payload["diagnostics"]["reservation_held"] is True, payload
                # A transport timeout can precede Chrome's watchdog. Keep the
                # fetch blocked until the browser timeout is actually retained;
                # releasing it sooner permits a valid late success instead.
                await _unknown_execution(first, operation_id)
            finally:
                gates[path][1].set()
                await pending

    for client in (first, second):
        await _busy(client, sid)
    assert await _js(second, "return window.__agent", other_sid) == "second"
    return operation_id


async def _matrix(url, client_ids, gates):
    evidence = []
    async with AsyncExitStack() as stack:
        first = await _client(stack)
        second = await _client(stack)
        owned = []
        try:
            if len(client_ids) > 1:
                result, payload = await _call(first, "open_new_tab", url=url)
                assert result.isError and payload["error_code"] == "ambiguous_browser", payload
                evidence.append("unselected browser refused without creating a tab")
            for client, browser_id in ((first, client_ids[0]), (second, client_ids[-1])):
                await _owned_tab(client, url, browser_id, owned)
            sid_a, sid_b = [item[1]["session_id"] for item in owned]
            await _ok(first, "switch_tab", session_id=sid_a)
            await _ok(second, "switch_tab", session_id=sid_b)
            await _js(first, "window.__agent = 'first'; return true")
            await _js(second, "window.__agent = 'second'; return true")
            assert await _js(first, "return window.__agent") == "first"
            assert await _js(second, "return window.__agent") == "second"
            evidence.append("each MCP process retains its own default tab")

            await _concurrent_calls(first, second, sid_a, sid_b, gates)
            evidence.append("same-process and cross-process calls run on different tabs; same-tab calls get target_busy")
            await _pending_operations(first, second, sid_a, sid_b, gates)
            evidence.append("async scripts retain their target until completion; repeatable result reads never replay execution")
            await _capture_ownership(first, second, sid_a, sid_b)
            evidence.append("capture ownership survives calls and rejects another process stopping or clearing it")
            await _abandoned_capture(second, sid_b)
            evidence.append("an exited MCP session's capture can be stopped and reclaimed")

            await _js(first, "document.querySelector('#editable').focus(); return true", sid_a)
            for selector in ("#disabled", "#readonly", "#button"):
                result, payload = await _call(
                    first, "page_type", text="must-not-land", selector=selector, session_id=sid_a,
                )
                assert result.isError, (selector, payload)
                assert payload["legacy"]["status"] == "not_interactable", (selector, payload)
                assert payload["legacy"]["input_dispatched"] is False, (selector, payload)
                assert await _js(first, "return document.querySelector('#editable').value", sid_a) == "seed"
            await _ok(first, "page_type", text="typed", selector="#editable", clear=True, session_id=sid_a)
            assert await _js(first, "return document.querySelector('#editable').value", sid_a) == "typed"
            evidence.append("invalid input targets preserve old focus value; valid background typing succeeds")

            for client, sid in ((first, sid_a), (second, sid_b)):
                await _ok(client, "console_capture_start", session_id=sid)
                await _js(client, "console.log('btap-capture-fixture'); return true", sid)
                await _ok(client, "console_capture_stop", session_id=sid)
                await _ok(client, "network_capture_start", session_id=sid)
                await _js(client, "return fetch(location.href).then(r => r.status)", sid)
                await _ok(client, "network_capture_stop", session_id=sid)
                await _ok(client, "page_type", text="after-capture", selector="#editable", clear=True, session_id=sid)
            evidence.append("capture stop releases debugger for subsequent page input")

            await _js(first, """
                document.querySelector('#editable').value = '';
                document.querySelector('#checked').checked = false;
                document.querySelector('#textarea').value = '';
                return true;
            """, sid_a)
            scan = await _ok(first, "scan_page", session_id=sid_a, cutlist=False, maxchars=1200)
            assert len(scan["content"]) <= 1200, scan
            assert 'value="seed"' not in scan["content"], scan
            assert 'checked=""' not in scan["content"], scan
            evidence.append("snapshot respects length budget and current form state")
            await _abandoned_execution(first, second, owned[0][1], sid_b, gates)
            owned.pop(0)
            evidence.append("MCP exit keeps pending JS isolated; the tab owner can close its own tab to recover")
            for mode in ("execute_script", "cdp", "manual"):
                created = await _owned_tab(first, url, client_ids[0], owned)
                sid = created["session_id"]
                # These cases intentionally use short execution deadlines.
                # Tab registration alone does not finish initial navigation.
                ready = await _ok(first, "wait_for_url", url_pattern=url,
                                  session_id=sid, timeout=15)
                assert ready["status"] == "success" and ready["ready_state"] == "complete", ready
                if mode == "execute_script":
                    operation_id = await _timed_out_execution(first, second, sid, sid_b, gates)
                else:
                    operation_id = await _uncertain_execution(
                        first, second, sid, sid_b, gates, manual=mode == "manual",
                    )
                await _ok(first, "close_tabs", tab_id=sid, session_id=sid, owner_id=created["owner_id"])
                owned.pop()
                ended = await _ok(first, "get_execute_js_result", operation_id=operation_id)
                assert ended["status"] == "navigated" and ended["reservation_held"] is False, ended
            evidence.append("executeScript/CDP watchdogs and handled manual dialogs retain uncertain execution until owned-tab closure; late page effects never permit conflicting calls")
        finally:
            cleanup_errors = []
            for client, created in reversed(owned):
                if not created.get("session_id"):
                    recovery = created.get("recovery")
                    if recovery:
                        result, payload = await _call(
                            client, "open_new_tab", url=url,
                            operation_id=recovery["operation_id"], client_id=recovery["client_id"],
                            owner_id=recovery["owner_id"], timeout=15,
                        )
                        created.update(payload.get("legacy", payload.get("data", payload)))
                    if not created.get("session_id"):
                        cleanup_errors.append(created)
                        continue
                result, payload = await _call(
                    client, "close_tabs", tab_id=created["session_id"],
                    session_id=created["session_id"], owner_id=created["owner_id"],
                )
                if result.isError:
                    cleanup_errors.append(payload)
            assert not cleanup_errors, cleanup_errors
    return evidence


def _browser_clients(driver):
    return [item["client_id"] for item in driver._remote_cmd({"cmd": "get_clients"})["r"]]


def test_independent_mcp_processes_one_browser(driver):
    clients = _browser_clients(driver)
    assert clients
    clients = clients[:1]
    before = driver.ext_cmd({"cmd": "tabs", "all": True}, client_id=clients[0])["data"]
    foreground = next((tab for tab in before if tab.get("active")), None)
    try:
        with _page_server() as (url, gates):
            evidence = asyncio.run(_matrix(url, clients, gates))
    finally:
        after = driver.ext_cmd({"cmd": "tabs", "all": True}, client_id=clients[0])["data"]
        restore = resolve_remembered_tab(foreground, after)
        if restore is not None and not next(tab.get("active") for tab in after if tab["id"] == restore):
            driver.ext_cmd({"cmd": "tabs", "method": "switch", "tabId": restore}, client_id=clients[0])
    print(json.dumps({"clients": clients, "checks": evidence}, ensure_ascii=True))
