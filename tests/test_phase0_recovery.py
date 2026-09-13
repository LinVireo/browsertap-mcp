from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S
from browsertap_mcp.browser_bridge import BrowserBridge, Session

BACKGROUND = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "browsertap_mcp"
    / "chrome_extension"
    / "background.js"
)


def _run_node_script(script: str) -> dict:
    handle, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
        completed = subprocess.run(["node", path], capture_output=True, text=True)
        if completed.returncode:
            raise AssertionError(f"node harness failed: {completed.stderr.strip()}")
        return json.loads(completed.stdout)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _create_operation_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    prefix_start = source.index("const CREATE_OPERATIONS_KEY")
    prefix_end = source.index("\nfunction newTabGeneration", prefix_start)
    create_start = source.index("async function createTabAck")
    create_end = source.index("\nasync function handleExtMessage", create_start)
    return source[prefix_start:prefix_end] + "\n" + source[create_start:create_end]


def _tab_generation_source() -> str:
    """The generation bookkeeping, sliced so node can run it for real.

    Everything this touches is `chrome.storage.session` and `chrome.tabs.query`,
    both of which the harness fakes, so the eviction behaviour that matters here
    is testable offline -- which the browser it protects is not.
    """
    source = BACKGROUND.read_text(encoding="utf-8")
    prefix_start = source.index("const TAB_GENERATIONS_KEY")
    prefix_end = source.index("\n// Native tab creation is a two-step side effect")
    body_start = source.index("function newTabGeneration")
    body_end = source.index("\nasync function validateTabCloseGenerations")
    return source[prefix_start:prefix_end] + "\n" + source[body_start:body_end]


def _tab_identity_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("const TAB_IDENTITIES_KEY")
    end = source.index("\nasync function validateTabCloseGenerations", start)
    return source[start:end]


def _run_generation_harness(script: str) -> dict:
    """Run one generation scenario, keeping the module's own logging off stdout.

    The failure path logs, and the harness passes its result back as the only
    thing on stdout, so an unstubbed `console.log` would corrupt the JSON. The
    collected lines are worth asserting on anyway: being silent is what let this
    failure go unnoticed.
    """
    full = (
        "const logs = [];\nconsole.log = (...args) => logs.push(args.join(' '));\n"
        + script.replace("__SOURCE__", _tab_generation_source())
    )
    completed = subprocess.run(
        ["node", "-e", full], capture_output=True, text=True
    )
    if completed.returncode:
        raise AssertionError(f"node harness failed: {completed.stderr.strip()}")
    return json.loads(completed.stdout)


def _run_identity_harness(script: str) -> dict:
    full = (
        "const logs = [];\nconsole.log = (...args) => logs.push(args.join(' '));\n"
        + script.replace("__SOURCE__", _tab_identity_source())
    )
    completed = subprocess.run(
        ["node", "-e", full], capture_output=True, text=True
    )
    if completed.returncode:
        raise AssertionError(f"node identity harness failed: {completed.stderr.strip()}")
    return json.loads(completed.stdout)


def _batch_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function batchDeadlineRemainingMs")
    end = source.index("\n\nasync function handleCDP", start)
    return source[start:end]


def _cdp_handlers_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function batchDeadlineRemainingMs")
    end = source.index("\n// Filter out chrome://", start)
    return source[start:end]


def _ws_exec_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function handleWsExec(data)")
    end = source.index("\n\nfunction connectWS()", start)
    return source[start:end]


def _websocket_keepalive_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function scheduleProbe()")
    end = source.index("\n\nasync function isServerAlive", start)
    return source[start:end]


def _websocket_connect_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function connectWS()")
    end = source.index("\n\n// Initial connect", start)
    return source[start:end]


class _Driver:
    def __init__(self, responses=None, default="chrome:profile:7"):
        self.default_session_id = default
        self.responses = list(responses or [])
        self.calls = []

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload, client_id, timeout))
        if payload.get("method") == "create_status":
            routed_client = client_id
            if routed_client is None and self.default_session_id:
                routed_client = str(self.default_session_id).rsplit(":", 1)[0]
            return {
                "data": {
                    "status": "not_found",
                    "operation_status": "not_found",
                    "operation_id": payload["operation_id"],
                    "may_have_created": False,
                    "retry_safe": True,
                },
                "client_id": routed_client or "chrome:profile",
            }
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {"data": {"ok": True}}


def _sessions(sid="chrome:profile:7", url="https://example.test/"):
    return [{"id": sid, "url": url, "browser": "chrome"}]


def _install(monkeypatch, driver, sessions=None):
    sessions = sessions or _sessions()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(
        S, "active_sessions", lambda timeout=None, fresh=False: list(sessions)
    )
    monkeypatch.setattr(S, "ensure_sessions", lambda *args, **kwargs: list(sessions))
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)


def _fresh_tab_ownership(monkeypatch):
    registry = S._TabOwnershipRegistry()
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", registry)
    return registry


def test_get_automation_profile_defaults_to_lab_and_env_can_force_safe(monkeypatch):
    monkeypatch.delenv("BROWSERTAP_MODE", raising=False)
    monkeypatch.delenv("BROWSERTAP_LAB_NO_ELICIT", raising=False)
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", None)
    profile = S.get_automation_profile()
    assert profile["mode"] == "lab"
    assert profile["no_elicit"] is True
    assert profile["physical_approval"] == "not_required"
    assert profile["site_permission_approval"] == "not_required"

    monkeypatch.setenv("BROWSERTAP_MODE", "safe")
    profile = S.get_automation_profile()
    assert profile["mode"] == "safe"
    assert profile["no_elicit"] is False
    assert profile["physical_approval"] == "every_action"
    assert profile["site_permission_approval"] == "every_allow"


def test_lab_can_explicitly_restore_session_level_approval(monkeypatch):
    monkeypatch.setenv("BROWSERTAP_MODE", "lab")
    monkeypatch.setenv("BROWSERTAP_LAB_NO_ELICIT", "0")
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", None)
    profile = S.get_automation_profile()
    assert profile["no_elicit"] is False
    assert profile["physical_approval"] == "once_per_session"
    assert profile["site_permission_approval"] == "once_per_session"


def test_set_automation_profile_is_process_local_and_validated(monkeypatch):
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", None)
    assert S.set_automation_profile("safe")["mode"] == "safe"
    assert S.set_automation_profile("lab")["mode"] == "lab"
    with pytest.raises(ValueError, match="lab.*safe"):
        S.set_automation_profile("fast")


@pytest.mark.anyio
async def test_lab_no_elicit_skips_physical_prompt_but_keeps_physical_gate(monkeypatch):
    """no_elicit removes the prompt, not the arbitration gate.

    resolve_leave_dialog's Enter fallback is the only physical caller left, so
    it is what drives this: the elicitation must not fire, and
    `run_physical_action` must still be entered exactly once.
    """
    monkeypatch.setenv("BROWSERTAP_MODE", "lab")
    monkeypatch.setenv("BROWSERTAP_LAB_NO_ELICIT", "1")
    monkeypatch.setattr(S, "_AUTOMATION_MODE_OVERRIDE", None)
    gate_calls = []
    pressed = []

    class Context:
        async def elicit(self, *args, **kwargs):
            pytest.fail("lab no_elicit must not prompt")

    async def run_sync(worker, *args, **kwargs):
        return worker()

    monkeypatch.setattr(S.anyio.to_thread, "run_sync", run_sync)
    monkeypatch.setattr(
        S,
        "switch_session",
        lambda session_id=None: session_id or "chrome_test:1",
    )
    # Protocol handling fails without being a transport timeout, which is the
    # only path that reaches the fallback at all.
    monkeypatch.setattr(
        S,
        "handle_dialog",
        lambda *args, **kwargs: {
            "status": "dialog_handle_failed",
            "handled": False,
            "error": "the dialog did not close",
        },
    )
    monkeypatch.setattr(
        S.physical_input,
        "run_physical_action",
        lambda summary, action: gate_calls.append(summary) or action(),
    )
    monkeypatch.setattr(
        S,
        "_pyautogui",
        lambda: SimpleNamespace(hotkey=lambda *args: pressed.append(args)),
    )
    monkeypatch.setattr(
        S,
        "_activate",
        lambda session_id=None: {
            "activated": True,
            "activated_session_id": session_id or "chrome_test:1",
            "on_screen": True,
        },
    )
    monkeypatch.setattr(S.time, "sleep", lambda _seconds: None)

    result = await S.resolve_leave_dialog(ctx=Context(), session_id="chrome_test:1")

    assert result["status"] == "ok"
    assert result["resolution"] == "physical_fallback"
    assert len(gate_calls) == 1
    assert pressed == [("enter",)]


def test_storage_timeout_is_structured_and_default_is_30_seconds(monkeypatch):
    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 30.0
        raise TimeoutError("bridge took too long")

    monkeypatch.setattr(S, "exec_js", fail)
    result = S.storage_get()
    assert result["status"] == "error"
    assert result["error_code"] == "timeout"
    assert result["retryable"] is True


def test_storage_no_response_is_structured_and_followup_list_tabs_still_works(
    monkeypatch,
):
    driver = _Driver()
    _install(monkeypatch, driver)

    def fail(*args, **kwargs):
        raise S.BridgeNoResponseError(
            "script was delivered but no result",
            delivery_state="delivered_no_result",
            retry_safe=False,
        )

    monkeypatch.setattr(S, "exec_js", fail)

    result = S.storage_get(timeout=0.01, session_id="chrome:profile:7")
    tabs = S.list_tabs()

    assert result["status"] == "error"
    assert result["error_code"] == "no_response"
    assert result["delivery_state"] == "delivered_no_result"
    assert result["retry_safe"] is True
    assert result["error_code"] == "no_response"
    assert result["retryable"] is True
    assert tabs["tabs"][0]["id"] == "chrome:profile:7"


@pytest.mark.anyio
async def test_execute_timeout_does_not_close_fastmcp_before_followup_list_tabs(
    monkeypatch,
):
    driver = _Driver([TimeoutError("policy transport timed out")])
    _install(monkeypatch, driver)

    # The fake driver raises immediately; host scheduling is not the timeout under test.
    result = await S.mcp.call_tool(
        "execute_js",
        {
            "script": "return new Promise(() => {})",
            "session_id": "chrome:profile:7",
            "timeout": 5,
        },
    )
    structured = result.structuredContent

    assert result.isError is True
    assert structured["ok"] is False
    assert structured["result_contract"] == "btap.result.v1"
    assert structured["version"] == 1
    assert structured["error_code"] == "timeout"
    assert "policy setup" in structured["error"]["message"]
    assert structured["target"]["session_id"] == "chrome:profile:7"

    _, structured = await S.mcp.call_tool("list_tabs", {})
    assert structured["data"]["tabs"][0]["id"] == "chrome:profile:7"


def test_storage_get_dump_has_item_and_byte_bounds(monkeypatch):
    seen = {}

    def execute(script, session_id=None, timeout=30.0):
        seen["script"] = script
        return {
            "data": json.dumps(
                {
                    "ok": True,
                    "items": {"a": "1"},
                    "total_keys": 9,
                    "next_offset": 1,
                    "bytes": 2,
                    "truncated": True,
                }
            )
        }

    monkeypatch.setattr(S, "exec_js", execute)
    result = S.storage_get(offset=0, max_items=1, max_bytes=64)
    assert result["status"] == "success"
    assert result["truncated"] is True
    assert result["next_offset"] == 1
    assert "maxItems = 1" in seen["script"]
    assert "maxBytes = 64" in seen["script"]


def test_lab_host_navigation_auto_accepts_but_intent_false_preserves_dismiss(
    monkeypatch,
):
    monkeypatch.setenv("BROWSERTAP_MODE", "lab")
    monkeypatch.setenv("BROWSERTAP_AUTO_BEFOREUNLOAD_HOSTS", "shell.,ttyd")
    driver = _Driver(
        [
            {"data": {"status": "ok", "url": "https://new.test/"}},
            {
                "data": {
                    "status": "blocked_by_beforeunload",
                    "url": "https://shell.example/",
                }
            },
        ]
    )
    _install(monkeypatch, driver, _sessions(url="https://shell.example/terminal"))

    automatic = S.open_url("https://new.test/", session_id="chrome:profile:7")
    staying = S.open_url(
        "https://new.test/", session_id="chrome:profile:7", intent_leave=False
    )

    assert driver.calls[0][0]["beforeunload"] == "accept"
    assert automatic["beforeunload_auto"] is True
    assert driver.calls[1][0]["beforeunload"] == "dismiss"
    assert staying["status"] == "blocked_by_beforeunload"
    assert "hint" in staying


def test_open_url_timeout_does_not_repeat_an_unknown_navigation(monkeypatch):
    driver = _Driver([TimeoutError("navigate result timed out")])
    _install(monkeypatch, driver)

    with pytest.raises(TimeoutError, match="navigate result timed out"):
        S.open_url(
            "https://side-effect.test/",
            session_id="chrome:profile:7",
            timeout=0.05,
        )

    assert len(driver.calls) == 1
    assert driver.calls[0][0]["cmd"] == "navigate"


def test_open_url_unknown_command_fallback_uses_one_total_deadline(monkeypatch):
    # Fake clock: the driver spends the budget without a real sleep. Three real
    # 0.03s sleeves plus dispatch overhead do fit inside 0.2s on this machine and
    # did not on a shared CI runner, where the third call found the deadline gone
    # and the result came back navigation_timeout instead of redirected. What the
    # test pins is how the budget is *split*, which the fake clock preserves
    # exactly. `S.time` is the time module itself, so this patch is process-wide.
    clock = [100.0]

    class DeadlineDriver:
        default_session_id = "chrome:profile:7"

        def __init__(self):
            self.calls = []

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            clock[0] += 0.03
            if payload.get("cmd") == "navigate":
                raise RuntimeError("Unknown cmd: navigate")
            if payload.get("method") == "Runtime.evaluate":
                return {
                    "data": {
                        "result": {
                            "value": {
                                "url": "https://landed.test/final",
                                "title": "Landed title",
                            }
                        }
                    }
                }
            return {"data": {"frameId": "frame-7"}}

    driver = DeadlineDriver()
    _install(monkeypatch, driver)
    monkeypatch.setattr(S.time, "monotonic", lambda: clock[0])

    result = S.open_url(
        "https://new.test/", session_id="chrome:profile:7", timeout=0.2
    )

    assert result["navigation_mode"] == "cdp_fallback"
    assert result["status"] == "redirected"
    assert result["url"] == "https://landed.test/final"
    assert result["title"] == "Landed title"
    assert len(driver.calls) == 3
    assert driver.calls[1][2] < driver.calls[0][2]
    assert driver.calls[1][0]["timeoutMs"] < driver.calls[0][0]["timeoutMs"]
    assert driver.calls[2][2] < driver.calls[1][2]


@pytest.mark.anyio
async def test_resolve_leave_dialog_no_dialog_returns_without_physical_fallback(
    monkeypatch,
):
    monkeypatch.setattr(
        S,
        "switch_session",
        lambda session_id=None: session_id or "chrome:profile:7",
    )
    calls = []
    monkeypatch.setattr(
        S,
        "handle_dialog",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "status": "no_dialog",
            "handled": False,
            "url": "https://example.test/",
        },
    )

    async def unexpected_physical(*args, **kwargs):
        pytest.fail("no_dialog must not send a physical Enter fallback")

    monkeypatch.setattr(S, "_run_approved_physical_action", unexpected_physical)

    started = time.monotonic()
    result = await S.resolve_leave_dialog(
        ctx=SimpleNamespace(), session_id="chrome:profile:7"
    )

    # Loose on purpose: the failure mode this guards is a physical-Enter
    # fallback or a re-armed 15s default, both an order of magnitude away. A
    # 0.2s ceiling here tripped on scheduler noise under coverage.
    assert time.monotonic() - started < 1.0
    assert result["status"] == "no_dialog"
    assert result["resolution"] == "none"
    assert result["url"] == "https://example.test/"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_resolve_leave_dialog_normalizes_remote_json_no_dialog_error(
    monkeypatch,
):
    monkeypatch.setattr(
        S,
        "switch_session",
        lambda session_id=None: session_id or "chrome:profile:7",
    )
    calls = []
    monkeypatch.setattr(
        S,
        "handle_dialog",
        lambda *args, **kwargs: calls.append(1) or {
            "status": "error",
            "error": '{"code":-32602,"message":"No dialog is showing"}',
        },
    )

    async def unexpected_physical(*args, **kwargs):
        pytest.fail("remote no_dialog must not send a physical Enter fallback")

    monkeypatch.setattr(S, "_run_approved_physical_action", unexpected_physical)
    result = await S.resolve_leave_dialog(
        ctx=SimpleNamespace(), session_id="chrome:profile:7"
    )

    assert result["status"] == "no_dialog"
    assert result["resolution"] == "none"
    assert calls == [1]


@pytest.mark.anyio
async def test_resolve_leave_dialog_timeout_does_not_send_physical_fallback(
    monkeypatch,
):
    monkeypatch.setattr(
        S,
        "switch_session",
        lambda session_id=None: session_id or "chrome:profile:7",
    )
    monkeypatch.setattr(
        S,
        "handle_dialog",
        lambda *args, **kwargs: {
            "status": "error",
            "error": "extension did not respond within 3s",
        },
    )

    async def unexpected_physical(*args, **kwargs):
        pytest.fail("transport timeout must not send a physical Enter fallback")

    monkeypatch.setattr(S, "_run_approved_physical_action", unexpected_physical)

    result = await S.resolve_leave_dialog(
        ctx=SimpleNamespace(), session_id="chrome:profile:7"
    )

    assert result["status"] == "no_response"
    assert result["retryable"] is True


@pytest.mark.parametrize(
    "message",
    [
        "No dialog is showing",
        "No JavaScript dialog open",
        '{"code":-32602,"message":"No dialog is showing"}',
    ],
)
def test_handle_dialog_normalizes_native_no_dialog_error(monkeypatch, message):
    driver = _Driver([{"data": {"ok": False, "error": message}}])
    _install(monkeypatch, driver)

    result = S.handle_dialog(
        "accept", session_id="chrome:profile:7", timeout=0.5
    )

    assert result["status"] == "no_dialog"
    assert result["handled"] is False
    assert result["dialog"] is None




def test_open_url_session_resolution_uses_one_bounded_snapshot(monkeypatch):
    # Fake clock so the snapshot provably consumes part of the budget. The final
    # assertion is that the driver gets *less* than the snapshot was given, and a
    # real 10ms sleep does not always show up as movement: Windows' timer
    # granularity is 15.6ms, so both sides read the same float and a strict `<`
    # failed. Relaxing it to `<=` would have made the test pass while no longer
    # asserting anything.
    clock = [100.0]
    driver = _Driver([{"data": {"status": "ok", "url": "https://new.test/"}}])
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)
    monkeypatch.setattr(S.time, "monotonic", lambda: clock[0])
    calls = []

    def sessions(timeout=None, fresh=False):
        calls.append((timeout, fresh))
        clock[0] += 0.01
        return _sessions(url="https://shell.example/terminal")

    monkeypatch.setattr(S, "active_sessions", sessions)
    monkeypatch.setenv("BROWSERTAP_MODE", "lab")
    monkeypatch.setenv("BROWSERTAP_AUTO_BEFOREUNLOAD_HOSTS", "shell.")

    result = S.open_url(
        "https://new.test/", session_id="chrome:profile:7", timeout=0.08
    )

    assert result["status"] == "ok"
    assert len(calls) == 1
    assert calls[0][1] is True
    # +1e-9 for float noise: the budget is (monotonic() + timeout) - monotonic(),
    # whose double round-trip lands just over the nominal timeout as often as just
    # under, depending on the clock's magnitude. An overrun that matters is orders
    # of magnitude larger.
    assert 0 < calls[0][0] <= 0.08 + 1e-9
    assert driver.calls[0][2] < calls[0][0]
    assert driver.calls[0][0]["beforeunload"] == "accept"


def test_open_url_does_not_dispatch_after_session_resolution_exhausts_deadline(
    monkeypatch,
):
    driver = _Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)

    def sessions(timeout=None, fresh=False):
        assert 0 < timeout <= 0.01 + 1e-9   # float noise, not an overrun
        assert fresh is True
        time.sleep(0.02)
        return _sessions()

    monkeypatch.setattr(S, "active_sessions", sessions)

    with pytest.raises(TimeoutError, match="during session resolution"):
        S.open_url(
            "https://new.test/", session_id="chrome:profile:7", timeout=0.01
        )

    assert driver.calls == []


def test_execute_js_unknown_policy_command_falls_back_to_direct_cdp(monkeypatch):
    driver = _Driver(
        [
            RuntimeError("Unknown cmd: set_dialog_policy"),
            {
                "data": {
                    "ok": True,
                    "data": {"result": {"value": {"ok": True, "data": 2}}},
                }
            },
        ]
    )
    _install(monkeypatch, driver)
    monkeypatch.setattr(
        S.simphtml,
        "execute_js_rich",
        lambda *args, **kwargs: pytest.fail("page route must not run after fallback"),
    )

    result = S.execute_js(
        "return 1 + 1", session_id="chrome:profile:7", no_monitor=True
    )
    assert result["status"] == "success"
    assert result["js_return"] == 2
    assert result["execution_mode"] == "cdp_fallback"
    assert driver.calls[1][0]["method"] == "Runtime.evaluate"


def test_execute_js_policy_timeout_never_dispatches_fallback_script(monkeypatch):
    driver = _Driver([TimeoutError("policy transport timed out")])
    _install(monkeypatch, driver)
    monkeypatch.setattr(
        S.simphtml,
        "execute_js_rich",
        lambda *args, **kwargs: pytest.fail("user script must not run after policy timeout"),
    )

    with pytest.raises(TimeoutError, match="policy setup"):
        S.execute_js(
            "window.sideEffect = true",
            session_id="chrome:profile:7",
            timeout=0.05,
        )

    assert len(driver.calls) == 1
    assert driver.calls[0][0]["cmd"] == "set_dialog_policy"


def test_direct_cdp_fallback_uses_only_remaining_deadline(monkeypatch):
    # Fake clock, same reason as the fallback test above: a real 0.03s sleep left
    # so little of the 0.08s deadline that a loaded runner exhausted it and the
    # page fallback raised instead of being handed the remainder. The assertions
    # read the split, not the elapsed time.
    clock = [100.0]

    class DeadlineDriver:
        default_session_id = "chrome:profile:7"

        def __init__(self):
            self.timeouts = []

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.timeouts.append(("ext", timeout, payload["timeoutMs"]))
            clock[0] += 0.03
            raise RuntimeError("Unknown cmd: cdp")

        def execute_js(self, payload, timeout=15.0, session_id=None):
            self.timeouts.append(("page", timeout, json.loads(payload)["timeoutMs"]))
            return {"data": {"result": {"value": 7}}}

    driver = DeadlineDriver()
    _install(monkeypatch, driver)
    monkeypatch.setattr(S.time, "monotonic", lambda: clock[0])
    deadline = time.monotonic() + 0.08

    result = S._direct_cdp(
        "Runtime.evaluate",
        {"expression": "7"},
        session_id="chrome:profile:7",
        client_id="chrome:profile",
        tab_id=7,
        timeout=0.08,
        deadline=deadline,
    )

    assert result == {"result": {"value": 7}}
    assert driver.timeouts[1][1] < driver.timeouts[0][1]
    assert driver.timeouts[1][2] < driver.timeouts[0][2]


def test_cdp_command_accepts_full_session_or_numeric_tab(monkeypatch):
    driver = _Driver(
        [
            {"data": {"ok": True, "data": {"value": 1}}},
            {"data": {"ok": True, "data": {"value": 2}}},
        ]
    )
    _install(monkeypatch, driver)

    first = S.cdp_command("Runtime.evaluate", session_id="chrome:profile:7")
    second = S.cdp_command("Runtime.evaluate", tab_id=7)
    assert first["data"]["value"] == 1
    assert second["data"]["value"] == 2
    assert driver.calls[0][0]["tabId"] == 7
    assert driver.calls[0][1] == "chrome:profile"
    assert driver.calls[1][0]["tabId"] == 7


def test_cdp_and_close_tabs_route_explicit_composite_across_default_browser(
    monkeypatch,
):
    driver = _Driver(
        [
            {"data": {"result": {"value": 7}}},
            {"data": {"ok": True}},
        ],
        default="edge:profile:3",
    )
    _fresh_tab_ownership(monkeypatch)
    sessions = [
        {"id": "edge:profile:3", "url": "https://edge.test/", "browser": "edge"},
        {"id": "chrome:profile:7", "url": "https://chrome.test/", "browser": "chrome"},
        {"id": "chrome:profile:8", "url": "https://close.test/", "browser": "chrome"},
    ]
    _install(monkeypatch, driver, sessions)

    cdp = S.cdp_command("Runtime.evaluate", tab_id="chrome:profile:7")
    closed = S.close_tabs("chrome:profile:8", only_if_agent_owned=False)

    assert cdp["data"] == {"result": {"value": 7}}
    assert cdp["session_id"] == "chrome:profile:7"
    assert driver.calls[0][1] == "chrome:profile"
    assert closed["closed"] == 8
    assert driver.calls[1][1] == "chrome:profile"
    assert driver.default_session_id == "edge:profile:3"


