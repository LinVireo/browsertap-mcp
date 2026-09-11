"""JS UTF-16 values must survive the real UTF-8 MCP result boundary."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ImageContent,
    JSONRPCMessage,
    JSONRPCResponse,
    TextContent,
)

from browsertap_mcp import server as S

HIGH = chr(0xD800)
LOW = chr(0xDC00)
SID = "chrome_unicode:7"
UNSAFE_VALUES = [
    pytest.param(HIGH, id="lone-high"),
    pytest.param(LOW, id="lone-low"),
    pytest.param(["中文😀", {"nested": LOW}], id="nested-low"),
    pytest.param({HIGH: LOW, r"\ud800": "literal"}, id="key-collision-control"),
    pytest.param("a" * 25000 + HIGH, id="large-high"),
]


@pytest.fixture(autouse=True)
def private_result_files(monkeypatch, tmp_path):
    """Keep real exclusive file writes in pytest's system temporary directory."""
    mkstemp = S.tempfile.mkstemp

    def create(*args, **kwargs):
        return mkstemp(*args, **{**kwargs, "dir": tmp_path})

    monkeypatch.setattr(S.tempfile, "mkstemp", create)
    return tmp_path, mkstemp


def _result_file_path(metadata):
    encoding = metadata.get("result_file_encoding")
    assert encoding in {None, "json"}
    filename = json.loads(metadata["result_file"]) if encoding == "json" else metadata["result_file"]
    return Path(filename)


def _read_result_file(metadata):
    assert metadata["result_externalized"] is True
    assert metadata["result_format"] == "utf-8-json"
    path = _result_file_path(metadata)
    payload = path.read_bytes()
    text = payload.decode("utf-8", errors="strict")
    assert metadata["result_bytes"] == len(payload)
    assert metadata["result_sha256"] == hashlib.sha256(payload).hexdigest()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return json.loads(text)


def _restored_envelope(result):
    envelope = result.structuredContent
    if envelope.get("result_file_scope") == "envelope":
        return _read_result_file(envelope)
    if envelope.get("result_file_scope") == "mcp-call-result":
        return _read_result_file(envelope)["structuredContent"]
    if envelope.get("result_json_scope") in {"envelope", "mcp-call-result"}:
        original = json.loads(envelope["result_json"])
        return original["structuredContent"] if envelope["result_json_scope"] == "mcp-call-result" else original
    return envelope


def _read_js_value(metadata):
    if metadata.get("result_externalized"):
        assert metadata["result_file_scope"] == "js-value"
        return _read_result_file(metadata)
    assert metadata["result_externalized"] is False
    assert metadata["result_json_scope"] == "js-value"
    assert metadata["result_json"].isascii()
    assert not {"result_file", "result_file_encoding", "result_file_scope", "result_bytes", "result_sha256"} & metadata.keys()
    return json.loads(metadata["result_json"])


def _wire_call(name, arguments, *, fastmcp=False):
    """Exercise SDK conversion, schema validity and JSONRPC wire serialization."""
    async def invoke():
        if fastmcp:
            raw = await S.mcp.call_tool(name, arguments)
            result = raw if isinstance(raw, CallToolResult) else CallToolResult(
                content=raw[0], structuredContent=raw[1],
            )
        else:
            request = CallToolRequest(params={"name": name, "arguments": arguments})
            server_result = await S.mcp._mcp_server.request_handlers[CallToolRequest](request)
            # The low-level handler returns a ServerResult; this is also the
            # serializer the server's request responder must be able to use.
            server_result.model_dump(mode="json", by_alias=True)
            server_result.model_dump_json(by_alias=True)
            result = server_result.root
        definitions = await S.mcp.list_tools()
        definition = next(tool for tool in definitions if tool.name == name)
        assert definition.outputSchema is not None
        assert isinstance(result, CallToolResult)
        assert isinstance(result.structuredContent, dict)
        # Explicit CallToolResult bypasses the SDK's own output check. Enforce
        # the advertised schema independently so a text-only result cannot pass.
        jsonschema.validate(result.structuredContent, definition.outputSchema)
        response = JSONRPCResponse(
            jsonrpc="2.0", id=7, result=result.model_dump(mode="json", by_alias=True),
        )
        wire = JSONRPCMessage(root=response).model_dump_json(by_alias=True).encode("utf-8", errors="strict")
        decoded = json.loads(wire)["result"]
        assert decoded["structuredContent"] == result.structuredContent
        assert decoded["isError"] == result.isError
        return result

    return asyncio.run(invoke())


