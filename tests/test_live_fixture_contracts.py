"""Offline checks for live callers' receipt and owned-tab cleanup contracts."""
from __future__ import annotations

import asyncio
import json
from functools import partial
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult

from browsertap_mcp import server as S
from tests import test_live_agent_concurrency as concurrency
from tests import test_live_browser as live
from tests.test_pending_bridge_operations import finish
from tests.test_wait_reservation_contract import pending_wait_bridge


@pytest.mark.parametrize("case,wait_timeout", [
    (live.TestWaitFor().test_absent_selector_times_out, 3),
    (live.TestWaitFor().test_gone_on_a_permanent_element_times_out, 3),
    (live.TestWaitFor().test_a_long_wait_stays_within_its_total_deadline, 4),
    (live.TestWaitForUrl().test_times_out_when_pattern_never_matches, 3),
])
def test_negative_live_wait_collects_late_reply_before_reusing_tab(monkeypatch, case, wait_timeout):
    bridge, now = pending_wait_bridge(monkeypatch)
    request = SimpleNamespace(session=SimpleNamespace(shouldstop=False))
    monkeypatch.setattr(live, "goto", lambda *args, **kwargs: None)
    wait_activity = bridge._wait_for_activity
    public_collect = S.get_execute_js_result
    collected = []

    def late_reply(seen, timeout):
        serial = wait_activity(seen, timeout)
        if now[0] >= 1000 + wait_timeout + 0.05:
            finish(bridge, bridge.sent[0]["id"], json.dumps({"met": False}))
        return serial

    def collect(operation_id, timeout):
        receipt = public_collect(operation_id, timeout=timeout)
        collected.append((operation_id, timeout, receipt))
        return receipt

    monkeypatch.setattr(bridge, "_wait_for_activity", late_reply)
    monkeypatch.setattr(S, "get_execute_js_result", collect)
    case(scratch_session="browser:1", request=request)
    assert len(collected) == 1
    operation_id, timeout, receipt = collected[0]
    assert operation_id == bridge.sent[0]["id"]
    assert timeout == 5.0
    assert receipt["status"] == "success" and receipt["reservation_held"] is False
    assert json.loads(receipt["js_return"]) == {"met": False}
    assert request.session.shouldstop is False
    assert len(bridge.sent) == 1
    bridge.ext_cmd({"cmd": "navigate", "tabId": 1}, requester_id="next")
    assert len(bridge.sent) == 2


@pytest.mark.parametrize("completion", ["pending", "failed", "unknown"])
def test_live_wait_unsettled_receipt_fails_and_stops_the_shared_sequence(monkeypatch, completion):
    bridge, now = pending_wait_bridge(monkeypatch)
    result = S.wait_for(selector="#missing", session_id="browser:1", timeout=1)
    operation_id = result["operation_id"]
    if completion != "pending":
        error = "probe failed" if completion == "failed" else "cdp_timeout: Runtime.evaluate"
        finish(bridge, operation_id, error, success=False)
    request = SimpleNamespace(session=SimpleNamespace(shouldstop=False))
    with pytest.raises(pytest.fail.Exception, match="did not settle safely"):
        live._collect_wait_result(result, request, timeout=0.2)
    assert operation_id in request.session.shouldstop
    assert now[0] <= 1001.2 + 1e-6
    assert len(bridge.sent) == 1


def test_live_wait_receipt_query_error_stops_the_shared_sequence(monkeypatch):
    bridge, _ = pending_wait_bridge(monkeypatch)
    result = S.wait_for(selector="#missing", session_id="browser:1", timeout=1)

    def disconnected(*args, **kwargs):
        raise ConnectionError("receipt transport unavailable")

    monkeypatch.setattr(S, "get_execute_js_result", disconnected)
    request = SimpleNamespace(session=SimpleNamespace(shouldstop=False))
    with pytest.raises(pytest.fail.Exception, match="receipt transport unavailable"):
        live._collect_wait_result(result, request)
    assert result["operation_id"] in request.session.shouldstop
    assert len(bridge.sent) == 1