def test_close_tabs_accepts_composite_identifiers(monkeypatch):
    driver = _Driver([{"data": {"ok": True}}])
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver)
    result = S.close_tabs(
        ["chrome:profile:7", 8], only_if_agent_owned=False
    )
    assert result["closed"] == [7, 8]
    assert driver.calls[0][0]["tabId"] == [7, 8]
    assert driver.calls[0][1] == "chrome:profile"


def test_open_new_tab_returns_ready_session_without_caller_polling(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            self.calls.append((url, client_id, timeout, active, operation_id))
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-ready",
                    "load_ready": True,
                    "status": "complete",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver()
    _fresh_tab_ownership(monkeypatch)
    fresh_calls = []

    def sessions(timeout=None, fresh=False):
        fresh_calls.append(fresh)
        return _sessions() + (
            [{"id": "chrome:profile:9", "url": "https://new.test/",
              "generation": "generation-ready"}]
            if fresh
            else []
        )

    _install(monkeypatch, driver)
    monkeypatch.setattr(S, "active_sessions", sessions)
    result = S.open_new_tab("https://new.test/", timeout=2.0)
    assert result["tab_id"] == 9
    assert result["session_id"] == "chrome:profile:9"
    assert result["ready"] is True
    assert result["owned"] is True
    assert result["owner_id"]
    assert True in fresh_calls
    create = next(call for call in driver.calls if len(call) == 5)
    assert create[3] is False


def test_open_new_tab_ready_session_executes_immediately_without_listing(monkeypatch):
    class NewTabThenExecDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-ready",
                    "status": "loading",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            if payload.get("method") == "create_status":
                return super().ext_cmd(payload, client_id=client_id, timeout=timeout)
            self.calls.append((payload, client_id, timeout))
            if payload.get("cmd") == "set_dialog_policy":
                return {"data": {"token": "scope-ready"}}
            return {"data": {"ok": True}}

    driver = NewTabThenExecDriver()
    _fresh_tab_ownership(monkeypatch)
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://example.com/",
            "browser": "chrome",
            "generation": "generation-ready",
        }
    ]
    _install(monkeypatch, driver, sessions)
    executed = []

    def execute(script, driver_arg, **kwargs):
        executed.append((script, kwargs["session_id"]))
        return {"status": "success", "js_return": 2, "tab_id": 9}

    monkeypatch.setattr(S.simphtml, "execute_js_rich", execute)

    created = S.open_new_tab("https://example.com/", active=False, timeout=1.0)
    immediate = S.execute_js(
        "1+1", session_id=created["session_id"], no_monitor=True, timeout=1.0
    )

    assert created["ready"] is True
    assert created["generation"] == "generation-ready"
    assert created["owned"] is True
    assert created["owner_id"]
    assert immediate["js_return"] == 2
    assert executed == [("/*__btap_dialog_scope:scope-ready*/\n/*__btap_js*/\n1+1", created["session_id"])]


def test_open_new_tab_reconciles_lost_create_ack_to_exact_session(monkeypatch):
    class LostAckDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            self.calls.append(("create", url, client_id, timeout, active, operation_id))
            raise TimeoutError("create ACK lost")

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            assert payload["method"] == "create_status"
            assert payload["operation_id"]
            status_calls = sum(
                1 for call in self.calls
                if isinstance(call[0], dict) and call[0].get("method") == "create_status"
            )
            if status_calls == 1:
                return {"data": {"status": "not_found", "operation_status": "not_found",
                                  "operation_id": payload["operation_id"]},
                        "client_id": "chrome:profile"}
            return {"data": {"status": "loading", "operation_status": "completed",
                              "operation_id": payload["operation_id"], "id": 42,
                              "generation": "generation-exact", "client_id": "chrome:profile",
                              "url": "https://new.test/", "load_ready": False},
                    "client_id": "chrome:profile"}

    driver = LostAckDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [{"id": "chrome:profile:42", "url": "https://new.test/",
                                    "browser": "chrome", "generation": "generation-exact"}])

    result = S.open_new_tab("https://new.test/", timeout=1.0)

    assert result["ready"] is True
    assert result["session_id"] == "chrome:profile:42"
    assert result["generation"] == "generation-exact"
    assert result["operation_id"]
    create = next(call for call in driver.calls if call[0] == "create")
    assert create[-1] == result["operation_id"]


def test_open_new_tab_structured_probe_unknown_is_safe_before_create(monkeypatch):
    driver = _Driver(default=None)
    _fresh_tab_ownership(monkeypatch)
    calls = []

    def ext_cmd(payload, client_id=None, timeout=15):
        calls.append((payload, client_id))
        assert payload["method"] == "create_status"
        return {
            "data": {
                "operation_id": payload["operation_id"],
                "operation_status": "unknown",
                "status": "unknown",
                "may_have_created": True,
                "retry_safe": False,
            },
            "client_id": "chrome:profile",
        }

    driver.ext_cmd = ext_cmd
    driver.newtab = lambda *args, **kwargs: pytest.fail("probe uncertainty must not dispatch create")
    _install(monkeypatch, driver)
    result = S.open_new_tab("https://probe-unknown.test/", timeout=1.0)
    assert result["status"] == "unknown"
    assert result["may_have_created"] is False
    assert result["retry_safe"] is True
    assert len(calls) == 1


def test_open_new_tab_retries_same_operation_only_when_status_is_not_found(monkeypatch):
    class RetryDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            self.calls.append(("create", url, client_id, timeout, active, operation_id))
            if sum(1 for call in self.calls if call[0] == "create") == 1:
                raise TimeoutError("first command was not delivered")
            return {"data": {"id": 43, "generation": "generation-retried", "url": url,
                              "status": "loading", "operation_status": "completed",
                              "operation_id": operation_id, "client_id": client_id,
                              "load_ready": False}, "client_id": client_id}

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            assert payload["operation_id"]
            return {"data": {"status": "not_found", "operation_status": "not_found",
                              "operation_id": payload["operation_id"]},
                    "client_id": "chrome:profile"}

    driver = RetryDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [{"id": "chrome:profile:43", "url": "https://retry.test/",
                                    "browser": "chrome", "generation": "generation-retried"}])

    result = S.open_new_tab("https://retry.test/", timeout=1.0)

    creates = [call for call in driver.calls if call[0] == "create"]
    assert len(creates) == 2
    assert creates[0][-1] == creates[1][-1] == result["operation_id"]
    assert result["session_id"] == "chrome:profile:43"


def test_open_new_tab_retries_status_timeout_without_replaying_create(monkeypatch):
    class SlowStatusDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            self.calls.append(("create", operation_id))
            return {"data": {"status": "pending", "operation_status": "pending",
                              "operation_id": operation_id, "may_have_created": True,
                              "retry_safe": False}, "client_id": client_id}

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append(("status", client_id, timeout, payload["operation_id"]))
            status_calls = sum(1 for call in self.calls if call[0] == "status")
            if status_calls == 1:
                return {"data": {"status": "not_found", "operation_status": "not_found",
                                  "operation_id": payload["operation_id"]},
                        "client_id": "chrome:profile"}
            if status_calls == 2:
                raise TimeoutError("bridge HTTP request timed out after 1s")
            return {"data": {"status": "loading", "operation_status": "completed",
                              "operation_id": payload["operation_id"], "id": 43,
                              "generation": "generation-slow-status",
                              "client_id": client_id, "url": "https://slow-status.test/"},
                    "client_id": client_id}

    driver = SlowStatusDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [{"id": "chrome:profile:43",
                                    "url": "https://slow-status.test/", "browser": "chrome",
                                    "generation": "generation-slow-status"}])

    result = S.open_new_tab("https://slow-status.test/", timeout=1.0)

    assert result["status"] == "ok"
    assert result["session_id"] == "chrome:profile:43"
    assert sum(1 for call in driver.calls if call[0] == "create") == 1
    status_operations = {call[3] for call in driver.calls if call[0] == "status"}
    assert status_operations == {result["operation_id"]}


def test_open_new_tab_reconciliation_never_claims_same_url_from_other_agent(monkeypatch):
    class LostAckDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            self.calls.append(("create", url, client_id, timeout, active, operation_id))
            raise TimeoutError("create ACK lost")

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            status_calls = sum(
                1 for call in self.calls
                if isinstance(call[0], dict) and call[0].get("method") == "create_status"
            )
            state = "not_found" if status_calls == 1 else "pending"
            return {"data": {"status": state, "operation_status": state,
                              "operation_id": payload["operation_id"],
                              "may_have_created": state == "pending", "retry_safe": state == "not_found"},
                    "client_id": "chrome:profile"}

    driver = LostAckDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [
        {"id": "chrome:profile:7", "url": "https://same.test/", "browser": "chrome",
         "generation": "user-generation"},
    ])

    result = S.open_new_tab("https://same.test/", timeout=0.08)

    assert result["status"] == "unknown"
    assert result["may_have_created"] is True
    assert result["retry_safe"] is False
    assert result["operation_id"]
    assert result["session_id"] is None
    assert result["owned"] is False


def test_open_new_tab_preserves_real_client_id_with_zero_scriptable_sessions(monkeypatch):
    class LostAckDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            assert client_id == "chrome:profile"
            self.calls.append(("create", url, client_id, timeout, active, operation_id))
            raise TimeoutError("create ACK lost")

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            status_calls = sum(
                1 for call in self.calls
                if isinstance(call[0], dict) and call[0].get("method") == "create_status"
            )
            assert client_id is None if status_calls == 1 else client_id == "chrome:profile"
            if status_calls == 1:
                return {"data": {"status": "not_found", "operation_status": "not_found",
                                  "operation_id": payload["operation_id"]},
                        "client_id": "chrome:profile"}
            return {"data": {"status": "loading", "operation_status": "completed",
                              "operation_id": payload["operation_id"], "id": 44,
                              "generation": "generation-restricted", "client_id": "chrome:profile",
                              "url": "chrome://extensions/"}, "client_id": "chrome:profile"}

    driver = LostAckDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [])
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [])

    result = S.open_new_tab("chrome://extensions/", timeout=1.0)

    assert result["session_id"] == "chrome:profile:44"
    assert result["client_id"] == "chrome:profile"
    assert result["operation_id"]
    assert result["owned"] is True


def test_open_new_tab_reconciliation_passes_one_total_deadline(monkeypatch):
    class DeadlineDriver(_Driver):
        def newtab(self, **kwargs):
            self.calls.append(("create", kwargs))
            raise TimeoutError("create ACK lost")

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            assert timeout <= 0.12 + 1e-9   # float noise, not an overrun
            time.sleep(min(timeout, 0.02))
            raise TimeoutError("status ACK lost")

    driver = DeadlineDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [])
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [])

    started = time.monotonic()
    result = S.open_new_tab("https://deadline.test/", timeout=0.12)
    elapsed = time.monotonic() - started

    # `assert timeout <= 0.12` inside the driver above is the contract: every
    # reconciliation call gets a slice of the one budget, never a fresh one.
    # This wall clock is only a backstop against a re-armed 15s default, so it
    # is loose -- a 0.25s ceiling tripped on load under coverage.
    assert elapsed < 1.0
    assert result["status"] == "unknown"
    assert result["may_have_created"] is False
    assert result["retry_safe"] is True
    assert not any(call[0] == "create" for call in driver.calls)


def test_open_new_tab_pins_probe_client_across_default_browser_changes(monkeypatch):
    class MultiClientDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            self.calls.append(("create", client_id, operation_id))
            assert client_id == "chrome:alpha"
            return {"data": {"status": "pending", "operation_status": "pending",
                              "operation_id": operation_id, "may_have_created": True,
                              "retry_safe": False}, "client_id": client_id}

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append(("status", client_id, payload["operation_id"]))
            status_calls = sum(1 for call in self.calls if call[0] == "status")
            if status_calls == 1:
                self.default_session_id = "edge:beta:8"
                return {"data": {"status": "not_found", "operation_status": "not_found",
                                  "operation_id": payload["operation_id"]},
                        "client_id": "chrome:alpha"}
            assert client_id == "chrome:alpha"
            return {"data": {"status": "loading", "operation_status": "completed",
                              "operation_id": payload["operation_id"], "id": 45,
                              "generation": "generation-pinned", "client_id": client_id,
                              "url": "https://pinned.test/"}, "client_id": client_id}

    driver = MultiClientDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [{"id": "chrome:alpha:45",
                                    "url": "https://pinned.test/", "browser": "chrome",
                                    "generation": "generation-pinned"}])

    result = S.open_new_tab("https://pinned.test/", timeout=1.0)

    assert result["client_id"] == "chrome:alpha"
    assert result["session_id"] == "chrome:alpha:45"
    assert [call[1] for call in driver.calls] == [None, "chrome:alpha", "chrome:alpha"]


def test_open_new_tab_pending_ack_reconciles_before_registering_ownership(monkeypatch):
    class PendingDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {"data": {"status": "pending", "operation_status": "pending",
                              "operation_id": operation_id, "id": 46,
                              "generation": "generation-pending", "may_have_created": True,
                              "retry_safe": False}, "client_id": client_id}

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append((payload, client_id, timeout))
            status_calls = len(self.calls)
            if status_calls == 1:
                return {"data": {"status": "not_found", "operation_status": "not_found",
                                  "operation_id": payload["operation_id"]},
                        "client_id": "chrome:profile"}
            assert registry._records == {}
            return {"data": {"status": "loading", "operation_status": "completed",
                              "operation_id": payload["operation_id"], "id": 46,
                              "generation": "generation-pending", "client_id": client_id,
                              "url": "https://pending-ack.test/"}, "client_id": client_id}

    driver = PendingDriver(default=None)
    registry = _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [{"id": "chrome:profile:46",
                                    "url": "https://pending-ack.test/", "browser": "chrome",
                                    "generation": "generation-pending"}])

    result = S.open_new_tab("https://pending-ack.test/", timeout=1.0)

    assert result["ready"] is True
    assert result["owned"] is True
    assert list(registry._records) == ["chrome:profile:46"]


def test_open_new_tab_retry_pending_then_missing_stays_unknown(monkeypatch):
    class RetryPendingDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            attempts = sum(1 for call in self.calls if call[0] == "create") + 1
            self.calls.append(("create", operation_id))
            if attempts == 1:
                raise TimeoutError("first create was not delivered")
            return {"data": {"status": "pending", "operation_status": "pending",
                              "operation_id": operation_id, "may_have_created": True,
                              "retry_safe": False}, "client_id": client_id}

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            self.calls.append(("status", client_id))
            return {"data": {"status": "not_found", "operation_status": "not_found",
                              "operation_id": payload["operation_id"],
                              "may_have_created": False, "retry_safe": True},
                    "client_id": "chrome:profile"}

    driver = RetryPendingDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [])

    result = S.open_new_tab("https://anomalous-registry.test/", timeout=0.08)

    assert result["status"] == "unknown"
    assert result["may_have_created"] is True
    assert result["retry_safe"] is False
    assert result["owned"] is False


def test_open_new_tab_preserves_pre_create_storage_failure_semantics(monkeypatch):
    class StorageFailureDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {"data": {"status": "unknown", "operation_status": "unknown",
                              "operation_id": operation_id, "may_have_created": False,
                              "retry_safe": False, "error": "storage write failed"},
                    "client_id": client_id}

    driver = StorageFailureDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [])

    result = S.open_new_tab("https://storage-fail.test/", timeout=0.1)

    assert result["status"] == "unknown"
    assert result["may_have_created"] is False
    assert result["retry_safe"] is False
    assert result["owned"] is False


def test_open_new_tab_missing_probe_client_never_matches_numeric_suffix(monkeypatch):
    class MissingClientDriver(_Driver):
        def newtab(self, **kwargs):
            pytest.fail("create must not run without a pinned client")

        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            return {"data": {"status": "not_found", "operation_status": "not_found",
                              "operation_id": payload["operation_id"]}}

    driver = MissingClientDriver(default=None)
    registry = _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [{"id": "edge:other:47", "url": "https://same.test/",
                                    "browser": "edge", "generation": "generation-other"}])

    result = S.open_new_tab("https://same.test/", timeout=0.1)

    assert result["status"] == "unknown"
    assert result["session_id"] is None
    assert result["may_have_created"] is False
    assert registry._records == {}


def test_open_new_tab_registers_generation_bound_agent_ownership(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            self.calls.append((url, client_id, timeout, active, operation_id))
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-agent",
                    "load_ready": True,
                    "status": "complete",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver(responses=[{"data": {"closed": [9], "alreadyGone": []}}])
    _fresh_tab_ownership(monkeypatch)
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://new.test/",
            "browser": "chrome",
            "generation": "generation-agent",
        }
    ]
    _install(monkeypatch, driver, sessions)

    created = S.open_new_tab("https://new.test/", timeout=1.0)
    closed = S.close_tabs(
        created["session_id"], owner_id=created["owner_id"]
    )

    assert created["owned"] is True
    assert created["opener"] == "agent"
    assert created["owner_id"]
    assert closed["closed"] == 9
    assert closed["closed_by"] == "agent"
    assert closed["already_gone"] == []
    assert closed["owner_id"] == created["owner_id"]
    assert driver.calls[-1][0]["expectedGenerations"] == {
        "9": "generation-agent"
    }


def test_outstanding_owned_tabs_are_readable_without_exposing_the_capability():
    """The live suite's only honest claim about a browser, and its one hazard.

    A diff of the browser at two moments cannot say who opened a tab, so the
    registry is where "the task leaked one of its own" is answered. That read is
    published -- `artifacts/live-preflight.json` goes into CI artifacts and into
    external reviews -- so the owner_id must not travel with it: it is the
    capability that closes the tab, and one that is never handed out cannot leak.
    """
    registry = S._TabOwnershipRegistry()
    record = registry.register("chrome:profile:9", "generation-agent")

    outstanding = registry.outstanding()

    assert outstanding == [
        {
            "session_id": "chrome:profile:9",
            "tab_id": 9,
            "generation": "generation-agent",
            "opener": "agent",
        }
    ]
    assert "owner_id" not in outstanding[0]
    assert record["owner_id"] not in repr(outstanding)


def test_counters_tell_nothing_leaked_apart_from_nothing_opened():
    """`outstanding() == []` has two meanings and they are not interchangeable.

    One is "every tab was cleaned up", the other is "this process never opened a
    tab, so the check measured nothing". Same shape as `ruff` over a path that
    matches no files: exit 0, empty diagnostics. The counters are what a reader
    compares against, which is why they are lifetime totals and never decrement.
    """
    registry = S._TabOwnershipRegistry()

    assert registry.counters() == {"registered": 0, "released": 0, "outstanding": 0}

    first = registry.register("chrome:profile:9", "generation-agent")
    registry.register("chrome:profile:10", "generation-agent", owner_id=first["owner_id"])
    registry.release(["chrome:profile:9"], owner_id=first["owner_id"])

    assert registry.counters() == {"registered": 2, "released": 1, "outstanding": 1}
    assert [rec["tab_id"] for rec in registry.outstanding()] == [10]

    registry.release(["chrome:profile:10"], owner_id="somebody-else")

    # A release that matched nothing did not settle a debt, so it is not counted.
    assert registry.counters() == {"registered": 2, "released": 1, "outstanding": 1}


def test_a_close_refused_over_a_generation_change_stays_outstanding(monkeypatch):
    """The half of a leak that is not a forgotten close.

    Chrome discarded the task's own background tab and restored it under a new
    id, `close_tabs` correctly refused to close a tab that is no longer the one
    that was claimed, and nothing was closed -- so the debt is still owed. The
    live suite's teardown reports both causes because they need different fixes.
    """
    driver = _Driver()
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://replacement.test/",
            "browser": "chrome",
            "generation": "generation-new",
        }
    ]
    _install(monkeypatch, driver, sessions)
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register("chrome:profile:9", "generation-old", owner_id="agent-owner")

    with pytest.raises(PermissionError, match="lifecycle generation changed"):
        S.close_tabs("chrome:profile:9", owner_id="agent-owner")

    assert [rec["session_id"] for rec in registry.outstanding()] == ["chrome:profile:9"]
    assert registry.counters()["released"] == 0


def test_reading_outstanding_tabs_never_raises_on_a_malformed_key():
    """It runs in teardown, where an exception replaces the suite's own verdict."""
    registry = S._TabOwnershipRegistry()
    registry.register("not-a-composite-id", "generation-agent")

    outstanding = registry.outstanding()

    assert outstanding[0]["session_id"] == "not-a-composite-id"
    assert outstanding[0]["tab_id"] is None


def test_close_tabs_default_refuses_preexisting_user_tab(monkeypatch):
    driver = _Driver()
    sessions = [
        {
            "id": "chrome:profile:7",
            "url": "https://user.test/",
            "browser": "chrome",
            "generation": "generation-user",
        }
    ]
    _install(monkeypatch, driver, sessions)
    _fresh_tab_ownership(monkeypatch)

    with pytest.raises(PermissionError, match="not owned by this MCP task"):
        S.close_tabs("chrome:profile:7", owner_id="not-the-owner")

    assert driver.calls == []


def test_close_tabs_without_owner_refuses_before_snapshot_or_mutation(monkeypatch):
    driver = _Driver()
    _fresh_tab_ownership(monkeypatch)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda *args, **kwargs: pytest.fail(
            "missing owner_id must be rejected before reading shared browser state"
        ),
    )

    with pytest.raises(PermissionError, match="owner_id is required"):
        S.close_tabs("chrome:profile:7")

    assert driver.calls == []


def test_two_agent_owner_capabilities_cannot_close_each_other_tabs(monkeypatch):
    driver = _Driver([{"data": {"closed": [9], "alreadyGone": []}}])
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://agent-a.test/",
            "browser": "chrome",
            "generation": "generation-a",
        }
    ]
    _install(monkeypatch, driver, sessions)
    agent_a = _fresh_tab_ownership(monkeypatch)
    record = agent_a.register(
        "chrome:profile:9", "generation-a", owner_id="agent-a-owner"
    )

    agent_b = S._TabOwnershipRegistry()
    monkeypatch.setattr(S, "_TAB_OWNERSHIP", agent_b)
    with pytest.raises(PermissionError, match="not owned by this MCP task"):
        S.close_tabs("chrome:profile:9", owner_id=record["owner_id"])
    assert driver.calls == []

    monkeypatch.setattr(S, "_TAB_OWNERSHIP", agent_a)
    closed = S.close_tabs("chrome:profile:9", owner_id=record["owner_id"])
    assert closed["closed"] == 9
    assert len(driver.calls) == 1


def test_owned_close_rejects_ambiguous_success_without_releasing_ownership(monkeypatch):
    driver = _Driver([{"data": {"ok": True}}])
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://agent-a.test/",
            "browser": "chrome",
            "generation": "generation-a",
        }
    ]
    _install(monkeypatch, driver, sessions)
    registry = _fresh_tab_ownership(monkeypatch)
    record = registry.register(
        "chrome:profile:9", "generation-a", owner_id="agent-a-owner"
    )

    with pytest.raises(
        RuntimeError,
        match="omitted explicit closed and alreadyGone lists",
    ):
        S.close_tabs("chrome:profile:9", owner_id=record["owner_id"])

    assert registry.outstanding() == [
        {
            "session_id": "chrome:profile:9",
            "tab_id": 9,
            "generation": "generation-a",
            "opener": "agent",
        }
    ]
    assert registry.counters()["released"] == 0
    assert len(driver.calls) == 1


def test_close_tabs_refuses_reused_native_id_with_new_generation(monkeypatch):
    driver = _Driver()
    sessions = [
        {
            "id": "chrome:profile:9",
            "url": "https://replacement.test/",
            "browser": "chrome",
            "generation": "generation-new",
        }
    ]
    _install(monkeypatch, driver, sessions)
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register(
        "chrome:profile:9", "generation-old", owner_id="agent-owner"
    )

    with pytest.raises(PermissionError, match="lifecycle generation changed"):
        S.close_tabs("chrome:profile:9", owner_id="agent-owner")

    assert driver.calls == []


def test_close_tabs_rebinds_owned_target_after_confirmed_tab_replacement(monkeypatch):
    driver = _Driver([{"data": {"closed": [12], "alreadyGone": []}}])
    old_sid = "chrome:profile:9"
    new_sid = "chrome:profile:12"
    sessions = [
        {
            "id": new_sid,
            "url": "https://replacement.test/",
            "browser": "chrome",
            "generation": "generation-agent",
        }
    ]
    _install(monkeypatch, driver, sessions)
    driver.resolve_session_target = lambda sid: (
        {
            "session_id": new_sid,
            "rebound_from": old_sid,
            "replacement_session_id": new_sid,
            "tab_identity": "tab_identity_1",
            "reason": "chrome.tabs.onReplaced",
        }
        if str(sid) == old_sid
        else {"session_id": str(sid)}
    )
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register(old_sid, "generation-agent", owner_id="agent-owner")

    result = S.close_tabs(
        old_sid,
        session_id=old_sid,
        owner_id="agent-owner",
    )

    assert result["closed"] == 12
    assert result["rebound_from"] == old_sid
    assert result["replacement_session_id"] == new_sid
    assert result["tab_identity"] == "tab_identity_1"
    assert driver.calls[0][0]["tabId"] == 12
    assert driver.calls[0][0]["expectedGenerations"] == {
        "12": "generation-agent"
    }
    assert registry.outstanding() == []


