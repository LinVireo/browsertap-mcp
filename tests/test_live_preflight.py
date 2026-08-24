"""The live layer's preconditions, tested offline.

The checks themselves need a browser; their reasoning does not, which is why it
lives in a pure module. What is pinned here is the part that used to be a
maintainer's judgement: which claims the live layer is allowed to make about a
browser it shares with a person, and that the only one it fails a run over is
about tabs it opened itself.
"""

from __future__ import annotations

from pathlib import Path

from tests import live_preflight as P

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")


def _helper_source(name: str) -> str:
    """One top-level definition from conftest, bounded by the next one.

    Slicing to a *named* later anchor widens silently the moment something is
    inserted in between, and it does not fail -- it quietly starts asserting
    about more code. That happened here: adding `_owned_tabs` between
    `_setup_status` and the `driver` fixture grew `_setup_status`'s slice and
    broke a count that was still correct about the function it named. Ending at
    the next top-level `def` or decorator is what the assertions actually mean.
    """
    body = CONFTEST.split(f"\ndef {name}(", 1)[1]
    return body.split("\ndef ", 1)[0].split("\n@", 1)[0]


def _tabs(*specs: tuple[int, str, bool]) -> list[dict[str, object]]:
    return [
        {"id": tab_id, "url": url, "title": f"tab {tab_id}", "active": active, "windowId": 1}
        for tab_id, url, active in specs
    ]


def test_inventory_keys_tabs_by_id_as_text():
    """Native ids arrive as ints and session ids carry them as text.

    Keying on the raw value would make one browser look like two sets of tabs,
    reported as every tab opening and closing at once.
    """
    indexed = P.inventory(_tabs((7, "https://example.com/", True)))

    assert list(indexed) == ["7"]
    assert indexed["7"] == {
        "id": "7",
        "url": "https://example.com/",
        "title": "tab 7",
        "active": True,
        "window": "1",
    }


def test_inventory_ignores_entries_it_cannot_identify():
    """An unreadable payload must not invent tabs; a missing id is not a tab."""
    indexed = P.inventory([{"url": "https://example.com/"}, "nonsense", None, {"id": 3}])

    assert list(indexed) == ["3"]
    assert indexed["3"]["url"] == ""
    assert indexed["3"]["active"] is False


def test_inventory_tolerates_no_payload_at_all():
    assert P.inventory(None) == {}


def test_the_foreground_tab_is_stable_when_several_windows_each_have_one():
    """chrome.tabs.query reports one active tab per window.

    Picking whichever came first would report the foreground moving on every
    sample and turn an idle browser into a busy one.
    """
    tabs = P.inventory(_tabs((9, "https://b.example/", True), (4, "https://a.example/", True)))

    assert P._focused(tabs) == "4"
    assert P._focused(P.inventory(_tabs((9, "https://b.example/", False)))) is None


def test_an_untouched_browser_produces_no_difference():
    tabs = P.inventory(_tabs((1, "https://a.example/", True), (2, "https://b.example/", False)))

    diff = P.compare(tabs, tabs)

    assert diff["changed"] is False
    assert diff["disturbed"] is False
    assert diff["opened"] == [] and diff["closed"] == [] and diff["navigated"] == []
    assert diff["focus_moved"] is None
    assert (diff["tabs_before"], diff["tabs_after"]) == (2, 2)
    assert P.busy_browser_note(diff) is None


def test_each_kind_of_change_is_reported_as_itself():
    before = P.inventory(
        _tabs(
            (1, "https://kept.example/", True),
            (2, "https://gone.example/", False),
            (3, "https://before.example/", False),
        )
    )
    after = P.inventory(
        _tabs(
            (1, "https://kept.example/", False),
            (3, "https://after.example/", True),
            (4, "https://new.example/", False),
        )
    )

    diff = P.compare(before, after)

    assert [tab["id"] for tab in diff["opened"]] == ["4"]
    assert [tab["id"] for tab in diff["closed"]] == ["2"]
    assert diff["navigated"] == [
        {
            "id": "3",
            "from": "https://before.example/",
            "to": "https://after.example/",
            "title": "tab 3",
        }
    ]
    assert diff["focus_moved"] == {"from": "1", "to": "3"}
    assert diff["disturbed"] is True and diff["changed"] is True


