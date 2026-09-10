"""A dispatched command that never replies must not hold its tab forever.

`_prune` only ever evicted operations that had already settled, so an
`in_progress` reservation whose MV3 reply was lost stayed in the registry for
the life of the daemon: the tab answered `target_busy` to every later call,
there was no terminal status to read, and enough of them exhausted admission
capacity for every requester.
"""

from __future__ import annotations

import pytest

from browsertap_mcp import pending_operations as pending_module
from browsertap_mcp.command_scope import TargetBusyError
from browsertap_mcp.pending_operations import (
    ACTIVE_STATUSES,
    OperationAccessError,
    PendingOperations,
)
from tests.test_pending_bridge_operations import make_bridge


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pending_module.time, "monotonic", lambda: now[0])
    return now


def _advance_past_ttl(clock):
    # The ceiling, which is what an operation reserved without a stated budget
    # is charged. Read from the module so a changed constant cannot leave this
    # helper advancing past a boundary that has moved.
    clock[0] += pending_module.MAX_SILENT_SECONDS + 1


def test_silent_operation_stops_reserving_its_tab(clock):
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner")
    assert operations._targets == {"browser:1": "a"}

    _advance_past_ttl(clock)
    snapshot = operations.read("a", "owner")

    assert snapshot["status"] == "abandoned"
    assert snapshot["reservation_held"] is False
    assert snapshot["abandoned_reason"] == "no_reply_within_ttl"
    assert snapshot["js_return_lost"] is True
    assert operations._targets == {}
    assert operations.pending_ids() == []


def test_the_tab_becomes_usable_again(clock):
    # The operator-visible symptom: target_busy on a tab nothing is using.
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner")
    with pytest.raises(TargetBusyError):
        operations.reserve("b", ["browser:1"], "other")

    _advance_past_ttl(clock)
    operations.reserve("b", ["browser:1"], "other")

    assert operations._targets == {"browser:1": "b"}


def test_abandonment_reopens_admission_capacity(clock, monkeypatch):
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS", 2, raising=False)
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    operations.reserve("b", ["b"], "owner")
    with pytest.raises(RuntimeError, match="capacity"):
        operations.reserve("c", ["c"], "owner")

    _advance_past_ttl(clock)
    operations.reserve("c", ["c"], "owner")
    operations.reserve("d", ["d"], "owner")

    assert set(operations.pending_ids()) == {"c", "d"}


def test_a_waiting_caller_is_never_abandoned_under_it(clock):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")

    with operations.retain_for_waiter("a", "owner"):
        _advance_past_ttl(clock)
        assert operations.read("a", "owner")["status"] == "in_progress"
        assert operations._targets == {"a": "a"}
        # The result its waiter is still waiting for.
        assert operations.complete("a", {"success": True, "data": 7}) is True

    assert operations.read("a", "owner")["wire_result"]["data"] == 7


def test_a_replied_unknown_outcome_expires_and_releases_tab(clock):
    # The timeout reply is retained as a diagnosis, but its reservation must
    # have a bounded lifetime because the extension cannot send a second reply.
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    operations.complete("a", {"success": False, "data": "cdp_timeout: x"}, outcome_unknown=True)

    _advance_past_ttl(clock)
    snapshot = operations.read("a", "owner")

    assert snapshot["status"] == "abandoned"
    assert snapshot["reservation_held"] is False
    assert snapshot["abandoned_reason"] == "unknown_outcome_reservation_ttl"
    assert snapshot["wire_result"]["data"] == "cdp_timeout: x"
    operations.reserve("b", ["a"], "other")


def test_manual_unknown_outcome_gets_a_fresh_grace_window(clock):
    operations = PendingOperations()
    operations.reserve("execution", ["a"], "owner", caller_budget=20.0)
    operations._operations["execution"].metadata["pending_execution"] = True
    operations.reserve("recovery", ["a"], "owner", kind="handle_dialog", cleanup=True)

    # Let the original execution deadline pass before the manual cleanup tells
    # us that its final result was lost. The observation must start a new,
    # bounded diagnostic window instead of being abandoned on the next read.
    clock[0] += 20 + pending_module.SILENT_GRACE_SECONDS + 1
    operations.observe_manual_execution("recovery")

    snapshot = operations.read("execution", "owner")
    assert snapshot["status"] == "outcome_unknown"
    assert snapshot["reservation_held"] is True
    assert snapshot["wire_result"]["data"]["code"] == "execution_outcome_unknown"

    clock[0] += pending_module.SILENT_GRACE_SECONDS + 20 + 1
    expired = operations.read("execution", "owner")
    assert expired["status"] == "abandoned"
    assert expired["reservation_held"] is False