@pytest.fixture
def register_probe():
    registered = []

    def register(producer):
        name = "unicode_result_probe"

        @S.mcp.tool(name=name)
        def unicode_result_probe() -> dict[str, Any]:
            return producer()

        registered.append(name)
        return name

    yield register
    for name in registered:
        S.mcp.remove_tool(name)


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_json_bytes_preserve_surrogate_keys_and_values(value):
    payload = S._serialize_execute_js_value(value)
    assert json.loads(payload.decode("utf-8", errors="strict")) == value
    assert b"\\ud800" in payload or b"\\udc00" in payload


@pytest.mark.parametrize("value", UNSAFE_VALUES)
def test_even_small_surrogate_values_use_lossless_result_files(value):
    original = {"status": "success", "js_return": value, "tab_id": 7}
    before = copy.deepcopy(original)
    result = S._externalize_execute_js_result(original)
    assert result["status"] == "success"
    assert result["js_return"] is None
    assert _read_result_file(result) == value
    assert result["result_inline_limit_bytes"] == S.EXECUTE_JS_INLINE_MAX_BYTES
    assert original == before


@pytest.mark.parametrize("value", ["中文😀𐀀", r"\ud800", {r"\udc00": "literal"}])
def test_valid_unicode_and_literal_backslash_escapes_stay_inline(value):
    original = {"status": "success", "js_return": value}
    assert S._externalize_execute_js_result(original) is original
    assert S._serialize_execute_js_value(value) == json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


@pytest.mark.parametrize("extra", [0, 1])
def test_normal_inline_limit_is_still_measured_in_encoded_bytes(extra):
    value = "中" * 8191 + "a" * (1 + extra)
    payload = S._serialize_execute_js_value(value)
    assert len(payload) == S.EXECUTE_JS_INLINE_MAX_BYTES + extra
    original = {"js_return": value}
    result = S._externalize_execute_js_result(original)
    if extra:
        assert _read_result_file(result) == value
    else:
        assert result is original


def test_host_object_diagnostic_fallback_remains_exportable():
    class HostValue:
        def __str__(self):
            return "host:" + HIGH

    assert json.loads(S._serialize_execute_js_value(HostValue())) == "host:" + HIGH
    cycle = []
    cycle.append(cycle)
    assert json.loads(S._serialize_execute_js_value(cycle)) == str(cycle)


def test_explicit_surrogate_pair_preserves_javascript_code_units():
    pair = chr(0xD83D) + chr(0xDE00)
    restored = json.loads(S._serialize_execute_js_value(pair))
    assert restored == "😀"
    assert restored.encode("utf-16-le") == pair.encode("utf-16-le", errors="surrogatepass")


@pytest.mark.parametrize("fastmcp", [False, True], ids=["lowlevel-wire", "fastmcp-wire"])
@pytest.mark.parametrize("failure", [False, True], ids=["success", "failed"])
def test_arbitrary_tool_values_survive_mcp_without_changing_the_verdict(register_probe, failure, fastmcp):
    payload = {
        "status": "failed" if failure else "success",
        "error": LOW,
        "value": {HIGH: [LOW, r"\ud800"]},
        "retry_safe": False,
        "delivery_state": "delivered_no_result",
        "operation_id": "unicode-operation",
    }
    before = copy.deepcopy(payload)
    name = register_probe(lambda: payload)
    result = _wire_call(name, {}, fastmcp=fastmcp)
    envelope = result.structuredContent
    assert envelope["ok"] is not failure
    assert result.isError is failure
    assert envelope["retryable"] is False
    assert envelope["retry_safe"] is False
    assert envelope["delivery_state"] == "delivered_no_result"
    assert envelope["operation_id"] == "unicode-operation"
    if failure:
        assert envelope["error"]["message_encoding"] == "json"
        assert json.loads(envelope["error"]["message"]) == LOW
    restored = _restored_envelope(result)
    assert restored == S._result_envelope("unicode_result_probe", before)
    assert payload == before


