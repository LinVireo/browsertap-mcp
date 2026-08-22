"""The live layer's preconditions, as code instead of prose.

The live suite drives the user's real browser, so it came with two written
rules: nobody may be using that browser while it runs, and the tab inventory it
started with has to be the inventory it leaves behind. Both were maintainer
notes, which is the same as not having them -- a violated precondition showed up
as a mystery failure, and the "did the suite close one of my tabs" question was
answered by hand, after the fact, from memory.

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
# break the run.
IDLE_WINDOW_SECONDS = 1.5

# Set to proceed against a browser someone is using, and to downgrade the
# end-of-run inventory check to a warning. It exists because "my browser is
# never idle" must not turn into "I stopped running the live layer", and every
# use of it is recorded in the report so the evidence is not silently weaker.
OVERRIDE_ENV = "BTAP_LIVE_ALLOW_BUSY_BROWSER"


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


def compare(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Diff two inventories.

    `disturbed` is the part that matters for a verdict -- a tab appeared,
    vanished, or went somewhere else. Focus moving is reported separately: it is
    proof that a human is present when nothing else moved, but the suite raises
    tabs itself, so on its own it is not damage.
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
    return {
        "opened": opened,
        "closed": closed,
        "navigated": navigated,
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
    focus = diff.get("focus_moved")
    if focus:
        lines.append(f"foreground tab: {focus.get('from')} -> {focus.get('to')}")
    return lines


def busy_browser_reason(diff: Mapping[str, Any]) -> str | None:
    """Why the live layer must not start, or None when the browser is idle.

    A browser that changes on its own during the idle window has a human in it,
    and the live suite both drives that browser and asserts on what it finds
    there. Running anyway produces a result that describes neither the product
    nor the human.
    """
    if not diff.get("changed"):
        return None
    return "\n".join(
        [
            "the browser is in use: its tabs changed while the live layer was "
            f"waiting {IDLE_WINDOW_SECONDS}s for it to settle.",
            *(f"  {line}" for line in describe(diff)),
            "Leave the browser alone and re-run, or set "
            f"{OVERRIDE_ENV}=1 to accept the weaker result.",
        ]
    )


def drift_problem(diff: Mapping[str, Any]) -> str | None:
    """Why the browser did not come out the way it went in, or None.

    The suite is allowed to raise tabs; it is not allowed to leave one behind,
    close one it did not create, or navigate a page the user was reading.
    """
    if not diff.get("disturbed"):
        return None
    return "\n".join(
        [
            "the live layer did not leave the browser as it found it "
            f"({diff.get('tabs_before')} tabs before, {diff.get('tabs_after')} after):",
            *(f"  {line}" for line in describe(diff)),
            "A fixture leak and a human using the browser look identical here; "
            "check which one it was before believing either.",
        ]
    )