def test_the_description_leads_with_the_change_that_cannot_be_undone():
    diff = P.compare(
        P.inventory(_tabs((1, "https://kept.example/", True), (2, "https://gone.example/", False))),
        P.inventory(_tabs((1, "https://moved.example/", True), (3, "https://new.example/", False))),
    )

    lines = P.describe(diff)

    assert lines[0].startswith("closed: 2 (https://gone.example/")
    assert lines[1].startswith("opened: 3 (https://new.example/")
    assert lines[2] == "navigated: 1 https://kept.example/ -> https://moved.example/"


def test_a_blank_url_is_still_named_in_the_description():
    """An empty pair of brackets sends the reader looking for a bug in the gate."""
    diff = P.compare(P.inventory([{"id": 5}]), {})

    assert P.describe(diff) == ["closed: 5 (about:blank)"]


def test_focus_moving_on_its_own_means_a_human_is_there():
    """Proof a person is present when the tab set did not move at all.

    Reported apart from the tab set because the suite raises tabs itself, so
    focus moving is not on its own evidence that anybody did anything.
    """
    before = P.inventory(_tabs((1, "https://a.example/", True), (2, "https://b.example/", False)))
    after = P.inventory(_tabs((1, "https://a.example/", False), (2, "https://b.example/", True)))

    diff = P.compare(before, after)

    assert diff["changed"] is True
    assert diff["disturbed"] is False
    assert P.busy_browser_note(diff) is not None


def test_the_busy_note_says_what_moved_and_that_it_is_allowed():
    """It used to end with an env var to set. There is nothing to set now: a
    browser in use is not a reason to withhold the live layer from it."""
    diff = P.compare({}, P.inventory(_tabs((1, "https://new.example/", True))))

    note = P.busy_browser_note(diff)

    assert "the browser was in use" in note
    assert "opened: 1 (https://new.example/)" in note
    assert str(P.IDLE_WINDOW_SECONDS) in note
    assert "That is allowed -- these are the user's tabs." in note
    assert "noisier" in note


def test_the_users_own_tabs_cannot_fail_the_run_and_the_suites_own_tab_can():
    """The whole point of the rescope, as one comparison.

    Before this, three scenarios produced one identical verdict: a person opening
    a tab of their own, a person closing a tab of their own, and the suite leaking
    its own scratch tab. A check that cannot separate those attributes nothing --
    the reader is told the browser changed, which they already knew, and not who
    changed it. Who opened a tab is not in a diff of the browser; it is in the
    ownership registry, so that is where the verdict comes from now.
    """
    baseline = P.inventory(_tabs((1, "https://mine.example/", True), (2, "https://also.example/", False)))

    user_opened = P.inventory(
        _tabs((1, "https://mine.example/", True), (2, "https://also.example/", False),
              (3, "https://theirs.example/", False))
    )
    user_closed = P.inventory(_tabs((1, "https://mine.example/", True)))
    user_navigated = P.inventory(
        _tabs((1, "https://elsewhere.example/", True), (2, "https://also.example/", False))
    )

    for after in (user_opened, user_closed, user_navigated):
        # Recorded in full, so a reader still sees it beside any failure...
        assert P.compare(baseline, after)["disturbed"] is True
        # ...and judged not at all, because the suite owns none of these tabs.
        assert P.leaked_tab_problem([], after.values()) is None

    # The one shape that does fail is not visible in any of those diffs: an
    # unclosed tab this task opened. Note that the browser here is *identical*
    # to the baseline -- the tab is still sitting in it, counted as unchanged.
    leaked = [{"session_id": "chrome:profile:2", "tab_id": 2, "generation": "gen-a"}]
    problem = P.leaked_tab_problem(leaked, baseline.values())

    assert problem is not None
    assert "left 1 tab(s) it opened behind" in problem


