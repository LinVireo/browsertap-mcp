from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from agent_browser_mcp import simphtml as S


LONG_PATH = "/a/path/that/is/comfortably/longer/than/thirty/characters"


class QueueDriver:
    def __init__(self, responses=(), sessions=None, default_session_id=None):
        self.responses = list(responses)
        self.sessions = sessions if sessions is not None else {}
        self.default_session_id = default_session_id
        self.calls = []
        self.session_calls = []

    def execute_js(self, script, **kwargs):
        self.calls.append((script, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_session_dict(self, **kwargs):
        self.session_calls.append(kwargs)
        if isinstance(self.sessions, BaseException):
            raise self.sessions
        return self.sessions


def test_optimize_html_cleans_assets_attributes_and_accepts_soup_input():
    long_text = "x" * 120
    soup = BeautifulSoup(
        f"""
        <svg style="color:red" viewBox="0 0 1 1"><path d="x"/></svg>
        <img src="data:image/png;base64,abc" style="x" alt="{long_text}">
        <img src="https://example.test/{'a' * 40}.png">
        <form action="https://example.test/{'submit' * 8}">
          <input value="{long_text}" title="{long_text}" data-v-a="1"
                 data-long="{'z' * 30}" data-short="ok" onclick="bad()"
                 aria-label="kept">
        </form>
        """,
        "html.parser",
    )

    result = S.optimize_html_for_tokens(soup)

    assert result is soup
    assert not result.svg.contents and result.svg.attrs == {}
    assert result.find_all("img")[0]["src"] == "__img__"
    assert result.find_all("img")[1]["src"] == "__url__"
    assert result.form["action"] == "__url__"
    assert result.input["value"].endswith(" ...")
    assert result.input["title"].endswith(" ...")
    assert result.input["data-long"] == "__data__"
    assert result.input["data-short"] == "ok"
    assert "data-v-a" not in result.input.attrs
    assert "onclick" not in result.input.attrs
    assert result.input["aria-label"] == "kept"


def test_optimize_html_preserves_ref_when_url_join_fails(monkeypatch):
    refs = {}
    monkeypatch.setattr(S, "urljoin", lambda *_args: (_ for _ in ()).throw(ValueError("bad base")))
    soup = S.optimize_html_for_tokens(
        f'<a href="{LONG_PATH}">go</a>', link_refs=refs, base_url="bad://base"
    )
    assert soup.a["href"] == "#r1"
    assert refs == {LONG_PATH: "r1"}


def test_execute_in_session_and_temp_monitor_helpers(capsys):
    driver = QueueDriver([
        {"data": "plain"},
        {"data": "pinned"},
        {"data": None},
        {"data": ["one", "one", "two"]},
        RuntimeError("monitor gone"),
    ])

    assert S._execute_in_session(driver, "a", 2, custom=True) == {"data": "plain"}
    assert S._execute_in_session(driver, "b", 3, session_id="c:7") == {"data": "pinned"}
    S.start_temp_monitor(driver, timeout=4, session_id="c:7")
    assert set(S.get_temp_texts(driver, timeout=5, session_id="c:7")) == {"one", "two"}
    assert S.get_temp_texts(driver) == []
    assert driver.calls[0][1] == {"timeout": 2, "custom": True}
    assert driver.calls[1][1] == {"timeout": 3, "session_id": "c:7"}
    assert "monitor gone" in capsys.readouterr().out


def test_start_temp_monitor_swallows_driver_failure():
    driver = QueueDriver([RuntimeError("sleeping worker")])
    S.start_temp_monitor(driver, session_id="c:1")
    assert len(driver.calls) == 1


def test_get_main_block_forwards_options_and_normalizes_text():
    driver = QueueDriver([{"data": "  alpha   beta\n   gamma\n\n \n delta  "}])
    result = S.get_main_block(
        driver,
        extra_js="window.prepared = true;",
        text_only=True,
        timeout=7,
        allow_failover=True,
        session_id="chrome:9",
    )
    assert result == "alpha beta\ngamma\n\ndelta"
    script, kwargs = driver.calls[0]
    assert "window.prepared = true" in script
    assert "return optHTML(true)" in script
    assert kwargs == {"timeout": 7, "allow_failover": True, "session_id": "chrome:9"}


@pytest.mark.parametrize(
    "response, message",
    [
        ({"result": "bridge timed out"}, "bridge timed out"),
        ({}, "no data returned"),
        ({"data": None}, "returned null"),
    ],
)
def test_get_main_block_reports_unavailable_page(response, message):
    with pytest.raises(S.PageUnavailable, match=message):
        S.get_main_block(QueueDriver([response]))


def test_find_changed_elements_handles_new_duplicates_reorder_and_truncation():
    duplicate = S.find_changed_elements("<p>x</p>", "<p>x</p><p>x</p><b>new</b>")
    assert duplicate["changed"] == 2
    assert "top_change" in duplicate

    reordered = S.find_changed_elements("<p>a</p><p>b</p>", "<p>b</p><p>a</p>")
    assert reordered["changed"] == 2

    long_change = S.find_changed_elements("<main></main>", f"<main><section>{'z' * 2200}</section></main>")
    assert long_change["changed"] == 1
    assert long_change["top_change"].endswith("...[TRUNCATED]")

    assert S.find_changed_elements("<p>x</p>", "<p>x</p>") == {"changed": 0}


def test_get_html_resolves_base_refs_and_restores_iframe(monkeypatch):
    page = (
        "<!--tmwd-base:https://example.test/root/page-->"
        f'<div data-tag="iframe"><a href="{LONG_PATH}">open</a></div>'
    )
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: page)
    refs = {}
    html = S.get_html(object(), link_refs=refs, session_id="c:2")
    assert "tmwd-base" not in html
    assert "<iframe>" in html
    assert 'href="#r1"' in html
    assert refs == {f"https://example.test{LONG_PATH}": "r1"}


def test_get_html_text_only_returns_without_parsing(monkeypatch):
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: "plain text")
    monkeypatch.setattr(S, "optimize_html_for_tokens", lambda *_args, **_kwargs: pytest.fail("must not parse"))
    assert S.get_html(object(), text_only=True) == "plain text"


