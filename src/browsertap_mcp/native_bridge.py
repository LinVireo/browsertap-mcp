"""Bounded Native Messaging connections on the shared bridge's HTTP listener.

The native host owns an opaque connection capability. Its queue implements the
same send/close interface as a WebSocket; browser commands and reply ownership
remain the responsibility of BrowserBridge's existing dispatcher.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from functools import wraps
from typing import Any, Literal

import bottle
from bottle import request

TRANSPORT_ENV = "BROWSERTAP_TRANSPORT"
NATIVE_HOST_NAME = "io.browsertap.host"
NATIVE_PROTOCOL = 1
NATIVE_POLL_SECONDS = 10.0
NATIVE_IDLE_SECONDS = 60.0
NATIVE_MAX_CONNECTIONS = 16
NATIVE_MAX_MESSAGE_BYTES = 64 * 1024 * 1024
NATIVE_MAX_QUEUE_MESSAGES = 64
NATIVE_MAX_QUEUE_BYTES = NATIVE_MAX_MESSAGE_BYTES
NATIVE_MAX_TOTAL_QUEUE_BYTES = 128 * 1024 * 1024
NATIVE_ENVELOPE_BYTES = 4096
_CONNECTION_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def extension_transport(value: object = None) -> Literal["native", "websocket"]:
    """Validate a mode before startup effects, without validating it at import."""
    if value is None:
        value = os.environ.get(TRANSPORT_ENV, "native")
    if isinstance(value, str):
        value = value.strip().lower()
    if value == "native":
        return "native"
    if value == "websocket":
        return "websocket"
    raise ValueError(f"{TRANSPORT_ENV} must be native or websocket")


class NativeBridgeError(RuntimeError):
    """An internal HTTP error containing only a fixed, payload-free message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = code


def _closed_error() -> NativeBridgeError:
    return NativeBridgeError(410, "native_connection_closed", "Native connection is closed")


def _invalid_request() -> NativeBridgeError:
    return NativeBridgeError(400, "native_invalid_request", "Invalid Native Messaging request")