def test_close_tabs_treats_owned_session_that_is_already_gone_as_user_closed(monkeypatch):
    driver = _Driver([{"data": {"closed": [], "alreadyGone": [9]}}])
    _install(monkeypatch, driver, [])
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [])
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register(
        "chrome:profile:9", "generation-old", owner_id="agent-owner"
    )

    result = S.close_tabs("chrome:profile:9", owner_id="agent-owner")

    assert result["status"] == "already_gone"
    assert result["closed"] == []
    assert result["already_gone"] == 9
    assert result["closed_by"] == "user"
    assert driver.calls[0][0]["tabId"] == 9
    assert driver.calls[0][0]["expectedGenerations"] == {
        "9": "generation-old"
    }


def test_close_tabs_closes_live_owned_tabs_and_skips_user_closed_owned_tabs(monkeypatch):
    driver = _Driver([{"data": {"closed": [10], "alreadyGone": [9]}}])
    sessions = [
        {
            "id": "chrome:profile:10",
            "url": "https://live-agent.test/",
            "browser": "chrome",
            "generation": "generation-live",
        }
    ]
    _install(monkeypatch, driver, sessions)
    registry = _fresh_tab_ownership(monkeypatch)
    registry.register(
        "chrome:profile:9", "generation-gone", owner_id="agent-owner"
    )
    registry.register(
        "chrome:profile:10", "generation-live", owner_id="agent-owner"
    )

    result = S.close_tabs(
        ["chrome:profile:9", "chrome:profile:10"], owner_id="agent-owner"
    )

    assert result["status"] == "ok"
    assert result["closed"] == [10]
    assert result["already_gone"] == [9]
    assert result["closed_by"] == "agent"
    assert driver.calls[0][0]["tabId"] == [9, 10]
    assert driver.calls[0][0]["expectedGenerations"] == {
        "9": "generation-gone",
        "10": "generation-live"
    }


def test_close_tabs_explicit_operator_override_can_close_unowned_tab(monkeypatch):
    driver = _Driver([{"data": {"ok": True}}])
    _install(monkeypatch, driver)
    _fresh_tab_ownership(monkeypatch)

    result = S.close_tabs("chrome:profile:7", only_if_agent_owned=False)

    assert result["closed"] == 7
    assert result["closed_by"] == "none"
    assert result["only_if_agent_owned"] is False
    assert "expectedGenerations" not in driver.calls[0][0]


