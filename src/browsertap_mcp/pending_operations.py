"""Daemon-owned tab reservations that outlive a caller's response timeout."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .command_scope import TargetBusyError

RESULT_TTL_SECONDS = 600.0
MAX_COMPLETED_OPERATIONS = 512
MAX_ACTIVE_OPERATIONS = 1024
MAX_RECOVERY_OPERATIONS = 128

# One requester's share of the global budget above.
#
# The global limit alone is not a fair-use bound: one bridge serves several
# agents at once, so a single requester leaking reservations could hold all 1024
# slots and every other agent would get operation_capacity_exceeded for work it
# never oversubscribed. The silence sweep bounds that in time (a leak clears
# after MAX_SILENT_SECONDS) but not in ownership -- inside the window the
# starvation is total.
#
# A quarter is a share, not a reservation: three idle requesters do not entitle a
# fourth to more, and nothing is held back for a requester that never arrives.
MAX_ACTIVE_OPERATIONS_PER_REQUESTER = MAX_ACTIVE_OPERATIONS // 4
ACTIVE_STATUSES = frozenset({"in_progress", "outcome_unknown", "blocked_by_dialog"})

# How long a dispatched command may stay silent before its target is released.
#
# The grace window is added to the caller's own budget rather than replacing it:
# when a caller stops waiting the script may still be running, and a reply on
# the original transport is still worth landing. A replacement WebSocket cannot
# complete an operation owned by the old socket. After an unknown outcome, the
# bounded window instead retains its diagnostic receipt for observation; it is
# not a promise of replay or result delivery after worker eviction/reconnect.
#
# The ceiling matches RESULT_TTL_SECONDS by coincidence of scale, not by
# derivation: a settled result and an unanswered command are simply not worth
# retaining for different orders of magnitude. Keep them independent.
#
# There is deliberately no separate floor constant. The grace window is already
# the floor -- it is added to the budget, so even a sub-second timeout keeps its
# tab for SILENT_GRACE_SECONDS. A `max(MIN, ...)` next to that would be a bound
# no input can reach, which is worse than no bound at all: it reads as enforced.
SILENT_GRACE_SECONDS = 60.0
MAX_SILENT_SECONDS = 600.0


def silent_deadline_seconds(caller_budget: float | None) -> float:
    """Resolve how long one operation may stay silent, from its own budget.

    An unusable budget (absent, non-numeric, NaN, infinite, non-positive) falls
    back to the ceiling. That direction is deliberate: guessing short would
    release a tab while its command is still legitimately running, and a
    reservation released too early is a wrong answer, where one released too late
    is only a slow one.
    """
    try:
        budget = float(caller_budget)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MAX_SILENT_SECONDS
    if not math.isfinite(budget) or budget <= 0:
        return MAX_SILENT_SECONDS
    return min(MAX_SILENT_SECONDS, budget + SILENT_GRACE_SECONDS)


class OperationCapacityError(RuntimeError):
    error_code = "operation_capacity_exceeded"
    delivery_state = "undelivered"
    retry_safe = True

    def __init__(self, active: int, limit: int, *, scope: str = "bridge") -> None:
        # Which budget ran out decides what the caller should do, so the scope is
        # a field and not just wording: at "bridge" the caller is one of several
        # and waiting may be the whole fix, at "requester" the pending work is
        # its own and no amount of waiting on others will help.
        self.diagnostics = {
            "active_operations": active, "operation_limit": limit, "capacity_scope": scope,
        }
        subject = (
            "Bridge operation capacity reached" if scope == "bridge"
            else "This requester's operation capacity reached"
        )
        super().__init__(
            f"{subject} ({active}/{limit}); nothing was dispatched. "
            "Collect pending results or finish target lifecycles before retrying."
        )


class OperationAccessError(RuntimeError):
    delivery_state = "undelivered"
    retry_safe = False

    def __init__(self, operation_id: str, code: str) -> None:
        self.error_code = code
        self.diagnostics = {"operation_id": operation_id}
        super().__init__(f"Execution operation {operation_id}: {code}")


@dataclass
class _Operation:
    operation_id: str
    requester_id: str | None
    targets: tuple[str, ...]
    kind: str
    acknowledged: bool = False
    status: str = "in_progress"
    result: dict[str, Any] | None = None
    completed_at: float | None = None
    # A late reply adds evidence to an abandoned receipt; it does not settle
    # the operation again or restart its retention deadline.
    late_result: dict[str, Any] | None = None
    late_reply_at: float | None = None
    late_reply_closed: bool = False
    # The lambda is not decoration: a bare ``default_factory=time.monotonic``
    # captures the function object when this class is defined, so a test that
    # substitutes the module clock would be silently ignored here while every
    # other timestamp in this file honoured it -- the two would then be
    # compared against each other. Resolve the attribute per call instead.
    started_at: float = field(default_factory=lambda: time.monotonic())
    # ``outcome_unknown`` can be observed long after dispatch (for example
    # when a manual-dialog lease disappears). Its bounded retention window
    # starts when that uncertainty is recorded, not when the original command
    # was sent.
    unknown_since: float | None = None
    silent_after: float = MAX_SILENT_SECONDS
    metadata: dict[str, Any] = field(default_factory=dict)
    last_wire_result: dict[str, Any] | None = None
    superseded_recoveries: dict[str, str] = field(default_factory=dict)
    # Reply routing is private operation state.  In particular, never put a
    # socket object in ``metadata``: that dict is returned to callers as part
    # of a result snapshot.
    reply_transport: str | None = None
    reply_owner: Any = None
    waiters: int = 0

    @property
    def accepts_late_result(self) -> bool:
        return self.status == "abandoned" and self.late_result is None and not self.late_reply_closed


class PendingOperations:
    def __init__(self, *, requester_liveness: Callable[[str | None], str] | None = None) -> None:
        self._lock = threading.RLock()
        self._operations: dict[str, _Operation] = {}
        self._targets: dict[str, str] = {}
        self._recoveries: dict[str, str] = {}
        self._requester_liveness = requester_liveness

    def _owner_is_dead(self, requester_id: str | None) -> bool:
        if self._requester_liveness is None or requester_id is None:
            return False
        try:
            return self._requester_liveness(requester_id) == "dead"
        except Exception:
            return False

    def _abandon_silent_operations(self, now: float) -> None:
        """Release a tab whose dispatched command never produced any reply.

        A side-effecting in-progress operation holds its target while the
        execution is unresolved. Read-only wait probes may release that lease
        earlier while keeping the same receipt and retention bounds. With no
        expiry a hold was permanent: one lost MV3 reply left the
        tab answering target_busy for the life of the daemon, with no terminal
        status for the caller to read and no id to poll, and enough of them
        exhausted admission capacity for every requester.

        How long "plausible" lasts is per operation, from the budget its caller
        actually chose (``silent_deadline_seconds``). A 20-second scan and a
        two-minute capture do not deserve the same hold, and charging both the
        ceiling made the common case pay for the rare one.

        Abandonment is terminal but not a verdict on the page: the script may
        well have run. The status says exactly that, and the reservation goes
        away once the bounded observation window ends. A later authenticated
        terminal reply can still be retained beside the abandonment receipt,
        without restoring the reservation or extending result retention.
        A blocked dialog is deliberately left alone: a human recovery command
        still has a live path back to the page.  An ``outcome_unknown`` reply is
        different.  For a CDP timeout the extension has already answered the
        command with its final timeout frame, so no late result can arrive to
        justify holding a target forever. Manual-dialog cleanup can also record
        an unknown outcome without receiving such a frame; that path anchors
        the same bounded deadline at the observation time. Keep the diagnostic
        receipt, but expire the reservation on the bounded deadline.
        """
        for operation in list(self._operations.values()):
            age_start = operation.started_at
            if operation.status == "outcome_unknown" and operation.unknown_since is not None:
                age_start = operation.unknown_since
            if operation.waiters or now - age_start <= operation.silent_after:
                continue
            if operation.status == "in_progress":
                if operation.result is not None:
                    continue
                operation.metadata.update({
                    "abandoned_reason": "no_reply_within_ttl",
                    # Which deadline was applied, so an operator reading the record
                    # does not have to re-derive it from the caller's timeout.
                    "abandoned_after_seconds": round(operation.silent_after, 3),
                    "js_return_lost": True,
                })
            elif operation.status == "outcome_unknown":
                operation.metadata.update({
                    "abandoned_reason": "unknown_outcome_reservation_ttl",
                    "abandoned_after_seconds": round(operation.silent_after, 3),
                    "js_return_lost": True,
                    "reservation_released": True,
                })
            else:
                continue
            operation.status = "abandoned"
            operation.completed_at = now
            self._release(operation)

    def _prune(self) -> None:
        now = time.monotonic()
        self._abandon_silent_operations(now)
        completed = sorted(
            (op.completed_at, key)
            for key, op in self._operations.items()
            if op.completed_at is not None and not op.waiters
        )
        excess = max(0, len(completed) - MAX_COMPLETED_OPERATIONS)
        for index, (stamp, key) in enumerate(completed):
            if index < excess or now - stamp > RESULT_TTL_SECONDS:
                self._operations.pop(key, None)

    def reserve(
        self,
        operation_id: str,
        targets: list[str],
        requester_id: str | None,
        *,
        kind: str = "execute_js",
        cleanup: bool = False,
        close_targets: bool = False,
        recover_dead_owner: bool = False,
        metadata: dict[str, Any] | None = None,
        reply_transport: str | None = None,
        reply_owner: Any = None,
        caller_budget: float | None = None,
    ) -> None:
        with self._lock:
            self._prune()
            if operation_id in self._operations:
                raise OperationAccessError(operation_id, "operation_already_exists")
            # Unknown side effects cannot be evicted. Refuse before dispatch,
            # with bounded headroom for dialog settlement and target closure.
            def is_active(op: _Operation) -> bool:
                return op.status in ACTIVE_STATUSES or op.waiters > 0

            active = sum(is_active(op) for op in self._operations.values())
            limit = MAX_ACTIVE_OPERATIONS + (MAX_RECOVERY_OPERATIONS if cleanup else 0)
            if active >= limit:
                raise OperationCapacityError(active, limit)
            # The global budget first: it is the harder bound, and reporting the
            # narrower one while the bridge itself is full would send a caller
            # off to tidy up its own operations when that cannot help.
            #
            # Cleanup is exempt. It is how a requester at its quota releases the
            # tabs that put it there, so charging it against the same quota would
            # make the limit self-sealing -- full, and no way to become less full.
            #
            # An anonymous requester is exempt too. All of them share the single
            # None key, so a per-requester cap on that key would be a cap on
            # everyone-without-an-id collectively: one such caller could starve
            # another, which is the very failure this bound exists to prevent.
            # They stay bounded by the global limit and the silence sweep.
            if requester_id is not None and not cleanup:
                mine = sum(
                    is_active(op) for op in self._operations.values()
                    if op.requester_id == requester_id
                )
                if mine >= MAX_ACTIVE_OPERATIONS_PER_REQUESTER:
                    raise OperationCapacityError(
                        mine, MAX_ACTIVE_OPERATIONS_PER_REQUESTER, scope="requester",
                    )
            held: list[str] = []
            borrowed: list[str] = []
            replaced: dict[str, str] = {}
            for target in sorted(set(targets)):
                incumbent_id = self._targets.get(target)
                recovery_id = self._recoveries.get(target)
                recovery = self._operations.get(recovery_id or "")
                can_replace_recovery = (
                    close_targets and cleanup and recovery is not None
                    and recovery.status == "outcome_unknown"
                    and (
                        recovery.requester_id == requester_id
                        or (recover_dead_owner and self._owner_is_dead(recovery.requester_id))
                    )
                )
                if incumbent_id is not None:
                    incumbent = self._operations[incumbent_id]
                    can_recover = cleanup and (
                        incumbent.requester_id == requester_id
                        or (recover_dead_owner and self._owner_is_dead(incumbent.requester_id))
                    )
                    if can_recover and recovery_id is None:
                        borrowed.append(target)
                        continue
                    if can_recover and can_replace_recovery:
                        borrowed.append(target)
                        replaced[target] = str(recovery_id)
                        continue
                    exc = TargetBusyError(target, dispatched=False)
                    exc.diagnostics.update({
                        "operation_in_progress": True,
                        "operation_kind": incumbent.kind,
                    })
                    raise exc
                if can_replace_recovery:
                    borrowed.append(target)
                    replaced[target] = str(recovery_id)
                    continue
                if target in self._recoveries:
                    raise TargetBusyError(target, dispatched=False)
                held.append(target)
            self._operations[operation_id] = _Operation(
                operation_id, requester_id, tuple(held + borrowed), kind,
                metadata=dict(metadata or {}), superseded_recoveries=dict(replaced),
                reply_transport=reply_transport, reply_owner=reply_owner,
                silent_after=silent_deadline_seconds(caller_budget),
            )
            for target in held:
                self._targets[target] = operation_id
            for target in borrowed:
                self._recoveries[target] = operation_id

    def acknowledge(self, operation_id: str) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is not None:
                operation.acknowledged = True

    def accepts_reply(
        self, operation_id: Any, transport: str, owner: Any = None, *, allow_late: bool = False,
    ) -> bool:
        """Whether an inbound reply belongs to this operation and transport.

        Result handlers may opt into an abandoned record's first late result.
        ACKs remain limited to active operations.

        WebSocket ownership is identity-based because a client id is supplied by
        the peer itself.  HTTP long-poll ownership is a session-id string.  A
        missing owner is accepted only for an operation whose transport is
        already pinned to HTTP, preserving old userscript result clients while
        still rejecting arbitrary operation ids.
        """
        key = str(operation_id) if operation_id is not None else ""
        with self._lock:
            self._prune()
            operation = self._operations.get(key)
            if operation is None or not (
                operation.status in ACTIVE_STATUSES or allow_late and operation.accepts_late_result
            ):
                return False
            if operation.reply_transport != transport:
                return False
            expected = operation.reply_owner
            # The legacy HTTP userscript reply shape has no sessionId.  Its
            # operation id is the only caller-provided correlation value, so
            # keep that protocol compatible while still rejecting an explicit
            # mismatched owner below.  WebSocket replies never get this
            # exemption because their socket identity is available.
            if transport == "http" and owner is None:
                return True
            if expected is None:
                return transport == "http"
            if transport == "ws" or transport == "ext_ws":
                return expected is owner
            return expected == owner

    def pending_ids(self) -> list[str]:
        with self._lock:
            return [key for key, op in self._operations.items() if op.status in ACTIVE_STATUSES]

    def retained_ids(self) -> set[str]:
        """The same retention boundary applies to the bridge's wire caches."""
        with self._lock:
            self._prune()
            return set(self._operations)

    @contextmanager
    def retain_for_waiter(self, operation_id: str, requester_id: str | None) -> Iterator[None]:
        """Keep a result until its waiting call returns, even during a reply burst."""
        with self._lock:
            self.read(operation_id, requester_id)
            operation = self._operations[operation_id]
            operation.waiters += 1
        try:
            yield
        finally:
            with self._lock:
                operation.waiters -= 1
                self._prune()

    def _holds_targets(self, operation: _Operation) -> bool:
        return any(
            self._targets.get(target) == operation.operation_id
            or self._recoveries.get(target) == operation.operation_id
            for target in operation.targets
        )

    def release_wait_probe(self, operation_id: str, requester_id: str | None) -> bool:
        """End a server-owned read-only probe's lease, keeping its reply receipt.

        Called by the bridge at its existing synchronous wait deadline, so
        releasing does not require a second request after the tool times out.
        Caller JS, uncertain outcomes and dialog recovery keep their leases.
        """
        with self._lock:
            self.read(operation_id, requester_id)
            operation = self._operations[operation_id]
            if (operation.kind != "wait_probe" or operation.status != "in_progress"
                    or not self._holds_targets(operation)
                    or operation.superseded_recoveries
                    or any(self._recoveries.get(target) == operation_id for target in operation.targets)):
                return False
            held = [target for target in operation.targets if self._targets.get(target) == operation_id]
            self._release(operation)
            operation.metadata.update({
                "reservation_released": True,
                "released_reason": "read_only_wait_probe_timeout",
                "released_targets": held,
            })
            return True

    def _release(self, operation: _Operation) -> None:
        for target in operation.targets:
            if self._targets.get(target) == operation.operation_id:
                self._targets.pop(target, None)
            if self._recoveries.get(target) == operation.operation_id:
                self._recoveries.pop(target, None)

        for target, recovery_id in operation.superseded_recoveries.items():
            if self._recoveries.get(target) is not None:
                continue
            recovery = self._operations.get(str(recovery_id))
            if (recovery is not None and recovery.status in ACTIVE_STATUSES
                    and target in recovery.targets):
                self._recoveries[target] = recovery.operation_id

    def complete(
        self, operation_id: str, result: dict[str, Any], *, outcome_unknown: bool = False,
        on_active_reply: Callable[[], None] | None = None,
    ) -> bool:
        with self._lock:
            self._prune()
            operation = self._operations.get(operation_id)
            if operation is None:
                return False
            data = result.get("data")
            pending = (
                result.get("success") is True and isinstance(data, dict)
                and data.get("pending_execution") is True
            )
            if operation.status not in ACTIVE_STATUSES:
                if (operation.accepts_late_result and not outcome_unknown and not pending
                        and operation.last_wire_result is not result):
                    operation.late_result = dict(result)
                    operation.late_reply_at = time.monotonic()
                return True
            if operation.last_wire_result is result:
                return False
            # The bridge must settle capture/dialog state before releasing a
            # target. Keep that work under this lock and only for an active
            # reply, so expiry cannot turn it into a late mutation of a successor.
            if on_active_reply is not None:
                on_active_reply()
            operation.last_wire_result = result
            operation.result = dict(result)
            if outcome_unknown:
                if operation.status != "outcome_unknown":
                    operation.unknown_since = time.monotonic()
                operation.status = "outcome_unknown"
                return False
            if (result.get("success") is True and isinstance(data, dict)
                    and data.get("pending_execution") is True
                    and (data.get("__btap_dialog_result") is True or operation.kind == "navigate")):
                operation.status = "blocked_by_dialog"
                operation.metadata["pending_execution"] = True
                return False
            operation.status = "completed"
            operation.completed_at = time.monotonic()
            self._release(operation)
            self._prune()
            return True

    def observe_manual_execution(self, recovery_id: str) -> None:
        """A missing manual lease does not prove that its page script ended."""
        with self._lock:
            recovery = self._operations.get(recovery_id)
            if recovery is None or recovery.kind != "handle_dialog":
                return
            for target in recovery.targets:
                operation_id = self._targets.get(target)
                operation = self._operations.get(operation_id or "")
                if (operation is None or operation is recovery
                        or operation.requester_id != recovery.requester_id
                        or not operation.metadata.get("pending_execution")):
                    continue
                operation.result = {
                    "success": False,
                    "data": {
                        "code": "execution_outcome_unknown",
                        "message": "The manual execution is no longer registered; its final result was not reported",
                        "dispatched": True,
                        "may_have_executed": True,
                    },
                }
                if operation.status != "outcome_unknown":
                    operation.unknown_since = time.monotonic()
                operation.status = "outcome_unknown"
                operation.metadata.update({
                    "pending_execution": False, "js_return_lost": True,
                    "last_observation": "handle_dialog",
                })

    def finish_target(self, target: str, reason: str) -> None:
        with self._lock:
            self._targets.pop(target, None)
            self._recoveries.pop(target, None)
            operation_ids = {
                operation.operation_id
                for operation in self._operations.values()
                if (operation.status in ACTIVE_STATUSES or operation.status == "abandoned")
                and target in operation.targets
            }
            for operation_id in operation_ids:
                if operation_id is None:
                    continue
                operation = self._operations.get(operation_id)
                if operation is None:
                    continue
                operation.targets = tuple(item for item in operation.targets if item != target)
                if operation.targets:
                    continue
                if operation.status == "abandoned":
                    # Keep the original receipt and any already observed reply,
                    # but the ended lifecycle cannot supply new late evidence.
                    operation.late_reply_closed = True
                    continue
                operation.result = None
                operation.status = "lifecycle_ended"
                operation.metadata["lifecycle_reason"] = reason
                operation.completed_at = time.monotonic()
                self._release(operation)
            # A close replacement may restore an older recovery while its
            # operation is being released. The target lifecycle event wins;
            # no reservation for the removed tab may survive this method.
            self._targets.pop(target, None)
            self._recoveries.pop(target, None)

    def forget(self, operation_id: str) -> None:
        with self._lock:
            operation = self._operations.pop(operation_id, None)
            if operation is not None:
                self._release(operation)

    def read(
        self, operation_id: str, requester_id: str | None, *, consume: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._prune()
            operation = self._operations.get(operation_id)
            if operation is None:
                raise OperationAccessError(operation_id, "operation_not_found")
            if operation.requester_id != requester_id:
                raise OperationAccessError(operation_id, "operation_owner_mismatch")
            status = operation.status
            if status == "consumed":
                raise OperationAccessError(operation_id, "operation_consumed")
            result = {
                **operation.metadata,
                "operation_id": operation_id,
                "status": status,
                "acknowledged": operation.acknowledged,
                "reservation_held": self._holds_targets(operation),
            }
            if operation.result is not None:
                result["wire_result"] = dict(operation.result)
            if operation.late_result is not None:
                result["late_result"] = dict(operation.late_result)
                if operation.late_reply_at is not None:
                    result["late_reply_age"] = round(max(0.0, time.monotonic() - operation.late_reply_at), 3)
            if consume and status not in ACTIVE_STATUSES:
                operation.result = None
                operation.last_wire_result = None
                operation.late_result = None
                operation.late_reply_at = None
                operation.status = "consumed"
            return result
