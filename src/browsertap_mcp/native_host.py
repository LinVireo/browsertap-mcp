"""Chrome-owned stdio transport to the existing shared BrowserTap daemon."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import queue
import re
import secrets
import struct
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

from .paths import configured_bridge_port, validate_bridge_port

HOST_NAME = "io.browsertap.host"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024 - 1
CHUNK_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 64 * 1024
CONFIG_ENVIRONMENT = frozenset({
    "BROWSERTAP_BRIDGE_HOST", "BROWSERTAP_BRIDGE_PORT", "BROWSERTAP_STATE_DIR",
    "BROWSERTAP_BRIDGE_TOKEN_FILE", "BROWSERTAP_TRANSPORT",
})


class NativeProtocolError(ValueError):
    """Invalid framing or JSON; messages never contain browser payloads."""


class NativeBridgeError(RuntimeError):
    """A bridge request failed; a possibly dispatched request is not replayed."""


def _reject_constant(value: str) -> None:
    raise NativeProtocolError("nonfinite JSON number")


def _decode_object(raw: bytes) -> dict[str, Any]:
    try:
        message = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise NativeProtocolError("invalid JSON object") from exc
    if not isinstance(message, dict):
        raise NativeProtocolError("expected a JSON object")
    return message


def _encode_object(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict):
        raise NativeProtocolError("expected a JSON object")
    try:
        return json.dumps(message, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise NativeProtocolError("message is not JSON serializable") from exc


def _read_exact(stream: BinaryIO, size: int, *, eof_allowed: bool = False) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not isinstance(part, bytes):
            raise NativeProtocolError("binary blocking input required")
        if not part:
            if not data and eof_allowed:
                return None
            raise NativeProtocolError("truncated native message")
        data.extend(part)
    return bytes(data)


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one little-endian byte-counted UTF-8 object, or clean header EOF."""
    header = _read_exact(stream, 4, eof_allowed=True)
    if header is None:
        return None
    length = struct.unpack("<I", header)[0]
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise NativeProtocolError("native message size is outside the supported limit")
    body = _read_exact(stream, length)
    if body is None:
        raise NativeProtocolError("missing native message body")
    return _decode_object(body)


def _write_frame(stream: BinaryIO, body: bytes) -> None:
    if not 0 < len(body) <= MAX_OUTPUT_BYTES:
        raise NativeProtocolError("output frame exceeds Chrome's limit")
    frame = memoryview(struct.pack("<I", len(body)) + body)
    while frame:
        count = stream.write(frame)
        if count is None or count <= 0:
            raise OSError("native output stopped accepting bytes")
        frame = frame[count:]
    stream.flush()


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    """Flush a logical message, chunking below Chrome's 1 MiB output limit."""
    body = _encode_object(message)
    if len(body) > MAX_MESSAGE_BYTES:
        raise NativeProtocolError("logical message exceeds the supported limit")
    if len(body) <= MAX_OUTPUT_BYTES:
        _write_frame(stream, body)
        return
    # ASCII-escaped JSON can split inside an escape: the receiver concatenates
    # every data string before parsing once. Re-escaping at most doubles size.
    identifier = secrets.token_hex(16)
    total = (len(body) + CHUNK_BYTES - 1) // CHUNK_BYTES
    for index in range(total):
        chunk = {
            "type": "native_chunk", "id": identifier, "index": index, "total": total,
            "data": body[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES].decode("ascii"),
        }
        _write_frame(stream, _encode_object(chunk))


def _loopback_host(host: str) -> str:
    if host.lower() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("native host requires a loopback bridge address") from exc
    if not address.is_loopback:
        raise ValueError("native host requires a loopback bridge address")
    return str(address)