def _list_page(item_count=7, text_size=760):
    return "<main>" + "".join(
        f'<article class="item">item-{i} {"x" * text_size}</article>'
        for i in range(item_count)
    ) + "</main>"


def test_get_html_cutlist_keeps_instruction_hit_and_emits_hint(monkeypatch):
    page = _list_page().replace("item-5", "item-5 TARGET")
    driver = QueueDriver([{"data": [{"selector": ".item"}]}])
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: page)
    html = S.get_html(driver, cutlist=True, instruction="TARGET", maxchars=100_000)
    assert "TARGET" in html
    kept = BeautifulSoup(html, "html.parser").select(".item")
    assert len(kept) == 1 and "TARGET" in kept[0].get_text()
    assert "[FAKE ELEMENT] 6 more items hidden" in html


def test_get_html_cutlist_covers_invalid_small_and_default_selection(monkeypatch, capsys):
    page = _list_page(6) + "<div>" + "".join('<i class="few">x</i>' for _ in range(4)) + "</div>"
    candidates = [None, {}, {"selector": "["}, {"selector": ".few"}, {"selector": ".item"}]
    driver = QueueDriver([{"data": candidates}])
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: page)
    html = S.get_html(driver, cutlist=True, maxchars=100_000)
    kept = BeautifulSoup(html, "html.parser").select(".item")
    assert [item.get_text().split()[0] for item in kept] == ["item-0", "item-1", "item-2"]
    output = capsys.readouterr().out
    assert "skip invalid selector" in output
    assert "Found 5 list" in output


def test_get_html_handles_dict_candidate_empty_page_and_parse_cap(monkeypatch):
    driver = QueueDriver([{"data": {"selector": ".item"}}])
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: _list_page(5))
    assert "[FAKE ELEMENT]" in S.get_html(driver, cutlist=True, maxchars=100_000)

    empty_driver = QueueDriver([{"data": "not-a-list"}])
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: "")
    assert S.get_html(empty_driver, cutlist=True) == ""

    seen = []
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: "x" * 1_500_010)
    monkeypatch.setattr(
        S,
        "optimize_html_for_tokens",
        lambda page, **_kwargs: seen.append(len(page)) or BeautifulSoup("<p>ok</p>", "html.parser"),
    )
    assert S.get_html(object()) == "<p>ok</p>"
    assert seen == [1_500_000]


