from __future__ import annotations

import io
import json
import queue
import struct
import sys
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from browsertap_mcp import native_host as native


def frame(message: dict[str, Any]) -> bytes:
    raw = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


class ShortReader(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            size = -1
        return super().read(min(size, 1) if size >= 0 else -1)


class ShortWriter(io.BytesIO):
    def write(self, data: bytes) -> int:  # type: ignore[override]
        return super().write(data[:3])


def messages(data: bytes) -> list[dict[str, Any]]:
    reader = io.BytesIO(data)
    result: list[dict[str, Any]] = []
    while (message := native.read_message(reader)) is not None:
        result.append(message)
    return result


def test_protocol_reads_partial_utf8_frames_and_clean_eof() -> None:
    payload = {"type": "result", "result": "中文😀", "id": "synthetic-operation"}
    reader = ShortReader(frame(payload) + frame({"type": "ping"}))
    assert native.read_message(reader) == payload
    assert native.read_message(reader) == {"type": "ping"}
    assert native.read_message(reader) is None


@pytest.mark.parametrize("raw", [
    b"\x01", b"\x02\x00", b"\x03\x00\x00", struct.pack("<I", 0),
    struct.pack("<I", 9) + b"{}", struct.pack("<I", 1) + b"\xff",
    struct.pack("<I", 1) + b"{", struct.pack("<I", 9) + b'[]',
    struct.pack("<I", 9) + b'null', struct.pack("<I", 9) + b'"hello"',
    struct.pack("<I", 9) + b'{"x":NaN}',
])
def test_protocol_rejects_truncated_or_invalid_messages(raw: bytes) -> None:
    with pytest.raises(native.NativeProtocolError):
        native.read_message(ShortReader(raw))


def test_size_limit_is_checked_before_reading_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "MAX_MESSAGE_BYTES", 64)
    reader = io.BytesIO(struct.pack("<I", 65) + b"sentinel")
    with pytest.raises(native.NativeProtocolError):
        native.read_message(reader)
    assert reader.tell() == 4


@pytest.mark.parametrize("value", [None, "text"])
def test_protocol_requires_binary_blocking_stream(value: Any) -> None:
    with pytest.raises(native.NativeProtocolError):
        native.read_message(SimpleNamespace(read=lambda size: value))  # type: ignore[arg-type]


def test_writer_counts_encoded_bytes_and_handles_short_writes() -> None:
    payload = {"result": "中😀\ud800/\udfff", "text": "literal \\uD800\r\n"}
    writer = ShortWriter()
    native.write_message(writer, payload)
    data = writer.getvalue()
    assert struct.unpack("<I", data[:4])[0] == len(data) - 4
    assert messages(data) == [payload]


def test_large_messages_reassemble_even_inside_escaped_strings() -> None:
    payload = {"type": "ext_cmd", "cmd": {"text": '"\\中文😀' * 60000}}
    writer = io.BytesIO()
    native.write_message(writer, payload)
    data = writer.getvalue()
    reader = io.BytesIO(data)
    chunks = []
    while header := reader.read(4):
        size = struct.unpack("<I", header)[0]
        assert size < 1024 * 1024
        chunks.append(json.loads(reader.read(size)))
    assert len(chunks) > 1
    assert {chunk["type"] for chunk in chunks} == {"native_chunk"}
    assert len({chunk["id"] for chunk in chunks}) == 1
    assert {chunk["total"] for chunk in chunks} == {len(chunks)}
    assert [chunk["index"] for chunk in chunks] == list(range(len(chunks)))
    assert json.loads("".join(chunk["data"] for chunk in chunks)) == payload


def test_oversized_logical_message_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "MAX_MESSAGE_BYTES", 32)
    writer = io.BytesIO()
    with pytest.raises(native.NativeProtocolError):
        native.write_message(writer, {"text": "a" * 33})
    assert writer.getvalue() == b""


@pytest.mark.parametrize("payload", [[1], {"x": object()}, {"x": float("nan")}])
def test_invalid_output_is_rejected_before_any_frame(payload: Any) -> None:
    writer = io.BytesIO()
    with pytest.raises(native.NativeProtocolError):
        native.write_message(writer, payload)
    assert not writer.getvalue()


