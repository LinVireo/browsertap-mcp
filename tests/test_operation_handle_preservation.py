"""A failed read must still name the operation it left running.

Two independent losses used to turn one timeout into an unusable tab:
`PageUnavailable` carried a message and nothing else, and the dict-shaped
failure envelope kept `operation_id` while dropping how to use it. The result
an operator reported was `no_response` followed by `target_busy` with no id to
poll.
"""

from __future__ import annotations

import pytest

from browsertap_mcp import server as S
from browsertap_mcp import simphtml

TIMEOUT_REPLY = {
    "result": "No response data in 15s (ACK received, script may still be running)",
    "error_code": "no_response",
    "delivery_state": "delivered_no_result",
    "retry_safe": False,
    "operation_id": "op-scan-1",
    "reservation_held": True,
}


class _Driver:
    """Answers every roundtrip with the same no-data timeout reply."""

    default_session_id = "chrome:profile:1"

    def __init__(self, reply):
        self.reply = reply

    def execute_js(self, _script, **_kwargs):
        return dict(self.reply)


def _install_scan(monkeypatch, *, reply):
    driver = _Driver(reply)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda *a, **k: [{"id": "chrome:profile:1"}])
    monkeypatch.setattr(S, "switch_session", lambda session_id=None, **k: session_id or "chrome:profile:1")
    monkeypatch.setattr(S, "compact_tabs", lambda *a, **k: [{"id": "chrome:profile:1"}])
    return driver


def test_page_unavailable_carries_the_bridge_operation_handle():
    with pytest.raises(simphtml.PageUnavailable) as caught:
        simphtml._page_from_response(dict(TIMEOUT_REPLY))

    error = caught.value
    assert error.payload["operation_id"] == "op-scan-1"
    assert error.payload["reservation_held"] is True
    assert error.payload["delivery_state"] == "delivered_no_result"
    assert error.payload["retry_safe"] is False
    # Attribute form too: the server's exception path reads these off the
    # exception, not out of a dict.
    assert error.operation_id == "op-scan-1"
    assert error.poll_with == "get_execute_js_result"
    assert "get_execute_js_result" in str(error)


def test_poll_with_is_supplied_only_alongside_an_id():
    with pytest.raises(simphtml.PageUnavailable) as caught:
        simphtml._page_from_response({"result": "bridge timed out"})

    assert caught.value.payload == {}
    assert not hasattr(caught.value, "operation_id")
    assert "get_execute_js_result" not in str(caught.value)


def test_null_data_reply_has_no_operation_to_collect():
    # A reply arrived, so nothing is still in flight; inventing a handle here
    # would name an operation that is already settled.
    with pytest.raises(simphtml.PageUnavailable, match="returned null") as caught:
        simphtml._page_from_response({"data": None, "operation_id": "op-settled"})

    assert caught.value.payload == {}


def test_get_main_block_propagates_the_handle():
    with pytest.raises(simphtml.PageUnavailable) as caught:
        simphtml.get_main_block(_Driver(TIMEOUT_REPLY))

    assert caught.value.payload["operation_id"] == "op-scan-1"


@pytest.mark.parametrize("cutlist", [False, True])
def test_scan_page_timeout_reports_a_pollable_operation(monkeypatch, cutlist):
    _install_scan(monkeypatch, reply=TIMEOUT_REPLY)

    result = S.scan_page(session_id="chrome:profile:1", cutlist=cutlist)

    assert result["status"] == "no_response"
    assert result["operation_id"] == "op-scan-1"
    assert result["poll_with"] == "get_execute_js_result"
    assert result["reservation_held"] is True
    assert result["delivery_state"] == "delivered_no_result"
    assert result["retry_safe"] is False


def test_scan_page_without_a_handle_invents_neither_id_nor_poll(monkeypatch):
    _install_scan(monkeypatch, reply={"result": "bridge timed out"})

    result = S.scan_page(session_id="chrome:profile:1")

    assert result["status"] == "no_response"
    assert "operation_id" not in result
    assert "poll_with" not in result
    assert "reservation_held" not in result


def test_scan_page_envelope_exposes_the_handle_in_diagnostics(monkeypatch):
    _install_scan(monkeypatch, reply=TIMEOUT_REPLY)

    envelope = S._result_envelope("scan_page", S.scan_page(session_id="chrome:profile:1"))

    assert envelope["ok"] is False
    assert envelope["error_code"] == "no_response"
    diagnostics = envelope["diagnostics"]
    assert diagnostics["operation_id"] == "op-scan-1"
    assert diagnostics["poll_with"] == "get_execute_js_result"
    assert diagnostics["reservation_held"] is True
    assert diagnostics["delivery_state"] == "delivered_no_result"
    # The compatibility copy keeps them readable at the top level as well.
    assert envelope["operation_id"] == "op-scan-1"
    assert envelope["poll_with"] == "get_execute_js_result"


def test_operation_status_reaches_diagnostics_from_a_dict_failure():
    # The exception path already reported operation_status; the dict path is
    # what this whitelist entry exists for.
    diagnostics = S._result_diagnostics(
        {"status": "no_response", "operation_id": "op-9", "operation_status": "outcome_unknown"},
        tool="scan_page",
    )

    assert diagnostics["operation_status"] == "outcome_unknown"
    assert diagnostics["operation_id"] == "op-9"


def test_page_content_never_reaches_the_handle_payload():
    # Only routing facts travel. A page-sized value on the envelope would
    # double an agent's context cost for a failed read, so the filter lives in
    # the exception itself rather than at each raise site.
    error = simphtml.PageUnavailable("x", payload={
        "result": "timed out",
        "operation_id": "op-1",
        "data": "<html>" + "x" * 5000 + "</html>",
        "html": "y" * 5000,
    })

    assert set(error.payload) == {"operation_id", "poll_with"}


def test_scan_page_restores_the_previous_default_target(monkeypatch):
    driver = _install_scan(monkeypatch, reply=TIMEOUT_REPLY)
    driver.default_session_id = "chrome:profile:old"

    S.scan_page(session_id="chrome:profile:1")

    assert driver.default_session_id == "chrome:profile:old"


def test_exception_metadata_reads_the_same_fields(monkeypatch):
    # scan_page returns a dict, but nothing stops another caller from letting
    # PageUnavailable escape. The attribute form must serve that path too.
    error = simphtml.PageUnavailable("no answer", payload=dict(TIMEOUT_REPLY))
    code, _message, retryable, diagnostics = S._exception_result_metadata(error)

    assert code == "no_response"
    assert retryable is False
    assert diagnostics["operation_id"] == "op-scan-1"
    assert diagnostics["poll_with"] == "get_execute_js_result"
    assert diagnostics["reservation_held"] is True


@pytest.mark.parametrize("payload", [None, "timed out", 42, []])
def test_handle_payload_ignores_a_non_dict(payload):
    assert simphtml.PageUnavailable("no answer", payload=payload).payload == {}


def test_scan_page_success_carries_no_operation_noise(monkeypatch):
    _install_scan(monkeypatch, reply={"data": "<main>ok</main>"})
    monkeypatch.setattr(S.simphtml, "get_html", lambda *a, **k: "ok")
    monkeypatch.setattr(S, "_render_probe", lambda *a, **k: None, raising=False)

    result = S.scan_page(session_id="chrome:profile:1")

    assert result["status"] == "success"
    assert "operation_id" not in result
    assert "poll_with" not in result