def test_a_leaked_tab_still_in_the_browser_is_named_with_its_session():
    outstanding = [
        {"session_id": "chrome:profile:9", "tab_id": 9, "generation": "gen-a"},
        {"session_id": "chrome:profile:10", "tab_id": 10, "generation": "gen-b"},
    ]
    tabs = _tabs((9, "https://left.example/", False), (10, "https://also-left.example/", False))

    problem = P.leaked_tab_problem(outstanding, tabs)

    assert "left 2 tab(s) it opened behind" in problem
    assert "chrome:profile:9 (generation gen-a): still open in the browser" in problem
    assert "chrome:profile:10 (generation gen-b): still open in the browser" in problem
    assert "must be closed with the owner_id it returned" in problem
    assert "Nothing here is about the user's own tabs" in problem


def test_a_leaked_tab_that_left_the_browser_names_the_refused_close():
    """The cause that is not a forgotten close, and needs a different fix.

    Chrome discarded the suite's own tab and restored it under a new id, so
    `close_tabs` refused it with `lifecycle generation changed` and released
    nothing. The record is outstanding and the tab id is not in the browser any
    more -- which is exactly what distinguishes this from a missing close_tabs.
    """
    outstanding = [{"session_id": "chrome:profile:9", "tab_id": 9, "generation": "gen-old"}]

    problem = P.leaked_tab_problem(outstanding, _tabs((11, "https://restored.example/", True)))

    assert "no longer in the browser" in problem
    assert "lifecycle generation changed" in problem


def test_a_leak_verdict_with_no_inventory_says_so_rather_than_guessing():
    """An unread sample is "not checked", never "not in the browser".

    The inventory read can fail on its own (the fixture records that and carries
    on), and reporting a tab as gone on the strength of a sample nobody took is
    the same vacuous shape this module exists to avoid. The leak is still
    reported: the registry alone is enough to know a close is owed.
    """
    outstanding = [{"session_id": "chrome:profile:9", "tab_id": 9, "generation": "gen-a"}]

    problem = P.leaked_tab_problem(outstanding, None)

    assert "left 1 tab(s) it opened behind" in problem
    assert "not checked" in problem
    assert "lifecycle generation changed" not in problem


def test_no_outstanding_tabs_is_no_problem_even_with_unreadable_input():
    """Runs in teardown, where raising would replace the suite's own result."""
    assert P.leaked_tab_problem([], _tabs((1, "https://a.example/", True))) is None
    assert P.leaked_tab_problem(None, None) is None
    assert P.leaked_tab_problem(["nonsense", None], None) is None


def test_a_tab_that_came_back_under_a_new_id_is_reported_as_the_same_tab():
    """Chrome discarding a background tab used to fail the end-of-run check.

    Memory saver restores the page under a new tab id, so the inventory shows one
    close and one open at the same URL with the tab count unchanged. Measured in
    a seal run: three tabs nobody had touched failed the check this way. Nothing
    fails over it now, and the pairing is still computed -- it is what tells a
    refused close apart from a forgotten one when the discarded tab was the
    suite's own.
    """
    before = P.inventory(_tabs((1, "https://kept.example/", True), (2, "https://saved.example/", False)))
    after = P.inventory(_tabs((1, "https://kept.example/", True), (7, "https://saved.example/", False)))

    diff = P.compare(before, after)

    assert diff["reidentified"] == [
        {"url": "https://saved.example/", "was": "2", "now": "7", "title": "tab 7"}
    ]
    assert diff["unpaired_opened"] == [] and diff["unpaired_closed"] == []
    # The idle window keeps its stricter reading of the same two samples: a
    # browser reorganising itself is still a moving target to measure against.
    assert diff["disturbed"] is True and diff["changed"] is True
    assert P.busy_browser_note(diff) is not None