def test_open_new_tab_does_not_match_same_numeric_id_from_another_browser(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-new",
                    "load_ready": True,
                    "status": "complete",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    sessions = [
        {"id": "chrome:profile:7", "url": "https://old.test/", "browser": "chrome"},
        {"id": "edge:profile:9", "url": "https://wrong.test/", "browser": "edge"},
    ]
    _install(monkeypatch, driver, sessions)

    result = S.open_new_tab("https://new.test/", timeout=0.11)

    assert result["ready"] is False
    assert result["status"] == "pending"
    assert result["session_id"] == "chrome:profile:9"
    assert result["url"] == "https://new.test/"
    assert result["owned"] is True


def test_open_new_tab_pending_still_returns_owned_cleanup_capability(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-pending",
                    "load_ready": False,
                    "status": "loading",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, _sessions())

    result = S.open_new_tab("chrome://extensions/", timeout=0.01)

    assert result["ready"] is False
    assert result["status"] == "pending"
    assert result["session_id"] == "chrome:profile:9"
    assert result["owned"] is True
    assert result["owner_id"]


def test_open_new_tab_uses_actual_extension_client_with_zero_scriptable_sessions(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            assert client_id == "chrome:profile"
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-zero-session",
                    "load_ready": False,
                    "status": "loading",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver(default=None)
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, [])
    monkeypatch.setattr(S, "active_sessions", lambda timeout=None, fresh=False: [])

    result = S.open_new_tab("https://new.test/", timeout=0.01)

    assert result["status"] == "pending"
    assert result["session_id"] == "chrome:profile:9"
    assert result["owned"] is True
    assert result["owner_id"]


def test_open_new_tab_does_not_make_an_unbounded_tabs_snapshot_after_deadline(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-pending",
                    "load_ready": False,
                    "status": "loading",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    _install(monkeypatch, driver, _sessions())
    monkeypatch.setattr(
        S,
        "compact_tabs",
        lambda *args, **kwargs: pytest.fail("deadline tail must not re-query tabs"),
    )

    result = S.open_new_tab("chrome://extensions/", timeout=0.01)

    assert result["status"] == "pending"
    assert "tabs" not in result


def test_open_new_tab_waits_for_the_returned_tab_generation(monkeypatch):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-new",
                    "load_ready": True,
                    "status": "complete",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    fresh_calls = []

    def sessions(timeout=None, fresh=False):
        fresh_calls.append(fresh)
        generation = "generation-old" if len(fresh_calls) == 1 else "generation-new"
        return [
            {"id": "chrome:profile:7", "url": "https://old.test/", "browser": "chrome"},
            {
                "id": "chrome:profile:9",
                "url": "https://new.test/",
                "browser": "chrome",
                "generation": generation,
            },
        ]

    _install(monkeypatch, driver)
    monkeypatch.setattr(S, "active_sessions", sessions)

    result = S.open_new_tab("https://new.test/", timeout=1.0)

    assert result["ready"] is True
    assert result["generation"] == "generation-new"
    assert len(fresh_calls) >= 2


def test_open_new_tab_exact_session_registration_is_ready_while_page_loads(
    monkeypatch,
):
    class NewTabDriver(_Driver):
        def newtab(self, url=None, client_id=None, timeout=15.0, active=True,
                   operation_id=None):
            return {
                "data": {
                    "id": 9,
                    "url": url,
                    "generation": "generation-loading",
                    "load_ready": False,
                    "status": "loading",
                    "operation_status": "completed",
                    "operation_id": operation_id,
                    "client_id": client_id,
                },
                "client_id": client_id,
            }

    driver = NewTabDriver(default="chrome:profile:7")
    _fresh_tab_ownership(monkeypatch)
    _install(
        monkeypatch,
        driver,
        [
            {
                "id": "chrome:profile:9",
                "url": "https://new.test/",
                "browser": "chrome",
                "generation": "generation-loading",
            }
        ],
    )

    result = S.open_new_tab("https://new.test/", timeout=1.0)

    assert result["ready"] is True
    assert result["status"] == "ok"
    assert result["load_status"] == "loading"
    assert result["owned"] is True
    assert result["owner_id"]


def test_extension_tab_create_ack_does_not_wait_for_page_load():
    function_source = _create_operation_source()
    script = f"""
const stored = {{}};
const chrome = {{
  storage: {{ session: {{
    get: async key => ({{ [key]: stored[key] }}),
    set: async value => Object.assign(stored, value),
  }} }},
  tabs: {{
    create: async () => ({{
      id: 19,
      pendingUrl: 'https://slow.test/',
      url: '',
      title: '',
      windowId: 2,
      status: 'loading',
    }}),
    get: async () => new Promise(() => {{}}),
  }},
}};
async function scheduleNewTabGeneration(tabId) {{ return `generation-${{tabId}}`; }}
async function tabGenerationFor() {{ return null; }}
async function sendTabsUpdate() {{ return new Promise(() => {{}}); }}
{function_source}
Promise.race([
  createTabAck({{operation_id: 'op-slow', url: 'https://slow.test/', active: false}}),
  new Promise((_, reject) => setTimeout(() => reject(new Error('ack waited for load')), 100)),
]).then(result => process.stdout.write(JSON.stringify(result)))
  .catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    result = json.loads(completed.stdout)
    assert result["data"]["id"] == 19
    assert result["data"]["generation"] == "generation-19"
    assert result["data"]["url"] == "https://slow.test/"
    assert result["data"]["status"] == "loading"
    assert result["data"]["operation_status"] == "completed"
    assert result["data"]["load_ready"] is False


def test_extension_tab_create_ack_reuses_generation_after_oncreated_finishes():
    function_source = _create_operation_source()
    script = f"""
const stored = {{}};
let createdActive = null;
const chrome = {{
  storage: {{ session: {{
    get: async key => ({{ [key]: stored[key] }}),
    set: async value => Object.assign(stored, value),
  }} }},
  tabs: {{ create: async opts => {{
    createdActive = opts.active;
    return {{
      id: 19, pendingUrl: 'https://slow.test/', url: '', title: '', windowId: 2,
      status: 'loading',
    }};
  }} }},
}};
const generations = new Map([[19, 'generation-existing']]);
let scheduleCalls = 0;
async function tabGenerationFor(tabId) {{ return generations.get(tabId); }}
async function scheduleNewTabGeneration(tabId) {{
  scheduleCalls += 1;
  generations.set(tabId, `generation-replaced-${{scheduleCalls}}`);
  return generations.get(tabId);
}}
async function sendTabsUpdate() {{}}
{function_source}
createTabAck({{operation_id: 'op-generation', url: 'https://slow.test/'}}).then(result =>
  process.stdout.write(JSON.stringify({{result, scheduleCalls, createdActive}}))
).catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    outcome = json.loads(completed.stdout)
    assert outcome["result"]["data"]["generation"] == "generation-existing"
    assert outcome["scheduleCalls"] == 0
    assert outcome["createdActive"] is False


def test_extension_create_operation_is_persisted_and_concurrent_requests_deduplicate():
    function_source = _create_operation_source()
    script = f"""
const stored = {{}};
let createCalls = 0;
const chrome = {{
  storage: {{ session: {{
    get: async (key) => ({{ [key]: stored[key] }}),
    set: async (value) => Object.assign(stored, value),
  }} }},
  tabs: {{ create: async (opts) => {{
    createCalls += 1;
    await new Promise(resolve => setTimeout(resolve, 10));
    return {{ id: 51, pendingUrl: opts.url, url: '', title: 'same', windowId: 3, status: 'loading' }};
  }} }},
}};
async function getClientId() {{ return 'chrome:profile'; }}
async function tabGenerationFor() {{ return 'generation-51'; }}
async function scheduleNewTabGeneration() {{ return 'generation-51'; }}
async function sendTabsUpdate() {{}}
{function_source}
(async () => {{
  const msg = {{ operation_id: 'op-dedup', url: 'https://dedup.test/', active: false }};
  const [first, second] = await Promise.all([createTabAck(msg), createTabAck(msg)]);
  const third = await createTabAck(msg);
  process.stdout.write(JSON.stringify({{ first, second, third, createCalls, stored }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    assert result["createCalls"] == 1
    assert result["first"]["data"] == result["second"]["data"] == result["third"]["data"]
    assert result["stored"]["btapCreateOperationsV1"]["op-dedup"]["status"] == "completed"


def test_extension_pending_persistence_failure_is_fail_closed():
    function_source = _create_operation_source()
    script = f"""
let createCalls = 0;
const chrome = {{
  storage: {{ session: {{
    get: async () => ({{}}),
    set: async () => {{ throw new Error('storage write failed'); }},
  }} }},
  tabs: {{ create: async () => {{ createCalls += 1; return {{id: 60}}; }} }},
}};
async function getClientId() {{ return 'chrome:profile'; }}
{function_source}
createTabAck({{operation_id: 'op-write-fail', url: 'https://write-fail.test/'}}).then(result =>
  process.stdout.write(JSON.stringify({{result, createCalls}}))
).catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    assert result["createCalls"] == 0
    assert result["result"]["data"]["operation_status"] == "unknown"
    assert result["result"]["data"]["may_have_created"] is False
    assert result["result"]["data"]["retry_safe"] is False


def test_extension_operation_storage_read_failure_is_unknown_and_does_not_create():
    function_source = _create_operation_source()
    script = f"""
console.log = () => {{}};
let createCalls = 0;
const chrome = {{
  storage: {{ session: {{
    get: async () => {{ throw new Error('storage read failed'); }},
    set: async () => {{}},
  }} }},
  tabs: {{ create: async () => {{ createCalls += 1; return {{id: 61}}; }} }},
}};
{function_source}
(async () => {{
  const status = await createTabStatus({{operation_id: 'op-read-fail'}});
  const create = await createTabAck({{operation_id: 'op-read-fail', url: 'https://read-fail.test/'}});
  process.stdout.write(JSON.stringify({{status, create, createCalls}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    assert result["createCalls"] == 0
    for reply in (result["status"], result["create"]):
        assert reply["data"]["operation_status"] == "unknown"
        assert reply["data"]["retry_safe"] is False


def test_extension_delayed_create_status_and_duplicate_create_never_double_open():
    function_source = _create_operation_source()
    script = f"""
const stored = {{}};
let createCalls = 0;
let releaseCreate;
const createGate = new Promise(resolve => {{ releaseCreate = resolve; }});
const chrome = {{
  storage: {{ session: {{
    get: async key => ({{ [key]: stored[key] }}),
    set: async value => Object.assign(stored, value),
  }} }},
  tabs: {{ create: async opts => {{
    createCalls += 1;
    await createGate;
    return {{id: 62, pendingUrl: opts.url, url: '', title: '', windowId: 4, status: 'loading'}};
  }} }},
}};
async function getClientId() {{ return 'chrome:profile'; }}
async function tabGenerationFor() {{ return 'generation-62'; }}
async function sendTabsUpdate() {{}}
{function_source}
(async () => {{
  const msg = {{operation_id: 'op-interleaved', url: 'https://interleaved.test/'}};
  const firstPromise = createTabAck(msg);
  while (createCalls === 0) await new Promise(resolve => setTimeout(resolve, 0));
  const status = await createTabStatus(msg);
  const secondPromise = createTabAck(msg);
  releaseCreate();
  const [first, second] = await Promise.all([firstPromise, secondPromise]);
  process.stdout.write(JSON.stringify({{status, first, second, createCalls}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    assert result["createCalls"] == 1
    assert result["status"]["data"]["operation_status"] == "pending"
    assert result["first"]["data"] == result["second"]["data"]
    assert result["first"]["data"]["operation_status"] == "completed"


def test_extension_post_create_failure_is_terminal_unknown_and_is_not_retry_safe():
    function_source = _create_operation_source()
    script = f"""
let createCalls = 0;
const stored = {{}};
const chrome = {{
  storage: {{ session: {{
    get: async key => ({{ [key]: stored[key] }}),
    set: async value => Object.assign(stored, value),
  }} }},
  tabs: {{ create: async opts => {{
    createCalls += 1;
    return {{ id: 52, pendingUrl: opts.url, url: '', title: '', windowId: 3, status: 'loading' }};
  }} }},
}};
async function getClientId() {{ return 'chrome:profile'; }}
async function tabGenerationFor() {{ throw new Error('generation unavailable'); }}
async function scheduleNewTabGeneration() {{ throw new Error('generation unavailable'); }}
{function_source}
(async () => {{
  const msg = {{ operation_id: 'op-uncertain', url: 'https://uncertain.test/' }};
  const first = await createTabAck(msg);
  const second = await createTabAck(msg);
  process.stdout.write(JSON.stringify({{ first, second, createCalls, stored }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    assert result["createCalls"] == 1
    assert result["first"]["data"]["operation_status"] == "unknown"
    assert result["first"]["data"]["resume_required"] is False
    assert result["first"]["data"]["may_have_created"] is True
    assert result["first"]["data"]["retry_safe"] is False
    assert result["second"]["data"] == result["first"]["data"]


def test_extension_completion_persistence_failure_preserves_generation():
    function_source = _create_operation_source()
    script = f"""
let createCalls = 0;
let writeCalls = 0;
const stored = {{}};
const chrome = {{
  storage: {{ session: {{
    get: async key => ({{ [key]: stored[key] }}),
    set: async value => {{
      writeCalls += 1;
      if (writeCalls === 2) throw new Error('completion write failed');
      Object.assign(stored, value);
    }},
  }} }},
  tabs: {{ create: async opts => {{
    createCalls += 1;
    return {{ id: 53, pendingUrl: opts.url, url: '', title: '', windowId: 3, status: 'loading' }};
  }} }},
}};
async function getClientId() {{ return 'chrome:profile'; }}
async function tabGenerationFor() {{ return 'generation-53'; }}
async function scheduleNewTabGeneration() {{ throw new Error('generation should already exist'); }}
{function_source}
(async () => {{
  const result = await createTabAck({{
    operation_id: 'op-completion-write-fail', url: 'https://write-fail.test/',
  }});
  process.stdout.write(JSON.stringify({{ result, createCalls, writeCalls, stored }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    operation = result["result"]["data"]
    assert result["createCalls"] == 1
    assert result["writeCalls"] == 3
    assert operation["operation_status"] == "unknown"
    assert operation["resume_required"] is False
    assert operation["generation"] == "generation-53"
    assert operation["may_have_created"] is True
    assert operation["retry_safe"] is False
    assert result["stored"]["btapCreateOperationsV1"][
        "op-completion-write-fail"
    ]["generation"] == "generation-53"


def test_extension_batch_deadline_stops_enter_after_delayed_command():
    batch_source = _batch_source()
    script = f"""
let now = 1000;
Date.now = () => now;
const calls = [];
const attachTimeouts = [];
async function attachBtapDebugger(target, timeoutMs) {{
  attachTimeouts.push(timeoutMs);
  return {{ attachment: {{ target }}, released: false }};
}}
async function detachBtapDebugger(lease) {{ lease.released = true; }}
function boundedCdpTimeout(value, fallback = 20000) {{
  const requested = Number(value);
  return Number.isFinite(requested) && requested > 0 ? requested : fallback;
}}
function debuggerFailureCode(error) {{ return error.code || 'cdp_error'; }}
async function sendDebuggerCommandWithTimeout(
  _lease, method, _params, timeoutMs, minimumMs
) {{
  calls.push({{ method, timeoutMs, minimumMs }});
  if (method === 'Runtime.evaluate') now += 80;
  return {{}};
}}
{batch_source}
(async () => {{
  const result = await handleBatch({{
    tabId: 53,
    deadlineEpochMs: 1050,
    commands: [
      {{ cmd: 'cdp', method: 'Input.insertText', params: {{ text: 'deadline-test' }} }},
      {{ cmd: 'cdp', method: 'Runtime.evaluate', params: {{ expression: 'delay' }} }},
      {{ cmd: 'cdp', method: 'Input.dispatchKeyEvent', params: {{ type: 'keyDown', key: 'Enter' }} }},
      {{ cmd: 'cdp', method: 'Input.dispatchKeyEvent', params: {{ type: 'keyUp', key: 'Enter' }} }},
    ],
  }}, {{}});
  process.stdout.write(JSON.stringify({{ result, calls, attachTimeouts }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["result"]["ok"] is False
    assert outcome["result"]["code"] == "cdp_timeout"
    assert [call["method"] for call in outcome["calls"]] == [
        "Input.insertText",
        "Runtime.evaluate",
    ]
    assert all(call["timeoutMs"] <= 50 for call in outcome["calls"])
    assert all(call["minimumMs"] == 1 for call in outcome["calls"])
    assert outcome["attachTimeouts"] == [49]


def test_extension_batch_retries_bounded_attach_before_sending_input():
    batch_source = _batch_source()
    script = f"""
let now = 1000;
Date.now = () => now;
const attachTimeouts = [];
const commands = [];
async function attachBtapDebugger(target, timeoutMs) {{
  attachTimeouts.push(timeoutMs);
  if (attachTimeouts.length === 1) {{
    now += timeoutMs;
    const error = new Error(`cdp_timeout: debugger attach exceeded ${{timeoutMs}}ms`);
    error.code = 'cdp_timeout';
    throw error;
  }}
  return {{ attachment: {{ target }}, released: false }};
}}
async function detachBtapDebugger(lease) {{ lease.released = true; }}
function boundedCdpTimeout(value, fallback = 20000) {{
  const requested = Number(value);
  return Number.isFinite(requested) && requested > 0 ? requested : fallback;
}}
function debuggerFailureCode(error) {{ return error.code || 'cdp_error'; }}
async function sendDebuggerCommandWithTimeout(
  _lease, method, _params, timeoutMs, minimumMs
) {{
  commands.push({{ method, timeoutMs, minimumMs }});
  return {{}};
}}
{batch_source}
(async () => {{
  const result = await handleBatch({{
    tabId: 54,
    timeoutMs: 15000,
    deadlineEpochMs: 16000,
    commands: [
      {{ cmd: 'cdp', method: 'Input.dispatchMouseEvent', params: {{ type: 'mousePressed' }} }},
    ],
  }}, {{}});
  process.stdout.write(JSON.stringify({{ result, attachTimeouts, commands }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["result"]["ok"] is True
    assert len(outcome["attachTimeouts"]) == 2
    assert 0 < outcome["attachTimeouts"][0] <= 5000
    assert 0 < outcome["attachTimeouts"][1] < 10000
    assert [call["method"] for call in outcome["commands"]] == [
        "Input.dispatchMouseEvent"
    ]


def test_extension_cdp_retryability_tracks_actual_command_dispatch():
    handlers = _cdp_handlers_source()
    script = f"""
const chrome = {{ tabs: {{ query: async () => [] }} }};
function isScriptable() {{ return true; }}
function boundedCdpTimeout(value, fallback = 20000) {{
  const requested = Number(value);
  return Number.isFinite(requested) && requested > 0 ? requested : fallback;
}}
function debuggerFailureCode(error) {{ return error.code || 'cdp_error'; }}
let mode = 'attach-failure';
let sendCalls = 0;
async function attachBtapDebugger(target) {{
  if (mode === 'attach-failure') {{
    const error = new Error('cdp_timeout: attach failed');
    error.code = 'cdp_timeout';
    throw error;
  }}
  return {{ attachment: {{ target }}, released: false }};
}}
async function detachBtapDebugger(lease) {{ lease.released = true; }}
async function sendDebuggerCommandWithTimeout() {{
  sendCalls += 1;
  const error = new Error('cdp_timeout: command outcome unknown');
  error.code = 'cdp_timeout';
  error.dispatched = true;
  throw error;
}}
{handlers}
(async () => {{
  const attachFailure = await handleCDP({{
    tabId: 7, method: 'Runtime.evaluate', params: {{ expression: '1' }},
  }}, {{}});
  mode = 'command-failure';
  const commandFailure = await handleCDP({{
    tabId: 7, method: 'Runtime.evaluate', params: {{ expression: 'sideEffect()' }},
  }}, {{}});
  process.stdout.write(JSON.stringify({{ attachFailure, commandFailure, sendCalls }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["attachFailure"]["dispatched"] is False
    assert outcome["attachFailure"]["commands_dispatched"] == 0
    assert outcome["attachFailure"]["retryable"] is True
    assert outcome["commandFailure"]["dispatched"] is True
    assert outcome["commandFailure"]["commands_dispatched"] == 1
    assert outcome["commandFailure"]["retryable"] is False
    assert outcome["sendCalls"] == 1


def test_extension_batch_error_reports_partial_progress_without_inviting_replay():
    handlers = _cdp_handlers_source()
    script = f"""
const chrome = {{ tabs: {{ query: async () => [] }} }};
function isScriptable() {{ return true; }}
function boundedCdpTimeout(value, fallback = 20000) {{
  const requested = Number(value);
  return Number.isFinite(requested) && requested > 0 ? requested : fallback;
}}
function debuggerFailureCode(error) {{ return error.code || 'cdp_error'; }}
async function attachBtapDebugger(target) {{
  return {{ attachment: {{ target }}, released: false }};
}}
async function detachBtapDebugger(lease) {{ lease.released = true; }}
let sendCalls = 0;
async function sendDebuggerCommandWithTimeout() {{
  sendCalls += 1;
  if (sendCalls === 1) return {{ value: 'first-completed' }};
  const error = new Error('debugger_detached: second command outcome unknown');
  error.code = 'debugger_detached';
  error.dispatched = true;
  throw error;
}}
{handlers}
(async () => {{
  const result = await handleBatch({{
    tabId: 8,
    commands: [
      {{ cmd: 'cdp', method: 'Runtime.evaluate', params: {{ expression: 'first()' }} }},
      {{ cmd: 'cdp', method: 'Runtime.evaluate', params: {{ expression: 'second()' }} }},
    ],
  }}, {{}});
  process.stdout.write(JSON.stringify({{ result, sendCalls }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    result = outcome["result"]
    assert result["ok"] is False
    assert result["results"] == [{"value": "first-completed"}]
    assert result["commands_completed"] == 1
    assert result["commands_dispatched"] == 2
    assert result["dispatched"] is True
    assert result["retryable"] is False
    assert outcome["sendCalls"] == 2


@pytest.mark.parametrize("waiting_state", ["recovery", "detaching"])
def test_extension_batch_attach_wait_obeys_original_deadline_without_input(
    waiting_state,
):
    debugger_setup = """
const waiting = {
  then(resolve) {
    now += 80;
    debuggerRecoveryPromises.delete('tab:55');
    resolve();
  },
};
debuggerRecoveryPromises.set('tab:55', waiting);
"""
    if waiting_state == "detaching":
        debugger_setup = """
const stale = {
  key: 'tab:55',
  target: { tabId: 55 },
  aliases: new Set(['tab:55']),
  refs: 0,
  attached: false,
  invalidated: false,
  invalidationReason: null,
  generation: 'stale-generation',
  detachingPromise: null,
  attachPromise: Promise.resolve(),
  attachAbortPromise: new Promise(() => {}),
  rejectAttach: null,
  attachSettled: false,
  attachTimedOut: false,
  attachTimeoutError: null,
  recoveryPromise: null,
  pendingCommands: new Set(),
};
const waiting = {
  then(resolve) {
    now += 80;
    stale.detachingPromise = null;
    debuggerAttachments.delete('tab:55');
    resolve();
  },
};
stale.detachingPromise = waiting;
debuggerAttachments.set('tab:55', stale);
"""

    script = """
const fs = require('fs');
const source = fs.readFileSync(__BACKGROUND__, 'utf8');
const debuggerStart = source.indexOf('function debuggerTargetKey');
const debuggerEnd = source.indexOf('\\nasync function handleProtocolDialog', debuggerStart);
const batchStart = source.indexOf('function batchDeadlineRemainingMs');
const batchEnd = source.indexOf('\\n\\nasync function handleCDP', batchStart);
if (debuggerStart < 0 || debuggerEnd < 0 || batchStart < 0 || batchEnd < 0) {
  throw new Error('debugger/batch helpers not found');
}
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let now = 1000;
Date.now = () => now;
let nextTimerId = 0;
const timers = [];
function setTimeout(callback, delay) {
  const timer = { id: ++nextTimerId, callback, delay, cleared: false };
  timers.push(timer);
  return timer;
}
function clearTimeout(timer) { if (timer) timer.cleared = true; }
let attachCalls = 0;
const sent = [];
const chrome = { debugger: {
  attach() { attachCalls += 1; return new Promise(() => {}); },
  detach() { return Promise.resolve(); },
  sendCommand(_target, method) { sent.push(method); return Promise.resolve({}); },
} };
function isScriptable() { return true; }
eval(source.slice(debuggerStart, debuggerEnd));
eval(source.slice(batchStart, batchEnd));
__WAIT_SETUP__
(async () => {
  const resultPromise = handleBatch({
    tabId: 55,
    deadlineEpochMs: 1100,
    commands: [{
      cmd: 'cdp', method: 'Input.dispatchMouseEvent',
      params: { type: 'mousePressed', x: 1, y: 1 },
    }],
  }, {});
  for (let i = 0; i < 50; i += 1) {
    await Promise.resolve();
    if (attachCalls === 1 && timers.some(timer => !timer.cleared)) break;
  }
  const activeTimers = timers.filter(timer => !timer.cleared);
  if (attachCalls !== 1 || activeTimers.length !== 1) {
    throw new Error(`expected one pending attach watchdog, got ${activeTimers.length}`);
  }
  const watchdog = activeTimers[0];
  now += watchdog.delay;
  watchdog.callback();
  const result = await resultPromise;
  process.stdout.write(JSON.stringify({
    result,
    attachCalls,
    sent,
    now,
    watchdogDelay: watchdog.delay,
    trackedAttachments: debuggerAttachments.size,
    trackedRecoveries: debuggerRecoveryPromises.size,
  }));
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__BACKGROUND__", json.dumps(str(BACKGROUND))).replace(
        "__WAIT_SETUP__", debugger_setup
    )
    outcome = _run_node_script(script)

    assert outcome["result"]["ok"] is False
    assert outcome["result"]["code"] == "cdp_timeout"
    assert outcome["result"]["results"] == []
    assert outcome["attachCalls"] == 1
    assert outcome["sent"] == []
    assert outcome["now"] <= 1100
    assert 0 < outcome["watchdogDelay"] < 20
    assert outcome["trackedAttachments"] == 0
    assert outcome["trackedRecoveries"] == 0


def test_browser_bridge_newtab_forwards_operation_and_client_id(monkeypatch):
    driver = BrowserBridge.__new__(BrowserBridge)
    seen = {}

    def ext_cmd(payload, client_id=None, timeout=15):
        seen.update(payload=payload, client_id=client_id, timeout=timeout)
        return {"ok": True}

    monkeypatch.setattr(driver, "ext_cmd", ext_cmd)
    result = driver.newtab(
        "https://payload.test/", client_id="chrome:profile", operation_id="op-payload",
        timeout=0.4, active=False,
    )
    assert result == {"ok": True}
    assert seen["payload"] == {
        "cmd": "tabs", "method": "create", "url": "https://payload.test/",
        "active": False, "operation_id": "op-payload", "client_id": "chrome:profile",
    }
    assert seen["client_id"] == "chrome:profile"
    assert seen["timeout"] == 0.4


def test_extension_pending_create_status_survives_service_worker_restart_without_create():
    function_source = _create_operation_source()
    script = f"""
const stored = {{ btapCreateOperationsV1: {{ 'op-pending': {{
  operation_id: 'op-pending', status: 'pending', url: 'https://pending.test/',
  client_id: 'chrome:profile', created_at: Date.now(), tab_status: 'pending',
}} }} }};
let createCalls = 0;
const chrome = {{
  storage: {{ session: {{
    get: async keys => Object.fromEntries(keys.map(key => [key, stored[key]])),
    set: async (value) => Object.assign(stored, value),
  }} }},
  tabs: {{ create: async () => {{ createCalls += 1; throw new Error('must not create'); }} }},
}};
{function_source}
createTabStatus({{ operation_id: 'op-pending' }}).then(result =>
  process.stdout.write(JSON.stringify({{ result, createCalls }}))
).catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    assert result["result"]["data"]["status"] == "pending"
    assert result["result"]["data"]["operation_status"] == "unknown"
    assert result["result"]["data"]["resume_required"] is False
    assert result["result"]["data"]["may_have_created"] is True
    assert result["result"]["data"]["retry_safe"] is False
    assert result["createCalls"] == 0


def test_extension_create_status_retries_after_transient_storage_read_failure():
    function_source = _create_operation_source()
    script = f"""
const logs = [];
console.log = (...args) => logs.push(args.join(' '));
const stored = {{ btapCreateOperationsV1: {{ 'op-recovered': {{
  operation_id: 'op-recovered', status: 'completed', url: 'https://recovered.test/',
  client_id: 'chrome:profile', id: 77, generation: 'generation-77',
  created_at: Date.now(), tab_status: 'complete', load_ready: true,
}} }} }};
let reads = 0;
const chrome = {{
  storage: {{ session: {{
    get: async keys => {{
      reads += 1;
      if (reads === 1) throw new Error('No SW');
      return Object.fromEntries(keys.map(key => [key, stored[key]]));
    }},
    set: async value => Object.assign(stored, value),
  }} }},
}};
{function_source}
(async () => {{
  const first = await createTabStatus({{ operation_id: 'op-recovered' }});
  const second = await createTabStatus({{ operation_id: 'op-recovered' }});
  process.stdout.write(JSON.stringify({{ first, second, reads, logs }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)
    assert result["first"]["data"]["status"] == "unknown"
    assert result["first"]["data"]["may_have_created"] is True
    assert result["second"]["data"]["operation_id"] == "op-recovered"
    assert result["second"]["data"]["id"] == 77
    assert result["reads"] == 2


def test_extension_tab_updates_publish_stable_lifecycle_generations():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "tabGenerationFor" in source
    assert "scheduleNewTabGeneration" in source
    assert "generation: await tabGenerationFor" in source


def test_extension_safe_close_rejects_generation_mismatch_before_remove():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function validateTabCloseGenerations")
    end = source.index("\n\n// --- Temporary, origin-scoped", start)
    function_source = source[start:end]
    script = f"""
const generations = new Map([[7, 'generation-live'], [8, 'generation-other']]);
async function tabGenerationFor(tabId) {{ return generations.get(tabId); }}
{function_source}
(async () => {{
  const ok = await validateTabCloseGenerations([7], {{'7': 'generation-live'}});
  const mismatch = await validateTabCloseGenerations([7], {{'7': 'generation-old'}});
  const mixed = await validateTabCloseGenerations(
    [7, 8], {{'7': 'generation-live', '8': 'generation-old'}}
  );
  process.stdout.write(JSON.stringify({{ok, mismatch, mixed}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    result = json.loads(completed.stdout)
    assert result["ok"] is None
    assert "generation changed" in result["mismatch"]
    assert "generation changed" in result["mixed"]


def test_extension_owned_close_atomically_reports_missing_and_closes_live_tabs():
    source = BACKGROUND.read_text(encoding="utf-8")
    # Slices from the *validator*, not from `closeTabsWithGenerations`, because the
    # close path delegates to it rather than carrying its own copy of the
    # comparison. Starting lower would eval a function whose refusal is an
    # undefined name, and the mismatch case below is what proves the call is still
    # there: without it this test passes on a batch that refuses nothing.
    start = source.index("async function validateTabCloseGenerations")
    end = source.index("\n\n// --- Temporary, origin-scoped", start)
    function_source = source[start:end]
    script = f"""
const live = new Map([[8, {{id: 8}}], [9, {{id: 9}}]]);
const generations = new Map([[8, 'generation-live'], [9, 'generation-new']]);
const removed = [];
const chrome = {{ tabs: {{
  get: async tabId => live.has(tabId) ? live.get(tabId) : Promise.reject(new Error(`No tab with id: ${{tabId}}.`)),
  remove: async tabIds => removed.push(...tabIds),
}} }};
async function tabGenerationFor(tabId) {{ return generations.get(tabId); }}
{function_source}
(async () => {{
  const result = await closeTabsWithGenerations(
    [7, 8], {{'7': 'generation-gone', '8': 'generation-live'}}
  );
  // A live tab Chrome has since re-minted must take the whole batch down with
  // it, and must not have removed the tab that did match.
  const refused = await closeTabsWithGenerations(
    [8, 9], {{'8': 'generation-live', '9': 'generation-old'}}
  ).then(() => null, error => String(error.message));
  process.stdout.write(JSON.stringify({{result, removed, refused}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    outcome = json.loads(completed.stdout)
    assert outcome["result"] == {"closed": [8], "alreadyGone": [7]}
    assert "generation changed" in outcome["refused"]
    # Only the first call's tab, so the refused batch removed nothing at all.
    assert outcome["removed"] == [8]


def test_bridge_replaces_same_session_id_when_tab_generation_changes():
    driver = BrowserBridge.__new__(BrowserBridge)
    driver.sessions, driver.results, driver.acks = {}, {}, {}
    driver.default_session_id = "chrome:profile:9"
    driver.latest_session_id = "chrome:profile:9"
    old_client = object()
    old = Session(
        "chrome:profile:9",
        {
            "url": "https://old.test/",
            "type": "ext_ws",
            "client_id": "chrome:profile",
            "browser": "chrome",
            "tab_id": 9,
            "generation": "generation-old",
        },
        old_client,
    )
    driver.sessions[old.id] = old
    new_client = object()

    driver._apply_extension_tabs(
        "chrome:profile",
        "chrome",
        [
            {
                "id": 9,
                "url": "https://new.test/",
                "title": "new",
                "generation": "generation-new",
            }
        ],
        new_client,
    )

    current = driver.sessions["chrome:profile:9"]
    assert current is not old
    assert old.is_active() is False
    assert current.info["generation"] == "generation-new"
    assert current.url == "https://new.test/"
    assert current.ws_client is new_client


def test_extension_manifest_matches_package_version_and_branding():
    manifest = json.loads((BACKGROUND.parent / "manifest.json").read_text(encoding="utf-8"))
    from browsertap_mcp import __version__

    assert manifest["version"] == __version__
    assert manifest["name"] == "__MSG_extensionName__"
    assert manifest["action"]["default_title"] == "__MSG_extensionName__"
    assert manifest["description"] == "__MSG_extensionDescription__"
    assert manifest["default_locale"] == "en"
    assert "activeTab" not in manifest["permissions"]
    assert "declarativeNetRequest" in manifest["permissions"]
    assert manifest["host_permissions"] == ["<all_urls>"]
    injected_files = {
        name
        for entry in manifest["content_scripts"]
        for name in entry.get("js", [])
    }
    assert "config.js" not in injected_files


def test_extension_reinjects_tabs_in_bounded_parallel_batches():
    """Extension reload recovery must not wait on every tab serially."""
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('const REINJECT_CONCURRENCY');
const end = source.indexOf('\\n\\n// --- Bounded, tab-scoped', start);
if (start < 0 || end < 0) throw new Error('reinject helper not found');
const tabs = [
  {{ id: 1, url: 'https://one.test/' }},
  {{ id: 2, url: 'https://two.test/' }},
  {{ id: 3, url: 'https://three.test/' }},
  {{ id: 4, url: 'https://four.test/' }},
  {{ id: 5, url: 'chrome://settings/' }},
  {{ id: 6, url: 'https://six.test/' }},
  {{ id: 7, url: 'https://seven.test/' }},
  {{ id: 8, url: 'https://eight.test/' }},
  {{ id: 9, url: 'https://nine.test/' }},
];
const calls = [];
let active = 0;
let maxActive = 0;
const chrome = {{
  tabs: {{ query: async () => tabs }},
  scripting: {{
    executeScript: async (request) => {{
      const tabId = request.target.tabId;
      calls.push(tabId);
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise(resolve => setTimeout(resolve, 5));
      active -= 1;
      if (tabId === 3) throw new Error('restricted');
      return {{}};
    }},
  }},
}};
function isScriptable(url) {{ return !String(url).startsWith('chrome://'); }}
eval(source.slice(start, end));
(async () => {{
  await reinjectAllTabs();
  process.stdout.write(JSON.stringify({{ calls, maxActive }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)
    assert outcome["calls"] == [1, 2, 3, 4, 6, 7, 8, 9]
    assert outcome["maxActive"] == 4


def test_extension_logs_an_evicted_worker_as_routine_not_as_an_extension_error():
    """An MV3 eviction mid-call must not read as a broken extension.

    Chromium answers a chrome.* call whose service worker already stopped with
    the bare message "No SW" (``DispatchForServiceWorker`` in
    extension_function_dispatcher.cc; "No RPH" is the same bail-out when the
    render process host went first). The catch here used to classify by the
    socket alone, and Chrome rejects the pending call before the WebSocket close
    is processed -- so readyState still read OPEN, the eviction took the
    `console.error` branch, and an install doing nothing wrong grew a red
    `tabs_update failed` entry on chrome://extensions. That is the whole defect:
    nothing behaved differently, the report did.

    The three outcomes have to stay distinguishable, which is why this drives the
    real function instead of grepping for the helper: an eviction and a departed
    bridge are both routine but say different things, and a genuine failure still
    has to be loud.
    """
    script = """
const fs = require('fs');
const source = fs.readFileSync(__BACKGROUND__, 'utf8');
const helperStart = source.indexOf('function isWorkerGoneError');
const helperEnd = source.indexOf('\\nfunction bridgeStatusMessage', helperStart);
const updateStart = source.indexOf('let tabsUpdatePromise');
const updateEnd = source.indexOf('\\nchrome.tabs.onUpdated.addListener', updateStart);
if (helperStart < 0 || helperEnd < 0 || updateStart < 0 || updateEnd < 0) {
  throw new Error('isWorkerGoneError or sendTabsUpdate not found');
}
const WebSocket = { OPEN: 1, CLOSED: 3 };
let ws = null;
let queryError = new Error('unset');
const chrome = { tabs: { query: () => Promise.reject(queryError) } };
function ensureConnected() {}
function isScriptable() { return true; }
function getBrowserType() { return 'chrome'; }
function getClientId() { return Promise.resolve('chrome_test'); }
function tabGenerationFor() { return Promise.resolve('gen'); }
eval(source.slice(helperStart, helperEnd));
eval(source.slice(updateStart, updateEnd));

const reportHarnessFailure = console.error.bind(console);
const calls = [];
console.log = (...args) => calls.push({ level: 'log', text: String(args[0]) });
console.error = (...args) => calls.push({ level: 'error', text: String(args[0]) });

async function run(label, message, readyState) {
  // A send() that throws is the point: every scenario must be decided in the
  // catch around chrome.tabs.query, never by reaching the wire.
  ws = { readyState, send() { throw new Error('sendTabsUpdate should not have sent'); } };
  queryError = new Error(message);
  calls.length = 0;
  await sendTabsUpdate();
  return { label, calls: calls.slice() };
}

(async () => {
  const scenarios = [];
  scenarios.push(await run('worker-gone', 'No SW', WebSocket.OPEN));
  scenarios.push(await run('render-process-gone', 'No RPH', WebSocket.OPEN));
  scenarios.push(await run('real-failure', 'boom', WebSocket.OPEN));
  scenarios.push(await run('bridge-gone', 'boom', WebSocket.CLOSED));
  const verdicts = [];
  for (const message of ['No SW', 'No RPH', 'No SWx', 'no sw', '', 'Error: No SW']) {
    verdicts.push({ message, verdict: isWorkerGoneError(new Error(message)) });
  }
  verdicts.push({ message: 'thrown-null', verdict: isWorkerGoneError(null) });
  verdicts.push({ message: 'thrown-undefined', verdict: isWorkerGoneError(undefined) });
  verdicts.push({ message: 'thrown-string', verdict: isWorkerGoneError('No SW') });
  process.stdout.write(JSON.stringify({ scenarios, verdicts }));
})().catch(error => { reportHarnessFailure(error); process.exit(1); });
""".replace(
        "__BACKGROUND__", json.dumps(str(BACKGROUND))
    )
    outcome = _run_node_script(script)

    calls = {entry["label"]: entry["calls"] for entry in outcome["scenarios"]}
    assert sorted(calls) == [
        "bridge-gone",
        "real-failure",
        "render-process-gone",
        "worker-gone",
    ]
    for label in ("worker-gone", "render-process-gone"):
        assert len(calls[label]) == 1, calls[label]
        assert calls[label][0]["level"] == "log", calls[label]
        assert "worker evicted mid-update" in calls[label][0]["text"]

    # A departed bridge stays a separate sentence: the operator reading the log
    # has to be able to tell which side went away.
    assert calls["bridge-gone"][0]["level"] == "log"
    assert "bridge went away mid-update" in calls["bridge-gone"][0]["text"]

    # And anything else is still loud, or this test would be a way to silence
    # real breakage.
    assert calls["real-failure"][0]["level"] == "error"
    assert "tabs_update failed" in calls["real-failure"][0]["text"]

    verdicts = {entry["message"]: entry["verdict"] for entry in outcome["verdicts"]}
    assert verdicts == {
        "No SW": True,
        "No RPH": True,
        # Chrome's message is matched whole. `Error: No SW` is what the console
        # prints for the same error, and treating that rendering as the message
        # would let any error whose text merely ends that way pass as routine.
        "No SWx": False,
        "no sw": False,
        "": False,
        "Error: No SW": False,
        "thrown-null": False,
        "thrown-undefined": False,
        "thrown-string": False,
    }


def test_no_extension_api_failure_in_the_ws_client_is_reported_as_an_error():
    """Keep the two remaining loud sites the two that cannot be an eviction.

    Every await on a chrome.* API in this section can come back "No SW", so a
    new `console.error` next to one of them is a new false red entry. The two
    listed here are neither: `new WebSocket()` is a platform constructor and
    `onerror` is the socket's own report, and no service worker lifecycle event
    produces either.
    """
    import re

    source = BACKGROUND.read_text(encoding="utf-8")
    client = source[source.index("// --- WebSocket client for BrowserBridge ---") :]
    loud = re.findall(r"console\.error\('\[BTAP-WS\][^']*'", client)
    assert loud == [
        "console.error('[BTAP-WS] Constructor error:'",
        "console.error('[BTAP-WS] Error:'",
    ], loud
    # The classification helper is what the rest of them go through now.
    assert source.count("isWorkerGoneError(e)") == 6, "5 call sites plus the definition"


def test_tabs_updates_are_single_flight_and_finish_with_the_latest_snapshot():
    """A burst must not let an old, slow tab query overwrite a newer result."""
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('let tabsUpdatePromise');
const end = source.indexOf('\\nchrome.tabs.onUpdated.addListener', start);
if (start < 0 || end < 0) throw new Error('tabs update scheduler not found');
const WebSocket = {{ OPEN: 1 }};
const sent = [];
const ws = {{
  readyState: WebSocket.OPEN,
  send(payload) {{ sent.push(JSON.parse(payload)); }},
}};
let resolveFirstQuery;
let queryCalls = 0;
let activeQueries = 0;
let maxActiveQueries = 0;
const chrome = {{ tabs: {{ query() {{
  queryCalls += 1;
  activeQueries += 1;
  maxActiveQueries = Math.max(maxActiveQueries, activeQueries);
  if (queryCalls === 1) {{
    return new Promise(resolve => {{
      resolveFirstQuery = tabs => {{ activeQueries -= 1; resolve(tabs); }};
    }});
  }}
  activeQueries -= 1;
  return Promise.resolve([{{ id: queryCalls, url: `https://tab-${{queryCalls}}.test/`, title: 'latest' }}]);
}} }} }};
function ensureConnected() {{}}
function isScriptable() {{ return true; }}
function getClientId() {{ return Promise.resolve('chrome_test'); }}
function getBrowserType() {{ return 'chrome'; }}
function tabGenerationFor(tabId) {{ return Promise.resolve(`generation-${{tabId}}`); }}
function isWorkerGoneError() {{ return false; }}
eval(source.slice(start, end) + '\\nglobalThis.__tabsUpdateSchedulerIdle = () => tabsUpdatePromise === null && tabsUpdateDirty === false;');
(async () => {{
  const first = sendTabsUpdate();
  const second = sendTabsUpdate();
  const third = sendTabsUpdate();
  const sharedPromise = first === second && second === third;
  resolveFirstQuery([{{ id: 1, url: 'https://tab-1.test/', title: 'old' }}]);
  await Promise.all([first, second, third]);
  const afterBurst = {{
    queryCalls,
    sent: sent.map(message => message.tabs[0]?.id),
    maxActiveQueries,
  }};
  await sendTabsUpdate();
  process.stdout.write(JSON.stringify({{
    sharedPromise,
    afterBurst,
    queryCalls,
    sent: sent.map(message => message.tabs[0]?.id),
    schedulerIdle: globalThis.__tabsUpdateSchedulerIdle(),
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["sharedPromise"] is True
    assert outcome["afterBurst"] == {
        "queryCalls": 2,
        "sent": [1, 2],
        "maxActiveQueries": 1,
    }
    assert outcome["queryCalls"] == 3
    assert outcome["sent"] == [1, 2, 3]
    assert outcome["schedulerIdle"] is True


def test_extension_keepalive_uses_interval_and_reconnects_failed_sockets():
    script = """
const intervals = [];
const clearedIntervals = [];
const reconnects = [];
const alarms = [];
const sent = [];
let now = 55999;
let sendTabsUpdates = 0;
let platformTouches = 0;
let closedSockets = 0;
let statusBroadcasts = 0;
const console = { log() {}, error() {} };
let ws = {
  readyState: 1,
  send(message) { sent.push(JSON.parse(message)); },
  close() { closedSockets += 1; this.readyState = 3; },
};
let lastPongAt = 1000;
const WebSocket = { OPEN: 1 };
const Date = { now() { return now; } };
const chrome = {
  alarms: {
    create(name, details) { alarms.push({ name, details }); },
  },
  runtime: {
    lastError: null,
    getPlatformInfo(callback) { platformTouches += 1; callback({}); },
  },
};
function setInterval(callback, delay) {
  const timer = { id: intervals.length + 1, callback, delay };
  intervals.push(timer);
  return timer.id;
}
function clearInterval(id) { clearedIntervals.push(id); }
function ensureConnected(reason) { reconnects.push(reason); }
function sendTabsUpdate() { sendTabsUpdates += 1; }
function broadcastBridgeStatus() { statusBroadcasts += 1; }
""" + _websocket_keepalive_source() + """

scheduleKeepalive();
scheduleKeepalive();
intervals[0].callback();

now = 56001;
intervals[0].callback();

ws = null;
scheduleKeepalive();
intervals[1].callback();

lastPongAt = now;
ws = {
  readyState: WebSocket.OPEN,
  send() { throw new Error('send failed'); },
  close() { closedSockets += 1; this.readyState = 3; },
};
scheduleKeepalive();
intervals[2].callback();

process.stdout.write(JSON.stringify({
  intervalDelays: intervals.map(timer => timer.delay),
  clearedIntervals,
  reconnects,
  alarms,
  sent,
  sendTabsUpdates,
  platformTouches,
  closedSockets,
  statusBroadcasts,
}));
"""

    result = _run_node_script(script)

    assert result["intervalDelays"] == [20000, 20000, 20000]
    assert result["sent"] == [{"type": "ping"}]
    assert result["sendTabsUpdates"] == 1
    assert result["platformTouches"] == 1
    assert result["closedSockets"] == 1
    assert result["statusBroadcasts"] == 3
    assert result["clearedIntervals"] == [1, 2, 3]
    assert result["reconnects"] == [
        "keepalive-pong-timeout",
        "keepalive-lost",
        "keepalive-send-failed",
    ]
    assert result["alarms"] == [
        {
            "name": "btap-ws-probe",
            "details": {"delayInMinutes": 0.5, "periodInMinutes": 1},
        },
    ] * 3
    assert "chrome.alarms.create('btap-ws-keepalive'" not in BACKGROUND.read_text(
        encoding="utf-8"
    )


def test_extension_connect_timeout_recovers_without_stale_socket_teardown():
    script = """
const sockets = [];
const timers = [];
const clearedTimers = [];
let probeCalls = 0;
let stopKeepaliveCalls = 0;
let scheduleKeepaliveCalls = 0;
let statusBroadcasts = 0;
let ws = null;
let connectInFlight = false;
let lastPongAt = 0;
const WS_URL = 'ws://127.0.0.1:18765';
const console = { log() {}, error() {} };

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.closeCalls = 0;
    sockets.push(this);
  }
  close() { this.closeCalls += 1; this.readyState = 3; }
  send() {}
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
const WebSocket = FakeWebSocket;

function setTimeout(callback, delay) {
  const timer = { id: timers.length + 1, callback, delay };
  timers.push(timer);
  return timer;
}
function clearTimeout(timer) { clearedTimers.push(timer.id); }
function scheduleProbe() { probeCalls += 1; }
function stopKeepalive() { stopKeepaliveCalls += 1; }
function scheduleKeepalive() { scheduleKeepaliveCalls += 1; }
function broadcastBridgeStatus() { statusBroadcasts += 1; }
async function getClientId() { return 'chrome:test'; }
function getBrowserType() { return 'chrome'; }
function isScriptable() { return true; }
async function tabGenerationFor() { return 'generation'; }
async function handleExtMessage() { return { ok: true }; }
async function handleWsExec() {}
const chrome = { tabs: { async query() { return []; } } };
""" + _websocket_connect_source() + """

connectWS();
const first = sockets[0];
timers[0].callback();
const firstClosedByWatchdog = first.closeCalls;
first.onclose();
const afterFirstClose = {
  connectInFlight,
  probeCalls,
  stopKeepaliveCalls,
  wsIsNull: ws === null,
};

connectWS();
const second = sockets[1];
const beforeStaleClose = { probeCalls, stopKeepaliveCalls };
first.onclose();
const afterStaleClose = {
  currentSocketPreserved: ws === second,
  connectInFlight,
  probeCalls,
  stopKeepaliveCalls,
};

process.stdout.write(JSON.stringify({
  timerDelays: timers.map(timer => timer.delay),
  clearedTimers,
  socketCount: sockets.length,
  firstClosedByWatchdog,
  afterFirstClose,
  beforeStaleClose,
  afterStaleClose,
  scheduleKeepaliveCalls,
  statusBroadcasts,
}));
"""

    result = _run_node_script(script)

    assert result["timerDelays"] == [5000, 5000]
    assert result["socketCount"] == 2
    assert result["firstClosedByWatchdog"] == 1
    assert result["afterFirstClose"] == {
        "connectInFlight": False,
        "probeCalls": 1,
        "stopKeepaliveCalls": 1,
        "wsIsNull": True,
    }
    assert result["afterStaleClose"] == {
        "currentSocketPreserved": True,
        "connectInFlight": True,
        **result["beforeStaleClose"],
    }
    assert result["scheduleKeepaliveCalls"] == 0
    assert result["statusBroadcasts"] == 3


def test_websocket_command_reply_preserves_explicit_falsy_payloads():
    source = _websocket_connect_source()
    script = f"""
const sent = [];
const sockets = [];
const timers = [];
class FakeWebSocket {{
  constructor() {{
    this.readyState = FakeWebSocket.OPEN;
    sockets.push(this);
  }}
  send(payload) {{ sent.push(JSON.parse(payload)); }}
  close() {{ this.readyState = 3; }}
}}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
const WebSocket = FakeWebSocket;
function setTimeout(callback, delay) {{
  const timer = {{ callback, delay }};
  timers.push(timer);
  return timer;
}}
function clearTimeout() {{}}
let ws = null;
let connectInFlight = false;
let lastPongAt = 0;
const WS_URL = 'ws://127.0.0.1:18765';
const console = {{ log() {{}}, error() {{}} }};
function broadcastBridgeStatus() {{}}
function scheduleProbe() {{}}
function scheduleKeepalive() {{}}
function stopKeepalive() {{}}
async function getClientId() {{ return 'chrome:test'; }}
function getBrowserType() {{ return 'chrome'; }}
function isScriptable() {{ return true; }}
async function tabGenerationFor() {{ return 'generation'; }}
function isWorkerGoneError() {{ return false; }}
async function handleWsExec() {{}}
async function handleExtMessage(message) {{
  if (message.cmd === 'batch-value') return {{ ok: true, results: message.value }};
  return {{ ok: true, data: message.value }};
}}
const chrome = {{ tabs: {{ query: async () => [] }} }};
{source}
(async () => {{
  connectWS();
  const socket = sockets[0];
  await socket.onopen();
  const values = [null, false, 0, ''];
  for (let index = 0; index < values.length; index += 1) {{
    await socket.onmessage({{ data: JSON.stringify({{
      id: `value-${{index}}`, cmd: {{ cmd: 'value', value: values[index] }},
    }}) }});
  }}
  await socket.onmessage({{ data: JSON.stringify({{
    id: 'batch', cmd: {{ cmd: 'batch-value', value: [null, false, 0, ''] }},
  }}) }});
  const replies = Object.fromEntries(
    sent.filter(item => item.id).map(item => [item.id, item.result]),
  );
  process.stdout.write(JSON.stringify({{ replies }}));
}})().catch(error => {{ process.stderr.write(String(error)); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["replies"] == {
        "value-0": None,
        "value-1": False,
        "value-2": 0,
        "value-3": "",
        "batch": [None, False, 0, ""],
    }


@pytest.mark.parametrize("failure_mode", ["throw_after_effect", "missing_result"])
def test_execute_script_unknown_outcome_is_never_replayed(failure_mode):
    source = _ws_exec_source()
    script = f"""
const failureMode = {json.dumps(failure_mode)};
const replies = [];
let sideEffects = 0;
let executeCalls = 0;
let cdpCalls = 0;
const console = {{ log() {{}}, error() {{}} }};
const WebSocket = {{ OPEN: 1 }};
const ws = {{
  readyState: WebSocket.OPEN,
  send(payload) {{ replies.push(JSON.parse(payload)); }},
}};
function currentExecDialogPolicy() {{ return null; }}
function claimManualExecDialogPolicy() {{ return null; }}
async function executeManualScript() {{ throw new Error('manual path not expected'); }}
function buildPageScript() {{ return 'wrapped'; }}
function buildSubframeScopeScript() {{ return 'scope'; }}
function buildCdpScript() {{ return 'cdp-wrapped'; }}
async function withCspOff(_tabId, fn) {{ return await fn(); }}
async function runCdpExecFallback() {{
  cdpCalls += 1;
  sideEffects += 1;
  return {{ ok: true, data: 'cdp-result' }};
}}
async function tabGenerationFor() {{ return 'generation'; }}
const listeners = new Set();
const chrome = {{
  scripting: {{
    async executeScript() {{
      executeCalls += 1;
      sideEffects += 1;
      if (failureMode === 'throw_after_effect') {{
        throw new Error('result channel failed after execution');
      }}
      return [{{ frameId: 0 }}];
    }},
  }},
  tabs: {{
    onCreated: {{
      addListener(listener) {{ listeners.add(listener); }},
      removeListener(listener) {{ listeners.delete(listener); }},
    }},
    get: async id => ({{ id, url: 'https://example.test/', title: 'Example' }}),
  }},
}};
{source}
(async () => {{
  await handleWsExec({{ id: 'exec-1', tabId: 9, code: 'sideEffect()' }});
  process.stdout.write(JSON.stringify({{
    sideEffects,
    executeCalls,
    cdpCalls,
    replies,
    listenerCount: listeners.size,
  }}));
}})().catch(error => {{ process.stderr.write(String(error)); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["sideEffects"] == 1
    assert outcome["executeCalls"] == 1
    assert outcome["cdpCalls"] == 0
    assert outcome["listenerCount"] == 0
    assert outcome["replies"][0] == {"type": "ack", "id": "exec-1"}
    error_reply = outcome["replies"][-1]
    assert error_reply["type"] == "error"
    assert error_reply["error"]["dispatched"] is True
    assert error_reply["error"]["may_have_executed"] is True
    assert error_reply["error"]["retryable"] is False


def test_execute_script_selects_the_marked_top_document_without_assuming_frame_zero():
    source = _ws_exec_source()
    script = f"""
const replies = [];
let cdpCalls = 0;
const console = {{ log() {{}}, error() {{}} }};
const WebSocket = {{ OPEN: 1 }};
const ws = {{
  readyState: WebSocket.OPEN,
  send(payload) {{ replies.push(JSON.parse(payload)); }},
}};
function currentExecDialogPolicy() {{ return null; }}
function claimManualExecDialogPolicy() {{ return null; }}
async function executeManualScript() {{ throw new Error('manual path not expected'); }}
function buildPageScript() {{ return 'wrapped'; }}
function buildSubframeScopeScript() {{ return 'scope'; }}
function buildCdpScript() {{ return 'cdp-wrapped'; }}
async function withCspOff(_tabId, fn) {{ return await fn(); }}
async function runCdpExecFallback() {{
  cdpCalls += 1;
  return {{ ok: true, data: 'cdp-result' }};
}}
async function tabGenerationFor() {{ return 'generation'; }}
const listeners = new Set();
const chrome = {{
  scripting: {{
    async executeScript() {{
      return [
        {{ frameId: 0, result: undefined }},
        {{
          frameId: 37,
          result: {{
            __btap_top_frame_result: true,
            value: {{ ok: true, data: 'marked-top-result' }},
          }},
        }},
      ];
    }},
  }},
  tabs: {{
    onCreated: {{
      addListener(listener) {{ listeners.add(listener); }},
      removeListener(listener) {{ listeners.delete(listener); }},
    }},
    get: async id => ({{ id, url: 'https://example.test/', title: 'Example' }}),
  }},
}};
{source}
(async () => {{
  await handleWsExec({{ id: 'exec-marker', tabId: 9, code: 'return 1' }});
  process.stdout.write(JSON.stringify({{ replies, cdpCalls, listenerCount: listeners.size }}));
}})().catch(error => {{ process.stderr.write(String(error)); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["cdpCalls"] == 0
    assert outcome["listenerCount"] == 0
    assert outcome["replies"][0] == {"type": "ack", "id": "exec-marker"}
    assert outcome["replies"][-1]["type"] == "result"
    assert outcome["replies"][-1]["result"] == "marked-top-result"


def test_execute_script_preserves_top_marker_when_page_csp_rejects_eval():
    source = _ws_exec_source()
    script = f"""
const replies = [];
let cdpCalls = 0;
const console = {{ log() {{}}, error() {{}} }};
const WebSocket = {{ OPEN: 1 }};
const ws = {{
  readyState: WebSocket.OPEN,
  send(payload) {{ replies.push(JSON.parse(payload)); }},
}};
globalThis.window = globalThis;
window.top = window;
function currentExecDialogPolicy() {{ return null; }}
function claimManualExecDialogPolicy() {{ return null; }}
async function executeManualScript() {{ throw new Error('manual path not expected'); }}
function buildPageScript() {{
  return "throw new EvalError('Refused to evaluate because unsafe-eval violates Content Security Policy')";
}}
function buildSubframeScopeScript() {{ return 'scope'; }}
function buildCdpScript() {{ return 'cdp-wrapped'; }}
async function withCspOff(_tabId, fn) {{ return await fn(); }}
async function runCdpExecFallback() {{
  cdpCalls += 1;
  return {{ ok: true, data: 'cdp-result' }};
}}
async function tabGenerationFor() {{ return 'generation'; }}
const listeners = new Set();
const chrome = {{
  scripting: {{
    async executeScript(options) {{
      return [{{ frameId: 37, result: await options.func(...options.args) }}];
    }},
  }},
  tabs: {{
    onCreated: {{
      addListener(listener) {{ listeners.add(listener); }},
      removeListener(listener) {{ listeners.delete(listener); }},
    }},
    get: async id => ({{ id, url: 'https://example.test/', title: 'Example' }}),
  }},
}};
{source}
(async () => {{
  await handleWsExec({{ id: 'exec-csp-marker', tabId: 9, code: 'return 1' }});
  process.stdout.write(JSON.stringify({{ replies, cdpCalls, listenerCount: listeners.size }}));
}})().catch(error => {{ process.stderr.write(String(error)); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["cdpCalls"] == 1
    assert outcome["listenerCount"] == 0
    assert outcome["replies"][0] == {"type": "ack", "id": "exec-csp-marker"}
    assert outcome["replies"][-1]["type"] == "result"
    assert outcome["replies"][-1]["result"] == "cdp-result"


def test_content_script_has_no_page_dom_privileged_command_channel():
    source = (BACKGROUND.parent / "content.js").read_text(encoding="utf-8")

    assert "TID" not in source
    assert "new MutationObserver" not in source
    for privileged in ("'cdp'", "'batch'", "'tabs'", "'bookmarks'", "'management'"):
        assert f"cmd === {privileged}" not in source


def test_extension_popup_binds_the_cookie_read_and_the_clipboard_copy_to_gestures():
    popup = (BACKGROUND.parent / "popup.html").read_text(encoding="utf-8")
    script = (BACKGROUND.parent / "popup.js").read_text(encoding="utf-8")

    assert 'data-i18n="extensionName"' in popup
    assert 'data-i18n="cookieViewerTitle"' in popup
    assert 'id="indicator-visible"' in popup
    assert 'id="copy"' in popup
    assert "cmd: 'cookies'" in script
    assert "btap_indicator_visible" in script
    assert "chrome.i18n.getMessage" in script
    assert "navigator.clipboard.writeText" in script
    assert "copyCookies" in script
    # Reverse gate for the leak this popup shipped with: DOMContentLoaded called
    # fetchCookies(), whose tail wrote every cookie of the active tab to the
    # clipboard -- so opening the popup to toggle the indicator checkbox replaced
    # the clipboard with session credentials, HttpOnly ones included. Nothing may
    # call fetchCookies without a gesture; the behaviour itself is pinned by
    # test_extension_popup_rejects_non_http_pages_and_handles_malformed_cookie_data.
    assert "fetchCookies();" not in script
    # The bridge port stays owned by the Python side (BROWSERTAP_BRIDGE_PORT).
    # Exposing it in a popup any user can open is a second source of truth that
    # silently breaks the bridge profile-wide and outlives a package reinstall,
    # so the popup must not read or write it. background.js keeps reading
    # btap_port from storage for the documented non-default-port setup.
    assert 'id="bridge-port"' not in popup
    assert "btap_port" not in script


def test_extension_popup_loads_indicator_preferences_in_one_storage_read_and_retries_migration():
    popup_path = BACKGROUND.parent / "popup.js"
    script = """
const fs = require('fs');
const source = fs.readFileSync(__POPUP__, 'utf8');
const indicator = { checked: null };
const document = {
  addEventListener() {},
  getElementById(id) { return id === 'indicator-visible' ? indicator : null; },
  documentElement: {},
  querySelectorAll() { return []; },
};
let stored = {};
let failNextSet = false;
const reads = [];
const writes = [];
const removals = [];
const chrome = {
  storage: { local: {
    get(keys) {
      reads.push(keys);
      const names = Array.isArray(keys) ? keys : [keys];
      return Promise.resolve(Object.fromEntries(
        names.filter(name => Object.hasOwn(stored, name)).map(name => [name, stored[name]]),
      ));
    },
    set(value) {
      writes.push(value);
      if (failNextSet) {
        failNextSet = false;
        return Promise.reject(new Error('storage unavailable'));
      }
      Object.assign(stored, value);
      return Promise.resolve();
    },
    remove(name) {
      removals.push(name);
      delete stored[name];
      return Promise.resolve();
    },
  } },
};
eval(source + '\\n;globalThis.__loadIndicatorVisibility = loadIndicatorVisibility;');

async function run(initial, failSet = false) {
  stored = { ...initial };
  failNextSet = failSet;
  reads.length = 0;
  writes.length = 0;
  removals.length = 0;
  indicator.checked = null;
  await globalThis.__loadIndicatorVisibility();
  return {
    checked: indicator.checked,
    reads: [...reads],
    writes: [...writes],
    removals: [...removals],
    stored: { ...stored },
  };
}

(async () => {
  const currentWins = await run({
    btap_indicator_visible: false,
    tmwd_indicator_visible: true,
  });
  const legacyMigrates = await run({ tmwd_indicator_visible: false });
  const missingDefaultsVisible = await run({});
  const failedMigration = await run({ tmwd_indicator_visible: true }, true);
  const retryAfterFailure = await run(stored);
  process.stdout.write(JSON.stringify({
    currentWins,
    legacyMigrates,
    missingDefaultsVisible,
    failedMigration,
    retryAfterFailure,
  }));
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__POPUP__", json.dumps(str(popup_path)))

    result = _run_node_script(script)
    expected_read = [["btap_indicator_visible", "tmwd_indicator_visible"]]

    assert result["currentWins"] == {
        "checked": False,
        "reads": expected_read,
        "writes": [],
        "removals": [],
        "stored": {
            "btap_indicator_visible": False,
            "tmwd_indicator_visible": True,
        },
    }
    assert result["legacyMigrates"] == {
        "checked": False,
        "reads": expected_read,
        "writes": [{"btap_indicator_visible": False}],
        "removals": ["tmwd_indicator_visible"],
        "stored": {"btap_indicator_visible": False},
    }
    assert result["missingDefaultsVisible"] == {
        "checked": True,
        "reads": expected_read,
        "writes": [],
        "removals": [],
        "stored": {},
    }
    assert result["failedMigration"] == {
        "checked": True,
        "reads": expected_read,
        "writes": [{"btap_indicator_visible": True}],
        "removals": [],
        "stored": {"tmwd_indicator_visible": True},
    }
    assert result["retryAfterFailure"] == {
        "checked": True,
        "reads": expected_read,
        "writes": [{"btap_indicator_visible": True}],
        "removals": ["tmwd_indicator_visible"],
        "stored": {"btap_indicator_visible": True},
    }


def test_extension_popup_rejects_non_http_pages_and_handles_malformed_cookie_data():
    popup_path = BACKGROUND.parent / "popup.js"
    script = """
const fs = require('fs');
const source = fs.readFileSync(__POPUP__, 'utf8');
const out = { textContent: '' };
const copyBtn = { textContent: '' };
const document = {
  addEventListener() {},
  getElementById(id) {
    if (id === 'out') return out;
    if (id === 'copy') return copyBtn;
    return null;
  },
  documentElement: {},
  querySelectorAll() { return []; },
};
let activeUrl = 'chrome://extensions';
let response = { ok: true, data: [] };
let sendMessageCalls = 0;
let clipboardShouldFail = false;
const clipboardWrites = [];
const chrome = {
  i18n: {
    getMessage(name) { return name; },
    getUILanguage() { return 'en'; },
  },
  tabs: {
    query() { return Promise.resolve([{ url: activeUrl }]); },
  },
  runtime: {
    sendMessage() { sendMessageCalls += 1; return Promise.resolve(response); },
  },
};
const navigator = {
  clipboard: {
    writeText(value) {
      if (clipboardShouldFail) return Promise.reject(new Error('denied'));
      clipboardWrites.push(value);
      return Promise.resolve();
    },
  },
};
eval(source + '\\n;globalThis.__fetchCookies = fetchCookies; globalThis.__copyCookies = copyCookies;');

(async () => {
  await globalThis.__fetchCookies();
  const unsupported = { text: out.textContent, sendMessageCalls };

  activeUrl = 'https://example.com/account';
  response = { ok: true, data: [{ name: 'sid', value: 'abc', secure: true }] };
  await globalThis.__fetchCookies();
  const valid = {
    text: out.textContent,
    sendMessageCalls,
    clipboardWrites: [...clipboardWrites],
  };

  await globalThis.__copyCookies();
  const copied = {
    clipboardWrites: [...clipboardWrites],
    button: copyBtn.textContent,
    listStillRendered: out.textContent,
  };

  clipboardShouldFail = true;
  await globalThis.__copyCookies();
  const refused = {
    clipboardWrites: [...clipboardWrites],
    button: copyBtn.textContent,
    listStillRendered: out.textContent,
  };
  clipboardShouldFail = false;

  // A failed refresh must invalidate the last successful read. Otherwise a
  // user who moves from an HTTP page to a restricted page can still copy the
  // previous page's credentials from the popup.
  activeUrl = 'chrome://extensions';
  response = { ok: true, data: [] };
  await globalThis.__fetchCookies();
  await globalThis.__copyCookies();
  const staleAfterUnsupported = {
    clipboardWrites: [...clipboardWrites],
    button: copyBtn.textContent,
  };

  activeUrl = 'https://example.com/account';
  response = { ok: true, data: null };
  let malformedThrew = false;
  try { await globalThis.__fetchCookies(); } catch (_) { malformedThrew = true; }
  const malformed = {
    text: out.textContent,
    malformedThrew,
    sendMessageCalls,
  };
  await globalThis.__copyCookies();
  const staleAfterMalformed = {
    clipboardWrites: [...clipboardWrites],
    button: copyBtn.textContent,
  };

  response = { ok: false, error: 'bridge unavailable' };
  await globalThis.__fetchCookies();
  await globalThis.__copyCookies();
  const staleAfterError = {
    clipboardWrites: [...clipboardWrites],
    button: copyBtn.textContent,
  };
  process.stdout.write(JSON.stringify({
    unsupported,
    valid,
    copied,
    refused,
    staleAfterUnsupported,
    staleAfterMalformed,
    staleAfterError,
    malformed,
  }));
})().catch(error => { console.error(error); process.exit(1); });
""".replace("__POPUP__", json.dumps(str(popup_path)))

    result = _run_node_script(script)

    assert result["unsupported"] == {
        "text": "cookiesUnavailable",
        "sendMessageCalls": 0,
    }
    # Rendering the list must not touch the clipboard. This assertion is the
    # regression: it used to read ["sid=abc"] here, because the copy ran at the
    # tail of fetchCookies and the popup called that on open.
    assert result["valid"] == {
        "text": "sid=abc [S]",
        "sendMessageCalls": 1,
        "clipboardWrites": [],
    }
    # Only the Copy button writes, and it copies what is on screen.
    assert result["copied"] == {
        "clipboardWrites": ["sid=abc"],
        "button": "copyDone",
        "listStillRendered": "sid=abc [S]",
    }
    # A refused clipboard reports on the button and leaves the rendered list
    # alone. The old code shared one try/catch with the render, so a clipboard
    # error replaced the cookies the user had just asked for with "Error: ...".
    assert result["refused"] == {
        "clipboardWrites": ["sid=abc"],
        "button": "copyFailed",
        "listStillRendered": "sid=abc [S]",
    }
    assert result["staleAfterUnsupported"] == {
        "clipboardWrites": ["sid=abc"],
        "button": "copyNothing",
    }
    assert result["staleAfterMalformed"] == {
        "clipboardWrites": ["sid=abc"],
        "button": "copyNothing",
    }
    assert result["staleAfterError"] == {
        "clipboardWrites": ["sid=abc"],
        "button": "copyNothing",
    }
    assert result["malformed"] == {
        "text": "errorPrefixunknownError",
        "malformedThrew": False,
        "sendMessageCalls": 2,
    }


def test_cookie_handler_rejects_unsupported_urls_before_cookie_api_calls():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("async function handleCookies")
    end = source.index("\nfunction batchDeadlineRemainingMs", start)
    handler_source = source[start:end]
    script = f"""
let cookieCalls = [];
const chrome = {{
  tabs: {{ get() {{ throw new Error('tabs.get should not be called'); }} }},
  cookies: {{
    getAll(query) {{
      cookieCalls.push(query);
      if (query.partitionKey) return Promise.resolve([
        {{
          name: 'partitioned', value: 'p', domain: 'example.com', path: '/',
          partitionKey: {{ topLevelSite: 'https://example.com' }},
        }},
        {{ name: 'base', value: 'duplicate', domain: 'example.com', path: '/' }},
        {{ name: 'base', value: 'scoped', domain: 'example.com', path: '/account' }},
      ]);
      return Promise.resolve([
        {{ name: 'base', value: 'b', domain: 'example.com', path: '/' }},
      ]);
    }},
  }},
}};
{handler_source}
(async () => {{
  const unsupported = [];
  for (const url of ['chrome://extensions', '', 'not a url']) {{
    unsupported.push(await handleCookies({{ cmd: 'cookies', url }}, {{}}));
  }}
  const callsAfterUnsupported = cookieCalls.length;
  const valid = await handleCookies(
    {{ cmd: 'cookies', url: 'https://example.com/account?from=popup' }}, {{}},
  );
  process.stdout.write(JSON.stringify({{
    unsupported,
    callsAfterUnsupported,
    valid,
    cookieCalls,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""

    result = _run_node_script(script)

    assert result["unsupported"] == [
        {
            "ok": False,
            "code": "unsupported_url_scheme",
            "error": "Cookies are only available for HTTP(S) pages.",
        }
    ] * 3
    assert result["callsAfterUnsupported"] == 0
    assert result["valid"] == {
        "ok": True,
        "data": [
            {"name": "base", "value": "b", "domain": "example.com", "path": "/"},
            {
                "name": "partitioned",
                "value": "p",
                "domain": "example.com",
                "path": "/",
                "partitionKey": {"topLevelSite": "https://example.com"},
            },
            {
                "name": "base",
                "value": "scoped",
                "domain": "example.com",
                "path": "/account",
            },
        ],
    }
    assert result["cookieCalls"] == [
        {"url": "https://example.com/account?from=popup"},
        {
            "url": "https://example.com/account?from=popup",
            "partitionKey": {"topLevelSite": "https://example.com"},
        },
    ]


def test_extension_ui_is_localized_and_indicator_can_hide_without_stopping_keepalive():
    extension = BACKGROUND.parent
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    content = (extension / "content.js").read_text(encoding="utf-8")
    english = json.loads((extension / "_locales" / "en" / "messages.json").read_text(encoding="utf-8"))
    chinese = json.loads((extension / "_locales" / "zh_CN" / "messages.json").read_text(encoding="utf-8"))

    assert manifest["default_locale"] == "en"
    assert set(english) == set(chinese)
    assert "extensionName" in english
    assert "btap_indicator_visible" in content
    assert "d.hidden = value === false" in content
    assert "chrome.runtime.connect({ name: 'btap_keepalive' })" in content
    assert "btap_status_request" in content
    assert "chrome.runtime.onMessage.addListener" in content
    assert "setInterval" not in content
    assert "setTimeout" not in content
    assert "alert(" not in content


def test_content_script_reinjection_is_timerless_and_old_callbacks_cannot_repaint():
    content_path = BACKGROUND.parent / "content.js"
    script = """
const fs = require('fs');
const source = fs.readFileSync(__CONTENT__, 'utf8');
const elements = new Map();
const document = {
  body: {
    appendChild(element) { elements.set(element.id, element); },
  },
  documentElement: {},
  getElementById(id) { return elements.get(id) || null; },
  createElement() {
    return {
      id: '', innerText: '', hidden: false, style: {},
      remove() {
        if (elements.get(this.id) === this) elements.delete(this.id);
      },
    };
  },
};
const window = {};
window.self = window;
window.top = window;
let timerCalls = 0;
function setInterval() { timerCalls += 1; }
function setTimeout() { timerCalls += 1; }
const ports = [];
const runtimeListeners = [];
const storageListeners = [];
const chrome = {
  i18n: {
    getMessage(name) { return name; },
  },
  runtime: {
    lastError: null,
    onMessage: { addListener(listener) { runtimeListeners.push(listener); } },
    connect() {
      const port = {
        sent: [],
        listeners: [],
        onMessage: { addListener(listener) { port.listeners.push(listener); } },
        postMessage(message) { port.sent.push(message); },
      };
      ports.push(port);
      return port;
    },
  },
  storage: {
    local: { get(_key, callback) { callback({ btap_indicator_visible: true }); } },
    onChanged: { addListener(listener) { storageListeners.push(listener); } },
  },
};

eval(source);
const firstBadge = elements.get('btap-indicator');
eval(source);
const secondBadge = elements.get('btap-indicator');
ports[1].listeners[0]({ type: 'btap_status', ws: true });
const badgeBeforeLateCallback = secondBadge.innerText;
runtimeListeners[0]({ type: 'btap_status', ws: false });
const badgeAfterLateCallback = secondBadge.innerText;
runtimeListeners[1]({ type: 'btap_status', ws: false });
process.stdout.write(JSON.stringify({
  timerCalls,
  ports: ports.length,
  portRequests: ports.map(port => port.sent),
  runtimeListenerCount: runtimeListeners.length,
  storageListenerCount: storageListeners.length,
  elementCount: elements.size,
  firstBadgeReplaced: firstBadge !== secondBadge,
  badgeBeforeLateCallback,
  badgeAfterLateCallback,
  badgeText: secondBadge.innerText,
  badgeColor: secondBadge.style.background,
}));
""".replace("__CONTENT__", json.dumps(str(content_path)))
    result = _run_node_script(script)

    assert result == {
        "timerCalls": 0,
        "ports": 2,
        "portRequests": [
            [{"type": "btap_status_request"}],
            [{"type": "btap_status_request"}],
        ],
        "runtimeListenerCount": 2,
        "storageListenerCount": 2,
        "elementCount": 1,
        "firstBadgeReplaced": True,
        "badgeBeforeLateCallback": "indicatorConnected",
        "badgeAfterLateCallback": "indicatorConnected",
        "badgeText": "indicatorDisconnected",
        "badgeColor": "#e67e22",
    }


def test_content_script_delayed_callbacks_do_not_access_invalidated_chrome_context():
    content_path = BACKGROUND.parent / "content.js"
    script = """
const fs = require('fs');
const source = fs.readFileSync(__CONTENT__, 'utf8');
const elements = new Map();
const document = {
  body: { appendChild(element) { elements.set(element.id, element); } },
  documentElement: {},
  getElementById(id) { return elements.get(id) || null; },
  createElement() {
    return {
      id: '', innerText: '', hidden: false, style: {},
      remove() { if (elements.get(this.id) === this) elements.delete(this.id); },
    };
  },
};
const window = {};
window.self = window;
window.top = window;
const runtimeListeners = [];
const storageListeners = [];
const storageCallbacks = [];
const ports = [];
let resolveStorage;
const storageResult = new Promise(resolve => { resolveStorage = resolve; });
let contextInvalidated = false;
let invalidChromeAccesses = 0;
const chromeTarget = {
  i18n: { getMessage(name) { return name; } },
  runtime: {
    lastError: null,
    onMessage: { addListener(listener) { runtimeListeners.push(listener); } },
    connect() {
      const port = {
        listeners: [],
        onMessage: { addListener(listener) { port.listeners.push(listener); } },
        postMessage() {},
      };
      ports.push(port);
      return port;
    },
  },
  storage: {
    local: {
      get(_key, callback) {
        storageCallbacks.push(callback);
        return storageResult;
      },
    },
    onChanged: { addListener(listener) { storageListeners.push(listener); } },
  },
};
const chrome = new Proxy(chromeTarget, {
  get(target, property) {
    if (contextInvalidated) invalidChromeAccesses += 1;
    return target[property];
  },
});

eval(source);
contextInvalidated = true;
const lateErrors = [];
for (const invoke of [
  () => runtimeListeners[0]({ type: 'btap_status', ws: true }),
  () => ports[0].listeners[0]({ type: 'btap_status', ws: false }),
  () => {
    if (typeof storageCallbacks[0] === 'function') {
      storageCallbacks[0]({ btap_indicator_visible: false });
    }
  },
  () => storageListeners[0](
    { btap_indicator_visible: { newValue: false } }, 'local',
  ),
]) {
  try { invoke(); } catch (error) { lateErrors.push(error.message); }
}
resolveStorage({ btap_indicator_visible: false });
Promise.resolve().then(() => Promise.resolve()).then(() => {
  process.stdout.write(JSON.stringify({
    invalidChromeAccesses,
    lateErrors,
    hidden: elements.get('btap-indicator').hidden,
  }));
});
""".replace("__CONTENT__", json.dumps(str(content_path)))

    result = _run_node_script(script)

    assert result == {
        "invalidChromeAccesses": 0,
        "lateErrors": [],
        "hidden": True,
    }


def test_background_replaces_duplicate_keepalive_port_without_losing_new_owner():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const mapLine = 'const keepalivePorts = new Map();';
const helperStart = source.indexOf('function bridgeStatusMessage');
const helperEnd = source.indexOf('\\n\\nfunction parseBridgePort', helperStart);
const handlerStart = source.indexOf('chrome.runtime.onConnect.addListener');
const handlerEnd = source.indexOf('// Popup / content can poke SW awake', handlerStart);
if (!source.includes(mapLine) || helperStart < 0 || helperEnd < 0 ||
    handlerStart < 0 || handlerEnd < 0) {{
  throw new Error('keepalive port handler not found');
}}
let connectListener;
let ensureConnectedCalls = 0;
let tabsUpdateCalls = 0;
let queryCalls = 0;
let resolveFirstQuery;
let resolveSecondMessage;
const secondMessageSent = new Promise(resolve => {{ resolveSecondMessage = resolve; }});
const tabMessages = [];
const chrome = {{ runtime: {{
  lastError: null,
  onConnect: {{ addListener(listener) {{ connectListener = listener; }} }},
}}, tabs: {{
  query() {{
    queryCalls += 1;
    if (queryCalls === 1) {{
      return new Promise(resolve => {{ resolveFirstQuery = resolve; }});
    }}
    return Promise.resolve([{{ id: 42, url: 'https://example.test/' }}]);
  }},
  sendMessage(tabId, message, options) {{
    tabMessages.push({{ tabId, message, options }});
    if (tabMessages.length === 2) resolveSecondMessage();
    return Promise.resolve();
  }},
}} }};
const WebSocket = {{ OPEN: 1 }};
let ws = {{ readyState: 0 }};
function isScriptable() {{ return true; }}
function ensureConnected(reason) {{
  if (reason === 'port-connect') ensureConnectedCalls += 1;
}}
function sendTabsUpdate() {{ tabsUpdateCalls += 1; }}
const keepalivePorts = new Map();
eval(source.slice(helperStart, helperEnd));
eval(source.slice(handlerStart, handlerEnd));
function makePort(tabId, frameId = 0) {{
  const disconnectListeners = [];
  const messageListeners = [];
  return {{
    name: 'btap_keepalive',
    sender: {{ tab: {{ id: tabId }}, frameId }},
    disconnectCalls: 0,
    posted: [],
    onDisconnect: {{ addListener(listener) {{ disconnectListeners.push(listener); }} }},
    onMessage: {{ addListener(listener) {{ messageListeners.push(listener); }} }},
    postMessage(message) {{ this.posted.push(message); }},
    disconnect() {{
      this.disconnectCalls += 1;
      for (const listener of disconnectListeners) listener();
    }},
    fireDisconnect() {{
      for (const listener of disconnectListeners) listener();
    }},
    fireMessage(message) {{
      for (const listener of messageListeners) listener(message);
    }},
  }};
}}
const first = makePort(42);
const second = makePort(42);
const otherFrame = makePort(42, 7);
(async () => {{
connectListener(first);
ws = {{ readyState: WebSocket.OPEN }};
first.fireMessage({{ type: 'btap_status_request' }});
connectListener(second);
connectListener(otherFrame);
first.fireDisconnect();
const afterLateOldDisconnect = {{
  ownerIsSecond: keepalivePorts.get('42:0') === second,
  otherFrameOwned: keepalivePorts.get('42:7') === otherFrame,
  size: keepalivePorts.size,
}};
ws = null;
broadcastBridgeStatus();
second.fireDisconnect();
const remainingKeys = [...keepalivePorts.keys()];
keepalivePorts.clear();
ws = {{ readyState: WebSocket.OPEN }};
broadcastBridgeStatus();
await Promise.resolve();
const queryCallsBeforeRelease = queryCalls;
resolveFirstQuery([{{ id: 42, url: 'https://example.test/' }}]);
await secondMessageSent;
process.stdout.write(JSON.stringify({{
  firstDisconnectCalls: first.disconnectCalls,
  secondDisconnectCalls: second.disconnectCalls,
  ensureConnectedCalls,
  tabsUpdateCalls,
  firstStatuses: first.posted,
  secondStatuses: second.posted,
  otherFrameStatuses: otherFrame.posted,
  tabMessages,
  queryCalls,
  queryCallsBeforeRelease,
  afterLateOldDisconnect,
  remainingKeys,
}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = _run_node_script(script)

    assert result == {
        "firstDisconnectCalls": 1,
        "secondDisconnectCalls": 0,
        "ensureConnectedCalls": 3,
        "tabsUpdateCalls": 2,
        "firstStatuses": [
            {"type": "btap_status", "ws": False},
            {"type": "btap_status", "ws": True},
        ],
        "secondStatuses": [
            {"type": "btap_status", "ws": True},
            {"type": "btap_status", "ws": False},
        ],
        "otherFrameStatuses": [
            {"type": "btap_status", "ws": True},
            {"type": "btap_status", "ws": False},
        ],
        "tabMessages": [
            {
                "tabId": 42,
                "message": {"type": "btap_status", "ws": True},
                "options": {"frameId": 0},
            },
            {
                "tabId": 42,
                "message": {"type": "btap_status", "ws": True},
                "options": {"frameId": 0},
            },
        ],
        "queryCalls": 2,
        "queryCallsBeforeRelease": 1,
        "afterLateOldDisconnect": {
            "ownerIsSecond": True,
            "otherFrameOwned": True,
            "size": 2,
        },
        "remainingKeys": ["42:7"],
    }

    background = BACKGROUND.read_text(encoding="utf-8")
    onopen = background[background.index("ws.onopen ="):background.index("ws.onmessage =")]
    onclose = background[background.index("ws.onclose ="):background.index("ws.onerror =")]
    assert "broadcastBridgeStatus();" in onopen
    assert "broadcastBridgeStatus();" in onclose


def test_extension_bridge_port_is_persistent_and_not_fixed_to_one_url():
    source = BACKGROUND.read_text(encoding="utf-8")

    # The current and pre-BTAP keys are read together: a cold worker pays one
    # storage round trip while an install already pointed at a non-default
    # bridge still migrates instead of silently falling back to 18765.
    assert "chrome.storage.local.get(['btap_port', 'tmwd_port'])" in source
    assert "stored.btap_port" in source
    assert "stored.tmwd_port" in source
    assert "chrome.storage.onChanged.addListener" in source
    assert "new WebSocket(WS_URL)" in source
    assert "HTTP_PROBE = `http://127.0.0.1:${bridgePort + 1}/link`" in source


def test_extension_load_bridge_port_handles_precedence_migration_and_storage_failures():
    """Exercise the real bridge-port loader instead of only checking its text.

    The loader runs while the service worker is cold, so an unnecessary second
    storage round trip is user-visible on every reconnect.  Its migration write
    is also deliberately retryable: losing that write must not make the next
    worker silently fall back to the default port.
    """
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('const DEFAULT_BRIDGE_PORT');
const end = source.indexOf('\\nconst bridgeConfigReady = loadBridgePort()', start);
if (start < 0 || end < 0) throw new Error('bridge port loader not found');
let stored = {{}};
let getCalls = 0;
let setCalls = 0;
let removeCalls = 0;
let failGet = false;
let failSet = false;
const logs = [];
console.log = (...args) => logs.push({{ level: 'log', text: String(args[0]) }});
console.error = (...args) => logs.push({{ level: 'error', text: String(args[0]) }});
const chrome = {{ storage: {{ local: {{
  get(keys) {{
    getCalls += 1;
    if (failGet) return Promise.reject(new Error('storage unavailable'));
    const result = {{}};
    for (const key of keys) {{
      if (Object.prototype.hasOwnProperty.call(stored, key)) result[key] = stored[key];
    }}
    return Promise.resolve(result);
  }},
  set(items) {{
    setCalls += 1;
    if (failSet) return Promise.reject(new Error('write unavailable'));
    Object.assign(stored, items);
    return Promise.resolve();
  }},
  remove(key) {{
    removeCalls += 1;
    delete stored[key];
    return Promise.resolve();
  }},
}} }} }};
eval(source.slice(start, end) + '\\nglobalThis.__loadBridgePort = loadBridgePort;'
  + '\\nglobalThis.__bridgePortState = () => ({{ bridgePort, WS_URL, HTTP_PROBE }});');

function reset(nextStored, {{ readFails = false, writeFails = false }} = {{}}) {{
  stored = {{ ...nextStored }};
  getCalls = 0;
  setCalls = 0;
  removeCalls = 0;
  failGet = readFails;
  failSet = writeFails;
  logs.length = 0;
}}

async function run(nextStored, options) {{
  reset(nextStored, options);
  await __loadBridgePort();
  return {{
    state: __bridgePortState(),
    stored: {{ ...stored }},
    getCalls,
    setCalls,
    removeCalls,
    logs: logs.slice(),
  }};
}}

(async () => {{
  const currentWins = await run({{ btap_port: 19001, tmwd_port: 19002 }});
  const legacyMigrates = await run({{ tmwd_port: 19002 }});
  const noValueUsesDefault = await run({{}});
  const readFailureUsesDefault = await run({{}}, {{ readFails: true }});
  const firstWriteFails = await run({{ tmwd_port: 19003 }}, {{ writeFails: true }});
  failSet = false;
  logs.length = 0;
  await __loadBridgePort();
  const retryAfterWriteFailure = {{
    state: __bridgePortState(),
    stored: {{ ...stored }},
    getCalls,
    setCalls,
    removeCalls,
    logs: logs.slice(),
  }};
  process.stdout.write(JSON.stringify({{
    currentWins,
    legacyMigrates,
    noValueUsesDefault,
    readFailureUsesDefault,
    firstWriteFails,
    retryAfterWriteFailure,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)

    assert outcome["currentWins"]["state"] == {
        "bridgePort": 19001,
        "WS_URL": "ws://127.0.0.1:19001",
        "HTTP_PROBE": "http://127.0.0.1:19002/link",
    }
    assert outcome["currentWins"]["getCalls"] == 1
    assert outcome["currentWins"]["setCalls"] == 0
    assert outcome["currentWins"]["removeCalls"] == 0

    assert outcome["legacyMigrates"]["state"]["bridgePort"] == 19002
    assert outcome["legacyMigrates"]["stored"] == {"btap_port": 19002}
    assert outcome["legacyMigrates"]["getCalls"] == 1
    assert outcome["legacyMigrates"]["setCalls"] == 1
    assert outcome["legacyMigrates"]["removeCalls"] == 1

    assert outcome["noValueUsesDefault"]["state"]["bridgePort"] == 18765
    assert outcome["noValueUsesDefault"]["getCalls"] == 1
    assert outcome["noValueUsesDefault"]["setCalls"] == 0

    assert outcome["readFailureUsesDefault"]["state"]["bridgePort"] == 18765
    assert outcome["readFailureUsesDefault"]["getCalls"] == 1
    assert outcome["readFailureUsesDefault"]["logs"] == [
        {
            "level": "error",
            "text": "[BTAP-WS] bridge port storage unavailable, using default",
        }
    ]

    assert outcome["firstWriteFails"]["state"]["bridgePort"] == 19003
    assert outcome["firstWriteFails"]["stored"] == {"tmwd_port": 19003}
    assert outcome["firstWriteFails"]["getCalls"] == 1
    assert outcome["firstWriteFails"]["setCalls"] == 1
    assert outcome["firstWriteFails"]["removeCalls"] == 0

    retry = outcome["retryAfterWriteFailure"]
    assert retry["state"]["bridgePort"] == 19003
    assert retry["stored"] == {"btap_port": 19003}
    assert retry["getCalls"] == 2
    assert retry["setCalls"] == 2
    assert retry["removeCalls"] == 1


def test_extension_reports_the_build_it_is_actually_running():
    """A build string typed by hand cannot go stale, so it misleads for free.

    `bridge_status.version` was a literal from the day it was written. It read
    the same through every release while `extension_version` beside it moved,
    so the one field a reader reaches for when asking whether an extension is
    stale was a constant -- and it cost a diagnosis. Both sites now read the
    manifest, which is also the only value a reload can change.
    """
    source = BACKGROUND.read_text(encoding="utf-8")
    # Counted over code rather than the whole file. The header comment explains why
    # the build stamp exists and names this API to do it, and a claim about call
    # sites must not be breakable by a sentence about call sites.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )

    assert "pass2-final" not in source
    assert code.count("chrome.runtime.getManifest().version") == 2
    # The unknown_cmd reply is what a version skew actually produces, so it is
    # the worst place of the two for a frozen string. `extensionVersion` is
    # scoped to the bridge_status branch, hence the second manifest read.
    unknown = source.split("code: 'unknown_cmd'", 1)[1]
    assert "version: chrome.runtime.getManifest().version," in unknown


def test_save_pdf_accepts_real_extension_ws_payload(tmp_path, monkeypatch):
    pdf_bytes = b"%PDF-1.7\nBTAP regression fixture\n%%EOF\n"
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    driver = _Driver([{"data": {"data": encoded}}])
    _install(monkeypatch, driver)
    # Redirect Downloads/browsertap to tmp_path for testing
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    downloads_browsertap = fake_home / "Downloads" / "browsertap"
    downloads_browsertap.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    target_relative = "capture.pdf"

    result = S.save_pdf(target_relative, session_id="chrome:profile:7")

    assert result["status"] == "success"
    assert result["size"] == len(pdf_bytes)
    assert (downloads_browsertap / "capture.pdf").read_bytes() == pdf_bytes
    assert driver.calls[0][0]["method"] == "Page.printToPDF"


def test_access_side_effect_tabs_are_flagged(monkeypatch):
    driver = _Driver()
    _install(
        monkeypatch,
        driver,
        _sessions(url="https://login.cloudflareaccess.com/cdn-cgi/access/verify-code/x"),
    )
    tab = S.list_tabs()["tabs"][0]
    assert tab["automation_attention"] == "authentication_required"
    assert "hint" in tab


def test_background_declares_bounded_debugger_command_and_forced_invalidation():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "sendDebuggerCommandWithTimeout" in source
    assert "forceInvalidateDebuggerAttachment" in source
    handle_cdp = source[
        source.index("async function handleCDP") : source.index(
            "// Filter out chrome://", source.index("async function handleCDP")
        )
    ]
    assert "sendDebuggerCommandWithTimeout" in handle_cdp
    assert "chrome.debugger.sendCommand" not in handle_cdp


def test_debugger_watchdog_detaches_clears_lease_and_allows_reattach():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveCommands = false;
const chrome = {{ debugger: {{
  attach() {{ attachCalls += 1; return Promise.resolve(); }},
  detach() {{ detachCalls += 1; return Promise.resolve(); }},
  sendCommand() {{
    return resolveCommands ? Promise.resolve({{ value: 2 }}) : new Promise(() => {{}});
  }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const first = await attachBtapDebugger({{ tabId: 42 }});
  let failure = null;
  try {{
    await sendDebuggerCommandWithTimeout(first, 'Runtime.evaluate', {{}}, 10);
  }} catch (error) {{
    failure = {{ code: error.code, timeoutMs: error.timeoutMs }};
  }}
  const afterTimeout = {{
    detachCalls,
    tracked: debuggerAttachments.size,
    pending: first.attachment.pendingCommands.size,
  }};
  resolveCommands = true;
  const second = await attachBtapDebugger({{ tabId: 42 }});
  const result = await sendDebuggerCommandWithTimeout(
    second, 'Runtime.evaluate', {{}}, 100,
  );
  await detachBtapDebugger(second);
  process.stdout.write(JSON.stringify({{
    failure, afterTimeout, attachCalls, detachCalls, result,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["failure"] == {"code": "cdp_timeout", "timeoutMs": 100}
    assert outcome["afterTimeout"] == {
        "detachCalls": 1,
        "tracked": 0,
        "pending": 0,
    }
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2
    assert outcome["result"] == {"value": 2}


def test_manual_dialog_reuses_the_execution_lease_without_reenabling_page():
    """Handling a paused manual evaluation must not queue a second Page.enable."""
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('async function handleProtocolDialog');
const end = source.indexOf('\\n\\nfunction classifyNavigationOutcome', start);
if (start < 0 || end < 0) throw new Error('dialog handler not found');
const pendingManualExecutions = new Map();
const pendingNavigations = new Map();
const protocolDialogStates = new Map([[42, {{ type: 'confirm', message: 'Continue?' }}]]);
const lease = {{ released: false, attachment: {{ invalidated: false }} }};
pendingManualExecutions.set(42, {{ debuggerLease: lease, released: false }});
let attachCalls = 0;
let detachCalls = 0;
const commands = [];
function validDialogPolicy(action) {{
  return action === 'dismiss' || action === 'accept' || action === 'manual';
}}
function currentProtocolDialog(tabId) {{ return protocolDialogStates.get(tabId) || null; }}
function boundedCdpTimeout(value, fallback) {{ return Number(value) || fallback; }}
async function attachBtapDebugger() {{ attachCalls += 1; return lease; }}
async function detachBtapDebugger() {{ detachCalls += 1; }}
async function sendDebuggerCommandWithTimeout(_lease, method) {{
  commands.push(method);
  return {{}};
}}
eval(source.slice(start, end));
(async () => {{
  const result = await handleProtocolDialog({{ tabId: 42, action: 'dismiss' }});
  process.stdout.write(JSON.stringify({{ result, attachCalls, detachCalls, commands }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["attachCalls"] == 0
    assert outcome["detachCalls"] == 0
    assert outcome["commands"] == ["Page.handleJavaScriptDialog"]
    assert outcome["result"]["ok"] is True


# The two tests below hand `attachBtapDebugger` a single-digit-millisecond
# budget, and the code re-reads `Date.now()` between fixing the deadline and
# arming the watchdog. Real milliseconds spent starting Node or resolving the
# target could blow the budget before the watchdog existed, so the caller failed
# out of `debuggerAttachRemainingMs` instead -- a different path carrying the
# same `cdp_timeout` code, which is why the failure read as unrelated. Measured
# under 16-way load before this was frozen: 3 of 120 runs for the first test and
# 6 of 200 for the second, and one of them was the Windows/3.13 CI failure on
# 0027e84.
#
# Freezing the clock the extension code reads leaves the watchdog as the only
# thing that can fire, on a real `setTimeout` that the frozen deadline sizes
# exactly. Timers still run on the real clock; only the deadline arithmetic is
# pinned. The harness keeps `RealDate` for its own elapsed-time measurement,
# which would otherwise read a constant 0 and assert nothing.
_FROZEN_ATTACH_CLOCK_JS = """const RealDate = globalThis.Date;
const FROZEN_NOW = RealDate.now();
class Date extends RealDate {
  static now() { return FROZEN_NOW; }
}"""


def test_debugger_attach_watchdog_cleans_late_attach_and_allows_reattach():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveFirstAttach;
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    if (attachCalls === 1) {{
      return new Promise(resolve => {{ resolveFirstAttach = resolve; }});
    }}
    return Promise.resolve();
  }},
  detach() {{ detachCalls += 1; return Promise.resolve(); }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
{_FROZEN_ATTACH_CLOCK_JS}
eval(source.slice(start, end));
(async () => {{
  let failure = null;
  try {{
    await attachBtapDebugger({{ tabId: 43 }}, 10);
  }} catch (error) {{
    failure = {{
      code: error.code,
      timeoutMs: error.timeoutMs,
      message: error.message,
    }};
  }}
  const afterTimeout = {{
    tracked: debuggerAttachments.size,
    detachCalls,
  }};
  resolveFirstAttach();
  await new Promise(resolve => setTimeout(resolve, 25));
  const afterLateAttach = {{
    tracked: debuggerAttachments.size,
    detachCalls,
  }};
  const second = await attachBtapDebugger({{ tabId: 43 }}, 100);
  await detachBtapDebugger(second);
  process.stdout.write(JSON.stringify({{
    failure, afterTimeout, afterLateAttach, attachCalls, detachCalls,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["failure"]["code"] == "cdp_timeout"
    assert outcome["failure"]["timeoutMs"] == 10
    assert "debugger attach" in outcome["failure"]["message"]
    assert outcome["afterTimeout"] == {"tracked": 0, "detachCalls": 0}
    assert outcome["afterLateAttach"] == {"tracked": 0, "detachCalls": 1}
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2


def test_debugger_attach_timeout_rejects_all_shared_waiters():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveFirstAttach;
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    if (attachCalls === 1) {{
      return new Promise(resolve => {{ resolveFirstAttach = resolve; }});
    }}
    return Promise.resolve();
  }},
  detach() {{ detachCalls += 1; return Promise.resolve(); }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
{_FROZEN_ATTACH_CLOCK_JS}
eval(source.slice(start, end));
(async () => {{
  const startedAt = RealDate.now();
  const firstPromise = attachBtapDebugger({{ tabId: 46 }}, 20);
  const secondPromise = attachBtapDebugger({{ tabId: 46 }}, 1000);
  const settled = await Promise.allSettled([firstPromise, secondPromise]);
  const elapsedMs = RealDate.now() - startedAt;
  const failures = settled.map(result => ({{
    status: result.status,
    code: result.reason?.code,
    timeoutMs: result.reason?.timeoutMs,
  }}));
  const afterTimeout = {{
    attachCalls,
    detachCalls,
    tracked: debuggerAttachments.size,
  }};
  resolveFirstAttach();
  await new Promise(resolve => setTimeout(resolve, 25));
  const third = await attachBtapDebugger({{ tabId: 46 }}, 100);
  await detachBtapDebugger(third);
  process.stdout.write(JSON.stringify({{
    failures, elapsedMs, afterTimeout, attachCalls, detachCalls,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["failures"] == [
        {"status": "rejected", "code": "cdp_timeout", "timeoutMs": 20},
        {"status": "rejected", "code": "cdp_timeout", "timeoutMs": 20},
    ]
    # The `failures` list above is the contract (each attach rejected at its
    # own 20ms timeout). This ceiling only proves the code did not fall back
    # to the multi-second default, so it is loose: a 500ms bound measured
    # inside a loaded Node process is close enough to trip on noise.
    assert outcome["elapsedMs"] < 2000
    assert outcome["afterTimeout"] == {
        "attachCalls": 1,
        "detachCalls": 0,
        "tracked": 0,
    }
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2


def test_abandoned_attach_promise_never_becomes_an_unhandled_rejection():
    """A caller can leave between creating the shared attach promise and racing it.

    `debuggerAttachRemainingMs` throws when the deadline is already gone, and it
    is checked once more after that promise exists -- so a budget spent
    resolving the target takes the promise's only reader with it. Chrome's
    attach then completes into a promise nobody reads, and the late-completion
    branch rejects it deliberately, to mark an abandoned lease. With no reader
    that rejection surfaces on `chrome://extensions` as an uncaught error for an
    install that is working: the same reader-facing damage as a mis-filed
    `No SW` (AGENTS.md section 8), and it was reached by an ordinary short
    budget, not by anything exotic.

    The clock is advanced inside `getTargets` rather than by sleeping, because
    the window under test is one microtask wide. Racing a real 20ms against it
    is what made two neighbouring tests flaky in CI to begin with.
    """
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const unhandled = [];
process.on('unhandledRejection', error => {{
  unhandled.push({{ code: error?.code, timeoutMs: error?.timeoutMs }});
}});
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveAttach;
const RealDate = globalThis.Date;
const BASE_NOW = RealDate.now();
let nowOffset = 0;
class Date extends RealDate {{
  static now() {{ return BASE_NOW + nowOffset; }}
}}
const chrome = {{ debugger: {{
  getTargets() {{
    // Spend the whole budget here: the deadline is fixed before this stage and
    // re-checked after it, which is the one-microtask window being tested.
    nowOffset = 5000;
    return Promise.resolve([{{ id: 'T1', type: 'page', tabId: 51 }}]);
  }},
  attach() {{
    attachCalls += 1;
    return new Promise(resolve => {{ resolveAttach = resolve; }});
  }},
  detach() {{ detachCalls += 1; return Promise.resolve(); }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  let failure = null;
  try {{
    await attachBtapDebugger({{ targetId: 'T1' }}, 20);
  }} catch (error) {{
    failure = {{
      code: error.code,
      timeoutMs: error.timeoutMs,
      message: error.message,
    }};
  }}
  const afterThrow = {{
    tracked: debuggerAttachments.size, attachCalls, detachCalls,
  }};
  resolveAttach();
  // Node reports an unhandled rejection once the microtask queue drains, so
  // yielding ticks is the observation; no wall-clock delay is involved.
  for (let i = 0; i < 3; i += 1) await new Promise(resolve => setTimeout(resolve, 0));
  process.stdout.write(JSON.stringify({{
    failure,
    afterThrow,
    afterLateAttach: {{ tracked: debuggerAttachments.size, detachCalls }},
    unhandled,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    # The caller still gets the honest bounded failure, and it names the stage.
    assert outcome["failure"]["code"] == "cdp_timeout"
    assert outcome["failure"]["timeoutMs"] == 20
    assert "calling chrome.debugger.attach" in outcome["failure"]["message"]
    # Leaving early still cleans up: the entry is dropped on the way out, and
    # the attach that lands afterwards is detached rather than left owning the
    # target.
    assert outcome["afterThrow"]["tracked"] == 0
    assert outcome["afterLateAttach"] == {"tracked": 0, "detachCalls": 1}
    # The point of the test. Dropping the terminal `.catch` on `attachPromise`
    # in `background.js` puts one `cdp_timeout` entry here instead.
    assert outcome["unhandled"] == []


@pytest.mark.parametrize("pending_stage", ["detach", "reattach"])
def test_debugger_conflict_recovery_stages_obey_attach_deadline(pending_stage):
    detach_impl = (
        "detachCalls += 1; return new Promise(() => {});"
        if pending_stage == "detach"
        else "detachCalls += 1; return Promise.resolve();"
    )
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    if (attachCalls === 1) {{
      return Promise.reject(new Error('Another debugger is already attached'));
    }}
    return new Promise(() => {{}});
  }},
  detach() {{ {detach_impl} }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const startedAt = Date.now();
  let failure = null;
  try {{
    await attachBtapDebugger({{ tabId: 48 }}, 20);
  }} catch (error) {{
    failure = {{ code: error.code, timeoutMs: error.timeoutMs }};
  }}
  process.stdout.write(JSON.stringify({{
    failure,
    elapsedMs: Date.now() - startedAt,
    attachCalls,
    detachCalls,
    trackedAttachments: debuggerAttachments.size,
    trackedRecoveries: debuggerRecoveryPromises.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["failure"] == {"code": "cdp_timeout", "timeoutMs": 20}
    # The `failures` list above is the contract (each attach rejected at its
    # own 20ms timeout). This ceiling only proves the code did not fall back
    # to the multi-second default, so it is loose: a 500ms bound measured
    # inside a loaded Node process is close enough to trip on noise.
    assert outcome["elapsedMs"] < 2000
    assert outcome["attachCalls"] == (1 if pending_stage == "detach" else 2)
    assert outcome["detachCalls"] == 1
    assert outcome["trackedAttachments"] == 0
    assert outcome["trackedRecoveries"] == 0


def test_debugger_recovery_timeout_rejects_long_and_short_waiters_together():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveRecoveryDetach;
let signalRecoveryDetach;
const recoveryDetachStarted = new Promise(resolve => {{ signalRecoveryDetach = resolve; }});
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    return Promise.reject(new Error('Another debugger is already attached'));
  }},
  detach() {{
    detachCalls += 1;
    signalRecoveryDetach();
    return new Promise(resolve => {{ resolveRecoveryDetach = resolve; }});
  }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const longWaiter = attachBtapDebugger({{ tabId: 49 }}, 1000);
  await recoveryDetachStarted;
  const startedAt = Date.now();
  const shortWaiter = attachBtapDebugger({{ tabId: 49 }}, 20);
  const settled = await Promise.allSettled([longWaiter, shortWaiter]);
  const stateAtTimeout = {{
    elapsedMs: Date.now() - startedAt,
    failures: settled.map(result => ({{
      status: result.status,
      code: result.reason?.code,
      timeoutMs: result.reason?.timeoutMs,
    }})),
    attachCalls,
    detachCalls,
    trackedAttachments: debuggerAttachments.size,
    trackedRecoveries: debuggerRecoveryPromises.size,
  }};
  resolveRecoveryDetach();
  await new Promise(resolve => setTimeout(resolve, 25));
  process.stdout.write(JSON.stringify({{
    ...stateAtTimeout,
    trackedAttachmentsAfterRecovery: debuggerAttachments.size,
    trackedRecoveriesAfterRecovery: debuggerRecoveryPromises.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    # The `failures` list above is the contract (each attach rejected at its
    # own 20ms timeout). This ceiling only proves the code did not fall back
    # to the multi-second default, so it is loose: a 500ms bound measured
    # inside a loaded Node process is close enough to trip on noise.
    assert outcome["elapsedMs"] < 2000
    assert outcome["failures"] == [
        {"status": "rejected", "code": "cdp_timeout", "timeoutMs": 20},
        {"status": "rejected", "code": "cdp_timeout", "timeoutMs": 20},
    ]
    assert outcome["attachCalls"] == 1
    assert outcome["detachCalls"] == 1
    assert outcome["trackedAttachments"] == 0
    assert outcome["trackedRecoveries"] == 0
    assert outcome["trackedAttachmentsAfterRecovery"] == 0
    assert outcome["trackedRecoveriesAfterRecovery"] == 0


def test_late_programmatic_detach_event_does_not_invalidate_replacement_lease():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
const manualExecutionGenerations = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveFirstDetach;
const chrome = {{ debugger: {{
  attach() {{ attachCalls += 1; return Promise.resolve(); }},
  detach() {{
    detachCalls += 1;
    if (detachCalls === 1) {{
      return new Promise(resolve => {{ resolveFirstDetach = resolve; }});
    }}
    return Promise.resolve();
  }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const first = await attachBtapDebugger({{ tabId: 47 }});
  const invalidation = forceInvalidateDebuggerAttachment(
    first.attachment, 'test forced invalidation',
  );
  while (detachCalls < 1) await Promise.resolve();
  const secondPromise = attachBtapDebugger({{ tabId: 47 }}, 1000);
  await Promise.resolve();
  const beforeDetachResolution = {{
    attachCalls,
    tracked: debuggerAttachments.size,
  }};
  resolveFirstDetach();
  await invalidation;
  const second = await secondPromise;
  handleDebuggerDetach({{ tabId: 47 }});
  const afterLateEvent = {{
    attachCalls,
    tracked: debuggerAttachments.size,
    currentIsReplacement: debuggerAttachments.get('tab:47') === second.attachment,
    attached: second.attachment.attached,
    invalidated: second.attachment.invalidated,
    refs: second.attachment.refs,
  }};
  await detachBtapDebugger(second);
  process.stdout.write(JSON.stringify({{
    beforeDetachResolution,
    afterLateEvent,
    detachCalls,
    trackedAfterCleanup: debuggerAttachments.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["beforeDetachResolution"] == {"attachCalls": 1, "tracked": 1}
    assert outcome["afterLateEvent"] == {
        "attachCalls": 2,
        "tracked": 1,
        "currentIsReplacement": True,
        "attached": True,
        "invalidated": False,
        "refs": 1,
    }
    assert outcome["detachCalls"] == 2
    assert outcome["trackedAfterCleanup"] == 0


def test_debugger_conflict_recovery_preserves_concurrent_lease_refs():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
let resolveRecoveryDetach;
let signalRecoveryDetach;
const recoveryDetachStarted = new Promise(resolve => {{ signalRecoveryDetach = resolve; }});
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    if (attachCalls === 1) return Promise.reject(new Error('Another debugger is already attached'));
    return Promise.resolve();
  }},
  detach() {{
    detachCalls += 1;
    if (detachCalls === 1) {{
      signalRecoveryDetach();
      return new Promise(resolve => {{ resolveRecoveryDetach = resolve; }});
    }}
    return Promise.resolve();
  }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const firstPromise = attachBtapDebugger({{ tabId: 42 }});
  const secondPromise = attachBtapDebugger({{ tabId: 42 }});
  await recoveryDetachStarted;
  const thirdPromise = attachBtapDebugger({{ tabId: 42 }});
  resolveRecoveryDetach();
  const [first, second, third] = await Promise.all([
    firstPromise, secondPromise, thirdPromise,
  ]);
  // chrome.debugger.onDetach may arrive after the recovery attach succeeds.
  handleDebuggerDetach({{ tabId: 42 }});
  const afterRecovery = {{
    refs: first.attachment.refs,
    attached: first.attachment.attached,
    tracked: debuggerAttachments.size,
    sameAttachment: first.attachment === second.attachment && second.attachment === third.attachment,
  }};
  await detachBtapDebugger(first);
  const afterFirstRelease = {{
    refs: third.attachment.refs,
    attached: third.attachment.attached,
    tracked: debuggerAttachments.size,
    detachCalls,
  }};
  await detachBtapDebugger(second);
  const afterSecondRelease = {{
    refs: third.attachment.refs,
    attached: third.attachment.attached,
    tracked: debuggerAttachments.size,
    detachCalls,
  }};
  await detachBtapDebugger(third);
  process.stdout.write(JSON.stringify({{
    attachCalls,
    detachCalls,
    afterRecovery,
    afterFirstRelease,
    afterSecondRelease,
    trackedAfterAll: debuggerAttachments.size,
    recoveriesAfterAll: debuggerRecoveryPromises.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["afterRecovery"] == {
        "refs": 3,
        "attached": True,
        "tracked": 1,
        "sameAttachment": True,
    }
    assert outcome["afterFirstRelease"] == {
        "refs": 2,
        "attached": True,
        "tracked": 1,
        "detachCalls": 1,
    }
    assert outcome["afterSecondRelease"] == {
        "refs": 1,
        "attached": True,
        "tracked": 1,
        "detachCalls": 1,
    }
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2
    assert outcome["trackedAfterAll"] == 0
    assert outcome["recoveriesAfterAll"] == 0


def test_debugger_target_aliases_share_one_lease_and_target_only_detach_invalidates_it():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map([[42, {{ open: true }}]]);
const dialogEventSequences = new Map([[42, 1]]);
const runtimeExecutionContexts = new Map([[42, new Map()]]);
const execDialogPolicies = new Map([[42, new Map()]]);
const manualExecutionGenerations = new Map([[42, 1]]);
let cancelledNavigation = 0;
let cancelledManual = 0;
function cancelNavigationPending() {{ cancelledNavigation += 1; }}
function cancelManualExecution() {{ cancelledManual += 1; }}
let attachCalls = 0;
let detachCalls = 0;
const attachedTargets = [];
const chrome = {{ debugger: {{
  getTargets() {{ return Promise.resolve([{{
    id: 'target-42', tabId: 42, extensionId: 'extension-42', type: 'page',
  }}]); }},
  attach(target) {{ attachCalls += 1; attachedTargets.push(target); return Promise.resolve(); }},
  detach() {{ detachCalls += 1; return Promise.resolve(); }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  const byTarget = await attachBtapDebugger({{ targetId: 'target-42' }});
  const byTab = await attachBtapDebugger({{ tabId: 42 }});
  const byExtension = await attachBtapDebugger({{ extensionId: 'extension-42' }});
  const beforeDetach = {{
    attachCalls,
    tracked: debuggerAttachments.size,
    refs: byTarget.attachment.refs,
    same: byTarget.attachment === byTab.attachment && byTab.attachment === byExtension.attachment,
    aliases: [...byTarget.attachment.aliases].sort(),
    attachedTargets,
  }};
  handleDebuggerDetach({{ targetId: 'target-42' }});
  process.stdout.write(JSON.stringify({{
    beforeDetach,
    trackedAfterDetach: debuggerAttachments.size,
    invalidated: byTarget.attachment.invalidated,
    refsAfterDetach: byTarget.attachment.refs,
    dialogTracked: dialogAttachedTabs.has(42),
    contextTracked: runtimeExecutionContexts.has(42),
    generationTracked: manualExecutionGenerations.has(42),
    cancelledNavigation,
    cancelledManual,
    detachCalls,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5, check=False
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["beforeDetach"] == {
        "attachCalls": 1,
        "tracked": 1,
        "refs": 3,
        "same": True,
        "aliases": ["extension:extension-42", "tab:42", "target:target-42"],
        "attachedTargets": [{"tabId": 42}],
    }
    assert outcome["trackedAfterDetach"] == 0
    assert outcome["invalidated"] is True
    assert outcome["refsAfterDetach"] == 0
    assert outcome["dialogTracked"] is False
    assert outcome["contextTracked"] is False
    assert outcome["generationTracked"] is False
    assert outcome["cancelledNavigation"] == 1
    assert outcome["cancelledManual"] == 1
    assert outcome["detachCalls"] == 0


def test_cdp_command_external_debugger_conflict_preserves_error_and_cleans_state():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nasync function handleProtocolDialog', start);
if (start < 0 || end < 0) throw new Error('debugger helpers not found');
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
const chrome = {{ debugger: {{
  attach() {{
    attachCalls += 1;
    return Promise.reject(new Error('Another debugger is already attached'));
  }},
  detach() {{
    detachCalls += 1;
    return Promise.reject(new Error('Debugger is not attached to the tab'));
  }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  let failure = null;
  try {{
    await attachBtapDebugger({{ tabId: 42 }});
  }} catch (error) {{
    failure = {{ message: error.message, code: debuggerFailureCode(error) }};
  }}
  process.stdout.write(JSON.stringify({{
    failure,
    attachCalls,
    detachCalls,
    tracked: debuggerAttachments.size,
    recoveries: debuggerRecoveryPromises.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["failure"] == {
        "message": "Another debugger is already attached",
        "code": "debugger_conflict",
    }
    assert outcome["attachCalls"] == 1
    assert outcome["detachCalls"] == 1
    assert outcome["tracked"] == 0
    assert outcome["recoveries"] == 0


def _cdp_exec_fallback_source() -> str:
    source = BACKGROUND.read_text(encoding="utf-8")
    # Start one declaration earlier than the classifier: the fallback now clamps
    # the caller's forwarded budget through boundedCdpTimeout, which closes over
    # MAX_CDP_TIMEOUT_MS. Both are lifted from the real file rather than restated
    # here, so a changed bound cannot leave this harness testing an old one.
    classify_start = source.index("const MAX_CDP_TIMEOUT_MS")
    classify_end = source.index("\n\nfunction clearDebuggerTabState", classify_start)
    fallback_start = source.index("async function runCdpExecFallback(")
    fallback_end = source.index("\n\nasync function navigateWithDialogPolicy", fallback_start)
    return source[classify_start:classify_end] + "\n" + source[fallback_start:fallback_end]


def _cdp_fallback_outcome(*, attach_js, evaluate_js=None):
    """Drive the real runCdpExecFallback with a scripted debugger."""
    evaluate_js = evaluate_js or "return { result: { value: { ok: true, data: 'ran' } } };"
    script = f"""
console.log = () => {{}};
const DEFAULT_CDP_TIMEOUT_MS = 5000;
let attachCalls = 0;
let evaluateCalls = 0;
let detachCalls = 0;
async function attachBtapDebugger({{ tabId }}) {{
  attachCalls += 1;
  {attach_js}
}}
async function sendDebuggerCommandWithTimeout() {{
  evaluateCalls += 1;
  {evaluate_js}
}}
async function detachBtapDebugger() {{ detachCalls += 1; }}
{_cdp_exec_fallback_source()}
(async () => {{
  const res = await runCdpExecFallback(11, 'return 1');
  process.stdout.write(JSON.stringify({{ res, attachCalls, evaluateCalls, detachCalls }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    return _run_node_script(script)


def test_cdp_exec_fallback_retries_an_attach_that_never_dispatched():
    """A CSP page's fallback used to die on the first attach failure.

    Attaching races anything else that touches the debugger (another BTAP command
    finishing, DevTools closing), and a lost race there means the caller's script
    never ran at all -- so retrying is safe and fixes a whole class of one-off
    'CDP fallback failed' returns.
    """
    outcome = _cdp_fallback_outcome(
        attach_js="""
  if (attachCalls === 1) throw new Error('Another debugger is already attached');
  return { tabId };
""",
    )
    assert outcome["attachCalls"] == 2
    assert outcome["evaluateCalls"] == 1
    assert outcome["res"] == {"ok": True, "data": "ran"}


def test_cdp_exec_fallback_never_reruns_a_script_that_may_have_executed():
    """The retry stops at the attach stage on purpose.

    'Detached while handling command' arrives *after* Runtime.evaluate went out,
    so the caller's JS may already have run -- and by then the tab has committed a
    new document, so a retry would run it a second time somewhere else. Report
    instead, and say why in the message.
    """
    outcome = _cdp_fallback_outcome(
        attach_js="return { tabId };",
        evaluate_js="throw new Error('Detached while handling command');",
    )
    assert outcome["attachCalls"] == 1
    assert outcome["evaluateCalls"] == 1
    assert outcome["res"]["error"]["code"] == "debugger_detached"
    assert outcome["res"]["error"]["dispatched"] is True
    assert "may already have executed" in outcome["res"]["error"]["message"]


def test_cdp_exec_fallback_reports_a_second_attach_failure_with_its_code():
    outcome = _cdp_fallback_outcome(
        attach_js="throw new Error('Another debugger is already attached');",
    )
    assert outcome["attachCalls"] == 2
    assert outcome["evaluateCalls"] == 0
    assert outcome["detachCalls"] == 0
    assert outcome["res"]["ok"] is False
    assert outcome["res"]["error"]["code"] == "debugger_conflict"
    assert outcome["res"]["error"]["dispatched"] is False


def test_cdp_exec_fallback_does_not_retry_an_error_a_retry_cannot_fix():
    """cdp_error is the bucket for 'this tab cannot be debugged' -- a closed tab,
    a chrome:// URL, a policy block. Retrying only doubles the latency."""
    outcome = _cdp_fallback_outcome(
        attach_js="throw new Error('No tab with given id 11');",
    )
    assert outcome["attachCalls"] == 1
    assert outcome["res"]["error"]["code"] == "cdp_error"
    assert outcome["res"]["error"]["dispatched"] is False


def test_worker_restart_sweep_releases_only_orphaned_debugger_attachments():
    """An eviction mid-command loses debuggerAttachments; Chrome stays attached.

    Without the boot sweep the tab keeps its "is debugging this browser" infobar
    and whatever CDP domains that command enabled, until the browser restarts:
    the reactive debugger_conflict recovery only fires for a tab something
    attaches to again, and nothing ever attaches to an orphan.
    """
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const debuggerStart = source.indexOf('function debuggerTargetKey');
const debuggerEnd = source.indexOf('\\nasync function handleProtocolDialog', debuggerStart);
const sweepStart = source.indexOf('// The same leak, one layer down');
const sweepEnd = source.indexOf('\\nasync function withCspOff', sweepStart);
if (debuggerStart < 0 || debuggerEnd < 0 || sweepStart < 0 || sweepEnd < 0) {{
  throw new Error('debugger helpers or boot sweep not found');
}}
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
const detached = [];
const logs = [];
console.log = (...args) => {{ logs.push(args.map(String).join(' ')); }};
const chrome = {{ debugger: {{
  getTargets() {{
    return Promise.resolve([
      {{ tabId: 11, attached: true }},   // orphan: release
      {{ tabId: 12, attached: false }},  // nothing attached: nothing to do
      {{ tabId: 13, attached: true }},   // a live lease is racing the sweep
      {{ tabId: 14, attached: true }},   // DevTools owns it: detach rejects
      {{ id: 'T-9', attached: true }},   // orphan addressed by targetId
      {{ attached: true }},              // no identifier at all: must not throw
    ]);
  }},
  detach(target) {{
    detached.push(target);
    return target.tabId === 14
      ? Promise.reject(new Error('Cannot access target'))
      : Promise.resolve();
  }},
  attach() {{ return Promise.resolve(); }},
  sendCommand() {{ return Promise.resolve({{}}); }},
}} }};
eval(source.slice(debuggerStart, debuggerEnd));
// A command woke this worker and took a lease on tab 13 before getTargets
// answered. Tearing that down would kill live work.
debuggerAttachments.set('tab:13', {{
  target: {{ tabId: 13 }}, aliases: new Set(['tab:13']), refs: 1, attached: true,
}});
eval(source.slice(sweepStart, sweepEnd));
(async () => {{
  const deadline = Date.now() + 3000;
  while (detached.length < 3 && Date.now() < deadline) {{
    await new Promise(resolve => setTimeout(resolve, 5));
  }}
  await new Promise(resolve => setTimeout(resolve, 20));
  process.stdout.write(JSON.stringify({{
    detached,
    logs,
    liveLeaseRefs: debuggerAttachments.get('tab:13')?.refs ?? null,
    tracked: debuggerAttachments.size,
    markers: (debuggerDetachMarkers.entries || []).length,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    # Order matters: the rejected detach sits between two that must happen, so a
    # sweep that aborted on the first failure would never reach T-9.
    assert outcome["detached"] == [{"tabId": 11}, {"tabId": 14}, {"targetId": "T-9"}]
    assert outcome["logs"] == ["[BTAP] released 2 stale debugger attachment(s)"]
    assert outcome["liveLeaseRefs"] == 1
    assert outcome["tracked"] == 1
    # Two markers for the two detaches that landed; the rejected one takes its own
    # marker back, so a later real detach event cannot be misattributed to it.
    assert outcome["markers"] == 2


def test_debugger_detach_watchdog_is_cleared_after_fast_or_bounded_completion():
    """A detach cap must not leave one live timer per successful cleanup."""
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function debuggerTargetKey');
const end = source.indexOf('\\nfunction consumeDebuggerDetach', start);
if (start < 0 || end < 0) throw new Error('debugger detach helpers not found');
const debuggerAttachments = new Map();
const timers = [];
function setTimeout(callback, delay) {{
  const timer = {{ callback, delay, cleared: false }};
  timers.push(timer);
  return timer;
}}
function clearTimeout(timer) {{ if (timer) timer.cleared = true; }}
let mode = 'fast';
const chrome = {{ debugger: {{
  detach() {{ return mode === 'fast' ? Promise.resolve() : new Promise(() => {{}}); }},
}} }};
eval(source.slice(start, end));
(async () => {{
  await detachDebuggerFromChrome({{ target: {{ tabId: 7 }} }});
  const fast = {{ total: timers.length, active: timers.filter(t => !t.cleared).length }};
  mode = 'bounded';
  const pending = detachDebuggerFromChrome({{ target: {{ tabId: 8 }} }});
  await Promise.resolve();
  const watchdog = timers.find(t => !t.cleared);
  if (!watchdog) throw new Error('detach watchdog was not armed');
  watchdog.callback();
  await pending;
  const bounded = {{
    total: timers.length,
    active: timers.filter(t => !t.cleared).length,
    delay: watchdog.delay,
  }};
  process.stdout.write(JSON.stringify({{ fast, bounded }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)
    assert outcome["fast"] == {"total": 1, "active": 0}
    assert outcome["bounded"] == {"total": 2, "active": 0, "delay": 1000}


def test_csp_relaxed_injection_watchdog_is_cleared_after_success_or_timeout():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('const CSP_RULE_BASE');
const end = source.indexOf('\\n// --- WebSocket client', start);
if (start < 0 || end < 0) throw new Error('CSP helpers not found');
const timers = [];
function setTimeout(callback, delay) {{
  const timer = {{ callback, delay, cleared: false }};
  timers.push(timer);
  return timer;
}}
function clearTimeout(timer) {{ if (timer) timer.cleared = true; }}
const ruleCalls = [];
const chrome = {{ declarativeNetRequest: {{
  getSessionRules() {{ return Promise.resolve([]); }},
  updateSessionRules(details) {{ ruleCalls.push(details); return Promise.resolve(); }},
}} }};
const cspRefs = new Map();
let cspNextRuleId = 90000;
eval(source.slice(start, end));
(async () => {{
  const fast = await withCspOff(4, async () => 'ok');
  const fastTimers = timers.filter(t => !t.cleared).length;
  let failure = null;
  const pending = withCspOff(5, () => new Promise(() => {{}}));
  await new Promise(setImmediate);
  const watchdog = timers.find(t => !t.cleared);
  if (!watchdog) throw new Error('CSP watchdog was not armed');
  watchdog.callback();
  try {{ await pending; }} catch (error) {{ failure = error.message; }}
  process.stdout.write(JSON.stringify({{
    fast, fastTimers,
    failure,
    activeTimers: timers.filter(t => !t.cleared).length,
    timeoutDelay: watchdog.delay,
    ruleCallCount: ruleCalls.length,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)
    assert outcome["fast"] == "ok"
    assert outcome["fastTimers"] == 0
    assert outcome["failure"] == "CSP-relaxed injection timed out"
    assert outcome["activeTimers"] == 0
    assert outcome["timeoutDelay"] == 30000
    assert outcome["ruleCallCount"] >= 2


def test_server_probe_clears_abort_watchdogs_on_each_fetch_outcome():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('async function isServerAlive');
const end = source.indexOf('\\nfunction ensureConnected', start);
if (start < 0 || end < 0) throw new Error('server probe helper not found');
const timers = [];
function setTimeout(callback, delay) {{
  const timer = {{ callback, delay, cleared: false }};
  timers.push(timer);
  return timer;
}}
function clearTimeout(timer) {{ if (timer) timer.cleared = true; }}
let fetchMode = 'primary';
const fetchCalls = [];
function fetch(url) {{
  fetchCalls.push(url);
  if (fetchMode === 'primary') {{
    fetchMode = 'fallback';
    return Promise.reject(new Error('primary unavailable'));
  }}
  return Promise.resolve({{ status: 426 }});
}}
const HTTP_PROBE = 'http://127.0.0.1:18766/link';
let bridgePort = 18765;
eval(source.slice(start, end));
(async () => {{
  const alive = await isServerAlive();
  process.stdout.write(JSON.stringify({{
    alive,
    fetchCalls,
    timers: timers.map(timer => ({{ delay: timer.delay, cleared: timer.cleared }})),
    active: timers.filter(timer => !timer.cleared).length,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    outcome = _run_node_script(script)
    assert outcome["alive"] is True
    assert outcome["fetchCalls"] == [
        "http://127.0.0.1:18766/link",
        "http://127.0.0.1:18765/",
    ]
    assert outcome["timers"] == [
        {"delay": 1500, "cleared": True},
        {"delay": 800, "cleared": True},
    ]
    assert outcome["active"] == 0


def test_extension_client_id_is_minted_once_under_concurrent_callers():
    """The client id is half of every session id the server holds.

    ext_ready and tabs_update both call getClientId, and on a cold worker they
    overlap. Caching only the resolved value lets both miss storage, both mint,
    and both write: the loser keeps announcing an id the profile no longer has,
    so the server ends up with two client namespaces for one browser.
    """
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('function getBrowserType');
const end = source.indexOf('\\nfunction scheduleProbe', start);
if (start < 0 || end < 0) throw new Error('client id helpers not found');
// The degraded path now asks whether the worker went away before it decides
// how loudly to report, and that predicate lives outside this slice. Eval the
// real one rather than stubbing it: a stub would keep passing while the thing
// the extension actually calls was broken.
const goneStart = source.indexOf('function isWorkerGoneError');
const goneEnd = source.indexOf('\\nfunction bridgeStatusMessage', goneStart);
if (goneStart < 0 || goneEnd < 0) throw new Error('isWorkerGoneError not found');
eval(source.slice(goneStart, goneEnd));
let CLIENT_ID = null;
let clientIdPromise = null;
const navigator = {{ userAgent: 'Mozilla/5.0 Chrome/126.0.0.0 Safari/537.36' }};
const errors = [];
console.error = (...args) => {{ errors.push(String(args[0])); }};
const stored = {{}};
let getCalls = 0;
let setCalls = 0;
let getFails = false;
const chrome = {{ storage: {{ local: {{
  get(key) {{
    getCalls += 1;
    if (getFails) return Promise.reject(new Error('storage unavailable'));
    // The await gap is the whole point: the next caller arrives while this one
    // is still inside storage.
    return new Promise(resolve => setTimeout(
      () => resolve(key in stored ? {{ [key]: stored[key] }} : {{}}), 0,
    ));
  }},
  set(items) {{
    setCalls += 1;
    return new Promise(resolve => setTimeout(() => {{
      Object.assign(stored, items);
      resolve();
    }}, 0));
  }},
  remove() {{ return Promise.resolve(); }},
}} }} }};
eval(source.slice(start, end));
(async () => {{
  const cold = await Promise.all(Array.from({{ length: 5 }}, () => getClientId()));
  const afterCold = {{
    ids: [...new Set(cold)], getCalls, setCalls, persisted: stored.btap_client_id,
  }};
  const warm = await Promise.all(Array.from({{ length: 3 }}, () => getClientId()));
  const afterWarm = {{ ids: [...new Set(warm)], getCalls, setCalls }};
  // Same race with storage unavailable: one ephemeral id, not four.
  CLIENT_ID = null;
  clientIdPromise = null;
  getFails = true;
  getCalls = 0;
  setCalls = 0;
  const degraded = await Promise.all(Array.from({{ length: 4 }}, () => getClientId()));
  process.stdout.write(JSON.stringify({{
    afterCold,
    afterWarm,
    degradedIds: [...new Set(degraded)],
    degradedGetCalls: getCalls,
    degradedSetCalls: setCalls,
    errors,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    cold = outcome["afterCold"]
    assert len(cold["ids"]) == 1, cold["ids"]
    assert cold["ids"][0].startswith("chrome_")
    # Current and legacy ids are read in one storage pass total.
    assert cold["getCalls"] == 1
    assert cold["setCalls"] == 1
    assert cold["persisted"] == cold["ids"][0]
    # Resolved value still short-circuits: no storage traffic once it is known.
    assert outcome["afterWarm"] == {
        "ids": cold["ids"],
        "getCalls": 1,
        "setCalls": 1,
    }
    assert len(outcome["degradedIds"]) == 1
    assert outcome["degradedIds"][0] != cold["ids"][0]
    assert outcome["degradedGetCalls"] == 1
    assert outcome["degradedSetCalls"] == 0
    assert outcome["errors"] == [
        "[BTAP-WS] storage unavailable, using ephemeral clientId"
    ]


def test_stopping_network_cancels_only_its_pending_body_watchdog():
    script = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const captureStart = source.indexOf('function boundedCaptureInteger');
const captureEnd = source.indexOf('\\nfunction handleDebuggerEvent', captureStart);
const debuggerStart = source.indexOf('function debuggerTargetKey');
const debuggerEnd = source.indexOf('\\nasync function handleProtocolDialog', debuggerStart);
if (captureStart < 0 || captureEnd < 0 || debuggerStart < 0 || debuggerEnd < 0) {{
  throw new Error('capture/debugger helpers not found');
}}
const networkCaptures = new Map();
const consoleCaptures = new Map();
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const protocolDialogStates = new Map();
const dialogEventSequences = new Map();
const runtimeExecutionContexts = new Map();
const execDialogPolicies = new Map();
let attachCalls = 0;
let detachCalls = 0;
const chrome = {{ debugger: {{
  attach() {{ attachCalls += 1; return Promise.resolve(); }},
  detach() {{
    detachCalls += 1;
    markCaptureTabInvalidated(42, 'debugger detached');
    return Promise.resolve();
  }},
  sendCommand(target, method) {{
    if (method === 'Network.getResponseBody') return new Promise(() => {{}});
    return Promise.resolve({{}});
  }},
}} }};
eval(source.slice(debuggerStart, debuggerEnd));
eval(source.slice(captureStart, captureEnd));
(async () => {{
  await handleNetworkCaptureCommand({{
    method: 'start', tabId: 42, includeBodies: true, bodyTimeoutMs: 100,
  }}, {{}});
  await handleConsoleCaptureCommand({{ method: 'start', tabId: 42 }}, {{}});
  const sharedAttachment = debuggerAttachments.get('tab:42');
  handleNetworkCaptureEvent(42, 'Network.requestWillBeSent', {{
    requestId: 'req-1', request: {{ url: 'https://example.test/', method: 'GET' }},
  }});
  handleNetworkCaptureEvent(42, 'Network.loadingFinished', {{
    requestId: 'req-1', encodedDataLength: 1,
  }});
  const beforeStop = {{
    refs: sharedAttachment.refs,
    pending: sharedAttachment.pendingCommands.size,
  }};
  const networkStop = await handleNetworkCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});
  await new Promise(resolve => setTimeout(resolve, 160));
  const consoleCapture = consoleCaptures.get(42);
  const afterBodyDeadline = {{
    consoleActive: consoleCapture?.active,
    refs: sharedAttachment.refs,
    attached: sharedAttachment.attached,
    tracked: debuggerAttachments.get('tab:42') === sharedAttachment,
    pending: sharedAttachment.pendingCommands.size,
    pendingTimers: [...sharedAttachment.pendingCommands].filter(command => command.timer !== null).length,
    detachCalls,
  }};
  const consoleStop = await handleConsoleCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});
  await handleNetworkCaptureCommand({{
    method: 'start', tabId: 42, includeBodies: false,
  }}, {{}});
  const soloNetworkStop = await handleNetworkCaptureCommand({{ method: 'stop', tabId: 42 }}, {{}});
  process.stdout.write(JSON.stringify({{
    attachCalls,
    detachCalls,
    beforeStop,
    networkStop: networkStop.data.status,
    networkBodyError: networkStop.data.requests[0]?.body_error,
    afterBodyDeadline,
    consoleStop: consoleStop.data.status,
    soloNetworkStop: soloNetworkStop.data,
    trackedAfterAll: debuggerAttachments.size,
    pendingAfterAll: sharedAttachment.pendingCommands.size,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["beforeStop"] == {"refs": 2, "pending": 1}
    assert outcome["networkStop"] == "stopped"
    assert "owning lease released" in outcome["networkBodyError"]
    assert outcome["afterBodyDeadline"] == {
        "consoleActive": True,
        "refs": 1,
        "attached": True,
        "tracked": True,
        "pending": 0,
        "pendingTimers": 0,
        "detachCalls": 0,
    }
    assert outcome["consoleStop"] == "stopped"
    assert outcome["soloNetworkStop"]["status"] == "stopped"
    assert "error" not in outcome["soloNetworkStop"]
    assert outcome["attachCalls"] == 2
    assert outcome["detachCalls"] == 2
    assert outcome["trackedAfterAll"] == 0
    assert outcome["pendingAfterAll"] == 0


# --- Generation snapshots have to survive a service-worker eviction ---------
# Measured on 2026-08-23: an eviction mid-live-run (bridge log, 04:53:01,671
# close / 04:53:01,837 reconnect) left all 15 scriptable tabs holding a
# generation minted in the same millisecond, counters 1..16 from a fresh
# `nextTabGeneration`. Nothing had been restored, everything had been re-minted,
# and the damaged map had been written back as the truth -- so two tabs the live
# suite owned could not be closed again and leaked into a real person's browser.


def test_tab_identity_snapshot_survives_extension_reload_in_local_storage():
    """Logical identity must outlive the worker, including a manual Reload.

    `storage.session` is intentionally used for lifecycle generations, but it
    is cleared by an extension reload. Reading identities from it would make an
    otherwise unchanged tab look unrelated and defeat evidence-backed rebinding.
    """
    outcome = _run_identity_harness(
        """
const localStored = {btapTabIdentitiesV1: {'7': 'tab-persisted'}};
const sessionStored = {btapTabIdentitiesV1: {'7': 'tab-wrong-session'}};
const calls = [];
const chrome = {
  storage: {
    local: {
      get: async key => { calls.push(['local.get', key]); return {[key]: localStored[key]}; },
      set: async value => { calls.push(['local.set', value]); Object.assign(localStored, value); },
    },
    session: {
      get: async key => { calls.push(['session.get', key]); return {[key]: sessionStored[key]}; },
      set: async value => { calls.push(['session.set', value]); Object.assign(sessionStored, value); },
    },
  },
  tabs: {query: async () => [{id: 7}]},
};
__SOURCE__
(async () => {
  const identity = await tabIdentityFor(7);
  process.stdout.write(JSON.stringify({identity, localStored, sessionStored, calls, logs}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    )

    assert outcome["identity"] == "tab-persisted"
    assert outcome["localStored"]["btapTabIdentitiesV1"]["7"] == "tab-persisted"
    assert outcome["sessionStored"]["btapTabIdentitiesV1"]["7"] == "tab-wrong-session"
    assert [call[0] for call in outcome["calls"]] == ["local.get", "local.set"]

_GENERATION_TAIL = """
(async () => {
  const out = await scenario();
  process.stdout.write(JSON.stringify({
    ...out,
    logs,
    stored,
    setCalls,
    getCalls,
    loaded: tabGenerationsLoaded,
    failures: tabGenerationLoadFailures,
    provisional: [...provisionalTabGenerations],
  }));
})().catch(error => { console.error(error); process.exit(1); });
"""


def test_a_readable_generation_snapshot_is_restored_rather_than_re_minted():
    """The case that has to keep working, and the one that was not happening.

    A worker that starts after an eviction has an empty map and a full store, so
    restoring is the entire mechanism. If this path silently stops working the
    only symptom is a refused close much later, which is why it is asserted on
    values rather than on "no exception".
    """
    outcome = _run_generation_harness(
        """
const stored = {btapTabGenerationsV1: {'7': 'gen-seven', '8': 'gen-eight'}};
let setCalls = 0;
let getCalls = 0;
const chrome = {
  storage: {session: {
    get: async key => { getCalls += 1; return {[key]: stored[key]}; },
    set: async value => { setCalls += 1; Object.assign(stored, value); },
  }},
  tabs: {query: async () => [{id: 7}, {id: 8}]},
};
__SOURCE__
async function scenario() {
  return {seven: await tabGenerationFor(7), eight: await tabGenerationFor(8)};
}
"""
        + _GENERATION_TAIL
    )

    assert outcome["seven"] == "gen-seven"
    assert outcome["eight"] == "gen-eight"
    assert outcome["failures"] == 0
    assert outcome["loaded"] is True
    assert outcome["provisional"] == []
    assert outcome["logs"] == []


def test_generation_storage_and_tab_reads_start_together():
    """Worker recovery should pay the slower API latency, not both in series."""
    outcome = _run_generation_harness(
        """
const stored = {btapTabGenerationsV1: {'7': 'gen-seven'}};
let setCalls = 0;
let getCalls = 0;
let storageStarted = false;
let tabsStarted = false;
let resolveStorage;
let resolveTabs;
const chrome = {
  storage: {session: {
    get: key => {
      getCalls += 1;
      storageStarted = true;
      return new Promise(resolve => { resolveStorage = () => resolve({[key]: stored[key]}); });
    },
    set: async value => { setCalls += 1; Object.assign(stored, value); },
  }},
  tabs: {query: () => {
    tabsStarted = true;
    return new Promise(resolve => { resolveTabs = () => resolve([{id: 7}]); });
  }},
};
__SOURCE__
async function scenario() {
  const pending = loadTabGenerations();
  await Promise.resolve();
  const startedTogether = storageStarted && tabsStarted;
  resolveStorage();
  await Promise.resolve();
  const tabsWereAlreadyPending = tabsStarted;
  resolveTabs();
  await pending;
  return {startedTogether, tabsWereAlreadyPending, seven: tabGenerations.get(7)};
}
"""
        + _GENERATION_TAIL
    )

    assert outcome["startedTogether"] is True
    assert outcome["tabsWereAlreadyPending"] is True
    assert outcome["seven"] == "gen-seven"
    assert outcome["getCalls"] == 1
    assert outcome["setCalls"] == 1


def test_an_unreadable_generation_snapshot_is_not_overwritten():
    """This is the defect. The store must come out of it untouched.

    The snapshot is written whole -- that is also how dead tabs get pruned -- so
    a write from a map that was never read deletes every generation it does not
    contain, and a fresh worker's map contains none of them. `No SW` from a
    `chrome.*` call during worker startup is routine, so this is not a rare path.
    """
    outcome = _run_generation_harness(
        """
const stored = {btapTabGenerationsV1: {'7': 'gen-seven', '8': 'gen-eight'}};
let setCalls = 0;
let getCalls = 0;
const chrome = {
  storage: {session: {
    get: async () => { getCalls += 1; throw new Error('No SW'); },
    set: async value => { setCalls += 1; Object.assign(stored, value); },
  }},
  tabs: {query: async () => [{id: 7}, {id: 8}]},
};
__SOURCE__
async function scenario() {
  return {seven: await tabGenerationFor(7)};
}
"""
        + _GENERATION_TAIL
    )

    # Nothing was published, so the durable answer is still the durable answer.
    assert outcome["setCalls"] == 0
    assert outcome["stored"]["btapTabGenerationsV1"] == {"7": "gen-seven", "8": "gen-eight"}
    assert outcome["loaded"] is False
    # The caller still gets an answer: breaking list_tabs would be worse than
    # handing back a generation that is only good for this worker's lifetime.
    assert isinstance(outcome["seven"], str) and outcome["seven"]
    assert outcome["seven"] != "gen-seven"
    assert outcome["provisional"] == [7]
    # And it is no longer silent, which is what made the original invisible.
    assert outcome["failures"] == 1
    assert any("tab generations unreadable" in line for line in outcome["logs"])


def test_a_failed_generation_read_is_retried_and_the_durable_value_wins():
    """A transient `No SW` must not cost the worker its whole lifetime.

    The load used to be memoised on the way in, so one failed read left every
    later caller guessing until the worker was collected again. Retrying is only
    half of it: the value that comes back from the store has to beat the one
    minted during the outage, because that is the one other callers were handed
    to keep.
    """
    outcome = _run_generation_harness(
        """
const stored = {btapTabGenerationsV1: {'7': 'gen-seven'}};
let setCalls = 0;
let getCalls = 0;
const chrome = {
  storage: {session: {
    get: async key => {
      getCalls += 1;
      if (getCalls === 1) throw new Error('No SW');
      return {[key]: stored[key]};
    },
    set: async value => { setCalls += 1; Object.assign(stored, value); },
  }},
  tabs: {query: async () => [{id: 7}]},
};
__SOURCE__
async function scenario() {
  const duringOutage = await tabGenerationFor(7);
  const afterRetry = await tabGenerationFor(7);
  return {duringOutage, afterRetry};
}
"""
        + _GENERATION_TAIL
    )

    assert outcome["getCalls"] == 2, "the failed read was memoised"
    assert outcome["duringOutage"] != "gen-seven"
    assert outcome["afterRetry"] == "gen-seven"
    assert outcome["stored"]["btapTabGenerationsV1"]["7"] == "gen-seven"
    assert outcome["loaded"] is True
    assert outcome["provisional"] == []


def test_a_tab_opened_during_the_outage_keeps_the_generation_it_was_given():
    """A provisional generation for a tab the store never knew is a real one.

    The tab was opened while the store was unreadable, so there is no durable
    value to lose and its caller is holding the only one there is. Discarding it
    on the retry would refuse that caller's close for no reason at all.
    """
    outcome = _run_generation_harness(
        """
const stored = {btapTabGenerationsV1: {'7': 'gen-seven'}};
let setCalls = 0;
let getCalls = 0;
const chrome = {
  storage: {session: {
    get: async key => {
      getCalls += 1;
      if (getCalls === 1) throw new Error('No SW');
      return {[key]: stored[key]};
    },
    set: async value => { setCalls += 1; Object.assign(stored, value); },
  }},
  tabs: {query: async () => [{id: 7}, {id: 9}]},
};
__SOURCE__
async function scenario() {
  const openedDuringOutage = await tabGenerationFor(9);
  const seven = await tabGenerationFor(7);
  const stillTheSame = await tabGenerationFor(9);
  return {openedDuringOutage, seven, stillTheSame};
}
"""
        + _GENERATION_TAIL
    )

    assert outcome["openedDuringOutage"] == outcome["stillTheSame"]
    assert outcome["seven"] == "gen-seven"
    # Kept *and* published, so the next eviction can restore it.
    assert (
        outcome["stored"]["btapTabGenerationsV1"]["9"] == outcome["openedDuringOutage"]
    )
    assert outcome["provisional"] == []


def test_losing_the_tab_list_does_not_wipe_the_generation_snapshot_either():
    """The same deletion through the other door.

    Restoring is gated on the tab being live, so an empty tab list drops every
    stored entry as belonging to a tab that no longer exists -- and the write at
    the end of the load then makes that permanent. `chrome.tabs.query` fails the
    same way `chrome.storage.session` does, at the same moment, for the same
    reason.
    """
    outcome = _run_generation_harness(
        """
const stored = {btapTabGenerationsV1: {'7': 'gen-seven', '8': 'gen-eight'}};
let setCalls = 0;
let getCalls = 0;
const chrome = {
  storage: {session: {
    get: async key => { getCalls += 1; return {[key]: stored[key]}; },
    set: async value => { setCalls += 1; Object.assign(stored, value); },
  }},
  tabs: {query: async () => { throw new Error('No SW'); }},
};
__SOURCE__
async function scenario() {
  return {seven: await tabGenerationFor(7)};
}
"""
        + _GENERATION_TAIL
    )

    assert outcome["setCalls"] == 0
    assert outcome["stored"]["btapTabGenerationsV1"] == {"7": "gen-seven", "8": "gen-eight"}
    assert outcome["failures"] == 1
    assert outcome["loaded"] is False


def test_the_generation_failsafe_is_advertised_without_being_required():
    """Advertised so it can be checked, not required because it is not a protocol.

    An extension without it still speaks version 3 and answers every command --
    it just re-mints generations after an eviction. Putting it in the required
    set would report a working install as broken, and would turn the required
    set from "cannot interoperate" into "any behavioural fix". The shared
    version number is what makes a stale build visible; this says which
    behaviour is loaded, and the counter says whether it ever fired.
    """
    source = BACKGROUND.read_text(encoding="utf-8")

    assert "tab_generation_load_failsafe: true," in source
    assert "tab_generation_load_failures: tabGenerationLoadFailures," in source
    assert "tab_generation_load_failsafe" not in S._REQUIRED_EXTENSION_CAPABILITIES