def test_exception_message_does_not_replace_no_replay_receipt(register_probe):
    error = S.BridgeNoResponseError(
        "dispatched:" + HIGH,
        delivery_state="delivered_no_result", retry_safe=False,
        operation_id="unicode-timeout", reservation_held=True,
        poll_with="get_execute_js_result",
    )

    def raise_error():
        raise error

    result = _wire_call(register_probe(raise_error), {})
    envelope = result.structuredContent
    assert result.isError is True and envelope["ok"] is False
    assert envelope["error_code"] == "no_response"
    assert envelope["retryable"] is False and envelope["retry_safe"] is False
    assert envelope["operation_id"] == "unicode-timeout"
    assert envelope["reservation_held"] is True
    assert envelope["poll_with"] == "get_execute_js_result"
    assert envelope["error"]["message_encoding"] == "json"
    assert json.loads(envelope["error"]["message"]) == str(error)
    assert _restored_envelope(result) == S._result_envelope("unicode_result_probe", exc=error)


@pytest.mark.parametrize("route", ["sync", "poll", "late-success", "late-failure"])
@pytest.mark.parametrize("large", [False, True], ids=["small", "large"])
def test_execution_and_repeated_receipts_survive_the_wire_without_replay(monkeypatch, route, large):
    value = {HIGH: ("a" * 25000 if large else "中文😀") + LOW}
    raw = {
        "status": "success" if route in {"sync", "poll"} else "unknown",
        "operation_id": "unicode-operation", "executed_tab_id": 7,
        "retry_safe": False, "delivery_state": "delivered_no_result",
        "reservation_held": False,
    }
    if route.startswith("late"):
        raw["late_result"] = {"success": route == "late-success", "data": value}
    else:
        raw["data"] = value
    before = copy.deepcopy(raw)
    queries = []
    executions = []

    def query(operation_id, **kwargs):
        queries.append(operation_id)
        return raw

    def execute(*args, **kwargs):
        executions.append(True)
        return {
            **{key: item for key, item in raw.items() if key not in {"data", "executed_tab_id"}},
            "js_return": raw["data"], "tab_id": 7,
        }

    driver = SimpleNamespace(default_session_id=SID, get_execute_js_result=query)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda **kwargs: [{"id": SID}])
    monkeypatch.setattr(S.simphtml, "execute_js_rich", execute)
    if route == "sync":
        result = _wire_call("execute_js", {"script": "synthetic", "session_id": SID})
        assert len(executions) == 1 and queries == []
        assert result.structuredContent["ok"] is True and result.isError is False
        assert _read_result_file(result.structuredContent["data"]) == value
    else:
        for _ in range(2):
            result = _wire_call("get_execute_js_result", {"operation_id": "unicode-operation"})
            envelope = _restored_envelope(result)
            assert result.isError is route.startswith("late")
            assert result.structuredContent["ok"] is (route == "poll")
            assert result.structuredContent["retry_safe"] is False
            assert result.structuredContent["retryable"] is False
            if route == "poll":
                assert _read_result_file(envelope["data"]) == value
            else:
                late = envelope["legacy"]["late_result"]
                assert late["success"] is (route == "late-success")
                restored_value = _read_result_file(late) if late.get("result_externalized") else late["data"]
                assert restored_value == value
        assert queries == ["unicode-operation", "unicode-operation"]
        assert executions == []
    assert raw == before


def test_native_result_content_and_metadata_are_recoverable(register_probe):
    original = CallToolResult(
        content=[
            TextContent(type="text", text="original:" + HIGH),
            ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
        ],
        structuredContent={"status": "failed", "error": LOW, "retry_safe": False},
        isError=True,
        _meta={"original": HIGH, "safe": 7},
    )
    before = original.model_dump(mode="python", by_alias=True)
    result = _wire_call(register_probe(lambda: original), {})
    assert result.isError is True and result.structuredContent["ok"] is False
    archive = _read_result_file(result.structuredContent)
    assert result.structuredContent["result_file_scope"] == "mcp-call-result"
    assert archive["content"] == before["content"]
    assert archive["_meta"] == before["_meta"]
    assert archive["isError"] is True
    assert archive["structuredContent"] == S._result_envelope("unicode_result_probe", original.structuredContent)
    assert any(isinstance(item, ImageContent) and item.data == "aW1hZ2U=" for item in result.content)
    assert result.meta["safe"] == 7
    assert result.structuredContent["result_content_externalized"] is True
    assert result.structuredContent["result_meta_externalized"] is True
    assert original.model_dump(mode="python", by_alias=True) == before