def test_pairing_a_re_identified_tab_is_one_to_one():
    """Two tabs closed at one URL and one opened there leaves a close over."""
    before = P.inventory(
        _tabs((1, "https://twice.example/", True), (2, "https://twice.example/", False))
    )
    after = P.inventory(_tabs((3, "https://twice.example/", True)))

    diff = P.compare(before, after)

    assert len(diff["reidentified"]) == 1
    assert [tab["id"] for tab in diff["unpaired_closed"]] == ["2"]
    assert diff["unpaired_opened"] == []


def test_an_unknown_url_is_not_evidence_that_two_tabs_are_one():
    """A tab with no URL pairs with nothing: it identifies no page to match."""
    diff = P.compare(P.inventory([{"id": 5}]), P.inventory([{"id": 6}]))

    assert diff["reidentified"] == []
    assert [tab["id"] for tab in diff["unpaired_closed"]] == ["5"]
    assert [tab["id"] for tab in diff["unpaired_opened"]] == ["6"]


def test_the_diff_no_longer_offers_a_verdict_to_read_by_mistake():
    """A reverse gate on the rescope itself.

    `damaged` was the flag that failed a run over the user's tabs. Leaving it in
    the payload -- even unread by the fixture -- is an invitation for the next
    reader to reach for it, and `artifacts/live-preflight.json` publishes every
    key here as if it meant something.
    """
    diff = P.compare(
        P.inventory(_tabs((1, "https://a.example/", True))),
        P.inventory(_tabs((2, "https://b.example/", True))),
    )

    assert "damaged" not in diff
    assert "damage" not in diff
    assert not hasattr(P, "drift_problem")
    assert not hasattr(P, "busy_browser_reason")
    # And nothing may reintroduce a skip over a browser somebody else is using.
    assert "OVERRIDE_ENV" not in dir(P)


def test_the_fields_read_here_are_the_fields_the_extension_sends():
    """Binds the reader to its source.

    A rename on the extension side should break this test rather than quietly
    reporting an unchanged browser because every field came back empty.
    """
    background = (ROOT / "src" / "browsertap_mcp" / "chrome_extension" / "background.js").read_text(
        encoding="utf-8"
    )

    assert (
        "{ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId }"
        in background
    )


def test_the_live_fixture_samples_twice_and_acts_on_the_one_verdict_it_owns():
    """A preflight nothing calls is prose with extra steps."""
    fixture = CONFTEST.split("def driver()", 1)[1]

    assert "time.sleep(P.IDLE_WINDOW_SECONDS)" in fixture
    assert fixture.count("_tab_inventory(record)") == 3  # first, baseline, final
    assert "P.busy_browser_note(idle)" in fixture
    assert "P.leaked_tab_problem(" in fixture
    assert "raise AssertionError(problem)" in fixture
    # The verdict is reached in teardown, so it must be reached even when a live
    # test failed: a suite that leaked one of its own tabs has to say so either way.
    assert "finally:" in fixture.split("yield d", 1)[1]


def test_the_fixture_reads_ownership_from_the_product_not_from_a_diff():
    """One implementation of "who opened this tab", and it is the product's.

    Deriving it a second time in the test layer is how the previous version got
    it wrong: an inventory diff was the only source, and it does not contain the
    answer at all.
    """
    fixture = CONFTEST.split("def driver()", 1)[1]

    assert "_TAB_OWNERSHIP.outstanding()" in CONFTEST
    assert "_TAB_OWNERSHIP.counters()" in CONFTEST
    assert "_owned_tabs(record)" in fixture
    # The counters are part of the verdict, not decoration: without them
    # "nothing outstanding" cannot be told from "nothing was ever opened".
    assert '"enforced": counters.get("registered", 0) > 0' in fixture


def test_the_users_tabs_are_recorded_and_nothing_gates_on_them():
    """The rescope, pinned against the fixture that has to keep honouring it.

    A skip here would be the previous behaviour returning: it read the user's
    tabs to decide whether the suite may run at all.
    """
    fixture = CONFTEST.split("def driver()", 1)[1]

    assert '"tab_activity"' in fixture
    assert '"browser_idle"' in fixture
    assert "pytest.skip" not in fixture.split("_setup_status(record)", 1)[1]
    assert "warnings" not in fixture
    assert "live-preflight.json" in CONFTEST


