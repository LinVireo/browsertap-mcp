"""Machine-readable result envelope coverage for the public MCP surface."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, ImageContent

from browsertap_mcp import server as S


def _call(name: str, arguments: dict):
    return asyncio.run(S.mcp.call_tool(name, arguments))


def _structured(result):
    if isinstance(result, tuple):
        return result[1]
    return result.structuredContent


def _install_page(monkeypatch, *, response):
    driver = SimpleNamespace(default_session_id="chrome:profile:1")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "ensure_sessions",
        lambda *args, **kwargs: [{"id": "chrome:profile:1", "url": "https://example.test/"}],
    )
    monkeypatch.setattr(
        S,
        "switch_session",
        lambda session_id=None, **kwargs: session_id or "chrome:profile:1",
    )
    monkeypatch.setattr(S, "exec_js", lambda *args, **kwargs: response)
    return driver


def test_page_success_has_common_envelope_and_target(monkeypatch):
    _install_page(
        monkeypatch,
        response={
            "data": json.dumps(
                {
                    "met": True,
                    "url": "https://example.test/",
                    "title": "Example",
                }
            )
        },
    )

    result = _call(
        "wait_for",
        {"text": "ready", "session_id": "chrome:profile:1", "timeout": 1},
    )
    structured = _structured(result)

    assert structured["ok"] is True
    assert structured["result_contract"] == "btap.result.v1"
    assert structured["version"] == 1
    assert structured["data"]["status"] == "success"
    assert structured["status"] == "success"  # legacy projection
    assert structured["error"] is None
    assert structured["target"] == {
        "session_id": "chrome:profile:1",
        "client_id": "chrome:profile",
        "tab_id": 1,
    }
    assert structured["diagnostics"]["tool"] == "wait_for"


def test_browser_success_without_target_is_enveloped(monkeypatch):
    driver = SimpleNamespace(default_session_id="chrome:profile:1")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: [{"id": "chrome:profile:1"}])

    _, structured = _call("list_tabs", {})

    assert structured["ok"] is True
    assert structured["data"]["tabs"] == [{"id": "chrome:profile:1"}]
    assert "tabs" not in structured
    assert structured["target"] is None
    assert structured["diagnostics"]["tool"] == "list_tabs"


def test_cdp_success_preserves_explicit_target(monkeypatch):
    class Driver:
        default_session_id = "chrome:profile:1"

        def ext_cmd(self, payload, **kwargs):
            assert payload["cmd"] == "cdp"
            return {"data": {"result": {"value": 7}}}

    monkeypatch.setattr(S, "require_driver", lambda: Driver())

    _, structured = _call(
        "cdp_command",
        {
            "method": "Runtime.evaluate",
            "params_json": "{}",
            "extension_id": "btap-extension",
            "session_id": "chrome:profile:1",
        },
    )

    assert structured["ok"] is True
    assert structured["data"]["data"]["result"]["value"] == 7
    assert structured["target"]["session_id"] == "chrome:profile:1"
    assert structured["target"]["client_id"] == "chrome:profile"
    assert structured["target"]["tab_id"] == 1


def test_legacy_failed_payload_is_error_with_full_legacy_copy(monkeypatch):
    _install_page(monkeypatch, response={"data": {"ok": False, "error": "quota exceeded"}})

    structured = _structured(_call(
        "storage_set",
        {"key": "setting", "value": "x", "session_id": "chrome:profile:1"},
    ))

    assert structured["ok"] is False
    assert structured["error_code"] == "failed"
    assert structured["error"]["message"] == "quota exceeded"
    assert structured["retryable"] is False
    assert structured["status"] == "failed"  # compatibility projection
    assert structured["legacy"]["status"] == "failed"
    assert structured["legacy"]["error"] == "quota exceeded"
    assert structured["target"]["session_id"] == "chrome:profile:1"


def test_partial_and_unknown_statuses_are_failures():
    for status in ("partial", "unknown"):
        structured = S._result_envelope("example_tool", {"status": status})
        assert structured["ok"] is False
        assert structured["error_code"] == status
        assert structured["legacy"]["status"] == status


def test_transformed_frame_refusal_is_a_structured_failure():
    structured = S._result_envelope(
        "page_click",
        {
            "status": "unsupported_frame_transform",
            "stage": "frame",
            "frame_transform": "matrix(1, 0, 0, 1, 12, 0)",
        },
    )

    assert structured["ok"] is False
    assert structured["error_code"] == "unsupported_frame_transform"
    assert structured["legacy"]["stage"] == "frame"


def test_success_status_with_error_field_is_not_misclassified():
    structured = S._result_envelope(
        "example_tool", {"status": "success", "error": "previous warning"}
    )

    assert structured["ok"] is True
    assert structured["data"]["error"] == "previous warning"


def test_nested_extension_failure_is_not_wrapped_as_success():
    result = S._extension_operation_result(
        {"data": {"status": "failed", "error": "permission denied", "code": "denied"}},
        operation="set_extension_enabled",
        extension_id="example-extension",
    )

    assert result["status"] == "failed"
    assert result["error"] == "permission denied"
    assert result["code"] == "denied"
    assert result["operation"] == "set_extension_enabled"
    assert result["extension_id"] == "example-extension"


def test_nested_explicit_extension_envelope_failure_is_not_success():
    result = S._extension_operation_result(
        {"data": {"ok": True, "data": {"ok": False, "error": "target failed"}}},
        operation="set_extension_enabled",
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"] == "target failed"
    assert result["operation"] == "set_extension_enabled"


def test_failed_call_tool_result_sets_mcp_is_error():
    value = CallToolResult(
        content=[],
        structuredContent={"status": "failed", "error": "boom"},
        isError=False,
    )

    adapted = S._adapt_tool_result("example_tool", value)

    assert adapted.structuredContent["ok"] is False
    assert adapted.isError is True


def test_setup_stale_statuses_are_diagnostic_successes():
    for status in ("stale_bridge", "stale_extension", "stale_package", "starting", "extension_unavailable"):
        structured = S._result_envelope(
            "get_setup_status", {"status": status, "action": "inspect"}
        )
        assert structured["ok"] is True
        assert structured["status"] == status


def test_setup_bridge_unreachable_remains_a_failure():
    structured = S._result_envelope(
        "get_setup_status", {"status": "bridge_unreachable", "action": "restart_bridge"}
    )

    assert structured["ok"] is False
    assert structured["error_code"] == "bridge_unreachable"


def test_bridge_no_response_keeps_delivery_diagnostics(monkeypatch):
    class Driver:
        default_session_id = "chrome:profile:1"

        def ext_cmd(self, payload, **kwargs):
            raise S.BridgeNoResponseError(
                "command was delivered but no result arrived",
                delivery_state="delivered_no_result",
                retry_safe=False,
                operation_id="op-123",
                reservation_held=True,
                poll_with="get_execute_js_result",
            )

    monkeypatch.setattr(S, "require_driver", lambda: Driver())

    structured = _structured(_call(
        "cdp_command",
        {
            "method": "Runtime.evaluate",
            "extension_id": "btap-extension",
            "session_id": "chrome:profile:1",
        },
    ))

    assert structured["ok"] is False
    assert structured["error_code"] == "no_response"
    assert structured["retryable"] is False
    assert structured["delivery_state"] == "delivered_no_result"
    assert structured["diagnostics"]["delivery_state"] == "delivered_no_result"
    assert structured["operation_id"] == "op-123"
    assert structured["reservation_held"] is True
    assert structured["poll_with"] == "get_execute_js_result"
    assert structured["diagnostics"]["operation_id"] == "op-123"
    assert structured["target"]["session_id"] == "chrome:profile:1"


def test_exec_js_preserves_bridge_operation_handle_on_no_response(monkeypatch):
    class Driver:
        default_session_id = "chrome:profile:1"

        def execute_js(self, script, *, timeout, session_id):
            return {
                "result": "delivered but no result",
                "error_code": "no_response",
                "delivery_state": "delivered_no_result",
                "retry_safe": False,
                "operation_id": "op-timeout",
                "reservation_held": True,
                "poll_with": "get_execute_js_result",
                "diagnostics": {"operation_id": "op-timeout"},
            }

    monkeypatch.setattr(S, "require_driver", lambda: Driver())

    with pytest.raises(S.BridgeNoResponseError) as caught:
        S.exec_js("return 1", session_id="chrome:profile:1", timeout=0.2)

    assert caught.value.operation_id == "op-timeout"
    assert caught.value.reservation_held is True
    assert caught.value.poll_with == "get_execute_js_result"
    assert caught.value.diagnostics["operation_id"] == "op-timeout"


def test_explicit_dead_session_is_refused_with_stable_code(monkeypatch):
    driver = SimpleNamespace(default_session_id="chrome:profile:1")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: [{"id": "chrome:profile:1"}])

    structured = _structured(_call(
        "execute_js",
        {"script": "1", "session_id": "chrome:profile:99", "timeout": 0.2},
    ))

    assert structured["ok"] is False
    assert structured["error_code"] == "session_not_connected"
    assert structured["retryable"] is False
    assert structured["target"] == {
        "session_id": "chrome:profile:99",
        "client_id": "chrome:profile",
        "tab_id": 99,
    }


def test_generic_timeout_is_structured(monkeypatch):
    monkeypatch.setattr(
        S,
        "_automation_profile",
        lambda: (_ for _ in ()).throw(TimeoutError("slow")),
    )

    structured = _structured(_call("get_automation_profile", {}))

    assert structured["ok"] is False
    assert structured["error_code"] == "timeout"
    assert structured["error"]["message"] == "slow"
    assert structured["retryable"] is False


def test_screenshot_keeps_image_content_and_envelope(monkeypatch):
    raw = b"not-a-real-image-but-valid-base64"
    encoded = base64.b64encode(raw).decode("ascii")

    class Driver:
        default_session_id = "chrome:profile:1"

        def ext_cmd(self, payload, **kwargs):
            return {"data": {"data": encoded}}

    driver = Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "ensure_sessions",
        lambda *args, **kwargs: [{"id": "chrome:profile:1"}],
    )
    monkeypatch.setattr(
        S,
        "switch_session",
        lambda session_id=None, **kwargs: session_id or driver.default_session_id,
    )

    result = _call(
        "capture_page_screenshot",
        {"session_id": "chrome:profile:1"},
    )

    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[1], ImageContent)
    structured = result.structuredContent
    assert structured["ok"] is True
    assert structured["result_contract"] == "btap.result.v1"
    assert structured["image_attached"] is True
    assert structured["data"]["image_attached"] is True
    assert "base64" not in structured


def test_all_registered_tools_declare_the_same_result_contract():
    assert len(S.TOOL_CAPABILITIES) == 49
    assert {
        metadata["result_contract"] for metadata in S.TOOL_CAPABILITIES.values()
    } == {"btap.result.v1"}
