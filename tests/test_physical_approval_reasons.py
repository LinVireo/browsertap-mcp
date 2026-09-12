"""Approval reasons survive the real MCP adapter; no OS input is permitted."""

from types import SimpleNamespace

import anyio
import pytest
from mcp.server.fastmcp import Context
from mcp.shared.exceptions import McpError
from mcp.types import ClientCapabilities, ElicitResult, ErrorData

from browsertap_mcp import native_dialog as N
from browsertap_mcp import server as S
from tests.test_native_dialog import _inspect, _sends
from tests.test_native_dialog import native as native
from tests.test_physical_input import _reach_the_leave_fallback


def _structured(result):
    if isinstance(result, tuple):
        return result[1]
    if isinstance(result, dict):
        return result
    return result.structuredContent


class Session:
    def __init__(self, outcome="accept", *, capabilities=None):
        self.client_params = SimpleNamespace(capabilities=(
            ClientCapabilities(elicitation={"form": {}})
            if capabilities is None else capabilities
        ))
        self.outcome = outcome
        self.calls = []

    async def elicit_form(self, **kwargs):
        self.calls.append(kwargs)
        if self.outcome == "timeout":
            await anyio.sleep_forever()
        if self.outcome == "method_missing":
            raise McpError(ErrorData(code=-32601, message="synthetic secret METHOD_NOT_FOUND"))
        if self.outcome == "not_implemented":
            raise NotImplementedError("synthetic secret unsupported")
        if self.outcome == "error":
            raise RuntimeError("synthetic secret approval failure")
        if self.outcome == "rpc_error":
            raise McpError(ErrorData(code=-32603, message="synthetic secret RPC failure"))
        if self.outcome in {"decline", "cancel"}:
            return ElicitResult(action=self.outcome)
        approve = {"reject": False, "malformed": 1}.get(self.outcome, True)
        return ElicitResult(action="accept", content={"approve": approve})


def _context(monkeypatch, session):
    context = Context(
        request_context=SimpleNamespace(session=session, request_id="approval-test"),
        fastmcp=S.mcp,
    )
    monkeypatch.setattr(S.mcp, "get_context", lambda: context)
    return context


@pytest.fixture(autouse=True)
def approval_policy(monkeypatch):
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", "lab")
    monkeypatch.setenv("BROWSERTAP_LAB_NO_ELICIT", "0")
    monkeypatch.setenv("BROWSERTAP_APPROVAL_TIMEOUT", "0.01")
    monkeypatch.setattr(S, "_LAB_PHYSICAL_APPROVALS", set())
    monkeypatch.setattr(S, "_LAB_APPROVAL_OWNERS", {})


def _forbid_physical_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("approval failure must precede lease, quiet, focus and OS input")
    monkeypatch.setattr(S, "_pyautogui", forbidden)
    monkeypatch.setattr(S, "_maybe_activate", forbidden)
    monkeypatch.setattr(S.physical_input, "run_physical_action", forbidden)
    monkeypatch.setattr(S.physical_input, "PhysicalInputLease", forbidden)
    monkeypatch.setattr(S.physical_input, "wait_for_quiet", forbidden)