def test_an_unreadable_inventory_does_not_fail_the_live_layer():
    """The manual step this replaces could not fail a run either."""
    reader = _helper_source("_tab_inventory")

    assert "except Exception as exc:" in reader
    assert reader.count("return None") == 2
    assert 'record["notes"].append' in reader


def test_an_unreadable_ownership_registry_does_not_fail_the_live_layer():
    """Being unable to ask is not evidence of a leak."""
    reader = _helper_source("_owned_tabs")

    assert "except Exception as exc:" in reader
    assert "return None" in reader
    assert 'record["notes"].append' in reader


# One `get_setup_status()` answer with nothing wrong with it, copied and spoiled
# one field at a time below. Written out in full rather than built from the real
# call because the point of these tests is that the offline layer can check the
# reasoning with no bridge and no browser.
_HEALTHY = {
    "status": "healthy",
    "action": "none",
    "package_version": "0.4.3",
    "bridge_version": "0.4.3",
    "extension_version": "0.4.3",
    "protocol_version": 3,
    "expected_protocol_version": 3,
    "missing_extension_capabilities": [],
    "reload_extension_required": False,
    "restart_bridge_required": False,
    "restart_mcp_session_required": False,
}


def test_components_that_match_the_checkout_do_not_stop_the_live_layer():
    """The gate has to be silent in the case that happens every time."""
    assert P.stale_component_reason(_HEALTHY) is None


def test_a_stale_extension_is_refused_and_named_with_the_click_that_fixes_it():
    """This is the case that went unnoticed for a whole release round.

    The extension keeps running the `background.js` it started with, so a live
    suite can pass 54/54 against the previous build while the seal it feeds says
    the current one. Naming the fix matters as much as refusing: reloading is the
    only thing that helps and it cannot be automated.
    """
    reason = P.stale_component_reason(
        dict(
            _HEALTHY,
            status="stale_extension",
            action="reload_extension",
            extension_version="0.4.2",
            reload_extension_required=True,
        )
    )

    assert reason is not None
    assert "the Chrome extension" in reason
    assert "chrome://extensions" in reason
    # Both builds, so the reader does not have to go and ask.
    assert "0.4.3" in reason and "0.4.2" in reason
    # And not a word about the two fixes that cannot help here.
    assert "bridge --restart" not in reason


def test_a_stale_bridge_asks_for_the_restart_and_not_the_reload():
    """The bridge is the other long-lived process, and it has its own fix.

    It outlives every MCP server and holds its own routing code, so an edit to
    `browser_bridge.py` is not in effect until it is restarted -- and pointing
    the reader at chrome://extensions instead would waste the one manual step
    they are willing to take.
    """
    reason = P.stale_component_reason(
        dict(
            _HEALTHY,
            status="stale_bridge",
            action="restart_bridge",
            bridge_version="0.4.2",
            restart_bridge_required=True,
        )
    )

    assert reason is not None
    assert "the bridge daemon" in reason
    assert "bridge --restart" in reason
    assert "chrome://extensions" not in reason


def test_a_newer_counterpart_does_not_ask_for_a_reload_that_cannot_help():
    """A component newer than this checkout makes the checkout the stale side.

    Reloading an extension built from a newer tree just reinstalls the newer
    build, so the verdict has to name this process instead. `get_setup_status()`
    already draws that distinction, which is exactly why the flags are read from
    it rather than derived from a version comparison here.
    """
    reason = P.stale_component_reason(
        dict(
            _HEALTHY,
            status="stale_package",
            action="restart_mcp_session",
            extension_version="0.5.0",
            restart_mcp_session_required=True,
        )
    )

    assert reason is not None
    assert "this process" in reason
    assert "newer build" in reason
    assert "chrome://extensions" not in reason