@pytest.mark.parametrize("count", [None, 0, -1])
def test_output_failure_does_not_spin(count: int | None) -> None:
    with pytest.raises(OSError):
        native.write_message(SimpleNamespace(write=lambda data: count), {"type": "pong"})  # type: ignore[arg-type]


def test_windows_stdio_sets_both_binary_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    reader, writer = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(native.sys, "platform", "win32")
    monkeypatch.setattr(native.os, "O_BINARY", 32768, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(setmode=lambda *args: calls.append(args)))
    monkeypatch.setattr(native.sys, "stdin", SimpleNamespace(fileno=lambda: 0, buffer=reader))
    monkeypatch.setattr(native.sys, "stdout", SimpleNamespace(fileno=lambda: 1, buffer=writer))
    assert native._binary_stdio() == (reader, writer)
    assert calls == [(0, 32768), (1, 32768)]


def test_nonwindows_stdio_uses_unbuffered_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    reader, writer = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.sys, "stdin", SimpleNamespace(buffer=SimpleNamespace(raw=reader)))
    monkeypatch.setattr(native.sys, "stdout", SimpleNamespace(buffer=SimpleNamespace(raw=writer)))
    assert native._binary_stdio() == (reader, writer)


def config_file(tmp_path: Path, **changes: Any) -> Path:
    config = {"schema": 1, "environment": {}, "extension_id": "a" * 32}
    config.update(changes)
    path = tmp_path / "native config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_launch_config_uses_resolved_overrides_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = {
        "BROWSERTAP_STATE_DIR": str(tmp_path / "状态"),
        "BROWSERTAP_BRIDGE_TOKEN_FILE": str(tmp_path / "token"),
        "BROWSERTAP_BRIDGE_PORT": "23567", "BROWSERTAP_BRIDGE_HOST": "::1",
        "BROWSERTAP_TRANSPORT": "websocket",
    }
    monkeypatch.setattr(native.os, "environ", {"unrelated": "keep"})
    assert native.load_launch_config(config_file(tmp_path, environment=environment)) == "a" * 32
    assert dict(native.os.environ) == {"unrelated": "keep", **environment}


@pytest.mark.parametrize("change", [
    {"schema": True}, {"schema": 2}, {"extension_id": "q" * 32}, {"extension_id": 2},
    {"environment": []}, {"environment": {"BROWSERTAP_BRIDGE_TOKEN": "synthetic-secret"}},
    {"environment": {"BROWSERTAP_STATE_DIR": "relative"}},
    {"environment": {"BROWSERTAP_BRIDGE_PORT": "65534"}},
    {"environment": {"BROWSERTAP_BRIDGE_PORT": 12345}},
    {"environment": {"BROWSERTAP_BRIDGE_HOST": "example.invalid"}},
    {"environment": {"BROWSERTAP_BRIDGE_HOST": "192.0.2.1"}},
    {"environment": {"BROWSERTAP_TRANSPORT": "invalid"}},
    {"environment": {"BROWSERTAP_STATE_DIR": "\0"}},
])
def test_invalid_launch_config_has_no_environment_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    before = {"unrelated": "keep"}
    monkeypatch.setattr(native.os, "environ", before.copy())
    with pytest.raises(ValueError):
        native.load_launch_config(config_file(tmp_path, **change))
    assert dict(native.os.environ) == before


def test_launch_config_size_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "MAX_CONFIG_BYTES", 10)
    with pytest.raises(ValueError):
        native.load_launch_config(config_file(tmp_path))


class FakeClient:
    port = 23001

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.closed: list[str] = []
        self.replies: queue.Queue[dict[str, Any] | Exception] = queue.Queue()
        self.close_event = threading.Event()

    def open(self) -> str:
        return "synthetic-connection"

    def send(self, connection: str, message: dict[str, Any]) -> None:
        self.sent.append((connection, message))

    def poll(self, connection: str) -> dict[str, Any] | None:
        try:
            reply = self.replies.get(timeout=0.1)
        except queue.Empty:
            return None
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self, connection: str) -> None:
        self.closed.append(connection)
        self.close_event.set()


