"""The live layer's preconditions, as code instead of prose.

The live suite drives the user's real browser, so it came with written rules
that lived only in maintainer notes -- which is the same as not having them. A
violated precondition showed up as a mystery failure, and "did the suite close
one of my tabs" was answered by hand, after the fact, from memory.

One of those rules was that the tab inventory the suite started with had to be
the inventory it left behind, and that rule was wrong: it made a claim about
tabs the suite never touched. Somebody opening or closing a tab of their own
during a five-minute run is not a defect, yet it failed the run in exactly the
same words as the suite leaking a tab of its own -- three scenarios, one
verdict, so the verdict identified nothing. What the suite is answerable for is
narrower, and it is bookkeeping the product already keeps: the tabs the suite
opened itself (`server._TAB_OWNERSHIP.outstanding()`). That is now the only
thing that can fail. Everything about the user's tabs is recorded as context.

The idle check kept its window for a different reason. A browser somebody is
using makes the timing-sensitive cases noisier, which is worth having written
down next to a failure -- but it is a note, not a gate. Refusing to run against
a busy browser would take the live layer away from anyone whose browser is
never idle, which is the same trade the quiet-input gate makes with `enforced`
and physical input makes with `on_screen`: report what could be observed
instead of withholding the capability.

A third precondition came from the same notes and was the last one still being
checked by hand. The live suite exercises three programs at once and only one of
them is the code pytest imported: the bridge daemon and the Chrome extension are
long-lived and keep running whatever build they started with. So a green live run
can certify code that is not in the tree, and nothing downstream noticed --
neither a gate nor a sealed artifact recorded which build answered. That one is
refused rather than noted, for the reasons in `stale_component_reason`.

Two neighbouring rules from the same notes are already mechanised elsewhere and
deliberately not repeated here: the failover flake became
`FAILOVER_SETTLE_SECONDS` in the driver (`browser_bridge._pick_failover_session`)
and the tight wall-clock budgets were widened in the tests that owned them.

Everything in this module is pure. `conftest.py` owns the sampling, the waiting
and the reporting; keeping the reasoning separate is what lets the offline layer
test it with no bridge and no browser.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# How long to watch before calling a browser idle. Two samples this far apart
# catch a tab being opened, closed, navigated or focused; a shorter window
# regularly misses a page load committing, which is the exact event that used to
# break the run. What the answer is used for is a note on the report, not a
# refusal -- see `busy_browser_note`.
IDLE_WINDOW_SECONDS = 1.5


def inventory(tabs: Iterable[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Index one `list_all_tabs()` payload by tab id.

    Ids are keyed as text on purpose: the extension reports native ints while
    session ids carry the same number as a string, and a comparison that mixed
    the two would report every tab as opened and closed at once.
    """
    indexed: dict[str, dict[str, Any]] = {}
    for tab in tabs or ():
        if not isinstance(tab, Mapping):
            continue
        raw_id = tab.get("id")
        if raw_id is None:
            continue
        indexed[str(raw_id)] = {
            "id": str(raw_id),
            "url": str(tab.get("url") or ""),
            "title": str(tab.get("title") or ""),
            "active": bool(tab.get("active")),
            "window": str(tab.get("windowId") or ""),
        }
    return indexed


def _focused(tabs: Mapping[str, Mapping[str, Any]]) -> str | None:
    """The foreground tab, or None. Several windows can each report one active
    tab, so the lowest id wins to keep the answer stable between samples."""
    active = sorted(key for key, tab in tabs.items() if tab.get("active"))
    return active[0] if active else None