def test_every_stale_component_is_named_not_just_the_first():
    """Two skews are one round trip to fix, if the report says both.

    `action` names a single next step by design, so a gate that echoed it would
    send someone to reload the extension, re-run the whole live suite, and only
    then learn about the bridge.
    """
    reason = P.stale_component_reason(
        dict(
            _HEALTHY,
            status="stale_bridge",
            action="restart_bridge",
            bridge_version="0.4.1",
            extension_version="0.4.2",
            reload_extension_required=True,
            restart_bridge_required=True,
        )
    )

    assert reason is not None
    assert "the Chrome extension" in reason and "the bridge daemon" in reason
    assert "chrome://extensions" in reason and "bridge --restart" in reason
    assert "are running a different build" in reason


def test_a_missing_capability_is_named_because_the_versions_can_still_match():
    """A version label is not proof that the loaded code is the loaded code.

    An extension whose manifest says the right number can still be missing a
    capability this build requires -- that is how a stale build presents when
    someone edits `background.js` without touching the version. The reason has
    to say which capability, because "reload it" and "the versions match" read
    as a contradiction otherwise.
    """
    reason = P.stale_component_reason(
        dict(
            _HEALTHY,
            status="stale_extension",
            action="reload_extension",
            missing_extension_capabilities=["content_command_channel_removed"],
            reload_extension_required=True,
        )
    )

    assert reason is not None
    assert "content_command_channel_removed" in reason


def test_an_unreachable_bridge_is_not_reported_as_a_stale_one():
    """No answer is not a wrong answer, and it has a different fix.

    An unreachable bridge reports no version at all, which every comparison
    downstream reads as a mismatch. The fixture already skips that case by
    name; a "stale bridge, press Reload" verdict here would describe it wrongly
    and send the reader to the one step that cannot help.
    """
    assert (
        P.stale_component_reason(
            dict(
                _HEALTHY,
                status="bridge_unreachable",
                action="restart_bridge",
                bridge_version=None,
                extension_version=None,
                restart_bridge_required=True,
                reload_extension_required=True,
            )
        )
        is None
    )


def test_a_status_that_cannot_be_read_does_not_stop_the_live_layer():
    """Being unable to ask is not evidence of a skew.

    Same rule the tab inventory follows: a precondition that can break the live
    layer is worse than the manual step it replaces.
    """
    for unusable in (None, "bridge said something else", 3, []):
        assert P.stale_component_reason(unusable) is None
        assert P.component_versions(unusable) is None


def test_a_flag_that_is_merely_truthy_is_not_a_verdict():
    """The flags are read with `is True`, and that is deliberate.

    An older bridge that answers a field it does not really implement -- a
    string, a number -- would otherwise refuse every live run on this machine
    with a verdict nothing can clear.
    """
    for truthy in ("no", "false", 1, [0]):
        assert P.stale_component_reason(dict(_HEALTHY, reload_extension_required=truthy)) is None


def test_the_recorded_summary_leaves_this_machine_out_of_the_published_evidence():
    """The record is published, so its fields are named rather than copied.

    `get_setup_status()` also answers where this machine keeps its state: the
    absolute state directory, the token file and the token's fingerprint. This
    summary goes into `artifacts/`, which live.yml uploads and which gets
    attached to an external review, so a field added upstream tomorrow has to be
    absent by default instead of published while nobody is looking.
    """
    recorded = P.component_versions(
        dict(
            _HEALTHY,
            state_paths={"state_dir": "C:/Users/someone/.browsertap",
                         "token_fingerprint": "sha256:deadbeef"},
            extension_path="C:/Users/someone/checkout/src",
            bridge_host="127.0.0.1",
            tabs=[{"url": "https://example.invalid/private"}],
        )
    )

    assert recorded is not None
    assert set(recorded) == {
        "status",
        "action",
        "package_version",
        "bridge_version",
        "extension_version",
        "protocol_version",
        "expected_protocol_version",
        "missing_extension_capabilities",
        "reload_extension_required",
        "restart_bridge_required",
        "restart_mcp_session_required",
    }
    # The evidence has to answer the question it exists for.
    assert recorded["extension_version"] == "0.4.3"