def test_host_forwards_browser_frames_and_ignores_legacy_hello() -> None:
    client, writer = FakeClient(), io.BytesIO()
    frames = [{"type": "ext_hello"}, {"type": "ext_ready", "clientId": "synthetic-client"},
              {"type": "ping", "clientId": "synthetic-client"}]
    assert native.run_host(io.BytesIO(b"".join(map(frame, frames))), writer, client) == 0  # type: ignore[arg-type]
    assert [msg for _, msg in client.sent] == frames[1:]
    assert client.closed == ["synthetic-connection"]
    assert messages(writer.getvalue())[0] == {"type": "host_ready", "protocol": 1, "bridgePort": client.port}


def test_host_bidirectional_delivery_keeps_original_envelopes() -> None:
    command_written = threading.Event()
    command = {"type": "ext_cmd", "id": "synthetic-operation", "cmd": {"cmd": "list_tabs"}}
    result = {"type": "result", "id": "synthetic-operation", "result": []}

    class WaitingInput(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert command_written.wait(3)
            return super().read(size)

    class ObservedOutput(io.BytesIO):
        def flush(self) -> None:
            if command in messages(self.getvalue()):
                command_written.set()

    client, writer = FakeClient(), ObservedOutput()
    client.replies.put(command)
    assert native.run_host(WaitingInput(frame(result)), writer, client) == 0  # type: ignore[arg-type]
    assert messages(writer.getvalue())[1:] == [command]
    assert client.sent == [("synthetic-connection", result)]
    assert client.closed == ["synthetic-connection"]


def test_input_failure_closes_once_and_never_logs_payload(capsys: pytest.CaptureFixture[str]) -> None:
    client, writer = FakeClient(), io.BytesIO()
    raw = b"synthetic-secret-browser-script"
    assert native.run_host(io.BytesIO(struct.pack("<I", len(raw)) + raw), writer, client) == 1  # type: ignore[arg-type]
    assert client.closed == ["synthetic-connection"]
    assert not client.sent
    captured = capsys.readouterr()
    assert "NativeProtocolError" in captured.err
    assert "synthetic-secret" not in captured.err


def test_startup_failure_sends_no_host_ready() -> None:
    client, writer = FakeClient(), io.BytesIO()
    client.open = lambda: (_ for _ in ()).throw(native.NativeBridgeError("failed"))  # type: ignore[method-assign]
    with pytest.raises(native.NativeBridgeError):
        native.run_host(io.BytesIO(), writer, client)  # type: ignore[arg-type]
    assert not writer.getvalue()
    assert client.closed == []


def test_output_failure_still_retires_its_connection(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient()
    writer = SimpleNamespace(write=lambda data: (_ for _ in ()).throw(BrokenPipeError("private")))
    assert native.run_host(io.BytesIO(), writer, client) == 1  # type: ignore[arg-type]
    assert client.closed == ["synthetic-connection"]
    assert "private" not in capsys.readouterr().err


class FakeOpener:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.calls: list[tuple[Any, float]] = []

    def open(self, request: Any, timeout: float) -> io.BytesIO:
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return io.BytesIO(json.dumps(self.response).encode("ascii"))


def test_http_client_uses_bearer_and_preserves_native_contract() -> None:
    client = native.NativeBridgeClient("::1", 23111, "synthetic-token")
    opener = FakeOpener({"connectionId": "synthetic-connection"})
    client._opener = opener  # type: ignore[assignment]
    assert client.open() == "synthetic-connection"
    request, timeout = opener.calls[0]
    assert request.full_url == "http://[::1]:23112/api/native/open"
    assert request.headers["Authorization"] == "Bearer synthetic-token"
    assert request.get_method() == "POST" and json.loads(request.data) == {}
    assert timeout == 15
    opener.response = {"ok": True}
    client.send("synthetic-connection", {"type": "ping"})
    assert json.loads(opener.calls[-1][0].data)["message"] == {"type": "ping"}
    opener.response = {"message": {"type": "pong"}}
    assert client.poll("synthetic-connection") == {"type": "pong"}
    opener.response = {"message": None}
    assert client.poll("synthetic-connection") is None
    opener.response = {"ok": True}
    client.close("synthetic-connection")
    assert opener.calls[-1][1] == 3


@pytest.mark.parametrize("response,action", [
    ({}, "open"), ({"connectionId": ""}, "open"), ({"connectionId": 5}, "open"),
    ({}, "poll"), ({"message": []}, "poll"), ({"ok": False}, "send"),
    ({"error_code": "native_connection_closed"}, "poll"),
    (urllib.error.URLError("synthetic-secret"), "send"),
])
def test_failed_requests_are_not_retried(response: dict[str, Any] | Exception, action: str) -> None:
    client = native.NativeBridgeClient("localhost", 23111, "synthetic-token")
    opener = FakeOpener(response)
    client._opener = opener  # type: ignore[assignment]
    with pytest.raises((native.NativeBridgeError, native.NativeProtocolError)):
        if action == "open":
            client.open()
        elif action == "send":
            client.send("synthetic-connection", {"type": "result"})
        else:
            client.poll("synthetic-connection")
    assert len(opener.calls) == 1


def test_http_client_disables_environment_proxies_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: list[Any] = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: handlers.extend(args))
    native.NativeBridgeClient("127.0.0.1", 23111, "synthetic-token")
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, None, None, "https://example.invalid") is None


