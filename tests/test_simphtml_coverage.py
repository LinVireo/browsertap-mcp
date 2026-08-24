from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

import pytest
from bs4 import BeautifulSoup

from browsertap_mcp import simphtml as S

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


def test_execute_in_session_and_temp_monitor_helpers(caplog):
    caplog.set_level("DEBUG", logger="browsertap_mcp.simphtml")
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
    assert "monitor gone" in caplog.text


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
        "<!--btap-base:https://example.test/root/page-->"
        f'<div data-tag="iframe"><a href="{LONG_PATH}">open</a></div>'
    )
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: page)
    refs = {}
    html = S.get_html(object(), link_refs=refs, session_id="c:2")
    assert "btap-base" not in html
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


def test_get_html_cutlist_covers_invalid_small_and_default_selection(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="browsertap_mcp.simphtml")
    page = _list_page(6) + "<div>" + "".join('<i class="few">x</i>' for _ in range(4)) + "</div>"
    candidates = [None, {}, {"selector": "["}, {"selector": ".few"}, {"selector": ".item"}]
    driver = QueueDriver([{"data": candidates}])
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: page)
    html = S.get_html(driver, cutlist=True, maxchars=100_000)
    kept = BeautifulSoup(html, "html.parser").select(".item")
    assert [item.get_text().split()[0] for item in kept] == ["item-0", "item-1", "item-2"]
    output = caplog.text
    assert "skipped invalid selector" in output
    assert "cutlist found 5 list" in output


def test_find_main_list_marks_containers_without_touching_id():
    """findMainList runs against the user's real DOM, so what it writes matters.

    It used to mint an id (`_ljq<n>`) on any container that lacked one. An
    injected id is visible to `document.getElementById`, to `#id` rules in the
    page's own stylesheet, to `:target` and to anything that serialises the
    document, so the mark is a namespaced data attribute now. The write cannot
    simply be dropped: the selector this function returns is matched against the
    snapshot `get_main_block` takes on a *second* round trip, and the pruning
    clone in `js_optHTML` renumbers siblings on purpose, so an attribute is the
    only handle that crosses. Reverse gate for the id, forward gate for the mark.
    """
    source = S.js_findMainList
    assert "_ljq" not in source
    assert "data-btap-list" in source
    # No assignment to a `.id` property anywhere in the function, however it is
    # spelled. Reads (`container.id`, `cId`) are fine and still present.
    assert re.search(r"\.id\s*=(?!=)", source) is None
    assert "container.id" in source


def test_find_main_list_is_syntactically_valid_javascript(tmp_path):
    """No linter looks at the JavaScript embedded in this module (only ruff runs,
    and it sees a Python string), so a syntax error here would ship and surface
    as a runtime failure in the caller's browser. `node --check` is the cheapest
    gate that would catch it."""
    script = tmp_path / "find_main_list.js"
    script.write_text(S.js_findMainList, encoding="utf-8")
    completed = subprocess.run(
        ["node", "--check", str(script)], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr.strip()


def test_get_html_cutlist_matches_a_data_attribute_prefixed_selector(monkeypatch):
    """The container mark has to survive into the snapshot to be worth writing.

    `optimize_html_for_tokens` strips most attributes; it keeps `data-*` whose
    value is 20 characters or fewer, which is why the mark is a short counter.
    This runs the whole cutlist path with the selector shape `describeResult`
    now produces, so a change to that allowlist fails here rather than silently
    turning every cutlist selector into a miss.
    """
    page = (
        '<main data-btap-list="1">'
        + "".join(
            f'<article class="item">item-{i} {"x" * 800}</article>' for i in range(7)
        )
        + "</main>"
    )
    selector = '[data-btap-list="1"] > .item'
    driver = QueueDriver([{"data": [{"selector": selector}]}])
    monkeypatch.setattr(S, "get_main_block", lambda *_args, **_kwargs: page)
    html = S.get_html(driver, cutlist=True, maxchars=100_000)
    soup = BeautifulSoup(html, "html.parser")
    # The mark itself crossed the pipeline...
    assert soup.select_one('[data-btap-list="1"]') is not None
    # ...so the prefixed selector still resolves, and the cut actually happened.
    assert len(soup.select(selector)) == 3
    assert "[FAKE ELEMENT] 4 more items hidden" in html


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


def test_execute_js_rich_navigation_location_failure_is_best_effort(caplog):
    caplog.set_level("DEBUG", logger="browsertap_mcp.simphtml")
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
    assert "location unavailable" in caplog.text


def test_execute_js_rich_returns_blocked_dialog_with_new_tabs():
    response = {
        "data": {"__btap_dialog_result": True, "status": "blocked_by_dialog"},
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
    assert "page refreshed" in result["suggestion"]


def test_execute_js_rich_reports_dom_diff_and_no_change(monkeypatch):
    driver = QueueDriver([{"data": "ok"}], sessions={"c:1": "old"})
    pages = iter(["<main><p>old</p></main>", "<main><p>new</p></main>"])
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: next(pages))
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: ["toast"])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)
    changed = S.execute_js_rich("change()", driver, timeout=2, before_sids={"c:1"}, session_id="c:1")
    assert "DOM changes" in changed["diff"]
    assert "Most significant change" in changed["diff"]

    driver = QueueDriver([{"data": "ok"}], sessions={"c:1": "old"})
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: "<p>same</p>")
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: [])
    unchanged = S.execute_js_rich("noop()", driver, timeout=2, before_sids={"c:1"}, session_id="c:1")
    assert "no page changes" in unchanged["diff"]
    assert unchanged["suggestion"] == "No visible page changes were detected."


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
    assert result["diff"] == "Page-change monitoring is unavailable."