def test_get_html_invokes_smart_truncate_for_large_cutlist_result(monkeypatch):
    driver = QueueDriver([{"data": []}])
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: "<p>" + "x" * 500 + "</p>")
    calls = []
    monkeypatch.setattr(S, "smart_truncate", lambda soup, budget: calls.append((str(soup), budget)) or "CUT")
    assert S.get_html(driver, cutlist=True, maxchars=50) == "CUT"
    assert calls and calls[0][1] == 50


def test_smart_truncate_under_budget_and_text_only_are_noops():
    soup = BeautifulSoup("<p>short</p>", "html.parser")
    assert S.smart_truncate(soup, 1000) is soup
    text = BeautifulSoup("only text", "html.parser")
    assert S.smart_truncate(text, 0) is text


def test_smart_truncate_recurses_tail_cuts_and_protects_hint():
    nested = BeautifulSoup(f"<main><section><p>{'x' * 200}</p></section></main>", "html.parser")
    assert S.smart_truncate(nested, 20) is nested

    many = BeautifulSoup("<main>" + "".join(f"<p>{'x' * 100}</p>" for _ in range(6)) + "</main>", "html.parser")
    S.smart_truncate(many.main, 0)
    assert not many.main.find_all("p")

    balanced = BeautifulSoup(
        "<main>"
        + f"<section>{'a' * 900}<div>[FAKE ELEMENT] keep this</div></section>"
        + f"<section>{'b' * 900}</section>"
        + "</main>",
        "html.parser",
    )
    S.smart_truncate(balanced.main, 700)
    rendered = str(balanced)
    assert "[TRUNCATED" in rendered
    assert "[FAKE ELEMENT] keep this" in rendered


def test_smart_truncate_recurses_into_large_allocations():
    soup = BeautifulSoup(
        "<main>"
        + f"<section><div>{'a' * 12_000}</div><div>{'b' * 2_000}</div></section>"
        + f"<aside>{'c' * 2_000}</aside>"
        + "</main>",
        "html.parser",
    )
    S.smart_truncate(soup.main, 13_000)
    assert len(str(soup)) < 16_200


@pytest.mark.parametrize(
    "deadline, cap, expected",
    [(90.0, None, 0.0), (110.0, None, 10.0), (110.0, 4.0, 4.0)],
)
def test_remaining_honors_zero_and_cap(monkeypatch, deadline, cap, expected):
    monkeypatch.setattr(S.time, "monotonic", lambda: 100.0)
    assert S._remaining(deadline, cap) == expected


def test_execute_js_rich_handles_monitor_session_and_execution_failures(monkeypatch):
    driver = QueueDriver([Exception({"message": "boom", "stack": "secret"})], sessions=RuntimeError("sessions down"))
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("snapshot down")))
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("monitor down")))
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)

    result = S.execute_js_rich("explode()", driver, timeout=2, session_id="chrome:not-a-number")

    assert result["status"] == "failed"
    assert "boom" in result["error"] and "stack" not in result["error"]
    assert result["tab_id"] is None
    assert result["transients"] == []


def test_execute_js_rich_expired_deadline_never_calls_driver(monkeypatch):
    driver = QueueDriver([], default_session_id="no-colon")
    monkeypatch.setattr(S.time, "monotonic", lambda: 10.0)
    result = S.execute_js_rich(
        "return 1", driver, no_monitor=True, timeout=3, before_sids=None, deadline=5.0
    )
    assert result["status"] == "no_response"
    assert result["tab_id"] is None
    assert driver.calls == []


def test_execute_js_rich_retries_undelivered_and_reports_switch_and_tabs(monkeypatch):
    driver = QueueDriver(
        [
            {"result": "No response data in 2s (no ACK, script may not have been delivered)"},
            {
                "data": 7,
                "executed_tab_id": 42,
                "switched_session": "c:42",
                "switched_from": "c:1",
                "newTabs": [{"id": "c:8", "url": "https://new.test"}],
            },
        ]
    )
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    result = S.execute_js_rich(
        "return 7", driver, no_monitor=True, timeout=2, before_sids=set(), session_id="c:1"
    )
    assert len(driver.calls) == 2
    assert result["status"] == "success"
    assert result["tab_id"] == 42
    assert result["switched_session"] == "c:42"
    assert result["newTabs"][0]["id"] == "c:8"