def test_native_result_with_safe_unicode_is_unchanged(register_probe):
    original = CallToolResult(
        content=[TextContent(type="text", text="中文😀 " + r"\ud800")],
        structuredContent={"status": "success", "value": "中文😀"},
        _meta={"label": "中文😀"},
    )
    result = _wire_call(register_probe(lambda: original), {})
    assert result.content == original.content
    assert result.meta == original.meta
    assert result.structuredContent["data"] == original.structuredContent
    assert "result_file_scope" not in result.structuredContent


def test_unsafe_error_code_and_target_are_explicit_and_recoverable(register_probe):
    payload = {
        "status": "failed", "code": HIGH, "error": LOW,
        "session_id": SID, "url": "https://example.test/" + HIGH,
        "retry_safe": False,
    }
    result = _wire_call(register_probe(lambda: payload), {})
    envelope = result.structuredContent
    assert envelope["error"]["code_encoding"] == "json"
    assert envelope["error_code_encoding"] == "json"
    assert json.loads(envelope["error_code"]) == HIGH
    assert envelope["target"]["session_id"] == SID
    assert _restored_envelope(result) == S._result_envelope("unicode_result_probe", payload)


@pytest.mark.parametrize("failure", [False, True], ids=["success", "failed"])
@pytest.mark.parametrize("native", [False, True], ids=["envelope", "native"])
def test_adapter_disk_failure_keeps_the_complete_receipt_without_recursion(
    monkeypatch, register_probe, failure, native,
):
    calls = []
    disk_error = OSError("disk full:" + HIGH)

    def cannot_write(payload):
        calls.append(payload)
        raise disk_error

    monkeypatch.setattr(S, "_write_execute_js_payload", cannot_write)
    payload = {
        "status": "failed" if failure else "success", "error": LOW,
        "value": {HIGH: "中文😀", r"\ud800": "literal"},
        "retry_safe": False, "operation_id": "unicode-operation",
    }
    value = CallToolResult(
        content=[TextContent(type="text", text=HIGH)],
        structuredContent=payload, isError=failure, _meta={"label": LOW},
    ) if native else payload
    result = _wire_call(register_probe(lambda: value), {})
    envelope = result.structuredContent
    assert len(calls) == 1
    assert result.isError is failure and envelope["ok"] is not failure
    assert envelope["retry_safe"] is False and envelope["retryable"] is False
    assert envelope["operation_id"] == "unicode-operation"
    assert envelope["result_externalized"] is False
    assert "result_file" not in envelope
    assert envelope["result_json"].isascii()
    assert envelope["legacy" if failure else "data"] == {"result_json_ref": "#/result_json"}
    assert json.loads(envelope["result_file_error"]["message_json"]) == str(disk_error)
    assert _restored_envelope(result) == S._result_envelope("unicode_result_probe", payload)
    if native:
        archive = json.loads(envelope["result_json"])
        assert archive["_meta"] == value.meta
        assert archive["content"] == value.model_dump(mode="python", by_alias=True)["content"]