def test_the_live_fixture_refuses_a_stale_build_instead_of_skipping_it():
    """A skip would report this as an absent condition, which it is not.

    `scripts/acceptance_report.py` fails the live gate on any skipped case, so a
    skip would not sneak a seal through -- it would cost the whole chain first
    and then name the wrong problem. A browser someone is using is a condition
    that was absent; a component running the previous build is a setup error
    with a one-click fix, and the run has to say so before it starts.
    """
    fixture = CONFTEST.split("def driver()", 1)[1]

    assert "P.stale_component_reason(status)" in fixture
    assert "pytest.fail(stale, pytrace=False)" in fixture
    assert "pytest.skip(stale)" not in fixture
    # Recorded as well as refused: the evidence has to name the build that ran.
    assert 'record["components"] = P.component_versions(status)' in fixture
    # And asked before the browser is watched, so a skew costs no waiting.
    assert fixture.index("stale_component_reason") < fixture.index("P.IDLE_WINDOW_SECONDS")


def test_an_unreadable_component_status_is_a_note_rather_than_a_failure():
    """The reader follows `_tab_inventory`, including how it gives up."""
    reader = _helper_source("_setup_status")

    assert "except Exception as exc:" in reader
    assert reader.count("return None") == 2
    assert 'record["notes"].append' in reader


LIVE_SUITE = (ROOT / "tests" / "test_live_browser.py").read_text(encoding="utf-8")


def test_a_tab_that_never_moved_resolves_to_the_id_it_already_had():
    """The common case has to stay byte-identical to remembering the id."""
    tabs = _tabs((7, "https://example.com/", True), (8, "https://b.example/", False))

    assert P.resolve_remembered_tab(tabs[0], tabs) == 7
    # Raw, not normalised: callers feed this back to `tabs.switch` and build
    # session ids out of it, and the extension speaks native ints.
    assert isinstance(P.resolve_remembered_tab(tabs[0], tabs), int)


def test_a_discarded_tab_resolves_to_the_id_chrome_gave_it_back():
    """The failure this exists for: same URL, same window, new id.

    Measured on a real browser during a seal run -- `1935857437` came back as
    `1935857441` with `damaged: false` and the tab count unchanged, and four
    places in the live suite then handed the retired id back to `tabs.switch`.
    """
    remembered = _tabs((1935857437, "https://gl.example.net/keys?groupId=2", True))[0]
    after = _tabs(
        (1935857441, "https://gl.example.net/keys?groupId=2", True),
        (12, "https://other.example/", False),
    )

    assert P.resolve_remembered_tab(remembered, after) == 1935857441


def test_a_tab_the_browser_retired_resolves_to_nothing():
    """Not an error and not a substitute. The caller does nothing, and the
    end-of-run inventory check is what reports the tab as gone."""
    remembered = _tabs((5, "https://gone.example/", True))[0]

    assert P.resolve_remembered_tab(remembered, _tabs((6, "https://other.example/", True))) is None
    assert P.resolve_remembered_tab(remembered, []) is None
    assert P.resolve_remembered_tab(remembered, None) is None


def test_two_tabs_at_one_url_resolve_to_nothing_rather_than_a_guess():
    """Substituting the wrong tab is the failure the whole module exists to
    avoid, so an ambiguous match is worth less than no match -- the same
    asymmetry `BrowserBridge` applies to a named session that died."""
    remembered = {"id": 5, "url": "https://dup.example/", "title": "tab 5", "windowId": 1}
    duplicates = _tabs((6, "https://dup.example/", True), (7, "https://dup.example/", False))

    assert P.resolve_remembered_tab(remembered, duplicates) is None
    # Unless the title breaks the tie, which is the one thing left to go on.
    duplicates[0]["title"] = "tab 5"
    assert P.resolve_remembered_tab(remembered, duplicates) == 6