def _invalid_message() -> NativeBridgeError:
    return NativeBridgeError(400, "native_invalid_message", "Invalid Native Messaging frame")


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _message_json(message: Any) -> str:
    if not isinstance(message, dict):
        raise _invalid_message()
    try:
        encoded = json.dumps(message, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise _invalid_message() from None
    # ensure_ascii makes code-unit count identical to the UTF-8 byte count,
    # including browser strings with lone UTF-16 surrogates.
    if len(encoded) > NATIVE_MAX_MESSAGE_BYTES:
        raise NativeBridgeError(413, "native_message_too_large", "Native message exceeds size limit")
    return encoded


class NativeConnection:
    """One HTTP host connection, also used as the pending-operation owner."""

    def __init__(self, registry: NativeConnections, connection_id: str) -> None:
        self._registry = registry
        self.connection_id = connection_id
        # Existing peer diagnostics may publish address. Never put the capability
        # in it: knowing a connection id is what admits messages for that host.
        self.address = ("native", 0)
        self._last_seen = registry._clock()
        self._queue: deque[tuple[str, int]] = deque()
        self._queued_bytes = 0
        self._polling = False
        self.closed = False

    def send_message(self, message: str) -> None:
        self._registry._send(self, message)

    def close(self) -> None:
        with self._registry._condition:
            self._registry._retire(self)


class NativeConnections:
    """Serialize Native lifecycle with bridge publication, without retrying frames."""

    def __init__(
        self,
        on_message: Callable[[NativeConnection, dict[str, Any]], bool],
        on_close: Callable[[NativeConnection], None],
        *,
        lock: Any = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # BrowserBridge passes its state RLock. A close during command dispatch
        # must use that same lock: a separate queue->state lock order deadlocks
        # against the state->queue order of concurrent command publication.
        self._condition = threading.Condition(lock)
        self._clock = clock or time.monotonic
        self._on_message = on_message
        self._on_close = on_close
        self._connections: dict[str, NativeConnection] = {}
        self._queued_bytes = 0
        self._stopped = False

    def _retire(self, connection: NativeConnection) -> None:
        if connection.closed:
            return
        connection.closed = True
        self._connections.pop(connection.connection_id, None)
        self._queued_bytes -= connection._queued_bytes
        connection._queue.clear()
        connection._queued_bytes = 0
        self._condition.notify_all()
        # Only this transport's registrations are retired. The shared daemon,
        # successor sockets and uncertain operation reservations survive.
        self._on_close(connection)

    def _active(self, connection: NativeConnection) -> None:
        if connection.closed:
            raise _closed_error()
        if self._clock() - connection._last_seen >= NATIVE_IDLE_SECONDS:
            self._retire(connection)
            raise _closed_error()

    def _get(self, connection_id: str) -> NativeConnection:
        connection = self._connections.get(connection_id)
        if connection is None:
            raise _closed_error()
        self._active(connection)
        connection._last_seen = self._clock()
        return connection

    def expire(self) -> None:
        """Called by the HTTP server's maintenance tick, even without requests."""
        with self._condition:
            now = self._clock()
            for connection in list(self._connections.values()):
                if now - connection._last_seen >= NATIVE_IDLE_SECONDS:
                    self._retire(connection)

    def open(self) -> str:
        with self._condition:
            if self._stopped:
                raise NativeBridgeError(503, "native_bridge_stopped", "Native bridge is stopping")
            self.expire()
            if len(self._connections) >= NATIVE_MAX_CONNECTIONS:
                raise NativeBridgeError(503, "native_connection_limit", "Native connection limit reached")
            connection_id = secrets.token_urlsafe(32)
            while connection_id in self._connections:
                connection_id = secrets.token_urlsafe(32)
            self._connections[connection_id] = NativeConnection(self, connection_id)
            return connection_id

    def receive(self, connection_id: str, message: dict[str, Any]) -> None:
        _message_json(message)
        with self._condition:
            connection = self._get(connection_id)
            accepted = self._on_message(connection, message)
            # A refused takeover or a failed pong can close the transport while
            # dispatching. It must not be reported as a successful registration.
            self._active(connection)
            if not accepted:
                raise _invalid_message()

    def _send(self, connection: NativeConnection, message: str) -> None:
        if isinstance(message, str) and len(message) > NATIVE_MAX_MESSAGE_BYTES:
            connection.close()
            raise NativeBridgeError(413, "native_message_too_large", "Native message exceeds size limit")
        try:
            data = json.loads(message, parse_constant=_reject_constant)
            encoded = _message_json(data)
        except (ValueError, TypeError, RecursionError):
            connection.close()
            raise _invalid_message() from None
        except NativeBridgeError:
            connection.close()
            raise
        with self._condition:
            self._active(connection)
            size = len(encoded)
            if (len(connection._queue) >= NATIVE_MAX_QUEUE_MESSAGES
                    or connection._queued_bytes + size > NATIVE_MAX_QUEUE_BYTES
                    or self._queued_bytes + size > NATIVE_MAX_TOTAL_QUEUE_BYTES):
                self._retire(connection)
                raise NativeBridgeError(503, "native_queue_full", "Native connection queue is full")
            connection._queue.append((encoded, size))
            connection._queued_bytes += size
            self._queued_bytes += size
            self._condition.notify_all()

    def poll(self, connection_id: str) -> dict[str, Any] | None:
        with self._condition:
            connection = self._get(connection_id)
            if connection._polling:
                raise NativeBridgeError(409, "native_poll_in_progress", "Native poll is already in progress")
            connection._polling = True
            deadline = self._clock() + NATIVE_POLL_SECONDS
            try:
                while True:
                    self._active(connection)
                    if connection._queue:
                        encoded, size = connection._queue.popleft()
                        connection._queued_bytes -= size
                        self._queued_bytes -= size
                        # Once dequeued, delivery is uncertain if HTTP fails.
                        # Never restore or acknowledge it here; the extension's
                        # ordinary ACK/result frames provide delivery evidence.
                        return json.loads(encoded)
                    remaining = min(
                        deadline - self._clock(),
                        NATIVE_IDLE_SECONDS - (self._clock() - connection._last_seen),
                    )
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
            finally:
                connection._polling = False

    def close(self, connection_id: str) -> None:
        with self._condition:
            connection = self._connections.get(connection_id)
            if connection is not None:
                self._retire(connection)

    def close_all(self) -> None:
        with self._condition:
            self._stopped = True
            for connection in list(self._connections.values()):
                self._retire(connection)


def register_native_routes(
    app: Any, connections: NativeConnections, *, token: str, transport: str,
) -> None:
    """Install the adapter after BrowserBridge has finished importing."""
    from .browser_bridge import check_link_token_drained, drain_request_body

    def response(payload: dict[str, Any], status: int = 200) -> bottle.HTTPResponse:
        return bottle.HTTPResponse(
            status=status,
            body=json.dumps(payload, ensure_ascii=True, allow_nan=False),
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        )

    def native_errors(callback):
        @wraps(callback)
        def guarded():
            try:
                return callback()
            except NativeBridgeError as exc:
                return response({"error": str(exc), "error_code": exc.error_code}, exc.status)
        return guarded

    def body(*, message: bool = False) -> dict[str, Any]:
        if not token:
            drain_request_body()
            raise NativeBridgeError(
                503, "native_auth_required", "Native Messaging requires bridge token authentication",
            )
        check_link_token_drained(request.headers, token)
        limit = NATIVE_ENVELOPE_BYTES + (NATIVE_MAX_MESSAGE_BYTES if message else 0)
        try:
            length = request.content_length
        except (ValueError, TypeError):
            drain_request_body()
            raise _invalid_request() from None
        if length > limit:
            drain_request_body()
            raise NativeBridgeError(413, "native_message_too_large", "Native request exceeds size limit")
        if length < 0 or request.content_type.split(";", 1)[0].strip() != "application/json":
            drain_request_body()
            raise _invalid_request()
        try:
            raw = request.body.read(limit + 1)
            if len(raw) > limit:
                raise NativeBridgeError(413, "native_message_too_large", "Native request exceeds size limit")
            data = json.loads(raw, parse_constant=_reject_constant)
        except (ValueError, TypeError, RecursionError, bottle.HTTPError):
            drain_request_body()
            raise _invalid_request() from None
        if not isinstance(data, dict):
            raise _invalid_request()
        return data

    def connection_id(data: dict[str, Any], *, message: bool = False) -> str:
        fields = {"connectionId", "message"} if message else {"connectionId"}
        value = data.get("connectionId")
        if set(data) != fields or not isinstance(value, str) or not _CONNECTION_ID.fullmatch(value):
            raise _invalid_request()
        return value

    @app.route("/api/extension/config", method="GET")
    def extension_config():
        drain_request_body()
        return response({"transport": transport, "nativeHost": NATIVE_HOST_NAME, "protocol": NATIVE_PROTOCOL})

    @app.route("/api/native/open", method="POST")
    @native_errors
    def native_open():
        if body():
            raise _invalid_request()
        return response({"connectionId": connections.open()})

    @app.route("/api/native/message", method="POST")
    @native_errors
    def native_message():
        data = body(message=True)
        identity = connection_id(data, message=True)
        connections.receive(identity, data["message"])
        return response({"ok": True})

    @app.route("/api/native/poll", method="POST")
    @native_errors
    def native_poll():
        return response({"message": connections.poll(connection_id(body()))})

    @app.route("/api/native/close", method="POST")
    @native_errors
    def native_close():
        connections.close(connection_id(body()))
        return response({"ok": True})
