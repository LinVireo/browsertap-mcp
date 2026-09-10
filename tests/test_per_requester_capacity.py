"""One requester must not be able to spend the whole bridge's admission budget.

The global cap bounds how much unknown side effect the daemon will carry, but on
its own it is not a fair-use bound: one bridge serves several agents, so a single
requester leaking reservations held every slot and every other agent was told
`operation_capacity_exceeded` for work it never oversubscribed. The silence sweep
bounds that in time -- a leak clears after `MAX_SILENT_SECONDS` -- but not in
ownership, and inside that window the starvation is total.
"""

from __future__ import annotations

import pytest

from browsertap_mcp import pending_operations as pending_module
from browsertap_mcp.pending_operations import PendingOperations


@pytest.fixture
def small_share(monkeypatch):
    """A two-slot share inside a roomy bridge, so only the narrow bound can fire."""
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS", 64, raising=False)
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS_PER_REQUESTER", 2, raising=False)
    monkeypatch.setattr(pending_module, "MAX_RECOVERY_OPERATIONS", 1, raising=False)


def _fill(operations, requester, count, *, offset=0):
    for index in range(count):
        operations.reserve(f"{requester}-{index + offset}", [f"t{requester}{index + offset}"], requester)


def test_a_requester_cannot_spend_more_than_its_share(small_share):
    operations = PendingOperations()
    _fill(operations, "a", 2)

    with pytest.raises(RuntimeError, match="capacity") as caught:
        operations.reserve("a-2", ["ta2"], "a")

    assert caught.value.error_code == "operation_capacity_exceeded"
    assert caught.value.diagnostics["capacity_scope"] == "requester"
    assert caught.value.diagnostics["operation_limit"] == 2
    # Nothing was dispatched, so the refusal must not have taken the tab either.
    assert "ta2" not in operations._targets


def test_one_requester_at_its_limit_does_not_block_another(small_share):
    # The defect this bound exists for: agent A leaks, agent B is told the bridge
    # is full while 62 of 64 slots are free.
    operations = PendingOperations()
    _fill(operations, "a", 2)

    operations.reserve("b-0", ["tb0"], "b")
    operations.reserve("b-1", ["tb1"], "b")

    assert operations._targets["tb0"] == "b-0"
    assert operations._targets["tb1"] == "b-1"


def test_the_global_limit_still_binds_across_requesters(monkeypatch):
    # A share is a share, not a licence: several requesters each under their own
    # quota may still not exceed what the daemon will carry in total.
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS", 2, raising=False)
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS_PER_REQUESTER", 100, raising=False)
    operations = PendingOperations()
    operations.reserve("a-0", ["ta0"], "a")
    operations.reserve("b-0", ["tb0"], "b")

    with pytest.raises(RuntimeError, match="capacity") as caught:
        operations.reserve("c-0", ["tc0"], "c")

    # The wider bound is reported, because tidying up c's own work cannot help.
    assert caught.value.diagnostics["capacity_scope"] == "bridge"


def test_the_bridge_limit_is_reported_ahead_of_the_requester_limit(monkeypatch):
    """When both budgets are exhausted, the caller needs the one it cannot fix."""
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS", 2, raising=False)
    monkeypatch.setattr(pending_module, "MAX_ACTIVE_OPERATIONS_PER_REQUESTER", 2, raising=False)
    operations = PendingOperations()
    _fill(operations, "a", 2)

    with pytest.raises(RuntimeError) as caught:
        operations.reserve("a-2", ["ta2"], "a")

    assert caught.value.diagnostics["capacity_scope"] == "bridge"


def test_releasing_its_own_work_restores_a_requester_s_share(small_share):
    operations = PendingOperations()
    _fill(operations, "a", 2)
    operations.complete("a-0", {"success": True, "data": 1})
    operations.finish_target("ta0", "a")

    operations.reserve("a-2", ["ta2"], "a")

    assert operations._targets["ta2"] == "a-2"


def test_cleanup_is_exempt_so_the_quota_cannot_seal_itself(small_share):
    # Cleanup is how a requester at its quota releases the tabs that put it
    # there. Charging it against the same quota would leave a caller full with
    # no way to become less full.
    operations = PendingOperations()
    operations.reserve("a-0", ["ta0"], "a")
    operations.complete("a-0", {"success": False}, outcome_unknown=True)
    operations.reserve("a-1", ["ta1"], "a")

    operations.reserve(
        "a-recover", ["ta0"], "a", cleanup=True, close_targets=True,
    )

    assert operations._recoveries["ta0"] == "a-recover"


def test_anonymous_requesters_are_not_capped_as_one_body(small_share):
    # They all share the single None key, so a per-requester cap on that key
    # would be a cap on everyone-without-an-id collectively -- one anonymous
    # caller starving another is the failure this bound exists to prevent.
    operations = PendingOperations()

    for index in range(5):
        operations.reserve(f"anon-{index}", [f"tanon{index}"], None)

    assert len(operations.pending_ids()) == 5


def test_a_settled_operation_no_longer_counts_against_the_share(small_share):
    # Only unknown side effect is worth refusing new work over; a result already
    # in hand is not.
    operations = PendingOperations()
    _fill(operations, "a", 2)
    operations.complete("a-0", {"success": True, "data": 1})
    operations.complete("a-1", {"success": True, "data": 2})

    operations.reserve("a-2", ["ta2"], "a")

    assert operations._targets["ta2"] == "a-2"


def test_an_unread_uncertain_result_still_counts(small_share):
    # outcome_unknown holds a tab and an unread diagnosis, so it is exactly the
    # state that must not be free to accumulate.
    operations = PendingOperations()
    operations.reserve("a-0", ["ta0"], "a")
    operations.complete("a-0", {"success": False}, outcome_unknown=True)
    operations.reserve("a-1", ["ta1"], "a")

    with pytest.raises(RuntimeError, match="capacity") as caught:
        operations.reserve("a-2", ["ta2"], "a")

    assert caught.value.diagnostics["capacity_scope"] == "requester"


def test_the_default_share_is_a_quarter_of_the_bridge_budget():
    # Computed, not restated: a literal here would keep asserting 256 after the
    # global budget moved.
    assert (
        pending_module.MAX_ACTIVE_OPERATIONS_PER_REQUESTER
        == pending_module.MAX_ACTIVE_OPERATIONS // 4
    )
    assert pending_module.MAX_ACTIVE_OPERATIONS_PER_REQUESTER < pending_module.MAX_ACTIVE_OPERATIONS