def test_live_wait_without_pending_handle_needs_no_receipt_query(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("completed wait must not query another operation")

    monkeypatch.setattr(S, "get_execute_js_result", unexpected)
    request = SimpleNamespace(session=SimpleNamespace(shouldstop=False))
    assert live._collect_wait_result({"status": "timeout"}, request) is None
    assert request.session.shouldstop is False


class _TabClient:
    def __init__(self, inventories, *, ready=False, hang=False):
        self.created = {
            "owned": True, "ready": ready, "session_id": "browser:1",
            "generation": "created-generation", "owner_id": "fixture-owner",
        }
        self.inventories = inventories
        self.hang = hang
        self.calls = []
        self.reads = 0

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "open_new_tab":
            data = self.created
        elif name == "list_tabs":
            self.reads += 1
            if self.hang:
                await asyncio.Future()
            data = {"tabs": self.inventories[min(self.reads - 1, len(self.inventories) - 1)]}
        elif name == "close_tabs":
            data = {"closed": [1]}
        else:
            raise AssertionError(f"unexpected browser command: {name}")
        return CallToolResult(content=[], structuredContent={"data": data})


@pytest.mark.parametrize("ready", [False, True])
def test_owned_tab_waits_for_its_exact_registered_session_and_generation(ready):
    client = _TabClient([
        [
            {"id": "browser:1", "generation": "replacement", "url": "https://example.test/"},
            {"id": "browser:2", "generation": "created-generation", "url": "https://example.test/"},
        ],
        [{"id": "browser:1", "generation": "created-generation"}],
    ], ready=ready)
    owned = []
    created = asyncio.run(concurrency._owned_tab(client, "https://example.test/", "browser", owned))
    assert created["ready"] is ready
    assert owned == [(client, created)]
    assert [name for name, _ in client.calls] == ["open_new_tab", "list_tabs", "list_tabs"]


@pytest.mark.parametrize("inventory,hang", [
    ([], False),
    ([{"id": "browser:1", "generation": "replacement"}], False),
    ([{"id": "browser:2", "generation": "created-generation"}], False),
    ([], True),
])
def test_owned_tab_registration_failure_keeps_matrix_cleanup_capability(monkeypatch, inventory, hang):
    client = _TabClient([inventory], hang=hang)

    async def connect(stack):
        return client

    monkeypatch.setattr(concurrency, "_client", connect)
    monkeypatch.setattr(concurrency, "_owned_tab", partial(concurrency._owned_tab, registration_timeout=0.03))
    with pytest.raises(AssertionError, match="never registered its exact session/generation"):
        asyncio.run(concurrency._matrix("https://example.test/", ["browser"], {}))
    assert client.reads >= 1
    assert client.calls[-1] == ("close_tabs", {
        "tab_id": "browser:1", "session_id": "browser:1", "owner_id": "fixture-owner",
    })
    assert all(name in {"open_new_tab", "list_tabs", "close_tabs"} for name, _ in client.calls)


@pytest.mark.parametrize("pending_receipts", [0, 2])
def test_cdp_watchdog_fixture_keeps_http_gate_held_until_unknown_receipt(monkeypatch, pending_receipts):
    gates = {}
    observations = []

    async def call(client, name, **arguments):
        started, released = next(iter(gates.values()))
        if name == "cdp_command":
            started.set()
            return CallToolResult(content=[], isError=True), {
                "diagnostics": {"operation_id": "held-cdp", "reservation_held": True},
            }
        assert name == "get_execute_js_result", name
        observations.append(released.is_set())
        # The transport can time out before Chrome's watchdog. Releasing the
        # fetch here would let Chrome publish a normal late success instead.
        if released.is_set():
            payload = {"status": "success", "reservation_held": False}
        elif len(observations) <= pending_receipts:
            payload = {"status": "in_progress", "reservation_held": True}
        else:
            payload = {
                "status": "in_progress", "operation_status": "outcome_unknown",
                "reservation_held": True, "retry_safe": False,
            }
        return CallToolResult(content=[], isError=not released.is_set()), payload

    async def busy(*args):
        return None

    async def other_page(*args):
        return "second"

    monkeypatch.setattr(concurrency, "_call", call)
    monkeypatch.setattr(concurrency, "_busy", busy)
    monkeypatch.setattr(concurrency, "_js", other_page)
    operation_id = asyncio.run(concurrency._uncertain_execution(
        object(), object(), "browser:1", "browser:2", gates,
        manual=False,
    ))
    assert operation_id == "held-cdp"
    assert observations and not any(observations)
    assert all(released.is_set() for _started, released in gates.values())
