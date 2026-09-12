"""Log categories and exception types, without caller scripts or approvals."""

import anyio
import pytest

from browsertap_mcp import server as S
from tests.test_execution_failure_contract import execution as execution
from tests.test_physical_input import _ApprovalContext


def test_invalid_timeout_configuration_does_not_log_raw_value(monkeypatch, caplog):
    secret = "synthetic-private-timeout-value"
    monkeypatch.setenv("BROWSERTAP_APPROVAL_TIMEOUT", secret)
    assert S._approval_timeout() == S._DEFAULT_APPROVAL_TIMEOUT
    assert secret not in caplog.text
    assert "BROWSERTAP_APPROVAL_TIMEOUT" in caplog.text
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("value", ["inf", "+Infinity", "nan", "-inf"])
def test_nonfinite_timeout_configuration_cannot_remove_approval_deadline(monkeypatch, value):
    monkeypatch.setenv("BROWSERTAP_APPROVAL_TIMEOUT", value)
    assert S._approval_timeout() == S._DEFAULT_APPROVAL_TIMEOUT


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["physical", "site_permission"])
async def test_approval_timeout_logs_only_category_reason_and_type(monkeypatch, caplog, kind):
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", "safe")
    secret = "synthetic-private-approval"
    context = _ApprovalContext(error=TimeoutError(secret))
    if kind == "physical":
        await S._request_physical_approval(context, secret)
    else:
        assert await S._request_site_permission_approval(
            context, secret, "https://synthetic-private-origin.test/", 300,
        ) is False
    assert secret not in caplog.text
    assert "synthetic-private-origin" not in caplog.text
    assert "timeout" in caplog.text.lower()
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)


def test_js_cleanup_log_does_not_replace_primary_mcp_error(execution, monkeypatch, caplog):
    driver, calls, _ = execution
    primary = "synthetic-private-script-primary"
    cleanup = "synthetic-private-cleanup-token"

    def run_script(*args, **kwargs):
        raise RuntimeError(primary)

    def extension(command, **kwargs):
        calls.append((command, kwargs))
        if command["cmd"] == "clear_dialog_policy":
            raise OSError(cleanup)
        return {"data": {"token": "scope"}}

    driver.ext_cmd = extension
    monkeypatch.setattr(S.simphtml, "execute_js_rich", run_script)
    result = anyio.run(S.mcp.call_tool, "execute_js", {
        "script": "return 'synthetic-private-input-token'",
        "session_id": "chrome:7",
    })
    structured = result.structuredContent if hasattr(result, "structuredContent") else result[1]
    assert structured["ok"] is False
    assert primary in structured["error"]["message"]
    assert [command["cmd"] for command, _ in calls] == [
        "set_dialog_policy", "clear_dialog_policy",
    ]
    assert primary not in caplog.text
    assert cleanup not in caplog.text
    assert "synthetic-private-input-token" not in caplog.text
    assert "OSError" in caplog.text
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)