def _pair_reidentified(
    opened: list[dict[str, Any]],
    closed: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Match up a tab that only changed its id, and return what is left over.

    Chrome's memory saver discards an idle background tab and restores it under
    a *new* tab id at the same URL, so the extension unregisters one session and
    registers another. Nothing was gained, lost or navigated -- but it is
    byte-for-byte the signature of a tab being closed and a different one being
    left behind. Measured in a seal run against a browser nobody was touching:
    three background tabs (chrome://extensions, a PyPI page, the Web Store) came
    back with new ids while the tab count was identical, and the end-of-run check
    of the day failed the run over it.

    It still matters after that check stopped judging the user's tabs, for a
    narrower reason: when one of the *suite's own* tabs is discarded this way,
    `close_tabs` refuses it with `lifecycle generation changed` and the ownership
    record stays outstanding. Pairing it here is what lets
    `leaked_tab_problem` tell that apart from a forgotten close.

    Matching is one-to-one and by URL, so two closes against one open still
    leave a close reported, and an empty URL never pairs -- an unknown location
    is not evidence that two tabs are the same tab.
    """
    pairs: list[dict[str, Any]] = []
    remaining = list(closed)
    opened_left: list[dict[str, Any]] = []
    for tab in opened:
        url = tab.get("url") or ""
        match = next((c for c in remaining if url and c.get("url") == url), None)
        if match is None:
            opened_left.append(tab)
            continue
        remaining.remove(match)
        pairs.append({"url": url, "was": match.get("id"), "now": tab.get("id"),
                      "title": tab.get("title", "")})
    return pairs, opened_left, remaining


def resolve_remembered_tab(
    remembered: Mapping[str, Any] | None,
    tabs: Iterable[Mapping[str, Any]] | None,
) -> Any | None:
    """Answer "which tab is that one now" for a tab sampled earlier in the run.

    Several live tests note the user's foreground tab so they can hand it back
    afterwards, and `AGENTS.md` section 3 is titled "Tab ids are not stable --
    never remember one". The product code obeys that rule; the tests were the
    half that did not, so the same memory-saver discard described in
    `_pair_reidentified` made them fail -- and fail while naming the tool they
    happened to be exercising rather than the retired id.

    Returned is the tab's id **as the browser reports it now**, raw rather than
    normalised, because the callers feed it back to `tabs.switch` and build
    session ids out of it. `None` means the tab is genuinely gone, and a caller
    restoring focus should then do nothing. Nothing fails over that: the tab was
    the user's, closing it is theirs to do, and the end-of-run record carries it
    as context under `tab_activity`.

    Resolution is the pairing rule of `_pair_reidentified` narrowed to one tab:
    the successor sits at the same URL, so an empty URL never resolves and an
    ambiguous match resolves to nothing at all. Substituting the wrong tab is
    the failure this whole module exists to avoid, and it is worse than
    reporting none -- exactly the asymmetry `BrowserBridge` applies to a named
    session that died.
    """
    if not isinstance(remembered, Mapping):
        return None
    was = remembered.get("id")
    if was is None:
        return None
    current = [
        tab for tab in tabs or ()
        if isinstance(tab, Mapping) and tab.get("id") is not None
    ]
    # Ids are compared as text for the reason `inventory` keys them that way.
    still_there = next((tab for tab in current if str(tab.get("id")) == str(was)), None)
    if still_there is not None:
        return still_there.get("id")

    url = str(remembered.get("url") or "")
    if not url:
        return None
    candidates = [tab for tab in current if str(tab.get("url") or "") == url]
    # Accepts both shapes a caller may have kept: a raw `list_all_tabs` entry
    # (`windowId`) or an `inventory` one (`window`).
    window = str(remembered.get("windowId") or remembered.get("window") or "")
    if window:
        scoped = [
            tab for tab in candidates
            if str(tab.get("windowId") or tab.get("window") or "") == window
        ]
        # Only narrows. A window that went away too leaves the URL as the best
        # evidence there is, and discarding it would refuse a real successor.
        if scoped:
            candidates = scoped
    if len(candidates) > 1:
        title = str(remembered.get("title") or "")
        titled = [tab for tab in candidates if str(tab.get("title") or "") == title]
        if len(titled) == 1:
            candidates = titled
    if len(candidates) != 1:
        return None
    return candidates[0].get("id")


def compare(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Diff two inventories of the whole browser, as description only.

    Nothing here attributes anything. This used to also answer "is any of it the
    suite's fault" via a `damaged` flag, and it could not: a user opening a tab,
    a user closing a tab and the suite leaking its own scratch tab all set it.
    Who opened a tab is not visible in a diff of the browser at two moments --
    it is in `server._TAB_OWNERSHIP`, so `leaked_tab_problem` answers that from
    there and this function stopped guessing.

    `disturbed` is still computed because one reader still wants it: the idle
    window, where "did anything at all move" means the run is about to be
    measured against a browser somebody is using. Focus is reported separately
    from that, because the suite raises tabs itself.

    `reidentified` pairs up a tab that only changed its id (see
    `_pair_reidentified`). It stays in the output for the same reason as the
    rest: over a five-minute run Chrome discarding a background tab is expected,
    and a reader looking at a leak wants to know it happened.
    """
    opened = [dict(tab) for key, tab in sorted(after.items()) if key not in before]
    closed = [dict(tab) for key, tab in sorted(before.items()) if key not in after]
    navigated = [
        {
            "id": key,
            "from": before[key]["url"],
            "to": after[key]["url"],
            "title": after[key]["title"],
        }
        for key in sorted(set(before) & set(after))
        if before[key]["url"] != after[key]["url"]
    ]
    focus_before, focus_after = _focused(before), _focused(after)
    focus_moved = (
        None
        if focus_before == focus_after
        else {"from": focus_before, "to": focus_after}
    )
    disturbed = bool(opened or closed or navigated)
    reidentified, opened_left, closed_left = _pair_reidentified(opened, closed)
    return {
        "opened": opened,
        "closed": closed,
        "navigated": navigated,
        "reidentified": reidentified,
        # What did not pair off as a re-identified tab. Context for a reader, and
        # deliberately not a verdict: an unpaired close is as likely to be the
        # user closing their own tab as anything else.
        "unpaired_opened": opened_left,
        "unpaired_closed": closed_left,
        "focus_moved": focus_moved,
        "disturbed": disturbed,
        "changed": disturbed or focus_moved is not None,
        "tabs_before": len(before),
        "tabs_after": len(after),
    }


def _tab_label(tab: Mapping[str, Any]) -> str:
    return f"{tab.get('id')} ({tab.get('url') or 'about:blank'})"


def describe(diff: Mapping[str, Any]) -> list[str]:
    """One line per difference, in the order a reader can act on them."""
    lines: list[str] = []
    if diff.get("closed"):
        lines.append(
            "closed: " + ", ".join(_tab_label(tab) for tab in diff["closed"])
        )
    if diff.get("opened"):
        lines.append(
            "opened: " + ", ".join(_tab_label(tab) for tab in diff["opened"])
        )
    for move in diff.get("navigated") or ():
        lines.append(f"navigated: {move['id']} {move['from']} -> {move['to']}")
    for pair in diff.get("reidentified") or ():
        lines.append(
            "same tab, new id (Chrome discarded and restored it): "
            f"{pair['was']} -> {pair['now']} ({pair['url'] or 'about:blank'})"
        )
    focus = diff.get("focus_moved")
    if focus:
        lines.append(f"foreground tab: {focus.get('from')} -> {focus.get('to')}")
    return lines


def busy_browser_note(diff: Mapping[str, Any]) -> str | None:
    """What the browser did during the idle window, or None when it sat still.

    This used to be `busy_browser_reason` and it skipped the whole live layer.
    Two things were wrong with that. It read the user's tabs to decide whether
    the suite may run, when the suite is only answerable for its own; and a
    browser that is never idle then means the live layer never runs at all,
    which is a worse outcome than a noisier one. The observation is still worth
    keeping -- a human in the browser makes the timing-sensitive cases flakier
    and is the first thing to check next to an odd failure -- so it is attached
    to the report and to the failure text instead of gating anything.

    Same idiom as `run_physical_action`'s `input_quiet` and `on_screen`: say what
    could be observed, do not withhold the capability.
    """
    if not diff.get("changed"):
        return None
    return "\n".join(
        [
            "the browser was in use: its tabs changed while the live layer "
            f"watched for {IDLE_WINDOW_SECONDS}s.",
            *(f"  {line}" for line in describe(diff)),
            "That is allowed -- these are the user's tabs. It is recorded "
            "because it makes the timing-sensitive cases noisier.",
        ]
    )


def leaked_tab_problem(
    outstanding: Iterable[Mapping[str, Any]] | None,
    tabs: Iterable[Mapping[str, Any]] | None = None,
) -> str | None:
    """Which tabs the suite opened and never closed, or None.

    This is the one claim the live layer is answerable for, and the only one it
    can make honestly. `server._TabOwnershipRegistry.release` runs after a close
    actually succeeded, so a record still present at teardown means a tab this
    process opened is still owed a close. No inventory diff can produce that
    fact: it needs to know who opened the tab, which only the registry knows.

    Both causes are named because they need different fixes and the message is
    the only place a reader finds out which one to look for. A forgotten close is
    a test-side bug. A `lifecycle generation changed` refusal is not: Chrome
    discarded the suite's own background tab and restored it under a new id, so
    `close_tabs` correctly refused to close a tab that is no longer the one that
    was claimed -- and correctly left the record outstanding, since nothing was
    closed. The second one is visible in the browser and the first is not, which
    is why the current tabs are cross-checked when they are available.
    """
    records = [rec for rec in outstanding or () if isinstance(rec, Mapping)]
    if not records:
        return None
    live_ids = {
        str(tab.get("id"))
        for tab in tabs or ()
        if isinstance(tab, Mapping) and tab.get("id") is not None
    }
    lines: list[str] = []
    for record in sorted(records, key=lambda rec: str(rec.get("session_id"))):
        sid = record.get("session_id")
        tab_id = record.get("tab_id")
        # An absent `tabs` argument is "not checked", not "not in the browser":
        # saying a tab is gone on the strength of a sample nobody took is the
        # same vacuous-pass shape this module exists to avoid.
        if not live_ids:
            state = "still in the browser?  not checked"
        elif str(tab_id) in live_ids:
            state = "still open in the browser"
        else:
            state = (
                "no longer in the browser -- close_tabs likely refused it with "
                "'lifecycle generation changed' after Chrome discarded and "
                "restored it"
            )
        lines.append(f"  {sid} (generation {record.get('generation')}): {state}")
    return "\n".join(
        [
            f"the live layer left {len(records)} tab(s) it opened behind:",
            *lines,
            "Every open_new_tab in the live suite must be closed with the "
            "owner_id it returned. Nothing here is about the user's own tabs; "
            "those are theirs to open and close.",
        ]
    )


# The `get_setup_status()` flags that mean the live layer would not be testing
# this checkout, each with the one thing that fixes it. Reading the bridge's own
# verdict is deliberate: it already handles the direction that matters -- a
# component *newer* than this process cannot be repaired by reloading it, so the
# verdict names the package as the stale side instead -- and the capability check
# that an old extension fails with no version skew at all. Deriving either again
# here would be a second implementation to keep in step with the first.
_STALE_COMPONENTS = (
    (
        "reload_extension_required",
        "the Chrome extension",
        "press Reload once on chrome://extensions (this cannot be automated)",
    ),
    (
        "restart_bridge_required",
        "the bridge daemon",
        "run `browsertap bridge --restart`",
    ),
    (
        "restart_mcp_session_required",
        "this process",
        "a counterpart is running a newer build than this checkout, so nothing "
        "here is the stale side: run the suite from that build instead",
    ),
)


def stale_component_reason(status: Mapping[str, Any] | None) -> str | None:
    """Why a live round would prove nothing about this checkout, or None.

    A live pass is a claim about the code in the tree, and two of the three
    processes it travels through are long-lived: the bridge daemon holds its own
    routing code and the extension holds its own `background.js` until each is
    restarted or reloaded by hand. Nothing about running pytest changes either.
    So the suite can pass, the tool evidence can come out 55/55 and the seal can
    be written, while the build that actually answered was the previous one.

    This is refused rather than skipped, and there is no override. A skip would
    not sneak a seal through -- `scripts/acceptance_report.py` fails the live gate
    on any skipped case -- but it would get there late, after the offline suite,
    the coverage round, the wheel build and the whole live suite had run, and it
    would report itself as "live cases were skipped" instead of naming the build
    skew. A skip is also the honest signal for a condition that was absent, which
    is what a browser someone is using is; a component running the previous build
    is a setup error with a one-click fix. Refusing up front says which click.
    """
    if not isinstance(status, Mapping):
        return None
    # An unreachable bridge is not a stale one: it reports no version at all,
    # which every comparison downstream then reads as a mismatch. That case has
    # its own skip in the fixture, and naming a reload here would send the
    # reader to the one thing that cannot help.
    if status.get("status") == "bridge_unreachable":
        return None
    stale = [
        (label, fix) for flag, label, fix in _STALE_COMPONENTS if status.get(flag) is True
    ]
    if not stale:
        return None
    missing = list(status.get("missing_extension_capabilities") or ())
    return "\n".join(
        [
            "the live layer would not be testing this checkout: "
            + ", ".join(label for label, _ in stale)
            + (" is" if len(stale) == 1 else " are")
            + " running a different build.",
            f"  package {status.get('package_version')}"
            f" / bridge {status.get('bridge_version')}"
            f" / extension {status.get('extension_version')}"
            f" (protocol {status.get('protocol_version')},"
            f" expected {status.get('expected_protocol_version')})",
            *([f"  the extension is missing: {', '.join(missing)}"] if missing else []),
            *(f"  {label}: {fix}" for label, fix in stale),
            "This is not a flake to re-run: a pass here would describe code that "
            "is not in the tree.",
        ]
    )


def component_versions(status: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What each process was running, narrowed to what may be published.

    Without this the evidence never says which build answered, which is the
    whole reason a stale extension could go unnoticed for a full release round.

    The fields are named one at a time rather than copied wholesale because
    `get_setup_status()` also answers where this machine keeps its state: an
    absolute state directory, the token file path and the token's fingerprint.
    This record is written into `artifacts/`, which live.yml uploads and which
    gets attached to an external review, so a whitelist keeps a field added
    upstream tomorrow out by default -- a blacklist would publish it and wait to
    be noticed. The two build stamps are safe to publish because they are
    content hashes with no local path: one came from the running worker and the
    other was derived from this checkout, so the record can show the comparison
    that produced `extension_build_verdict` without exposing this machine.
    """
    if not isinstance(status, Mapping):
        return None
    return {
        field: status.get(field)
        for field in (
            "status",
            "action",
            "package_version",
            "bridge_version",
            "extension_version",
            "protocol_version",
            "expected_protocol_version",
            "missing_extension_capabilities",
            "extension_build_verdict",
            "extension_build_enforced",
            "extension_build_stamp",
            "expected_extension_build_stamp",
            "reload_extension_required",
            "restart_bridge_required",
            "restart_mcp_session_required",
        )
    }
