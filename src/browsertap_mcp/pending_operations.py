"""Daemon-owned tab reservations that outlive a caller's response timeout."""

from __future__ import annotations

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
ACTIVE_STATUSES = frozenset({"in_progress", "outcome_unknown", "blocked_by_dialog"})


class OperationCapacityError(RuntimeError):
    error_code = "operation_capacity_exceeded"
    delivery_state = "undelivered"
    retry_safe = True

    def __init__(self, active: int, limit: int) -> None:
        self.diagnostics = {"active_operations": active, "operation_limit": limit}
        super().__init__(
            f"Bridge operation capacity reached ({active}/{limit}); nothing was dispatched. "
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
    metadata: dict[str, Any] = field(default_factory=dict)
    last_wire_result: dict[str, Any] | None = None
    superseded_recoveries: dict[str, str] = field(default_factory=dict)
    # Reply routing is private operation state.  In particular, never put a
    # socket object in ``metadata``: that dict is returned to callers as part
    # of a result snapshot.
    reply_transport: str | None = None
    reply_owner: Any = None
    waiters: int = 0


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

    def _prune(self) -> None:
        now = time.monotonic()
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
    ) -> None:
        with self._lock:
            self._prune()
            if operation_id in self._operations:
                raise OperationAccessError(operation_id, "operation_already_exists")
            # Unknown side effects cannot be evicted. Refuse before dispatch,
            # with bounded headroom for dialog settlement and target closure.
            active = sum(
                op.status in ACTIVE_STATUSES or op.waiters > 0
                for op in self._operations.values()
            )
            limit = MAX_ACTIVE_OPERATIONS + (MAX_RECOVERY_OPERATIONS if cleanup else 0)
            if active >= limit:
                raise OperationCapacityError(active, limit)
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

    def accepts_reply(self, operation_id: Any, transport: str, owner: Any = None) -> bool:
        """Whether an inbound ACK/result belongs to a live operation.

        WebSocket ownership is identity-based because a client id is supplied by
        the peer itself.  HTTP long-poll ownership is a session-id string.  A
        missing owner is accepted only for an operation whose transport is
        already pinned to HTTP, preserving old userscript result clients while
        still rejecting arbitrary operation ids.
        """
        key = str(operation_id) if operation_id is not None else ""
        with self._lock:
            operation = self._operations.get(key)
            if operation is None or operation.status not in ACTIVE_STATUSES:
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
    ) -> bool:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return False
            if operation.status not in ACTIVE_STATUSES:
                return True
            if operation.last_wire_result is result:
                return False
            operation.last_wire_result = result
            operation.result = dict(result)
            if outcome_unknown:
                operation.status = "outcome_unknown"
                return False
            data = result.get("data")
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
                if operation.status in ACTIVE_STATUSES and target in operation.targets
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
                "reservation_held": status in ACTIVE_STATUSES,
            }
            if operation.result is not None:
                result["wire_result"] = dict(operation.result)
            if consume and status not in ACTIVE_STATUSES:
                operation.result = None
                operation.last_wire_result = None
                operation.status = "consumed"
            return result
