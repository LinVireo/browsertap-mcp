"""Site approval reasons cross the MCP adapter before any permission grant."""

from types import SimpleNamespace

import anyio
import pytest
from mcp.types import ClientCapabilities

from browsertap_mcp import server as S
from tests.test_physical_approval_reasons import FAILURES, Session, _context, _structured


@pytest.fixture(autouse=True)
def approval_policy(monkeypatch):
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", "safe")
    monkeypatch.setenv("BROWSERTAP_LAB_NO_ELICIT", "0")
    monkeypatch.setenv("BROWSERTAP_APPROVAL_TIMEOUT", "0.01")
    monkeypatch.setattr(S, "_LAB_SITE_PERMISSION_APPROVALS", set())
    monkeypatch.setattr(S, "_LAB_PHYSICAL_APPROVALS", set())
    monkeypatch.setattr(S, "_LAB_APPROVAL_OWNERS", {})


@pytest.fixture
def permissions(monkeypatch):
    calls = []
    driver = SimpleNamespace(default_session_id="chrome:old")

    def ext_cmd(payload, **kwargs):
        calls.append((payload, kwargs))
        return {"data": {"ok": True}}

    def select(session_id=None):
        driver.default_session_id = session_id or "chrome:7"
        return driver.default_session_id

    driver.ext_cmd = ext_cmd
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "switch_session", select)
    return driver, calls


async def _call_site(setting="allow"):
    return _structured(await S.mcp.call_tool("set_site_permission", {
        "permission": "camera", "setting": setting,
        "origin": "https://example.test/path", "duration_seconds": 300,
        "session_id": "chrome:7",
    }))


@pytest.mark.anyio
@pytest.mark.parametrize("outcome,reason", FAILURES)
async def test_mcp_site_failure_keeps_reason_and_never_grants(
    permissions, monkeypatch, caplog, outcome, reason,
):
    driver, calls = permissions
    session = Session(outcome)
    _context(monkeypatch, session)
    with anyio.fail_after(2):
        result = await _call_site()
    assert result["ok"] is False
    assert result["error"]["code"] == "requires_user_action"
    assert result["reason"] == reason
    assert result["legacy"]["reason"] == reason
    assert result["diagnostics"]["reason"] == reason
    assert session.calls
    assert calls == []
    assert driver.default_session_id == "chrome:old"
    assert S._LAB_SITE_PERMISSION_APPROVALS == set()
    assert S._TOOL_LOCK.locked() is False
    assert "synthetic secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.anyio
@pytest.mark.parametrize("capabilities", [
    ClientCapabilities(), ClientCapabilities(elicitation={"url": {}}),
])
async def test_site_missing_form_support_does_not_prompt_or_grant(
    permissions, monkeypatch, capabilities,
):
    driver, calls = permissions
    session = Session(capabilities=capabilities)
    _context(monkeypatch, session)
    result = await _call_site()
    assert result["error"]["code"] == "requires_user_action"
    assert result["reason"] == "elicitation_unsupported"
    assert result["diagnostics"]["reason"] == "elicitation_unsupported"
    assert session.calls == []
    assert calls == []
    assert driver.default_session_id == "chrome:old"


@pytest.mark.anyio
async def test_site_missing_elicit_method_has_specific_reason(permissions, monkeypatch):
    driver, calls = permissions
    monkeypatch.setattr(S.mcp, "get_context", lambda: object())
    result = await _call_site()
    assert result["error"]["code"] == "requires_user_action"
    assert result["reason"] == "elicitation_unsupported"
    assert calls == []
    assert driver.default_session_id == "chrome:old"


@pytest.mark.anyio
@pytest.mark.parametrize("action,data", [
    ("accept", None), ("accept", SimpleNamespace()),
    ("accept", SimpleNamespace(approve=1)),
    ("accept", SimpleNamespace(approve="true")),
    ("accept", SimpleNamespace(approve=None)),
    ("unknown", SimpleNamespace(approve=True)),
])
async def test_inprocess_site_approval_requires_explicit_boolean_accept(action, data):
    async def elicit(**kwargs):
        return SimpleNamespace(action=action, data=data)

    decision = await S._request_site_permission_approval(
        SimpleNamespace(elicit=elicit), "camera", "https://example.test", 300,
    )
    assert decision.approved is False
    assert decision.reason == "error"
    assert S._LAB_SITE_PERMISSION_APPROVALS == set()