def test_unknown_outcome_is_still_readable_before_its_reservation_expires(clock):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner", caller_budget=20.0)
    operations.complete("a", {"success": False, "data": "cdp_timeout: x"}, outcome_unknown=True)

    clock[0] += pending_module.SILENT_GRACE_SECONDS + 19
    snapshot = operations.read("a", "owner")

    assert snapshot["status"] == "outcome_unknown"
    assert snapshot["reservation_held"] is True
    assert snapshot["wire_result"]["data"] == "cdp_timeout: x"


def test_a_dialog_block_keeps_its_tab(clock):
    # blocked_by_dialog means a human is expected to act; the reservation is
    # what keeps another caller out of that tab meanwhile.
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner", kind="navigate")
    operations.complete("a", {"success": True, "data": {"pending_execution": True}})

    _advance_past_ttl(clock)

    assert operations.read("a", "owner")["status"] == "blocked_by_dialog"
    assert operations._targets == {"a": "a"}


def test_an_abandoned_record_is_readable_then_pruned(clock):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")

    _advance_past_ttl(clock)
    assert operations.read("a", "owner")["status"] == "abandoned"

    # Terminal, so the ordinary result TTL applies from the moment it settled.
    # A different constant from the silence deadline above, on purpose.
    clock[0] += pending_module.RESULT_TTL_SECONDS + 1
    with pytest.raises(OperationAccessError, match="operation_not_found"):
        operations.read("a", "owner")


def test_abandonment_is_not_reported_as_a_released_reservation(clock):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    _advance_past_ttl(clock)

    snapshot = operations.read("a", "owner")

    # The script may well have run. Nothing here may read as "it did not".
    assert snapshot["status"] not in ACTIVE_STATUSES
    assert snapshot["status"] != "completed"
    assert "wire_result" not in snapshot


def test_a_late_reply_cannot_land_on_an_abandoned_operation(clock):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner", reply_transport="ws", reply_owner=object())
    _advance_past_ttl(clock)
    operations.read("a", "owner")

    assert operations.accepts_reply("a", "ws", operations._operations["a"].reply_owner) is False


def test_get_execute_js_result_reports_the_unknown_outcome(clock):
    bridge = make_bridge()
    operations = bridge._operation_state()
    operations.reserve("a", ["a"], "owner")
    _advance_past_ttl(clock)

    result = bridge.get_execute_js_result("a", timeout=0, requester_id="owner")

    assert result["status"] == "unknown"
    assert result["delivery_state"] == "delivered_no_result"
    assert result["retry_safe"] is False
    assert result["js_return_lost"] is True
    assert result["reservation_held"] is False
    assert result["abandoned_reason"] == "no_reply_within_ttl"


def test_an_operation_is_kept_until_the_ttl_actually_passes(clock):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")

    clock[0] += pending_module.MAX_SILENT_SECONDS - 1
    snapshot = operations.read("a", "owner")

    assert snapshot["status"] == "in_progress"
    assert snapshot["reservation_held"] is True
    assert operations._targets == {"a": "a"}


def test_the_expiry_clock_starts_at_reservation_not_at_the_first_read(clock):
    operations = PendingOperations()
    operations.reserve("a", ["a"], "owner")
    clock[0] += pending_module.MAX_SILENT_SECONDS - 1
    assert operations.read("a", "owner")["status"] == "in_progress"

    clock[0] += 2

    assert operations.read("a", "owner")["status"] == "abandoned"


def test_a_short_budget_releases_its_tab_sooner_than_the_ceiling(clock):
    # The whole point of the per-operation deadline: a 20-second scan must not
    # hold a tab for the ten minutes a two-minute capture might need.
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner", caller_budget=20.0)

    clock[0] += pending_module.SILENT_GRACE_SECONDS + 21
    snapshot = operations.read("a", "owner")

    assert snapshot["status"] == "abandoned"
    assert operations._targets == {}
    # Under the ceiling, so a sweep still reading the module constant would
    # have left this operation in_progress.
    assert clock[0] - 1000.0 < pending_module.MAX_SILENT_SECONDS