FAILURES = [
    ("decline", "declined"), ("reject", "declined"), ("cancel", "cancelled"),
    ("timeout", "timeout"), ("method_missing", "elicitation_unsupported"),
    ("not_implemented", "elicitation_unsupported"),
    ("error", "error"), ("rpc_error", "error"), ("malformed", "error"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("outcome,reason", FAILURES)
async def test_native_mcp_failure_keeps_reason_receipt_and_consumes_ticket(
    native, monkeypatch, outcome, reason,
):
    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    _forbid_physical_work(monkeypatch)
    session = Session(outcome)
    _context(monkeypatch, session)
    with anyio.fail_after(2):
        result = _structured(await S.mcp.call_tool(
            "cancel_native_file_dialog", {"ticket": ticket, "desktop_opt_in": True},
        ))
    assert result["ok"] is False
    assert result["error"]["code"] == "requires_user_action"
    assert result["reason"] == reason
    assert result["legacy"]["reason"] == reason
    assert result["diagnostics"]["reason"] == reason
    assert result["diagnostics"]["dispatched"] is False
    assert result["diagnostics"]["delivery_state"] == "undelivered"
    assert result["diagnostics"]["desktop"]["ticket_consumed"] is True
    assert session.calls
    assert _sends(os) == []
    assert not os.properties
    assert ticket not in manager._tickets


@pytest.mark.anyio
@pytest.mark.parametrize("outcome,reason", FAILURES)
async def test_leave_fallback_mcp_failure_exposes_reason_before_physical_work(
    monkeypatch, outcome, reason,
):
    _reach_the_leave_fallback(monkeypatch, no_elicit=False)
    _forbid_physical_work(monkeypatch)
    _context(monkeypatch, Session(outcome))
    result = _structured(await S.mcp.call_tool("resolve_leave_dialog", {"session_id": "client:7"}))
    assert result["error"]["code"] == "requires_user_action"
    assert result["reason"] == reason
    assert result["legacy"]["reason"] == reason
    assert result["legacy"]["physical"]["reason"] == reason
    assert result["diagnostics"]["reason"] == reason
    assert S._TOOL_LOCK.locked() is False


@pytest.mark.anyio
@pytest.mark.parametrize("capabilities", [
    ClientCapabilities(), ClientCapabilities(elicitation={"url": {}}),
])
async def test_unsupported_form_capability_is_reported_without_sending_prompt(
    native, monkeypatch, capabilities,
):
    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    _forbid_physical_work(monkeypatch)
    session = Session(capabilities=capabilities)
    _context(monkeypatch, session)
    result = _structured(await S.mcp.call_tool(
        "cancel_native_file_dialog", {"ticket": ticket, "desktop_opt_in": True},
    ))
    assert result["error"]["code"] == "requires_user_action"
    assert result["diagnostics"]["reason"] == "elicitation_unsupported"
    assert session.calls == []
    assert _sends(os) == []
    assert not os.properties


@pytest.mark.anyio
async def test_missing_elicit_method_has_specific_reason(native, monkeypatch):
    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    _forbid_physical_work(monkeypatch)
    monkeypatch.setattr(S.mcp, "get_context", lambda: object())
    result = _structured(await S.mcp.call_tool(
        "cancel_native_file_dialog", {"ticket": ticket, "desktop_opt_in": True},
    ))
    assert result["diagnostics"]["reason"] == "elicitation_unsupported"
    assert _sends(os) == []


@pytest.mark.anyio
@pytest.mark.parametrize("capabilities", [
    ClientCapabilities(elicitation={}),
    ClientCapabilities(elicitation={"form": {}}),
])
async def test_form_approval_works_through_mcp_and_keeps_session_cache(
    native, monkeypatch, capabilities,
):
    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    session = Session(capabilities=capabilities)
    context = _context(monkeypatch, session)
    result = _structured(await S.mcp.call_tool(
        "cancel_native_file_dialog", {"ticket": ticket, "desktop_opt_in": True},
    ))
    assert result["ok"] is True
    assert len(_sends(os)) == 1
    assert len(session.calls) == 1
    assert S._approval_key(context) in S._LAB_PHYSICAL_APPROVALS
    assert "reason" not in result["diagnostics"]


@pytest.mark.anyio
async def test_ticket_expiry_keeps_its_error_and_adds_the_approval_reason(native, monkeypatch):
    manager, os, clock = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    ticket = _inspect(manager)
    session = Session("decline")
    original = session.elicit_form

    async def expire(**kwargs):
        clock.now += 16
        return await original(**kwargs)

    session.elicit_form = expire
    _context(monkeypatch, session)
    _forbid_physical_work(monkeypatch)
    result = _structured(await S.mcp.call_tool(
        "cancel_native_file_dialog", {"ticket": ticket, "desktop_opt_in": True},
    ))
    assert result["error"]["code"] == "native_dialog_ticket_expired"
    assert result["diagnostics"]["reason"] == "declined"
    assert _sends(os) == []
    assert not os.properties


@pytest.mark.anyio
async def test_mcp_task_cancellation_propagates_and_cleans_the_ticket(native, monkeypatch):
    manager, os, _ = native
    monkeypatch.setattr(N, "_MANAGER", manager)
    monkeypatch.setenv("BROWSERTAP_APPROVAL_TIMEOUT", "60")
    ticket = _inspect(manager)
    _forbid_physical_work(monkeypatch)
    entered = anyio.Event()
    cancelled = []
    scopes = []
    session = Session()

    async def wait_for_user(**kwargs):
        entered.set()
        await anyio.sleep_forever()

    session.elicit_form = wait_for_user
    _context(monkeypatch, session)

    async def call():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            try:
                await S.mcp.call_tool(
                    "cancel_native_file_dialog", {"ticket": ticket, "desktop_opt_in": True},
                )
            except anyio.get_cancelled_exc_class():
                cancelled.append(True)
                raise

    with anyio.fail_after(2):
        async with anyio.create_task_group() as group:
            group.start_soon(call)
            await entered.wait()
            scopes[0].cancel()
    assert cancelled == [True]
    assert _sends(os) == []
    assert not os.properties
    assert ticket not in manager._tickets
