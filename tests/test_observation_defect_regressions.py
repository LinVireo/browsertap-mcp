"""Observation failures must not turn into successful empty observations."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from browsertap_mcp import simphtml as S
from tests.test_simphtml_coverage import QueueDriver


def test_svg_keeps_accessible_names_and_descriptions_without_geometry():
    soup = S.optimize_html_for_tokens(
        '<button><svg role="img" aria-label="Next page" aria-hidden="false" '
        'aria-labelledby="icon-title icon-desc" title="Next" class="next-icon" '
        'viewBox="0 0 20 20" onclick="bad()">'
        '<title id="icon-title">Next page</title>'
        '<desc id="icon-desc">Move to the next results</desc>'
        '<g><path d="M 0 0 L 10 10"/><circle r="5"/></g>'
        '</svg></button>'
    )
    icon = soup.svg
    assert icon["role"] == "img"
    assert icon["aria-label"] == "Next page"
    assert icon["aria-hidden"] == "false"
    assert icon["aria-labelledby"] == "icon-title icon-desc"
    assert icon["title"] == "Next"
    assert icon.title["id"] == "icon-title"
    assert icon.title.get_text() == "Next page"
    assert icon.desc["id"] == "icon-desc"
    assert icon.desc.get_text() == "Move to the next results"
    assert not icon.find_all(["g", "path", "circle"])
    assert "viewbox" not in icon.attrs and "onclick" not in icon.attrs


def test_deep_single_child_html_truncates_without_python_recursion():
    depth = 1200
    markup = "<div>" * depth + "<p>" + "content " * 4000 + "</p>" + "</div>" * depth
    soup = BeautifulSoup(markup, "html.parser")
    assert S.smart_truncate(soup, 18000) is soup
    assert len(str(soup)) < len(markup)
    assert "[TRUNCATED" in soup.get_text()
    assert len(soup.find_all("div")) == depth


def test_deep_branching_html_keeps_large_content_without_python_recursion():
    depth = 1050
    markup = (
        "<section><i>x</i>" * depth
        + "<p>" + "content " * 7500 + "</p>"
        + "</section>" * depth
    )
    soup = BeautifulSoup(markup, "html.parser")
    assert S.smart_truncate(soup, 40000) is soup
    assert len(str(soup)) < len(markup)
    assert "[TRUNCATED" in soup.get_text()
    assert len(soup.find_all("section")) == depth


@pytest.mark.parametrize("response", [
    {},
    {"delivery_state": "sent_unconfirmed", "operation_id": "monitor-read"},
    {"data": None},
    {"data": "not a text list"},
    {"data": ["text", 3]},
])
def test_transient_reader_rejects_missing_or_invalid_observations(response):
    with pytest.raises(S.PageUnavailable):
        S.get_temp_texts(QueueDriver([response]), session_id="test:7")


def test_transient_reader_preserves_transport_failure():
    with pytest.raises(RuntimeError, match="monitor response lost"):
        S.get_temp_texts(QueueDriver([RuntimeError("monitor response lost")]))


@pytest.mark.parametrize("transient_response", [
    RuntimeError("monitor response lost"),
    {"delivery_state": "sent_unconfirmed"},
    {"data": None},
    {},
])
def test_unavailable_transients_do_not_prove_no_page_changes(monkeypatch, transient_response):
    driver = QueueDriver([{"data": 7}, transient_response])
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: "<p>same</p>")
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    result = S.execute_js_rich(
        "submit_once()", driver, timeout=5, before_sids=set(), session_id="test:7",
    )
    assert result["status"] == "success" and result["js_return"] == 7
    assert "no page changes" not in result["diff"]
    assert result.get("suggestion") != "No visible page changes were detected."
    assert result["transients"] == []
    assert result["transients_available"] is False
    assert sum(script == "submit_once()" for script, _ in driver.calls) == 1


def test_successful_empty_transient_observation_can_prove_no_page_changes(monkeypatch):
    driver = QueueDriver([{"data": 7}, {"data": []}])
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: "<p>same</p>")
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    result = S.execute_js_rich("noop()", driver, timeout=5, before_sids=set())
    assert result["transients"] == []
    assert result["transients_available"] is True
    assert "no page changes" in result["diff"]


@pytest.mark.parametrize("response", [
    {},
    {"result": 42},
    {"result": "unrecognised reply"},
    {"delivery_state": "future_unknown_state"},
    {
        "delivery_state": "future_unknown_state", "retry_safe": True,
        "result": "script not polled", "operation_id": "original-operation",
    },
    {"delivery_state": {"future": True}, "result": "script not polled"},
])
def test_missing_returns_and_unknown_delivery_evidence_never_replay(response):
    driver = QueueDriver([response])
    result = S.execute_js_rich(
        "submit_once()", driver, no_monitor=True, timeout=5, before_sids=set(),
    )
    assert S.no_response_kind(response) == "after_ack"
    assert result["status"] == "no_response"
    assert result["retry_safe"] is False
    assert result["btap_retried"] is False
    assert len(driver.calls) == 1
    assert result["delivery_state"] == response.get("delivery_state", "sent_unconfirmed")
    if response.get("operation_id"):
        assert result["operation_id"] == response["operation_id"]
        assert result["poll_with"] == "get_execute_js_result"


@pytest.mark.parametrize("value", [None, 0, 2, ""])
def test_explicit_data_is_a_return_even_with_unrecognised_metadata(value):
    response = {"data": value, "delivery_state": "future_state", "result": "script not polled"}
    driver = QueueDriver([response])
    result = S.execute_js_rich(
        "read_once()", driver, no_monitor=True, timeout=5, before_sids=set(),
    )
    assert S.no_response_kind(response) is None
    assert result["status"] == "success"
    assert result["js_return"] == value
    assert len(driver.calls) == 1
