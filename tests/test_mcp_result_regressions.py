"""Exercise result semantics through the registered lowlevel MCP handler."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolResult, ImageContent, TextContent

from browsertap_mcp import server as S


def _wire_call(mcp, name, arguments=None):
    request = CallToolRequest(params={"name": name, "arguments": arguments or {}})
    handler = mcp._mcp_server.request_handlers[CallToolRequest]
    result = asyncio.run(handler(request)).root
    assert isinstance(result, CallToolResult)
    return result


@pytest.fixture
def profile_reply(monkeypatch):
    def reply(payload=None, *, exc=None):
        def implementation():
            if exc is not None:
                raise exc
            return payload

        monkeypatch.setattr(S, "_automation_profile", implementation)
        return _wire_call(S.mcp, "get_automation_profile")

    return reply


@pytest.mark.parametrize("status", [
    "failed", "partial", "unknown", "not_interactable", "focus_failed",
    "ambiguous", "invalid_selector", "cross_origin_frame", "closed_shadow_root",
])
def test_dictionary_failure_reaches_the_mcp_error_flag(profile_reply, status):
    payload = {"status": status}

    result = profile_reply(payload)

    assert result.isError is True
    assert result.structuredContent["ok"] is False
    assert result.structuredContent["legacy"] == payload
    assert json.loads(result.content[0].text) == result.structuredContent


def test_storage_write_failure_is_an_mcp_error(monkeypatch):
    monkeypatch.setattr(
        S, "exec_js", lambda *args, **kwargs: {"data": {"ok": False, "error": "quota exceeded"}},
    )

    result = _wire_call(
        S.mcp, "storage_set", {"key": "setting", "value": "x", "session_id": "chrome_test:42"},
    )

    assert result.isError is True
    assert result.structuredContent["error"]["message"] == "quota exceeded"
    assert result.structuredContent["target"]["session_id"] == "chrome_test:42"


@pytest.mark.parametrize("serialize", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_exceptions_are_mcp_errors_for_each_runner(monkeypatch, serialize, asynchronous):
    mcp = FastMCP("result-regression")
    monkeypatch.setattr(S, "_mcp_tool", mcp.tool)

    def fail():
        raise S.SessionTargetNotFoundError("chrome_test:42", [])

    if asynchronous:
        async def probe() -> dict:
            return fail()
    else:
        def probe() -> dict:
            return fail()

    S._threaded_tool(serialize=serialize)(probe)

    result = _wire_call(mcp, "probe")

    assert result.isError is True
    envelope = result.structuredContent
    assert envelope["error_code"] == "session_not_connected"
    assert envelope["retryable"] is False
    assert envelope["diagnostics"]["retry_safe"] is False
    assert json.loads(result.content[0].text) == envelope


@pytest.mark.parametrize("facts", [
    {"retry_safe": False, "retryable": True},
    {"retry_safe": True, "retryable": False},
    {"retry_safe": True, "delivery_state": "sent_unconfirmed"},
    {"retry_safe": True, "delivery_state": "delivered_no_result"},
    {"retry_safe": True, "delivery_state": "navigated"},
    {"retry_safe": True, "delivery_state": "unknown"},
    {"retry_safe": True, "dispatched": True},
    {"retry_safe": True, "may_have_executed": True},
    {"retry_safe": True, "may_have_created": True},
    {"retry_safe": True, "diagnostics": {"retry_safe": False}},
    {"retry_safe": True, "diagnostics": {"may_have_executed": True}},
])
@pytest.mark.parametrize("exception", [False, True])
def test_delivery_facts_and_explicit_false_prevent_retry(profile_reply, facts, exception):
    if exception:
        exc = ConnectionError("reply lost")
        for key, value in facts.items():
            setattr(exc, key, value)
        result = profile_reply(exc=exc)
    else:
        result = profile_reply({"status": "error", "code": "transport_error", **facts})

    assert result.isError is True
    envelope = result.structuredContent
    assert envelope["retryable"] is False
    assert envelope["error"]["retryable"] is False
    assert envelope.get("retry_safe", False) is False
    assert envelope["diagnostics"].get("retry_safe", False) is False
    assert envelope["diagnostics"].get("retryable", False) is False


def test_proven_undelivered_failure_can_still_retry(profile_reply):
    result = profile_reply(exc=S.BridgeNoResponseError(
        "not dispatched", delivery_state="undelivered", retry_safe=True,
    ))

    assert result.isError is True
    assert result.structuredContent["retryable"] is True
    assert result.structuredContent["diagnostics"]["delivery_state"] == "undelivered"


@pytest.mark.parametrize("status,body_key,is_error", [
    ("success", "data", False),
    ("failed", "legacy", True),
])
def test_large_bodies_are_not_duplicated_by_compatibility_projection(
    profile_reply, status, body_key, is_error,
):
    html = "<article>unique-page-body" + "x" * 16000 + "</article>"
    payload = {
        "status": status,
        "html": html,
        "tabs": [{"id": "chrome_test:42", "title": "unique-tab-title"}],
        "result": {"rows": ["unique-query-row"]},
        "count": 1,
    }

    result = profile_reply(payload)

    assert result.isError is is_error
    envelope = result.structuredContent
    assert envelope[body_key] == payload
    assert envelope["status"] == status
    assert envelope["count"] == 1
    assert all(key not in envelope for key in ("html", "tabs", "result"))
    encoded = json.dumps(envelope)
    for marker in ("unique-page-body", "unique-tab-title", "unique-query-row"):
        assert encoded.count(marker) == 1
    assert json.loads(result.content[0].text) == envelope


def test_successful_mcp_image_keeps_content_and_metadata(profile_reply):
    original = CallToolResult(
        _meta={"trace": "capture"},
        content=[
            TextContent(type="text", text="Screenshot attached"),
            ImageContent(type="image", data="dGVzdA==", mimeType="image/png"),
        ],
        structuredContent={"status": "success", "image_attached": True},
    )

    result = profile_reply(original)

    assert result.isError is False
    assert result.meta == original.meta
    assert result.content == original.content
    assert result.structuredContent["data"] == original.structuredContent