def test_a_short_budget_still_gets_its_full_grace_window(clock):
    # The grace window is added to the budget, not shared with it: the caller
    # has stopped waiting, but the reply is exactly what makes the record worth
    # keeping, and an evicted MV3 worker needs time to restart and deliver it.
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner", caller_budget=20.0)

    clock[0] += 20 + pending_module.SILENT_GRACE_SECONDS - 1

    assert operations.read("a", "owner")["status"] == "in_progress"
    assert operations._targets == {"browser:1": "a"}


def test_a_long_budget_is_clamped_to_the_ceiling(clock):
    # A caller cannot buy an unbounded hold by naming a large timeout.
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner", caller_budget=86_400.0)

    clock[0] += pending_module.MAX_SILENT_SECONDS + 1

    assert operations.read("a", "owner")["status"] == "abandoned"


def test_a_sub_second_budget_still_holds_its_tab_for_the_grace_window(clock):
    # A sub-second timeout says the caller is impatient, not that the browser
    # will be. The grace window is the floor here -- there is no separate
    # minimum constant, because adding one no input could reach would read as a
    # bound that is enforced.
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner", caller_budget=0.2)

    clock[0] += pending_module.SILENT_GRACE_SECONDS - 1
    assert operations.read("a", "owner")["status"] == "in_progress"

    clock[0] += 2
    assert operations.read("a", "owner")["status"] == "abandoned"


@pytest.mark.parametrize(
    "budget",
    [None, 0, -5.0, float("nan"), float("inf"), object(), "not-a-number"],
    ids=["absent", "zero", "negative", "nan", "inf", "object", "unparsable"],
)
def test_an_unusable_budget_falls_back_to_the_ceiling(budget):
    # Fail long, not short: a reservation released while its command is still
    # running is a wrong answer, one released late is only a slow one.
    assert (
        pending_module.silent_deadline_seconds(budget)
        == pending_module.MAX_SILENT_SECONDS
    )


def test_a_numeric_string_budget_is_honoured_not_discarded(budget="20"):
    # float() accepts it, so treating it as unusable would silently charge the
    # ceiling to a caller that did state a budget.
    assert pending_module.silent_deadline_seconds(budget) == 20 + pending_module.SILENT_GRACE_SECONDS


def test_the_applied_deadline_is_recorded_on_the_abandoned_record(clock):
    # Which deadline was charged, so the release is explicable without
    # re-deriving it from a caller timeout nobody kept.
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner", caller_budget=20.0)
    _advance_past_ttl(clock)

    snapshot = operations.read("a", "owner")

    assert snapshot["abandoned_after_seconds"] == 20.0 + pending_module.SILENT_GRACE_SECONDS


def test_execute_js_charges_the_hold_against_its_own_timeout(monkeypatch):
    # Without this the new parameter has no reader and the field is decoration.
    bridge = make_bridge()
    seen: dict[str, object] = {}
    inner = PendingOperations.reserve

    def spy(self, *args, **kwargs):
        seen.update(kwargs)
        return inner(self, *args, **kwargs)

    monkeypatch.setattr(PendingOperations, "reserve", spy)
    bridge.execute_js("return 1", timeout=20, session_id="browser:1")

    assert 0 < float(seen["caller_budget"]) <= 20


def test_ext_cmd_charges_the_hold_against_its_own_timeout(monkeypatch):
    bridge = make_bridge()
    seen: dict[str, object] = {}
    inner = PendingOperations.reserve

    def spy(self, *args, **kwargs):
        seen.update(kwargs)
        return inner(self, *args, **kwargs)

    monkeypatch.setattr(PendingOperations, "reserve", spy)
    bridge.ext_cmd({"cmd": "tabs", "method": "list"}, timeout=15, client_id="browser")

    assert 0 < float(seen["caller_budget"]) <= 15


def test_a_recovery_borrow_is_released_too(clock):
    operations = PendingOperations()
    operations.reserve("a", ["browser:1"], "owner")
    operations.complete("a", {"success": False}, outcome_unknown=True)
    operations.reserve(
        "recover", ["browser:1"], "owner", cleanup=True, close_targets=True,
    )
    assert operations._recoveries == {"browser:1": "recover"}

    _advance_past_ttl(clock)
    operations.read("recover", "owner")

    assert operations._recoveries.get("browser:1") != "recover"