@pytest.mark.parametrize("late", [False, True], ids=["completed", "late-unknown"])
def test_unicode_result_file_failure_does_not_replay_or_reclassify_the_operation(monkeypatch, late):
    receipt = {
        "status": "unknown" if late else "success", "operation_id": "unicode-operation",
        "retry_safe": False, "delivery_state": "delivered_no_result",
    }
    if late:
        receipt["late_result"] = {"success": True, "data": {HIGH: LOW}}
    else:
        receipt["data"] = {HIGH: LOW}
    before = copy.deepcopy(receipt)
    queries = []
    writes = []

    def query(operation_id, **kwargs):
        queries.append(operation_id)
        return receipt

    def cannot_write(payload):
        writes.append(payload)
        raise PermissionError("read-only temporary directory")

    driver = SimpleNamespace(get_execute_js_result=query)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "_write_execute_js_payload", cannot_write)
    for _ in range(2):
        result = _wire_call("get_execute_js_result", {"operation_id": "unicode-operation"})
        assert result.isError is late and result.structuredContent["ok"] is not late
        assert result.structuredContent["retryable"] is False
        assert result.structuredContent["retry_safe"] is False
        restored = _restored_envelope(result)
        if late:
            late_result = restored["legacy"]["late_result"]
            assert late_result["success"] is True and late_result["data"] is None
            assert _read_js_value(late_result) == before["late_result"]["data"]
        else:
            assert restored["data"]["js_return"] is None
            assert _read_js_value(restored["data"]) == before["data"]
    assert queries == ["unicode-operation", "unicode-operation"]
    assert len(writes) == 2
    assert receipt == before


@pytest.mark.parametrize("disk_failure", [False, True], ids=["file", "inline-fallback"])
def test_adapter_descriptor_cannot_reuse_stale_result_metadata(monkeypatch, register_probe, disk_failure):
    payload = {
        "status": "success", "value": HIGH,
        "result_externalized": True, "result_file": "previous-value.json",
        "result_file_encoding": "json",
        "result_file_scope": "envelope", "result_bytes": 7,
        "result_sha256": "previous-value-digest",
        "result_json": '{"previous": true}', "result_json_scope": "envelope",
    }
    if disk_failure:
        def cannot_write(payload):
            raise OSError("disk full")

        monkeypatch.setattr(S, "_write_execute_js_payload", cannot_write)
    result = _wire_call(register_probe(lambda: payload), {})
    envelope = result.structuredContent
    if disk_failure:
        assert not {"result_file", "result_file_encoding", "result_file_scope", "result_bytes", "result_sha256"} & envelope.keys()
    else:
        assert "result_json" not in envelope and "result_json_scope" not in envelope
    assert _restored_envelope(result) == S._result_envelope("unicode_result_probe", payload)


@pytest.mark.parametrize("source", ["content", "meta"])
def test_native_only_surrogates_also_survive_the_wire(register_probe, source):
    original = CallToolResult(
        content=[
            TextContent(type="text", text=HIGH if source == "content" else "safe"),
            ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
        ],
        structuredContent={"status": "success", "value": 7},
        _meta={"label": LOW if source == "meta" else "safe"},
    )
    result = _wire_call(register_probe(lambda: original), {})
    assert result.isError is False and result.structuredContent["ok"] is True
    archive = _read_result_file(result.structuredContent)
    assert archive["structuredContent"]["data"] == original.structuredContent
    assert archive["content"] == original.model_dump(mode="python", by_alias=True)["content"]
    assert archive["_meta"] == original.meta
    assert any(isinstance(item, ImageContent) for item in result.content)


