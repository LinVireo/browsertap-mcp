"""The live layer's preconditions, tested offline.

The checks themselves need a browser; their reasoning does not, which is why it
lives in a pure module. What is pinned here is the part that used to be a
maintainer's judgement: what counts as a browser someone is using, what counts as
the suite having disturbed it, and that the two verdicts are not the same one.
"""

from __future__ import annotations

from pathlib import Path

from tests import live_preflight as P

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")


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
    assert P.busy_browser_reason(diff) is None
    assert P.drift_problem(diff) is None


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
    """Nothing was damaged, so it is not drift -- but it is not an idle browser.

    The suite raises tabs itself, which is why the two verdicts differ on
    exactly this case.
    """
    before = P.inventory(_tabs((1, "https://a.example/", True), (2, "https://b.example/", False)))
    after = P.inventory(_tabs((1, "https://a.example/", False), (2, "https://b.example/", True)))

    diff = P.compare(before, after)

    assert diff["changed"] is True
    assert diff["disturbed"] is False
    assert P.busy_browser_reason(diff) is not None
    assert P.drift_problem(diff) is None


def test_the_busy_message_says_what_moved_and_how_to_proceed_anyway():
    diff = P.compare({}, P.inventory(_tabs((1, "https://new.example/", True))))

    reason = P.busy_browser_reason(diff)

    assert "the browser is in use" in reason
    assert "opened: 1 (https://new.example/)" in reason
    assert str(P.IDLE_WINDOW_SECONDS) in reason
    assert P.OVERRIDE_ENV in reason


def test_the_drift_message_carries_both_counts_and_the_ambiguity():
    before = P.inventory(_tabs((1, "https://a.example/", True), (2, "https://b.example/", False)))
    after = P.inventory(_tabs((1, "https://a.example/", True)))

    problem = P.drift_problem(P.compare(before, after))

    assert "did not leave the browser as it found it" in problem
    assert "(2 tabs before, 1 after)" in problem
    assert "closed: 2 (https://b.example/)" in problem
    # The maintainer notes say it themselves: rule out a human before believing
    # the fixtures leaked.
    assert "A fixture leak and a human using the browser look identical" in problem


def test_a_tab_that_came_back_under_a_new_id_is_not_a_leak():
    """Chrome discarding a background tab used to fail the end-of-run check.

    Memory saver restores the page under a new tab id, so the inventory shows one
    close and one open at the same URL with the tab count unchanged -- identical
    in shape to the suite closing someone's tab and leaving its own behind.
    Measured in a seal run: three tabs nobody had touched failed the check this
    way. The idle check must still trip on it, because a browser reorganising
    itself is a moving target; the damage verdict must not.
    """
    before = P.inventory(_tabs((1, "https://kept.example/", True), (2, "https://saved.example/", False)))
    after = P.inventory(_tabs((1, "https://kept.example/", True), (7, "https://saved.example/", False)))

    diff = P.compare(before, after)

    assert diff["reidentified"] == [
        {"url": "https://saved.example/", "was": "2", "now": "7", "title": "tab 7"}
    ]
    assert diff["damaged"] is False
    assert P.drift_problem(diff) is None
    # ...and the idle check keeps its stricter reading of the same two samples.
    assert diff["disturbed"] is True and diff["changed"] is True
    assert P.busy_browser_reason(diff) is not None


def test_a_re_identified_tab_does_not_cover_for_a_real_leak():
    before = P.inventory(_tabs((1, "https://saved.example/", True)))
    after = P.inventory(
        _tabs((8, "https://saved.example/", True), (9, "https://leaked.example/", False))
    )

    problem = P.drift_problem(P.compare(before, after))

    assert "opened: 9 (https://leaked.example/)" in problem
    assert "saved.example" not in problem  # the pair is context, not damage
    assert "(1 more tab(s) only changed id, not counted)" in problem


def test_pairing_a_re_identified_tab_is_one_to_one():
    """Two tabs closed at one URL and one opened there is still a lost tab."""
    before = P.inventory(
        _tabs((1, "https://twice.example/", True), (2, "https://twice.example/", False))
    )
    after = P.inventory(_tabs((3, "https://twice.example/", True)))

    diff = P.compare(before, after)

    assert len(diff["reidentified"]) == 1
    assert [tab["id"] for tab in diff["damage"]["closed"]] == ["2"]
    assert diff["damaged"] is True


def test_an_unknown_url_is_not_evidence_that_two_tabs_are_one():
    """A tab with no URL pairs with nothing: it identifies no page to match."""
    diff = P.compare(P.inventory([{"id": 5}]), P.inventory([{"id": 6}]))

    assert diff["reidentified"] == []
    assert diff["damaged"] is True


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


def test_the_live_fixture_samples_twice_and_acts_on_both_verdicts():
    """A preflight nothing calls is prose with extra steps."""
    fixture = CONFTEST.split("def driver()", 1)[1]

    assert "time.sleep(P.IDLE_WINDOW_SECONDS)" in fixture
    assert fixture.count("_tab_inventory(record)") == 3  # first, baseline, final
    assert "P.busy_browser_reason(idle)" in fixture
    assert "pytest.skip(reason)" in fixture
    assert "P.drift_problem(drift)" in fixture
    assert "raise AssertionError(problem)" in fixture
    # The verdict is reached in teardown, so it must be reached even when a live
    # test failed: a suite that closed a user tab has to say so either way.
    assert "finally:" in fixture.split("yield d", 1)[1]


def test_an_unreadable_inventory_does_not_fail_the_live_layer():
    """The manual step this replaces could not fail a run either."""
    reader = CONFTEST.split("def _tab_inventory", 1)[1].split("def _write_live_preflight", 1)[0]

    assert "except Exception as exc:" in reader
    assert reader.count("return None") == 2
    assert 'record["notes"].append' in reader


def test_the_override_is_recorded_rather_than_silent():
    """Evidence produced against a browser in use has to say that it was."""
    fixture = CONFTEST.split("def driver()", 1)[1]

    assert "P.OVERRIDE_ENV" in fixture
    assert '"override": override' in fixture
    assert "warnings.warn(problem" in fixture
    assert "live-preflight.json" in CONFTEST


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
    reader = CONFTEST.split("def _setup_status", 1)[1].split("@pytest.fixture", 1)[0]

    assert "except Exception as exc:" in reader
    assert reader.count("return None") == 2
    assert 'record["notes"].append' in reader