def test_execute_js_rich_after_ack_does_not_retry():
    driver = QueueDriver([{"result": "No response data in 2s (ACK received, script may still be running)"}])
    result = S.execute_js_rich("slow()", driver, no_monitor=True, timeout=2, before_sids=set())
    assert result["status"] == "no_response"
    assert result["delivery_state"] == "delivered_no_result"
    assert result["retry_safe"] is False
    assert len(driver.calls) == 1
    assert "before retrying side effects" in result["suggestion"]
    assert "wait_for" in result["suggestion"]


def _run_temp_monitor_harness(body: str) -> dict:
    """Drive the injected monitor script under node with a fake DOM and clock.

    Real timers would make this slow and flaky, so `setInterval` is replaced by
    a manual queue and `Date.now` by a settable counter: `advance(ms)` moves the
    clock and fires one tick, which is all the expiry logic needs.
    """
    harness = """
        let now = 1000000;
        Date.now = () => now;
        const timers = new Map();
        let nextTimerId = 1;
        globalThis.setInterval = (fn) => { const id = nextTimerId++; timers.set(id, fn); return id; };
        globalThis.clearInterval = (id) => { timers.delete(id); };
        function advance(ms) { now += ms; for (const [id, fn] of [...timers]) if (timers.has(id)) fn(); }
        const textNodes = [{textContent: '  a transient toast message  '}];
        globalThis.NodeFilter = {SHOW_TEXT: 4};
        globalThis.document = {
            body: {},
            createTreeWalker: () => { let i = 0; return {nextNode: () => (i < textNodes.length ? textNodes[i++] : null)}; },
        };
        globalThis.window = globalThis;
        SCRIPT_UNDER_TEST
        BODY
    """
    # The builder appends its own `startStrMonitor(...)` call; each test drives
    # the function directly so it can control the lifetime it passes.
    declaration = S.build_temp_monitor_js(10).replace("startStrMonitor(450, 10000);", "")
    script = harness.replace("SCRIPT_UNDER_TEST", declaration).replace("BODY", body)
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
        completed = subprocess.run(["node", path], capture_output=True, text=True)
        if completed.returncode:
            raise AssertionError(f"node harness failed: {completed.stderr.strip()}")
        return json.loads(completed.stdout)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def test_temp_monitor_stops_itself_once_its_lifetime_is_up():
    """The monitor's own expiry is the only cleanup on two of its paths.

    `execute_js_rich` skips the Python-side stop when the deadline is exhausted
    and when the transient read raises, and both of those are states in which
    there is no budget left to send anything. Whatever ships here therefore has
    to stop the 450ms full-document TreeWalker without another roundtrip, or it
    keeps running on a real page for as long as the document lives.
    """
    report = _run_temp_monitor_harness(
        """
        startStrMonitor(450, 10000);
        const started = {live: !!window.__btap_tm, timers: timers.size};
        advance(500);
        const collecting = {live: !!window.__btap_tm, seen: [...window.__btap_tm.all].length};
        advance(10000);
        console.log(JSON.stringify({
            started, collecting,
            afterExpiry: {live: !!window.__btap_tm, timers: timers.size},
        }));
        """
    )
    assert report["started"] == {"live": True, "timers": 1}
    assert report["collecting"]["live"] is True
    assert report["collecting"]["seen"] == 1
    # Both halves matter: a cleared interval that leaves the object behind is
    # still residue on the page, and a deleted object whose interval survives
    # keeps walking the document forever.
    assert report["afterExpiry"] == {"live": False, "timers": 0}