@pytest.mark.parametrize("route", ["sync", "poll", "late-unknown"])
@pytest.mark.parametrize("value", ["a" * 25000, "中文😀" * 7000], ids=["ascii", "unicode"])
@pytest.mark.parametrize("disk_failure", [False, True], ids=["file", "inline-fallback"])
def test_normal_large_results_keep_the_value_and_verdict_after_file_failure(
    monkeypatch, route, value, disk_failure,
):
    raw = {
        "status": "unknown" if route == "late-unknown" else "success",
        "operation_id": "large-operation", "executed_tab_id": 7,
        "retry_safe": False, "reservation_held": False,
        "delivery_state": "delivered_no_result",
    }
    if route == "late-unknown":
        raw["late_result"] = {"success": True, "data": value}
    else:
        raw["data"] = value
    before = copy.deepcopy(raw)
    queries = []
    executions = []
    writes = []
    writer = S._write_execute_js_payload

    def write(payload):
        writes.append(payload)
        if disk_failure:
            raise OSError("disk full")
        return writer(payload)

    def query(operation_id, **kwargs):
        queries.append(operation_id)
        return raw

    def execute(*args, **kwargs):
        executions.append(True)
        return {
            **{key: item for key, item in raw.items() if key not in {"data", "executed_tab_id"}},
            "js_return": raw["data"], "tab_id": 7,
        }

    monkeypatch.setattr(S, "_write_execute_js_payload", write)
    monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace(
        default_session_id=SID, get_execute_js_result=query,
    ))
    monkeypatch.setattr(S, "ensure_sessions", lambda **kwargs: [{"id": SID}])
    monkeypatch.setattr(S.simphtml, "execute_js_rich", execute)
    if route == "sync":
        result = _wire_call("execute_js", {"script": "synthetic", "session_id": SID})
        assert executions == [True] and queries == []
    else:
        result = _wire_call("get_execute_js_result", {"operation_id": "large-operation"})
        assert queries == ["large-operation"] and executions == []
    envelope = result.structuredContent
    assert result.isError is (route == "late-unknown")
    assert envelope["ok"] is (route != "late-unknown")
    assert envelope["retryable"] is False and envelope["retry_safe"] is False
    assert envelope["operation_id"] == "large-operation"
    assert envelope["delivery_state"] == "delivered_no_result"
    if route == "late-unknown":
        metadata = envelope["legacy"]["late_result"]
        assert envelope["status"] == "unknown"
        assert metadata["success"] is True and metadata["data"] is None
    else:
        metadata = envelope["data"]
        assert metadata["js_return"] is None
    assert _read_js_value(metadata) == value
    if disk_failure:
        assert metadata["result_encoding_reason"] == "result-file-write-failed"
        assert json.loads(metadata["result_file_error"]["message_json"]) == "disk full"
    assert len(writes) == 1
    assert raw == before


@pytest.mark.parametrize("disk_failure", [False, True], ids=["file", "inline-fallback"])
def test_js_value_descriptor_cannot_reuse_stale_result_metadata(monkeypatch, disk_failure):
    original = {
        "status": "success", "js_return": "a" * 25000,
        "result_file": "previous-value.json", "result_file_scope": "envelope",
        "result_file_encoding": "json",
        "result_bytes": 7, "result_sha256": "previous-digest",
        "result_json": '{"previous": true}', "result_json_scope": "envelope",
        "result_file_error": {"message_json": '"previous"'},
        "result_note": "unrelated metadata stays", "retry_safe": False,
    }
    before = copy.deepcopy(original)
    if disk_failure:
        def cannot_write(payload):
            raise OSError("disk full")

        monkeypatch.setattr(S, "_write_execute_js_payload", cannot_write)
    result = S._externalize_execute_js_result(original)
    assert _read_js_value(result) == before["js_return"]
    assert result["result_note"] == before["result_note"]
    assert result["retry_safe"] is False
    if not disk_failure:
        assert not {"result_json", "result_json_scope", "result_file_error"} & result.keys()
    assert original == before


_RESULT_FILE_DIRECTORY_KINDS = ["chinese", "literal-escape"]
if os.name == "nt":
    _RESULT_FILE_DIRECTORY_KINDS.extend(["high-surrogate", "low-surrogate"])


@pytest.fixture(params=_RESULT_FILE_DIRECTORY_KINDS)
def result_file_directory(monkeypatch, private_result_files, request):
    root, mkstemp = private_result_files
    kind = request.param
    has_surrogate = kind.endswith("surrogate")
    name = {
        "chinese": "中文😀",
        "literal-escape": "ud800" if os.name == "nt" else r"\ud800",
        "high-surrogate": "high-" + HIGH,
        "low-surrogate": "low-" + LOW,
    }[kind]
    directory = root / name
    assert directory.resolve().parent == root.resolve()
    directory.mkdir()
    created = []

    def create(*args, **kwargs):
        descriptor, filename = mkstemp(*args, **{**kwargs, "dir": directory})
        created.append(Path(filename))
        return descriptor, filename

    monkeypatch.setattr(S.tempfile, "mkstemp", create)
    try:
        yield directory, has_surrogate
    finally:
        # Remove only files recorded by this test's actual mkstemp call. This
        # also cleans a second archive created by the broken r1 adapter.
        for path in created:
            assert path.resolve().parent == directory.resolve()
            path.unlink(missing_ok=True)
        directory.rmdir()
        assert not directory.exists()


