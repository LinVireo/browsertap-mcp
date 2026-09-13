"""Real stdio pipes and HTTP sockets, using only synthetic browser messages."""
from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from browsertap_mcp import browser_bridge as B
from tests.conftest import _free_port_base
from tests.test_browser_bridge_coverage import driver_stub

TOKEN = "synthetic-native-integration-token"
EXTENSION_ID = "a" * 32
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native_http_bridge(monkeypatch):
    driver = driver_stub()
    driver.port = _free_port_base()
    monkeypatch.setenv("BROWSERTAP_TRANSPORT", "native")
    monkeypatch.setattr(B, "bridge_token", lambda: TOKEN)
    driver.start_http_server()
    try:
        deadline = time.monotonic() + 5
        while driver.http_server is None and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert driver.http_server is not None, "synthetic HTTP listener did not start"
        yield driver
    finally:
        driver.stop_http_server()


def http_json(driver, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed loopback fixture, no proxy
        f"http://127.0.0.1:{driver.port + 1}{path}", data=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        return json.load(response)


class NativeProcess:
    def __init__(self, driver, directory, *, cli=False, pause_output_after=None):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("BROWSERTAP_")}
        environment.update({
            "BROWSERTAP_BRIDGE_HOST": "127.0.0.1",
            "BROWSERTAP_BRIDGE_PORT": str(driver.port),
            "BROWSERTAP_STATE_DIR": str(directory),
            "BROWSERTAP_BRIDGE_TOKEN": TOKEN,
            "BROWSERTAP_NO_SPAWN": "1",
            "PYTHONPATH": str(ROOT / "src"),
        })
        if cli:
            arguments = ["-m", "browsertap_mcp.cli", "bridge", "--mode", "native"]
        else:
            token_file = directory / "synthetic-token"
            B._persist_token(token_file, TOKEN)
            config = directory / "native-config.json"
            config.write_text(json.dumps({
                "schema": 1, "extension_id": EXTENSION_ID,
                "environment": {
                    "BROWSERTAP_BRIDGE_HOST": "127.0.0.1",
                    "BROWSERTAP_BRIDGE_PORT": str(driver.port),
                    "BROWSERTAP_STATE_DIR": str(directory),
                    "BROWSERTAP_BRIDGE_TOKEN_FILE": str(token_file),
                },
            }), encoding="utf-8")
            # Installed config must discard Chrome's inherited raw credential.
            environment["BROWSERTAP_BRIDGE_TOKEN"] = "wrong-inherited-synthetic-token"
            arguments = ["-m", "browsertap_mcp.native_host", "--config", str(config),
                         f"chrome-extension://{EXTENSION_ID}/", "--parent-window=0"]
        self.process = subprocess.Popen(
            [sys.executable, *arguments], cwd=directory, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        self.frames = queue.Queue()
        self.lengths = []
        self.pause_output_after = pause_output_after
        self.output_paused = threading.Event()
        self.resume_output = threading.Event()
        self.reader = threading.Thread(target=self._collect, daemon=True)
        self.reader.start()

    def _collect(self):
        def read_exact(size):
            data = bytearray()
            while len(data) < size:
                part = self.process.stdout.read(size - len(data))
                if not part:
                    if not data:
                        return None
                    raise AssertionError("partial native output frame")
                data.extend(part)
            return bytes(data)

        try:
            while (header := read_exact(4)) is not None:
                length, = struct.unpack("<I", header)
                assert 0 < length < 1024 * 1024
                self.lengths.append(length)
                if self.pause_output_after is not None and len(self.lengths) > self.pause_output_after:
                    self.output_paused.set()
                    self.resume_output.wait()
                body = read_exact(length)
                assert body is not None
                self.frames.put(json.loads(body))
        except Exception as error:
            self.frames.put(error)

    def frame(self):
        result = self.frames.get(timeout=10)
        if isinstance(result, Exception):
            raise result
        return result

    def message(self):
        first = self.frame()
        if first.get("type") != "native_chunk":
            return first
        assert first["index"] == 0
        parts = [first["data"]]
        for index in range(1, first["total"]):
            part = self.frame()
            assert (part["type"], part["id"], part["index"], part["total"]) == (
                "native_chunk", first["id"], index, first["total"],
            )
            parts.append(part["data"])
        return json.loads("".join(parts))

    def write(self, raw):
        remaining = memoryview(raw)
        while remaining:
            count = self.process.stdin.write(remaining)
            assert count is not None and count > 0
            remaining = remaining[count:]

    def send(self, message):
        data = json.dumps(message, ensure_ascii=True).encode("utf-8")
        self.write(struct.pack("<I", len(data)) + data)

    def finish(self, *, eof=True):
        if eof:
            self.process.stdin.close()
        code = self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        assert not self.reader.is_alive()
        return code, self.process.stderr.read().decode("utf-8", errors="replace")

    def close(self):
        self.resume_output.set()
        if not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()


@pytest.mark.parametrize("cli", [False, True])
def test_stdio_http_round_trip_chunks_and_eof_leave_shared_bridge_alive(native_http_bridge, tmp_path, cli):
    driver = native_http_bridge
    host = NativeProcess(driver, tmp_path, cli=cli)
    try:
        assert host.message() == {"type": "host_ready", "protocol": 1, "bridgePort": driver.port}
        host.send({"type": "ext_ready", "clientId": "synthetic-native", "browser": "Chrome", "tabs": []})
        host.send({"type": "ping"})
        assert host.message() == {"type": "pong"}
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(http_json, driver, "/link", {
                "cmd": "ext_cmd", "clientId": "synthetic-native", "timeout": 8,
                "payload": {"cmd": "synthetic_echo", "value": "中文\n\u001a"},
            })
            command = host.message()
            assert command["cmd"] == {"cmd": "synthetic_echo", "value": "中文\n\u001a"}
            host.send({"type": "ack", "id": command["id"]})
            host.send({"type": "result", "id": command["id"], "result": {"value": command["cmd"]["value"]}})
            assert pending.result(timeout=10)["r"]["data"] == {"value": "中文\n\u001a"}

            large = "x" * (1100 * 1024)
            pending = pool.submit(driver.ext_cmd, {"cmd": "synthetic_echo", "value": large},
                                  client_id="synthetic-native", timeout=8)
            command = host.message()
            assert command["cmd"] == {"cmd": "synthetic_echo", "value": large}
            result = {"unicode": "汉字" * 50_000, "surrogate": "\ud800"}
            host.send({"type": "result", "id": command["id"], "result": result})
            assert pending.result(timeout=10)["data"] == result
        code, diagnostic = host.finish()
        assert code == 0, diagnostic
        assert diagnostic == "" and host.frames.empty()
        assert len(host.lengths) >= 7 and max(host.lengths) < 1024 * 1024
        assert not driver.ext_clients and not driver._native_connections._connections
        assert http_json(driver, "/api/extension/config")["transport"] == "native"
    finally:
        host.close()


def test_truncated_stdio_exits_with_no_payload_in_stderr_and_keeps_http_listener(native_http_bridge, tmp_path):
    host = NativeProcess(native_http_bridge, tmp_path)
    try:
        assert host.message()["type"] == "host_ready"
        host.write(struct.pack("<I", 200) + b"synthetic-private-payload")
        code, diagnostic = host.finish()
        assert code == 1 and "NativeProtocolError" in diagnostic
        assert "synthetic-private-payload" not in diagnostic
        assert host.frames.empty()
        assert not native_http_bridge._native_connections._connections
        assert http_json(native_http_bridge, "/api/extension/config")["protocol"] == 1
    finally:
        host.close()


def test_bridge_failure_exits_with_browser_stdin_still_open(native_http_bridge, tmp_path):
    host = NativeProcess(native_http_bridge, tmp_path)
    try:
        assert host.message()["type"] == "host_ready"
        native_http_bridge._native_connections.close_all()
        code, diagnostic = host.finish(eof=False)
        assert code == 1 and "NativeBridgeError" in diagnostic
        assert "Fatal Python error" not in diagnostic and "_enter_buffered_busy" not in diagnostic
        assert host.frames.empty()
        assert http_json(native_http_bridge, "/api/extension/config")["protocol"] == 1
    finally:
        host.close()


@pytest.mark.parametrize("cause", ["eof", "bridge_failure", "stdout_closed"])
def test_stdout_backpressure_does_not_prevent_host_cleanup(native_http_bridge, tmp_path, cause):
    driver = native_http_bridge
    host = NativeProcess(driver, tmp_path, pause_output_after=1)
    try:
        assert host.message()["type"] == "host_ready"
        connection = next(iter(driver._native_connections._connections.values()))
        connection.send_message(json.dumps({"id": "synthetic-backpressure", "code": "x" * 300_000}))
        assert host.output_paused.wait(5), "host did not begin writing the large frame"
        if cause == "eof":
            host.process.stdin.close()
        elif cause == "bridge_failure":
            driver._native_connections.close_all()
        else:
            host.process.stdout.close()
        assert host.process.wait(timeout=5) == (0 if cause == "eof" else 1)
        diagnostic = host.process.stderr.read().decode("utf-8", errors="replace")
        assert "Fatal Python error" not in diagnostic and "_enter_buffered_busy" not in diagnostic
        assert not driver._native_connections._connections
        assert http_json(driver, "/api/extension/config")["protocol"] == 1
    finally:
        host.close()