def test_main_keeps_incidental_stdout_away_from_protocol(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    writer = io.BytesIO()
    monkeypatch.setattr(native, "_binary_stdio", lambda: (io.BytesIO(), writer))

    def connect() -> FakeClient:
        print("incidental dependency output")
        return FakeClient()

    monkeypatch.setattr(native, "_connect_bridge", connect)
    assert native.main([]) == 0
    captured = capsys.readouterr()
    assert not captured.out
    assert "incidental dependency output" in captured.err
    assert messages(writer.getvalue())[0]["type"] == "host_ready"


def test_main_rejects_foreign_origin_before_bridge_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    writer = io.BytesIO()
    monkeypatch.setattr(native, "_binary_stdio", lambda: (io.BytesIO(), writer))
    monkeypatch.setattr(native, "_connect_bridge", lambda: pytest.fail("foreign origin started a bridge"))
    assert native.main(["--config", str(config_file(tmp_path)), "chrome-extension://" + "b" * 32 + "/"]) == 1
    assert not writer.getvalue()
    assert "ValueError" in capsys.readouterr().err


def test_main_accepts_chrome_launcher_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = io.BytesIO()
    monkeypatch.setattr(native, "_binary_stdio", lambda: (io.BytesIO(), writer))
    monkeypatch.setattr(native, "_connect_bridge", FakeClient)
    assert native.main(["--config", str(config_file(tmp_path)),
                        "chrome-extension://" + "a" * 32 + "/", "--parent-window=0"]) == 0
    assert messages(writer.getvalue())[0]["type"] == "host_ready"


def test_launch_config_clears_stale_browser_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native.os, "environ", {
        "BROWSERTAP_BRIDGE_PORT": "21111", "BROWSERTAP_BRIDGE_TOKEN": "stale-synthetic-token",
        "unrelated": "preserved",
    })
    native.load_launch_config(config_file(tmp_path))
    assert dict(native.os.environ) == {"unrelated": "preserved"}