def test_execute_js_rich_classifies_navigation_and_reads_landing(monkeypatch):
    driver = QueueDriver(
        [
            {"result": "Session c:5 reloaded.", "closed": 1},
            {"data": json.dumps({"url": "https://landed.test", "title": "Landed"})},
        ]
    )
    result = S.execute_js_rich(
        "location.href='x'", driver, no_monitor=True, timeout=2, before_sids=set(), session_id="c:5"
    )
    assert result["status"] == "navigated"
    assert result["reloaded"] is True
    assert result["landed_url"] == "https://landed.test"
    assert result["landed_title"] == "Landed"


def test_execute_js_rich_navigation_location_failure_is_best_effort(capsys):
    driver = QueueDriver(
        [
            {"result": "Session c:5 reloaded.", "closed": 1},
            RuntimeError("location unavailable"),
        ]
    )
    result = S.execute_js_rich(
        "location.href='x'", driver, no_monitor=True, timeout=2, before_sids=set(), session_id="c:5"
    )
    assert result["status"] == "navigated"
    assert "landed_url" not in result
    assert "location unavailable" in capsys.readouterr().out


def test_execute_js_rich_returns_blocked_dialog_with_new_tabs():
    response = {
        "data": {"__tmwd_dialog_result": True, "status": "blocked_by_dialog"},
        "newTabs": [{"id": "c:2"}],
        "executed_tab_id": 1,
    }
    result = S.execute_js_rich(
        "click()",
        QueueDriver([response]),
        no_monitor=True,
        timeout=2,
        before_sids=set(),
        session_id="c:1",
    )
    assert result["js_return"]["status"] == "blocked_by_dialog"
    assert result["newTabs"] == [{"id": "c:2"}]


def test_execute_js_rich_detects_new_sessions(monkeypatch):
    driver = QueueDriver([{"data": "ok"}], sessions={"c:1": "old", "c:2": "new"})
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: "<p>same</p>")
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: ["flash"])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    result = S.execute_js_rich(
        "open()", driver, timeout=2, before_sids={"c:1"}, session_id="c:1"
    )
    assert result["newTabs"] == [{"id": "c:2", "url": "new"}]
    assert "页面已刷新" in result["suggestion"]


def test_execute_js_rich_reports_dom_diff_and_no_change(monkeypatch):
    driver = QueueDriver([{"data": "ok"}], sessions={"c:1": "old"})
    pages = iter(["<main><p>old</p></main>", "<main><p>new</p></main>"])
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: next(pages))
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: ["toast"])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    changed = S.execute_js_rich("change()", driver, timeout=2, before_sids={"c:1"}, session_id="c:1")
    assert "DOM变化量" in changed["diff"]
    assert "最显著变化" in changed["diff"]

    driver = QueueDriver([{"data": "ok"}], sessions={"c:1": "old"})
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: "<p>same</p>")
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: [])
    unchanged = S.execute_js_rich("noop()", driver, timeout=2, before_sids={"c:1"}, session_id="c:1")
    assert "页面无变化" in unchanged["diff"]
    assert unchanged["suggestion"] == "页面无明显变化"


def test_execute_js_rich_marks_diff_unavailable(monkeypatch):
    driver = QueueDriver([{"data": "ok"}], sessions={})
    pages = iter(["<p>before</p>", RuntimeError("after failed")])

    def get_html(*_args, **_kwargs):
        value = next(pages)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(S, "get_html", get_html)
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    result = S.execute_js_rich("noop()", driver, timeout=2, before_sids=set())
    assert result["diff"] == "页面变化监控不可用"


def test_execute_js_rich_after_ack_does_not_retry():
    driver = QueueDriver([{"result": "No response data in 2s (ACK received, script may still be running)"}])
    result = S.execute_js_rich("slow()", driver, no_monitor=True, timeout=2, before_sids=set())
    assert result["status"] == "no_response"
    assert len(driver.calls) == 1
    assert "勿盲目重试" in result["suggestion"]
