"""Create/reconcile must retain the exact operation and cleanup capability."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


@pytest.fixture
def create_driver(monkeypatch):
    now = [0.0]
    statuses, creates, dispatched = [], [], []
    def response(value):
        now[0] += 0.02
        if isinstance(value, BaseException):
            raise value
        return value
    def status(payload, **kwargs):
        dispatched.append(("status", payload, kwargs))
        return response(statuses.pop(0) if statuses else reply("pending"))
    def create(**kwargs):
        dispatched.append(("create", kwargs))
        return response(creates.pop(0) if creates else reply("completed"))
    driver = SimpleNamespace(default_session_id="chrome:1", ext_cmd=status, newtab=create)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(S.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(S.secrets, "token_urlsafe", lambda size: "fixture")
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)
    monkeypatch.setattr(S, "active_sessions", lambda **kw: [
        {"id": "chrome:7", "generation": "g7", "url": "https://created.test/"},
    ])
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", S._TabOwnershipRegistry())
    return driver, statuses, creates, dispatched, now


def reply(state, *, client="chrome", **fields):
    return {"client_id": client, "data": {
        "operation_id": "open-tab-fixture", "operation_status": state,
        "id": 7, "generation": "g7", **fields,
    }}


@pytest.mark.parametrize("arguments", [
    {"operation_id": " "}, {"client_id": " "}, {"owner_id": " "},
    {"session_id": "chrome:1", "client_id": "edge"},
])
def test_create_validates_capabilities_before_contacting_the_browser(create_driver, arguments):
    _, _, _, dispatched, _ = create_driver
    with pytest.raises(ValueError):
        S.open_new_tab("https://created.test/", **arguments)
    assert dispatched == []


@pytest.mark.parametrize("probe", [reply("not_found", client="edge"), {"data": {}}])
def test_discovery_cannot_silently_select_another_or_unknown_browser(create_driver, probe):
    driver, statuses, _, dispatched, _ = create_driver
    statuses.append(probe)
    if "client_id" not in probe:
        driver.default_session_id = None
    result = S.open_new_tab("https://created.test/")
    assert result["status"] == "unknown"
    assert result["may_have_created"] is False
    assert [call[0] for call in dispatched] == ["status"]


@pytest.mark.parametrize("phase", ["discovery", "create"])
def test_tab_creation_never_dispatches_after_the_deadline_expires(create_driver, monkeypatch, phase):
    _, statuses, _, dispatched, _ = create_driver
    statuses.append(reply("not_found"))
    ticks = iter([0, 2] if phase == "discovery" else [0, 0, 2])
    monkeypatch.setattr(S.time, "monotonic", lambda: next(ticks, 2))

    result = S.open_new_tab("https://created.test/", timeout=1)

    assert result["status"] == "unknown"
    assert result["owned"] is False
    assert [call[0] for call in dispatched] == ([] if phase == "discovery" else ["status"])
    assert S._TAB_OWNERSHIP.outstanding() == []


def test_ambiguous_browser_discovery_stays_a_routing_error(create_driver):
    driver, statuses, _, dispatched, _ = create_driver
    driver.default_session_id = None
    failure = S.AmbiguousBrowserError(["chrome", "edge"])
    statuses.append(failure)

    with pytest.raises(S.AmbiguousBrowserError) as raised:
        S.open_new_tab("https://created.test/")

    assert raised.value is failure
    assert [call[0] for call in dispatched] == ["status"]


def test_completed_recovery_claims_the_original_tab_without_creating_another(create_driver):
    _, statuses, _, dispatched, _ = create_driver
    statuses.append(reply("completed"))

    result = S.open_new_tab(
        "https://created.test/", operation_id="open-tab-fixture", client_id="chrome", owner_id="owner",
    )

    assert result["status"] == "ok"
    assert result["session_id"] == "chrome:7"
    assert result["generation"] == "g7"
    assert result["owner_id"] == "owner"
    assert result["owned"] is True
    assert [call[0] for call in dispatched] == ["status"]


@pytest.mark.parametrize("phase", ["resume", "create", "reconcile"])
def test_terminal_unknown_create_does_not_recommend_polling_a_dead_producer(create_driver, phase):
    _, statuses, creates, dispatched, _ = create_driver
    terminal = reply("unknown", resume_required=False, may_have_created=True, retry_safe=False)
    options = {"owner_id": "owner"}
    if phase == "resume":
        statuses.append(terminal)
        options.update(operation_id="open-tab-fixture", client_id="chrome")
    else:
        statuses.append(reply("not_found"))
        creates.append(terminal if phase == "create" else reply("pending"))
        if phase == "reconcile":
            statuses.append(terminal)
    result = S.open_new_tab("https://created.test/", **options)
    assert result["status"] == "unknown"
    assert result["resume_required"] is False
    assert result["reconciliation"]["resume_required"] is False
    assert result["may_have_created"] is True
    assert result["retry_safe"] is False
    assert result["owned"] is False
    assert result["owner_id"] == "owner"
    assert "open_new_tab again" not in result["recovery"]["instruction"]
    assert "list_tabs()" in result["recovery"]["instruction"]
    assert len(dispatched) == {"resume": 1, "create": 2, "reconcile": 3}[phase]


@pytest.mark.parametrize("fields", [
    {"client_id": "edge"}, {"id": None}, {"generation": ""}, {"id": "invalid"},
])
def test_incomplete_create_identity_never_becomes_owned(create_driver, fields):
    _, statuses, creates, _, _ = create_driver
    statuses.append(reply("not_found"))
    completed = reply("completed")
    completed["data"].update(fields)
    creates.append(completed)
    result = S.open_new_tab("https://created.test/", owner_id="owner")
    assert result["status"] == "unknown"
    assert result["may_have_created"] is True
    assert result["owned"] is False
    assert result["owner_id"] == "owner"
    assert S._TAB_OWNERSHIP.outstanding() == []


@pytest.mark.parametrize("phase", ["create", "status", "retry"])
def test_browser_mismatch_at_each_reply_preserves_uncertainty(create_driver, phase):
    _, statuses, creates, dispatched, _ = create_driver
    statuses.append(reply("not_found"))
    if phase == "create":
        creates.append(reply("completed", client="edge"))
    else:
        creates.append(TimeoutError("lost ACK"))
        statuses.append(reply("pending", client="edge") if phase == "status" else reply("not_found"))
        if phase == "retry":
            creates.append(reply("completed", client="edge"))
    result = S.open_new_tab("https://created.test/", owner_id="owner")
    assert result["status"] == "unknown"
    assert result["retry_safe"] is False
    assert result["owner_id"] == "owner"
    assert all(call[-1].get("client_id") == "chrome" for call in dispatched)


@pytest.mark.parametrize("nested_error", [None, "browser rejected the requested URL"])
def test_nested_operation_failure_is_not_lost_during_unwrapping(create_driver, nested_error):
    _, statuses, creates, _, _ = create_driver
    statuses.append(reply("not_found"))
    nested = reply("not_found")["data"]
    if nested_error:
        nested["error"] = nested_error
    creates.append({"client_id": "chrome", "data": {
        "error": "browser refused create", "data": nested,
    }})
    result = S.open_new_tab("https://created.test/")
    assert result["status"] == "error"
    assert result["error"] == (nested_error or "browser refused create")
    assert result["may_have_created"] is False


@pytest.mark.parametrize("probe", [
    TimeoutError("client discovery timed out"),
    reply("unknown", operation_id="open-tab-pre-dispatch",
          may_have_created=True, retry_safe=False),
])
def test_predispatch_unknown_directs_a_fresh_create(create_driver, monkeypatch, probe):
    _, statuses, _, dispatched, _ = create_driver
    operation_tokens = iter(["pre-dispatch", "fixture"])
    monkeypatch.setattr(S.secrets, "token_urlsafe", lambda size: next(operation_tokens))
    statuses.append(probe)

    first = S.open_new_tab("https://created.test/", owner_id="owner")

    assert first["status"] == "unknown"
    assert first["may_have_created"] is False
    assert first["retry_safe"] is True
    assert [call[0] for call in dispatched] == ["status"]
    description = S.mcp._tool_manager.get_tool("open_new_tab").description
    assert "When retry_safe=true" in description
    assert "retry with no operation_id" in description

    statuses.append(reply("not_found"))
    retried = S.open_new_tab("https://created.test/", owner_id="owner")
    assert retried["status"] == "ok"
    assert retried["operation_id"] != first["operation_id"]
    creates = [call[1] for call in dispatched if call[0] == "create"]
    assert len(creates) == 1
    assert creates[0]["operation_id"] == retried["operation_id"]


def test_missing_recovery_record_directs_inspection_without_a_safe_create_claim(create_driver):
    _, statuses, _, dispatched, _ = create_driver
    statuses.append(reply("not_found", id=None, generation=None,
                          may_have_created=False, retry_safe=True))

    result = S.open_new_tab("https://created.test/", operation_id="open-tab-fixture",
                            client_id="chrome", owner_id="owner")

    assert result["status"] == "unknown"
    assert result["may_have_created"] is True
    assert result["retry_safe"] is False
    assert result["owner_id"] == "owner"
    assert result["owned"] is False
    assert result["session_id"] is None
    assert result["reconciliation"]["may_have_created"] is True
    assert result["reconciliation"]["retry_safe"] is False
    assert result["reconciliation"]["resume_required"] is False
    assert result["reconciliation"].get("new_operation_safe") is not True
    instruction = result["recovery"]["instruction"]
    assert "list_tabs()" in instruction
    assert "open_new_tab again with operation_id" not in instruction
    assert "close_tabs" in instruction
    assert "generation" in instruction
    assert "already registered as owned" in instruction
    assert "owner_id alone" in instruction
    assert "does not prove" in instruction
    assert "new create may duplicate" in instruction
    assert [call[0] for call in dispatched] == ["status"]
    assert S._TAB_OWNERSHIP.outstanding() == []


@pytest.mark.parametrize("probe", [
    TimeoutError("operation store did not respond"),
    reply("unknown", may_have_created=False, retry_safe=True),
])
def test_unreadable_recovery_record_keeps_read_only_recovery(create_driver, probe):
    _, statuses, _, dispatched, _ = create_driver
    statuses.append(probe)

    result = S.open_new_tab("https://created.test/", operation_id="open-tab-fixture",
                            client_id="chrome", owner_id="owner")

    assert result["may_have_created"] is True
    assert result["retry_safe"] is False
    assert result["owner_id"] == "owner"
    assert "open_new_tab again with operation_id" in result["recovery"]["instruction"]
    assert result["reconciliation"].get("new_operation_safe") is not True
    assert [call[0] for call in dispatched] == ["status"]


@pytest.mark.parametrize("status", ["not_found", "unknown"])
def test_pending_recovery_never_recreates_a_disappeared_operation(create_driver, status):
    _, statuses, _, dispatched, _ = create_driver
    statuses.extend([reply("pending"), reply(status)])
    result = S.open_new_tab("https://created.test/", operation_id="open-tab-fixture", owner_id="owner")
    assert result["status"] == "unknown"
    assert result["may_have_created"] is True
    assert result["owner_id"] == "owner"
    assert result["retry_safe"] is False
    assert "open_new_tab again with operation_id" in result["recovery"]["instruction"]
    assert result["reconciliation"].get("new_operation_safe") is not True
    assert all(call[0] == "status" for call in dispatched)


@pytest.mark.parametrize("retry", ["completed", "not_found", "unknown", "pending", "timeout", "error"])
def test_lost_create_ack_retries_only_the_same_durable_operation(create_driver, retry):
    _, statuses, creates, dispatched, _ = create_driver
    statuses.extend([reply("not_found"), reply("not_found")])
    creates.append(TimeoutError("lost ACK"))
    creates.append(TimeoutError("still lost") if retry == "timeout" else
                   RuntimeError("failed transport") if retry == "error" else reply(retry))
    if retry == "timeout":
        statuses.append(reply("not_found"))
    elif retry in {"pending", "error"}:
        statuses.append(reply("completed"))
    result = S.open_new_tab("https://created.test/", timeout=1)
    mutations = [call[1] for call in dispatched if call[0] == "create"]
    assert len(mutations) == 2
    assert {call["operation_id"] for call in mutations} == {"open-tab-fixture"}
    assert {call["client_id"] for call in mutations} == {"chrome"}
    if retry in {"completed", "pending", "error"}:
        assert result["owned"] is True
        assert result["session_id"] == "chrome:7"
    else:
        assert result["owned"] is False
        assert result["status"] == ("unknown" if retry == "unknown" else "error")


def test_failed_create_and_status_keep_a_resumable_capability(create_driver):
    _, statuses, creates, dispatched, _ = create_driver
    statuses.extend([reply("not_found"), RuntimeError("status failed")])
    creates.append(RuntimeError("create failed"))
    result = S.open_new_tab("https://created.test/", owner_id="owner")
    assert result["status"] == "unknown"
    assert result["recovery"]["owner_id"] == "owner"
    assert result["reconciliation"]["phase"] == "reconciliation"
    assert sum(call[0] == "create" for call in dispatched) == 1


def test_status_timeout_is_reconciled_without_replaying_pending_create(create_driver):
    _, statuses, creates, dispatched, _ = create_driver
    statuses.extend([reply("not_found"), TimeoutError("slow status"), reply("completed")])
    creates.append(reply("pending"))
    result = S.open_new_tab("https://created.test/", session_id="chrome:1")
    assert result["owned"] is True
    assert sum(call[0] == "create" for call in dispatched) == 1


def test_missing_content_session_does_not_erase_native_close_ownership(create_driver, monkeypatch):
    _, statuses, _, _, _ = create_driver
    statuses.append(reply("not_found"))
    def unavailable(**kwargs):
        raise RuntimeError("content registration unavailable")
    monkeypatch.setattr(S, "active_sessions", unavailable)
    result = S.open_new_tab("https://created.test/", timeout=0.2, owner_id="owner")
    assert result["ready"] is False
    assert result["owned"] is True
    assert result["generation"] == "g7"
    assert result["owner_id"] == "owner"