def test_poll_failure_exits_even_when_browser_keeps_stdin_open(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient()

    class Input:
        def read(self, size: int | None) -> bytes:
            assert client.close_event.wait(3)
            return b""

    client.replies.put(native.NativeBridgeError("synthetic-private-url"))
    assert native.run_host(Input(), io.BytesIO(), client) == 1  # type: ignore[arg-type]
    assert client.closed == ["synthetic-connection"]
    assert not client.sent
    assert "synthetic-private" not in capsys.readouterr().err


def test_uncertain_post_is_never_replayed_and_closes_connection(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient()

    def fail_after_dispatch(connection: str, message: dict[str, Any]) -> None:
        client.sent.append((connection, message))
        raise OSError("synthetic-private-result")

    client.send = fail_after_dispatch  # type: ignore[method-assign]
    result = {"type": "result", "id": "synthetic-operation"}
    assert native.run_host(io.BytesIO(frame(result)), io.BytesIO(), client) == 1  # type: ignore[arg-type]
    assert client.sent == [("synthetic-connection", result)]
    assert client.closed == ["synthetic-connection"]
    assert "synthetic-private" not in capsys.readouterr().err


def test_close_failure_is_bounded_and_reported_once(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient()

    def fail_close(connection: str) -> None:
        client.closed.append(connection)
        raise OSError("synthetic-private-close")

    client.close = fail_close  # type: ignore[method-assign]
    assert native.run_host(io.BytesIO(), io.BytesIO(), client) == 1  # type: ignore[arg-type]
    assert client.closed == ["synthetic-connection"]
    assert "synthetic-private" not in capsys.readouterr().err


def test_http_size_limits_do_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "MAX_MESSAGE_BYTES", 10)
    client = native.NativeBridgeClient("127.0.0.1", 23111, "synthetic-token")
    opener = FakeOpener({"message": {"text": "a" * 5000}})
    client._opener = opener  # type: ignore[assignment]
    with pytest.raises(native.NativeProtocolError):
        client.send("synthetic-connection", {"text": "a" * 5000})
    assert not opener.calls
    with pytest.raises(native.NativeProtocolError):
        client.poll("synthetic-connection")
    assert len(opener.calls) == 1


def test_native_host_requires_nonempty_authentication() -> None:
    with pytest.raises(ValueError):
        native.NativeBridgeClient("127.0.0.1", 23111, "")


@pytest.mark.parametrize("port_open,spawn_result,no_spawn", [
    (True, False, False), (False, True, False), (False, False, False), (False, True, True),
])
def test_connect_reuses_shared_daemon_and_spawn_contract(
    monkeypatch: pytest.MonkeyPatch,
    port_open: bool,
    spawn_result: bool,
    no_spawn: bool
) -> None:
    from browsertap_mcp import browser_bridge, server
    calls: list[str] = []
    monkeypatch.setenv("BROWSERTAP_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "23111")
    monkeypatch.setenv("BROWSERTAP_TRANSPORT", "native")
    monkeypatch.setenv("BROWSERTAP_NO_SPAWN", "1" if no_spawn else "0")
    monkeypatch.setattr(browser_bridge, "bridge_token", lambda: "synthetic-token")
    monkeypatch.setattr(browser_bridge, "tcp_port_open", lambda *args: port_open)

    def spawn_and_append() -> bool:
        calls.append("spawn")
        return spawn_result

    monkeypatch.setattr(server, "spawn_bridge_daemon", spawn_and_append)
    if port_open or (spawn_result and not no_spawn):
        result = native._connect_bridge()
        assert result.port == 23111
    else:
        with pytest.raises(native.NativeBridgeError):
            native._connect_bridge()
    assert calls == ([] if port_open or no_spawn else ["spawn"])


@pytest.mark.parametrize("environment", [
    {"BROWSERTAP_BRIDGE_HOST": "192.0.2.1"},
    {"BROWSERTAP_BRIDGE_PORT": "0"},
    {"BROWSERTAP_TRANSPORT": "bad"},
])
def test_bad_configuration_has_no_token_or_network_effect(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str]
) -> None:
    from browsertap_mcp import browser_bridge
    monkeypatch.setenv("BROWSERTAP_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "23111")
    monkeypatch.setenv("BROWSERTAP_TRANSPORT", "native")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(browser_bridge, "bridge_token", lambda: pytest.fail("invalid config touched authentication"))
    with pytest.raises(ValueError):
        native._connect_bridge()

def test_main_keyboard_interrupt_has_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "_binary_stdio", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert native.main([]) == 0


def test_missing_console_stdio_is_reported_without_stdout(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(native.sys, "stdin", None)
    assert native.main([]) == 1
    assert "RuntimeError" in capsys.readouterr().err