@pytest.mark.anyio
@pytest.mark.parametrize("mode,first_prompts", [("safe", 2), ("lab", 1)])
async def test_site_policy_preserves_per_allow_or_session_approval(
    permissions, monkeypatch, mode, first_prompts,
):
    driver, calls = permissions
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", mode)
    first, second = Session(), Session()
    first_context = _context(monkeypatch, first)
    # Physical approval cannot authorize a site-permission grant.
    S._LAB_PHYSICAL_APPROVALS.add(S._approval_key(first_context))
    for session in (first, first, second):
        _context(monkeypatch, session)
        result = await _call_site()
        assert result["ok"] is True
        assert driver.default_session_id == "chrome:old"
    assert len(first.calls) == first_prompts
    assert len(second.calls) == 1
    assert len(calls) == 3
    assert all(payload["setting"] == "allow" for payload, _ in calls)
    assert len(S._LAB_SITE_PERMISSION_APPROVALS) == (2 if mode == "lab" else 0)


@pytest.mark.anyio
@pytest.mark.parametrize("capabilities", [
    ClientCapabilities(elicitation={}),
    ClientCapabilities(elicitation={"form": {}}),
])
async def test_site_form_capability_compatibility(permissions, monkeypatch, capabilities):
    _, calls = permissions
    session = Session(capabilities=capabilities)
    _context(monkeypatch, session)
    result = await _call_site()
    assert result["ok"] is True
    assert len(session.calls) == 1
    assert calls == [({
        "cmd": "site_permission", "action": "set", "tabId": 7,
        "permission": "camera", "setting": "allow",
        "origin": "https://example.test", "durationSeconds": 300,
    }, {"client_id": "chrome", "timeout": 20.0})]


@pytest.mark.anyio
@pytest.mark.parametrize("mode,no_elicit,allowed", [
    ("lab", "1", True), ("lab", "0", False), ("safe", "1", False),
])
async def test_site_no_elicit_bypasses_form_only_in_lab(
    permissions, monkeypatch, mode, no_elicit, allowed,
):
    _, calls = permissions
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", mode)
    monkeypatch.setenv("BROWSERTAP_LAB_NO_ELICIT", no_elicit)
    session = Session(capabilities=ClientCapabilities())
    _context(monkeypatch, session)
    result = await _call_site()
    assert result["ok"] is allowed
    assert len(calls) == int(allowed)
    assert session.calls == []
    if not allowed:
        assert result["reason"] == "elicitation_unsupported"


@pytest.mark.anyio
@pytest.mark.parametrize("setting", ["block", "ask"])
async def test_site_non_grant_does_not_require_form_support(
    permissions, monkeypatch, setting,
):
    driver, calls = permissions
    session = Session(capabilities=ClientCapabilities())
    _context(monkeypatch, session)
    result = await _call_site(setting)
    assert result["ok"] is True
    assert [payload["setting"] for payload, _ in calls] == [setting]
    assert session.calls == []
    assert driver.default_session_id == "chrome:old"


@pytest.mark.anyio
async def test_site_mcp_cancellation_propagates_and_restores_target(permissions, monkeypatch):
    driver, calls = permissions
    monkeypatch.setenv("BROWSERTAP_APPROVAL_TIMEOUT", "60")
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
                await _call_site()
            except anyio.get_cancelled_exc_class():
                cancelled.append(True)
                raise

    with anyio.fail_after(2):
        async with anyio.create_task_group() as group:
            group.start_soon(call)
            await entered.wait()
            scopes[0].cancel()
    assert cancelled == [True]
    assert calls == []
    assert driver.default_session_id == "chrome:old"
    assert S._LAB_SITE_PERMISSION_APPROVALS == set()
    assert S._TOOL_LOCK.locked() is False
