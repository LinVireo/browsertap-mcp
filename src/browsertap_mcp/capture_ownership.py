"""Keep capture ownership in the daemon across individual MCP calls."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class CaptureBusyError(RuntimeError):
    error_code = "capture_busy"
    retry_safe = True
    delivery_state = "undelivered"

    def __init__(self, client_id: str, tab_id: int, kind: str) -> None:
        target = f"{client_id}:{tab_id}"
        super().__init__(
            f"The {kind} capture on {target} belongs to another MCP session; "
            "wait for its owner to stop the capture."
        )
        self.diagnostics = {
            "busy_target": target,
            "capture_kind": kind,
            "may_have_executed": False,
        }


@dataclass(frozen=True)
class _CaptureOwner:
    requester_id: str | None


@dataclass(frozen=True)
class CaptureOperation:
    client_id: str
    tab_id: int
    kind: str
    method: str
    _owner: _CaptureOwner | None


class CaptureOwnershipRegistry:
    """Reserve capture mutations before dispatch and release only a proven stop.

    A failed or unanswered start can still have created a browser capture. Its
    owner therefore remains until a successful stop or explicit lifecycle cleanup.
    """

    def __init__(
        self,
        *,
        requester_liveness: Callable[[str | None], str] | None = None,
    ) -> None:
        self._owners: dict[tuple[str, int, str], _CaptureOwner] = {}
        self._lock = threading.Lock()
        self._requester_liveness = requester_liveness

    def _owner_is_dead(self, owner: _CaptureOwner) -> bool:
        if self._requester_liveness is None or owner.requester_id is None:
            return False
        try:
            return self._requester_liveness(owner.requester_id) == "dead"
        except Exception:
            # A failed liveness probe is not proof that ownership can be taken.
            return False

    def prepare(
        self,
        client_id: str,
        command: dict[str, Any],
        requester_id: str | None,
    ) -> CaptureOperation | None:
        """Claim/check a capture mutation; ordinary commands and reads pass through."""
        name = command.get("cmd")
        kind = {"network_capture": "network", "console": "console"}.get(name) if isinstance(name, str) else None
        method = command.get("method")
        if kind is None or not isinstance(method, str) or not (
            method in {"start", "stop"}
            or (kind == "console" and method == "get" and command.get("clear") is True)
        ):
            return None
        if not isinstance(client_id, str) or not client_id:
            raise ValueError("capture mutation requires a non-empty client_id")
        if requester_id is not None and not isinstance(requester_id, str):
            raise ValueError("capture requester_id must be a string or None")
        requester_id = requester_id if requester_id and requester_id.strip() else None
        tab_id = command.get("tabId")
        if isinstance(tab_id, str) and tab_id.isdecimal():
            tab_id = int(tab_id)
        if isinstance(tab_id, bool) or not isinstance(tab_id, int) or tab_id <= 0:
            raise ValueError("capture mutation requires a positive integer tabId")

        key = (client_id, tab_id, kind)
        with self._lock:
            owner = self._owners.get(key)
            if owner is not None and owner.requester_id != requester_id:
                if not self._owner_is_dead(owner):
                    raise CaptureBusyError(client_id, tab_id, kind)
                # Cleanup also transfers ownership so an unanswered stop or clear
                # stays with its new caller and an old reply cannot release it.
                owner = _CaptureOwner(requester_id)
                self._owners[key] = owner
            if method == "start" and owner is None:
                owner = _CaptureOwner(requester_id)
                self._owners[key] = owner
            return CaptureOperation(client_id, tab_id, kind, str(method), owner)

    def finish(self, operation: CaptureOperation | None, *, success: bool) -> bool:
        """Release a successful stop; return whether this call removed ownership."""
        if operation is None or not success or operation.method != "stop":
            return False
        key = (operation.client_id, operation.tab_id, operation.kind)
        with self._lock:
            # A delayed stop must not release a new capture created after tab
            # teardown, even if the same requester owns both generations.
            if operation._owner is None or self._owners.get(key) is not operation._owner:
                return False
            del self._owners[key]
            return True

    def release_tab(self, client_id: str, tab_id: int) -> int:
        """Forget captures after a confirmed tab lifecycle end, not a transport loss."""
        with self._lock:
            keys = [key for key in self._owners if key[:2] == (client_id, tab_id)]
            for key in keys:
                del self._owners[key]
            return len(keys)

    def release_client(self, client_id: str) -> int:
        """Forget captures only when the browser/worker lifecycle is known to have ended."""
        with self._lock:
            keys = [key for key in self._owners if key[0] == client_id]
            for key in keys:
                del self._owners[key]
            return len(keys)