@pytest.mark.parametrize("kind", ["large-js", "surrogate-js", "error-envelope", "native-result"])
def test_result_file_directory_survives_the_complete_wire_and_actual_read(
    monkeypatch, register_probe, result_file_directory, kind,
):
    directory, has_surrogate = result_file_directory
    if kind in {"large-js", "surrogate-js"}:
        value = "a" * 25000 if kind == "large-js" else {HIGH: LOW}
        receipt = {
            "status": "success", "data": value, "operation_id": "path-operation",
            "retry_safe": False, "delivery_state": "delivered_no_result",
        }
        before = copy.deepcopy(receipt)
        queries = []

        def query(operation_id, **kwargs):
            queries.append(operation_id)
            return receipt

        monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace(get_execute_js_result=query))
        result = _wire_call("get_execute_js_result", {"operation_id": "path-operation"})
        envelope = result.structuredContent
        metadata = envelope["data"]
        assert result.isError is False and envelope["ok"] is True
        assert envelope["retry_safe"] is False and envelope["retryable"] is False
        assert envelope["operation_id"] == "path-operation"
        assert metadata["result_file_scope"] == "js-value"
        assert _read_result_file(metadata) == value
        assert receipt == before and queries == ["path-operation"]
    else:
        payload = {"status": "failed", "error": HIGH, "retry_safe": False}
        native = CallToolResult(
            content=[ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png")],
            structuredContent=payload, _meta={"original": LOW}, isError=True,
        )
        result = _wire_call(register_probe(lambda: native if kind == "native-result" else payload), {})
        metadata = result.structuredContent
        assert result.isError is True and metadata["ok"] is False
        assert metadata["retry_safe"] is False and metadata["retryable"] is False
        restored = _read_result_file(metadata)
        expected_envelope = S._result_envelope("unicode_result_probe", payload)
        if kind == "native-result":
            assert metadata["result_file_scope"] == "mcp-call-result"
            assert restored["structuredContent"] == expected_envelope
            assert restored["_meta"] == native.meta
            assert restored["content"] == native.model_dump(mode="python", by_alias=True)["content"]
        else:
            assert metadata["result_file_scope"] == "envelope"
            assert restored == expected_envelope
    if has_surrogate:
        assert metadata["result_file_encoding"] == "json"
        assert metadata["result_file"].isascii()
        assert json.loads(metadata["result_file"]) == str(_result_file_path(metadata))
    else:
        assert "result_file_encoding" not in metadata
        assert metadata["result_file"] == str(_result_file_path(metadata))
    assert _result_file_path(metadata).parent == directory


@pytest.mark.parametrize("surrogate", [HIGH, LOW], ids=["high", "low"])
def test_simulated_surrogate_path_is_lossless_through_the_complete_wire(monkeypatch, surrogate):
    # A portable spelling probe: files are genuinely written, while this map
    # supplies the synthetic filesystem name on hosts that reject that name.
    writer = S._write_execute_js_payload
    aliases = {}

    def write(payload):
        path, size, digest = writer(payload)
        alias = path.with_name("synthetic-" + surrogate + "-" + path.name)
        aliases[str(alias)] = path
        return alias, size, digest

    value = "中文😀" * 7000
    monkeypatch.setattr(S, "_write_execute_js_payload", write)
    monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace(
        get_execute_js_result=lambda *args, **kwargs: {
            "status": "success", "data": value, "retry_safe": False,
        },
    ))
    result = _wire_call("get_execute_js_result", {"operation_id": "synthetic-path"})
    metadata = result.structuredContent["data"]
    assert result.isError is False and result.structuredContent["ok"] is True
    assert metadata["result_file_encoding"] == "json"
    alias = json.loads(metadata["result_file"])
    assert surrogate in alias
    assert len(aliases) == 1
    payload = aliases[alias].read_bytes()
    assert json.loads(payload.decode("utf-8", errors="strict")) == value
    assert metadata["result_bytes"] == len(payload)
    assert metadata["result_sha256"] == hashlib.sha256(payload).hexdigest()