def test_an_unknown_url_never_resolves():
    """Same rule as `_pair_reidentified`: an unknown location is not evidence
    that two tabs are the same tab."""
    remembered = {"id": 5, "url": "", "title": "", "windowId": 1}

    assert P.resolve_remembered_tab(remembered, _tabs((6, "", True))) is None


def test_resolution_prefers_the_window_the_tab_was_in_but_survives_losing_it():
    remembered = {"id": 5, "url": "https://dup.example/", "title": "x", "windowId": 2}
    two_windows = [
        {"id": 6, "url": "https://dup.example/", "title": "a", "windowId": 1, "active": True},
        {"id": 7, "url": "https://dup.example/", "title": "b", "windowId": 2, "active": False},
    ]

    assert P.resolve_remembered_tab(remembered, two_windows) == 7
    # The window went away as well: the URL is the best evidence there is, and
    # refusing it here would refuse a real successor.
    moved = [{"id": 8, "url": "https://dup.example/", "title": "b", "windowId": 9, "active": True}]
    assert P.resolve_remembered_tab(remembered, moved) == 8


def test_unreadable_input_resolves_to_nothing_instead_of_raising():
    """This runs in cleanup, where an exception replaces the test's own result."""
    assert P.resolve_remembered_tab(None, _tabs((1, "https://a.example/", True))) is None
    assert P.resolve_remembered_tab("nonsense", []) is None
    assert P.resolve_remembered_tab({"url": "https://a.example/"}, []) is None
    assert P.resolve_remembered_tab({"id": 1}, ["nonsense", None, {"url": "x"}]) is None


def test_the_live_suite_never_hands_a_remembered_tab_id_back_to_the_browser():
    """The reverse gate. `AGENTS.md` section 3 is titled "never remember one",
    and the product code obeys it; these four cleanup paths did not, which cost
    a seal run two failures that named `open_url` instead of the retired id.

    Pinned as source text because the defect is invisible offline: the tests it
    lives in only run against a real browser, and only when Chrome happens to
    discard a tab mid-run.
    """
    assert "def restore_foreground(" in LIVE_SUITE
    assert "P.resolve_remembered_tab(remembered, tabs)" in LIVE_SUITE
    # Nothing may reach `tabs.switch` without having been resolved first. The
    # id sits on the same line, so only the text right after the marker counts.
    switches = LIVE_SUITE.split('"method": "switch"')[1:]
    assert len(switches) == 2, "a new tabs.switch call site needs the same rule"
    for block in switches:
        assert block[:40].strip().startswith(', "tabId": '), block[:40]
        assert '"tabId": tab_id' in block[:40] or '"tabId": restored' in block[:40], block[:40]
    # And no assertion may compare against the sampled id either.
    assert 'original_active["id"]' not in LIVE_SUITE
    assert "original['id']" not in LIVE_SUITE


def test_the_live_suite_only_ever_closes_tabs_it_opened():
    """The other half of "the user's tabs are theirs", pinned at the source.

    The teardown verdict is now built entirely from the ownership registry, and
    that only means anything while every close in the live suite goes through
    ownership. `only_if_agent_owned=False` is the documented operator escape
    hatch in `close_tabs`; a live test reaching for it would close a real
    person's tab, and no offline test can see that happen.

    Pinned as source text for the same reason as its neighbour above: these
    tests only run against a real browser.
    """
    assert "only_if_agent_owned" not in LIVE_SUITE
    # Every close names the capability the matching open_new_tab handed back, so
    # a close can never outnumber the opens that authorised one.
    opens = LIVE_SUITE.count("S.open_new_tab(")
    closes = LIVE_SUITE.count("S.close_tabs(")
    assert opens > 0, "the reverse gate measured nothing"
    assert closes == LIVE_SUITE.count('owner_id=created.get("owner_id")')
    assert closes == opens, (opens, closes)
    # The shared scratch tab is the one open that lives in the fixture instead.
    assert CONFTEST.count("S.open_new_tab(") == 1
    assert "S.close_tabs(sid, session_id=sid, owner_id=owner_id)" in CONFTEST