def load_launch_config(path: Path) -> str:
    """Validate the entire nonsecret installer config before changing the env."""
    with path.open("rb") as handle:
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("native launcher configuration is too large")
    config = _decode_object(raw)
    if type(config.get("schema")) is not int or config["schema"] != 1:
        raise ValueError("unsupported native launcher configuration schema")
    identifier = config.get("extension_id")
    if not isinstance(identifier, str) or re.fullmatch(r"[a-p]{32}", identifier) is None:
        raise ValueError("invalid configured extension identifier")
    environment = config.get("environment")
    if not isinstance(environment, dict) or environment.keys() - CONFIG_ENVIRONMENT:
        raise ValueError("invalid native launcher environment")
    if any(not isinstance(value, str) or not value or "\0" in value
           for value in environment.values()):
        raise ValueError("native launcher environment values must be nonempty strings")
    for name in ("BROWSERTAP_STATE_DIR", "BROWSERTAP_BRIDGE_TOKEN_FILE"):
        if name in environment and not Path(environment[name]).is_absolute():
            raise ValueError("native launcher paths must be absolute")
    _loopback_host(environment.get("BROWSERTAP_BRIDGE_HOST", "127.0.0.1"))
    validate_bridge_port(environment.get("BROWSERTAP_BRIDGE_PORT", "18765"))
    if environment.get("BROWSERTAP_TRANSPORT", "native") not in {"native", "websocket"}:
        raise ValueError("invalid native transport preference")
    # Chrome can retain an old shell environment. The installed configuration
    # is authoritative; an inherited raw token is not an installed credential.
    for name in CONFIG_ENVIRONMENT | {"BROWSERTAP_BRIDGE_TOKEN"}:
        os.environ.pop(name, None)
    os.environ.update(environment)
    return identifier


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class NativeBridgeClient:
    """No proxies, redirects or retries; every uncertain POST is sent once."""

    def __init__(self, host: str, port: int, token: str):
        host = _loopback_host(host)
        port = validate_bridge_port(port)
        if not token:
            raise ValueError("native host requires bridge token authentication")
        url_host = f"[{host}]" if ":" in host else host
        self.base_url = f"http://{url_host}:{port + 1}/api/native/"
        self.port = port
        self._token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, action: str, payload: dict[str, Any], *, timeout: float = 15) -> dict[str, Any]:
        raw = _encode_object(payload)
        if len(raw) > MAX_MESSAGE_BYTES + 4096:
            raise NativeProtocolError("native HTTP message exceeds the supported limit")
        # Fixed HTTP scheme and validated loopback host; redirects are disabled.
        request = urllib.request.Request(  # noqa: S310
            self.base_url + action, data=raw, method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                data = response.read(MAX_MESSAGE_BYTES + 4097)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise NativeBridgeError("native bridge request failed") from exc
        if len(data) > MAX_MESSAGE_BYTES + 4096:
            raise NativeProtocolError("native bridge response exceeds the supported limit")
        result = _decode_object(data)
        if result.get("error") or result.get("error_code"):
            raise NativeBridgeError("native bridge rejected the request")
        return result

    def open(self) -> str:
        result = self._request("open", {})
        connection_id = result.get("connectionId")
        if not isinstance(connection_id, str) or not 1 <= len(connection_id) <= 256:
            raise NativeProtocolError("invalid native connection identifier")
        return connection_id

    def send(self, connection_id: str, message: dict[str, Any]) -> None:
        result = self._request("message", {"connectionId": connection_id, "message": message})
        if result.get("ok") is not True:
            raise NativeBridgeError("native bridge did not acknowledge the message")

    def poll(self, connection_id: str) -> dict[str, Any] | None:
        result = self._request("poll", {"connectionId": connection_id})
        if "message" not in result or (result["message"] is not None
                                        and not isinstance(result["message"], dict)):
            raise NativeProtocolError("invalid native poll response")
        return result["message"]

    def close(self, connection_id: str) -> None:
        self._request("close", {"connectionId": connection_id}, timeout=3)


def _diagnostic(operation: str, error: BaseException) -> None:
    # Decoder and transport exception text can carry sensitive payload bytes.
    if sys.stderr is not None:
        print(f"Native host {operation} failed ({type(error).__name__})", file=sys.stderr)


def run_host(reader: BinaryIO, writer: BinaryIO, client: NativeBridgeClient) -> int:
    """Forward one owned connection; EOF/error closes it without daemon stop."""
    connection_id = client.open()
    stop = threading.Event()
    outgoing: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=16)
    failures: queue.SimpleQueue[tuple[str, BaseException]] = queue.SimpleQueue()

    def receive_browser() -> None:
        try:
            while not stop.is_set():
                message = read_message(reader)
                if message is None:
                    break
                if message.get("type") != "ext_hello":
                    client.send(connection_id, message)
        except Exception as exc:
            failures.put(("input", exc))
        finally:
            stop.set()

    def receive_bridge() -> None:
        try:
            while not stop.is_set():
                message = client.poll(connection_id)
                if message is None:
                    continue
                while not stop.is_set():
                    try:
                        outgoing.put(message, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except Exception as exc:
            if not stop.is_set():
                failures.put(("poll", exc))
                stop.set()

    def write_browser() -> None:
        try:
            while not stop.is_set():
                try:
                    message = outgoing.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not stop.is_set():
                    write_message(writer, message)
        except Exception as exc:
            failures.put(("output", exc))
            stop.set()

    threads: list[threading.Thread] = []
    try:
        write_message(writer, {"type": "host_ready", "protocol": PROTOCOL_VERSION, "bridgePort": client.port})
        for target in (receive_browser, receive_bridge, write_browser):
            thread = threading.Thread(target=target, daemon=True, name=f"native-{target.__name__}")
            thread.start()
            threads.append(thread)
        while not stop.wait(0.1):
            pass
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        failures.put(("output", exc))
    finally:
        stop.set()
        try:
            client.close(connection_id)
        except Exception as exc:
            failures.put(("close", exc))
        # Either browser pipe may remain open or blocked after disconnect.
        # main uses raw pipes, so daemon I/O cannot hold a buffered stream lock
        # during shutdown. Only this thread owns connection cleanup.
        for thread in threads:
            thread.join(timeout=0.2)
    failed = False
    while not failures.empty():
        operation, error = failures.get()
        _diagnostic(operation, error)
        failed = True
    return 1 if failed else 0


def _binary_stdio() -> tuple[BinaryIO, BinaryIO]:
    if sys.stdin is None or sys.stdout is None:
        raise RuntimeError("native host requires console Python with stdio")
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    return getattr(reader, "raw", reader), getattr(writer, "raw", writer)


def _connect_bridge() -> NativeBridgeClient:
    host = _loopback_host(os.environ.get("BROWSERTAP_BRIDGE_HOST", "127.0.0.1"))
    port = configured_bridge_port()
    if os.environ.get("BROWSERTAP_TRANSPORT", "native") not in {"native", "websocket"}:
        raise ValueError("invalid native transport preference")
    from .browser_bridge import bridge_token, tcp_port_open
    token = bridge_token()
    if not tcp_port_open(host, port + 1):
        from .server import spawn_bridge_daemon
        if os.environ.get("BROWSERTAP_NO_SPAWN") == "1" or not spawn_bridge_daemon():
            raise NativeBridgeError("shared bridge startup failed")
    return NativeBridgeClient(host, port, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BrowserTap Chrome Native Messaging host")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--parent-window", help=argparse.SUPPRESS)
    parser.add_argument("origin", nargs="?", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        reader, writer = _binary_stdio()
        # Raw output is held separately, so incidental dependency print() calls
        # cannot corrupt Chrome's frame header.
        with contextlib.redirect_stdout(sys.stderr):
            identifier = load_launch_config(args.config) if args.config is not None else None
            if args.origin:
                from .extension_origin import default_extension_origin
                expected = f"chrome-extension://{identifier}" if identifier else default_extension_origin()
                if not expected or args.origin not in {expected, expected + "/"}:
                    raise ValueError("native caller origin does not match this installation")
            return run_host(reader, writer, _connect_bridge())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _diagnostic("startup", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