def test_a_superseded_temp_monitor_clears_itself_and_not_the_newcomer():
    """A stale interval must never reach through the global at a live monitor.

    The tick reads `window.__btap_tm` back out of the page, so an interval that
    has been superseded is looking at somebody else's object. Comparing the
    captured handle against `m.id` is what keeps it from clearing the newcomer
    and deleting a monitor a later call is about to read. This harness keeps the
    first tick registered on purpose -- the real `clearInterval` in the
    replacement path is what a browser would have run, and the point is that the
    survivor is safe even when it did not.
    """
    report = _run_temp_monitor_harness(
        """
        startStrMonitor(450, 10000);
        const first = window.__btap_tm.id;
        const firstTick = timers.get(first);
        startStrMonitor(450, 60000);
        const second = window.__btap_tm.id;
        timers.set(first, firstTick);
        advance(500);
        console.log(JSON.stringify({
            distinct: first !== second,
            firstGone: !timers.has(first),
            live: !!window.__btap_tm,
            stillSecond: window.__btap_tm ? window.__btap_tm.id === second : null,
        }));
        """
    )
    assert report["distinct"] is True
    # The stale tick removed itself rather than the monitor now on the page.
    assert report["firstGone"] is True
    assert report["live"] is True
    assert report["stillSecond"] is True


def test_temp_monitor_script_is_namespaced_and_self_limiting():
    """Reverse gate for both halves of the fix.

    `window._tm` was a three-character global on a real page; the expiry check
    is the part with no other enforcement, since no linter reads this module's
    JavaScript and no offline test can observe a browser leak directly.
    """
    source = S.build_temp_monitor_js(12)
    assert "window._tm" not in source
    assert source.count("window.__btap_tm") >= 5
    assert "clearInterval(id)" in source
    assert "Date.now() > expires" in source
    assert "startStrMonitor(450, 12000);" in source


def test_temp_monitor_js_is_syntactically_valid_javascript(tmp_path):
    script = tmp_path / "temp_monitor.js"
    script.write_text(S.build_temp_monitor_js(3), encoding="utf-8")
    completed = subprocess.run(
        ["node", "--check", str(script)], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr.strip()


def test_execute_js_rich_stops_the_monitor_when_nothing_will_read_it(monkeypatch):
    """A truthy `kind` returns early, and that return used to leak the interval.

    The path has no reader for the transients, so the stop cannot come from
    `get_temp_texts`; it has to be sent explicitly while a channel may still
    exist. The script's own expiry covers the tab that is already gone.
    """
    stops = []
    monkeypatch.setattr(S, "get_html", lambda *_args, **_kwargs: "<p>baseline</p>")
    monkeypatch.setattr(
        S, "stop_temp_monitor", lambda *_args, **kwargs: stops.append(kwargs)
    )
    monkeypatch.setattr(S, "get_temp_texts", lambda *_args, **_kwargs: ["never read"])
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)

    driver = QueueDriver(
        [{"result": "No response data in 2s (ACK received, script may still be running)"}],
        sessions={},
    )
    result = S.execute_js_rich(
        "slow()", driver, timeout=4, before_sids=set(), session_id="c:3"
    )

    assert result["status"] == "no_response"
    assert "transients" not in result
    assert len(stops) == 1
    assert stops[0]["session_id"] == "c:3"


def test_execute_js_rich_does_not_stop_a_monitor_it_never_started(monkeypatch):
    """`no_monitor=True` shares the same early return, and there is nothing to
    stop there. Sending the stop anyway would spend a roundtrip on every
    no-monitor call, which is the flag's whole purpose to avoid."""
    stops = []
    monkeypatch.setattr(
        S, "stop_temp_monitor", lambda *_args, **kwargs: stops.append(kwargs)
    )
    driver = QueueDriver(
        [{"result": "No response data in 2s (ACK received, script may still be running)"}]
    )
    S.execute_js_rich("slow()", driver, no_monitor=True, timeout=2, before_sids=set())
    assert stops == []
