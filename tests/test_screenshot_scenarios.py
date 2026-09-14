"""Regressions for tab recovery while a requested session is reconnecting."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from browsertap_mcp import browser_bridge as bridge
from browsertap_mcp import server
from tests.test_browser_bridge_coverage import FakeSocket, driver_stub
from tests.test_pending_bridge_operations import finish
from tests.test_phase0_recovery import _create_operation_source, _run_node_script
from tests.test_wait_reservation_contract import pending_wait_bridge


@pytest.mark.parametrize(
    ("replacement_identity", "allow_rebind", "should_execute"),
    [("tab-a", True, True), ("tab-b", True, False), ("tab-a", False, False)],
)
def test_replacement_arriving_during_reconnect_grace(
    monkeypatch, replacement_identity, allow_rebind, should_execute,
):
    driver = driver_stub()
    socket = FakeSocket()
    driver._apply_extension_tabs(
        "one", "chrome",
        [{"id": 1, "url": "https://same.test", "generation": "g1", "tab_identity": "tab-a"}],
        socket,
    )
    driver.sessions["one:1"].mark_disconnected()
    now = [0.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: now[0])

    def replacement_arrives(serial, timeout):
        now[0] += min(timeout, 0.1)
        if "one:2" not in driver.sessions:
            driver._apply_extension_tabs(
                "one", "chrome",
                [{
                    "id": 2, "url": "https://same.test", "generation": "g1",
                    "tab_identity": replacement_identity,
                }],
                socket,
            )
        return (serial or 0) + 1

    def reply(message):
        payload = json.loads(message)
        socket.messages.append(payload)
        driver.results[payload["id"]] = {"success": True, "data": 1}

    monkeypatch.setattr(driver, "_wait_for_activity", replacement_arrives)
    monkeypatch.setattr(socket, "send_message", reply)
    if should_execute:
        result = driver.execute_js(
            "return 1", session_id="one:1", timeout=1, allow_rebind=allow_rebind,
        )
        assert result["data"] == 1
        assert result["rebound_from"] == "one:1"
        assert result["replacement_session_id"] == "one:2"
        assert result["tab_identity"] == "tab-a"
        assert len(socket.messages) == 1
        assert socket.messages[0]["tabId"] == 2
    else:
        with pytest.raises(bridge.SessionNotConnectedError):
            driver.execute_js(
                "return 1", session_id="one:1", timeout=1, allow_rebind=allow_rebind,
            )
        assert socket.messages == []


def test_raw_create_missing_id_explains_safe_entry_without_creating_a_tab():
    observed = _run_node_script(_create_operation_source() + """
let creates = 0;
const chrome = {tabs: {create() { creates++; throw new Error('must not create'); }}};
createTabAck({url: 'https://example.test/'}).then(result => {
  process.stdout.write(JSON.stringify({result, creates}));
});
""")
    assert observed["creates"] == 0
    result = observed["result"]
    assert result["ok"] is False
    assert "operation_id" in result["error"]
    # The message itself survives the extension error frame and raw /link
    # clients, which do not necessarily forward optional diagnostics.
    assert "open_new_tab" in result["error"]
    assert "create_status" in result["error"]


@pytest.mark.parametrize(
    ("content", "indicator_text", "visible"),
    [
        ("Loading!", "BTAP: connected", True),
        ("Loading!", "BTAP：已连接", True),
        ("Loading!", "", True),
        ("Loading!", None, False),
        ("BTAP: connected", "BTAP: connected", False),
        ("BTAP: connected", "BTAP: connected", True),
        ("Business content has finished rendering", "BTAP: connected", True),
    ],
)
def test_render_probe_excludes_only_the_visible_btap_indicator(content, indicator_text, visible):
    # Execute the shipped probe rather than supplying a pre-counted response:
    # the real badge pushed an eight-character SPA shell over the readiness
    # threshold while the existing Python-only tests stayed green.
    body_text = content + ("\n" + indicator_text if indicator_text and visible else "")
    prefix = "'use strict';\nconst fixture = " + json.dumps({
        "body_text": body_text, "indicator_text": indicator_text, "visible": visible,
    }) + ";\n" + """
const indicator = fixture.indicator_text === null ? null : Object.freeze({
  innerText: fixture.indicator_text,
  checkVisibility() { return fixture.visible; },
});
const body = Object.freeze({
  innerText: fixture.body_text,
  innerHTML: '<main>' + 'x'.repeat(310000) + '</main>',
  querySelector(selector) { return selector === '#btap-indicator' ? indicator : null; },
});
const document = Object.freeze({
  body, readyState: 'complete', fonts: Object.freeze({status: 'loaded'}),
  querySelector() { return null; },
});
"""

    def execute(code, **_kwargs):
        return {"data": _run_node_script(
            prefix + "process.stdout.write(JSON.stringify(eval(" + json.dumps(code) + ")));",
        )}

    result = server._page_render_state(SimpleNamespace(execute_js=execute), "fixture:1", 1)
    assert result is not None
    assert result["text_chars"] == len(content)
    assert result["state"] == ("shell_only" if len(content) <= 16 else "content")
    assert result["content_ready"] is (len(content) > 16)


@pytest.mark.parametrize("acknowledged", [True, False])
def test_scan_render_timeout_does_not_block_the_next_page_command(monkeypatch, acknowledged):
    driver, now = pending_wait_bridge(monkeypatch, acknowledged=acknowledged)
    monkeypatch.setattr(server.simphtml, "get_html", lambda *_args, **_kwargs: "Checkout form")

    result = server.scan_page(session_id="browser:1", timeout=4)

    assert result["status"] == "success"
    assert result["content"] == "Checkout form"
    assert "content_ready" not in result  # A timed-out probe is not a readiness verdict.
    assert len(driver.sent) == 1
    assert now[0] == pytest.approx(1001)
    operation_id = driver.sent[0]["id"]

    # Exercise the real registry: the next call must dispatch before the late
    # readiness reply arrives, without waiting for the ordinary JS hold TTL.
    driver.ext_cmd({"cmd": "cdp", "method": "Page.getFrameTree", "tabId": 1}, requester_id="next")
    assert len(driver.sent) == 2
    finish(driver, operation_id, {"has_body": True})
    late = driver.get_execute_js_result(operation_id)
    assert late["status"] == "success"
    assert late["reservation_held"] is False
