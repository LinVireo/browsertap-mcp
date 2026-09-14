"""High-level iframe routing and retained CDP identity regressions."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from browsertap_mcp import server as S
from browsertap_mcp.page_frames import frame_locator_payload
from browsertap_mcp.page_input import click_commands, type_commands

ROOT = Path(__file__).resolve().parents[1]


def run_frames(mode="same_process", action="click", *, gone=False, point=False, **options):
    locator = {"frame": ["#outer"], "css": "#control"}
    if mode == "nested":
        locator["frame"].append("#inner")
    if point:
        locator = {"frame": ["#outer"], "x": 70, "y": 35}
    payload = frame_locator_payload(
        locator, action=action, clear=action == "type", gone=gone,
        center_x=action == "click" and not point, center_y=action == "click" and not point,
    )
    payload["commands"] = type_commands("", "new text", submit_key="Enter")[1:] if action == "type" else click_commands(0, 0)
    payload.update(options)
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests/fixtures/frame_locator_harness.cjs")],
        input=json.dumps({"module": str(ROOT / "src/browsertap_mcp/chrome_extension/frame_locator.js"),
                          "mode": mode, "payload": payload}),
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def frame_driver(monkeypatch):
    class Driver:
        default_session_id = "browser:9"

        def __init__(self):
            self.calls = []

        def ext_cmd(self, payload, **kwargs):
            self.calls.append((payload, kwargs))
            assert payload["cmd"] == "frame_locator"
            if payload["action"] == "query":
                return {"data": {"met": True, "url": "https://frame.test/", "title": "Frame"}}
            return {"data": {
                "status": "success", "found": True, "input_dispatched": True,
                "x": 32, "y": 48, "hitVerified": True,
                "focusConfirmed": True, "targetKind": "element",
                "challenge_check": {"enforced": True},
            }}

    driver = Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    sessions = [{"id": "browser:7", "url": "https://parent.test/"}]
    monkeypatch.setattr(S, "active_sessions", lambda **_kwargs: sessions)
    monkeypatch.setattr(S, "ensure_sessions", lambda **_kwargs: sessions)
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: session_id)
    monkeypatch.setattr(S, "exec_js", lambda *_args, **_kwargs: {"data": {
        "found": False, "status": "cross_origin_frame", "met": False,
    }})
    return driver


@pytest.mark.parametrize("tool", ["page_click", "page_type", "wait_for"])
def test_high_level_tools_enter_cross_origin_frames(frame_driver, tool):
    selector = {"frame": ["#outer", "#inner"], "css": "#control"}
    kwargs = {"selector": selector, "session_id": "browser:7", "timeout": 1}
    result = getattr(S, tool)("iframe text", **kwargs) if tool == "page_type" else getattr(S, tool)(**kwargs)

    assert result["status"] == "success", result
    assert len(frame_driver.calls) == 1
    payload, routing = frame_driver.calls[0]
    assert payload["tabId"] == 7
    assert routing["client_id"] == "browser"
    assert len(payload["frameSelectors"]) == 2
    assert 0 < payload["timeoutMs"] <= 1000
    assert frame_driver.default_session_id == "browser:9"


@pytest.mark.parametrize("mode", ["same_process", "oopif", "nested", "padding", "hidden_duplicate"])
@pytest.mark.parametrize("action", ["click", "type"])
def test_frame_input_uses_exact_dom_targets_and_releases_objects(mode, action):
    result = run_frames(mode, action)
    assert result["result"]["data"]["status"] == "success", result
    assert result["inputEvents"]
    assert result["attached"] == result["detached"]
    assert len(result["attached"]) == (2 if mode in {"oopif", "nested"} else 1)
    assert result["objectCount"] == 0
    if action == "type":
        assert result["value"] == "new text"
        assert any(item["params"].get("text") == "\r" for item in result["inputEvents"])
    else:
        expected = (192, 112) if mode == "nested" else (177, 94) if mode == "padding" else (172, 87)
        assert {(item["params"]["x"], item["params"]["y"]) for item in result["inputEvents"]} == {expected}
        assert result["result"]["data"]["hitVerified"] is True


@pytest.mark.parametrize("mode,expected", [
    ("missing", "not_found"), ("ambiguous", "ambiguous"),
    ("disabled", "not_interactable"), ("not_a_frame", "not_a_frame"),
    ("parent_overlay", "obscured"), ("child_overlay", "obscured"),
    ("transformed", "unsupported_frame_transform"), ("zoomed", "unsupported_frame_transform"),
    ("offscreen", "outside_viewport"), ("replaced", "stale_frame"),
    ("navigation", "stale_frame"), ("context_reused", "stale_frame"),
    ("moving", "stale_frame"), ("wrong_target", "stale_frame"),
    ("no_document_focus", "focus_failed"),
])
def test_frame_click_refuses_before_any_input(mode, expected):
    result = run_frames(mode)
    assert result["result"]["data"]["status"] == expected, result
    assert result["result"]["data"]["input_dispatched"] is False
    assert result["inputEvents"] == []
    assert result["objectCount"] == 0
    assert result["attached"] == result["detached"]


@pytest.mark.parametrize("mode,expected", [("focus_lost", "focus_failed"), ("readonly", "not_interactable")])
def test_frame_type_proves_focus_and_editability(mode, expected):
    result = run_frames(mode, "type")
    assert result["result"]["data"]["status"] == expected, result
    assert result["inputEvents"] == []


@pytest.mark.parametrize("mode,gone,met,status", [
    ("oopif", False, True, "found"), ("nested", False, True, "found"),
    ("transformed", False, True, "found"),
    ("missing", True, True, "not_found"), ("ambiguous", True, False, "ambiguous"),
    ("navigation", True, False, "stale_frame"),
    ("missing_replaced", True, False, "stale_frame"),
])
def test_frame_wait_queries_do_not_focus_or_input(mode, gone, met, status):
    result = run_frames(mode, "query", gone=gone)
    assert result["result"]["data"]["met"] is met, result
    assert result["result"]["data"]["locator_status"] == status
    assert not any(item["method"].startswith(("Input.", "Emulation.")) for item in result["calls"])
    assert result["objectCount"] == 0


def test_frame_point_coordinates_keep_the_no_hit_test_contract():
    result = run_frames(point=True)
    assert result["result"]["data"]["status"] == "success", result
    assert result["result"]["data"]["hitVerified"] is False
    assert result["inputEvents"][0]["params"]["x"] == 172


def test_frame_sequence_stops_on_navigation_after_partial_input():
    result = run_frames("partial_navigation", "type")
    assert result["result"]["data"]["status"] == "stale_frame"
    assert result["result"]["data"]["input_dispatched"] is True
    assert len(result["inputEvents"]) == 1
    assert result["value"] == "new text"


@pytest.mark.parametrize("mode,expected", [
    ("xterm_navigation", "stale_frame"), ("xterm_focus", "focus_failed"),
    ("root_focus_lost", "focus_failed"),
])
def test_frame_submit_rechecks_after_terminal_delay(mode, expected):
    result = run_frames(mode, "type", submitDelayMs=100)
    assert result["result"]["data"]["status"] == expected, result
    assert result["result"]["data"]["input_dispatched"] is True
    assert [event["method"] for event in result["inputEvents"]] == ["Input.insertText"]
    assert result["attached"] == result["detached"]


def test_frame_challenge_observation_and_blocked_marker_do_not_replay():
    first = run_frames("challenge")
    data = first["result"]["data"]
    assert data["beforeMarker"] and data["afterMarker"] == data["beforeMarker"], first
    blocked = run_frames("challenge", blockedMarker=data["beforeMarker"])
    assert blocked["result"]["data"]["status"] == "challenge_stalled"
    assert blocked["result"]["data"]["input_dispatched"] is False
    assert blocked["inputEvents"] == []
    cleared = run_frames("challenge_cleared")
    assert cleared["result"]["data"]["challenge_check"]["enforced"] is True
    assert cleared["result"]["data"]["afterMarker"] is None


@pytest.mark.parametrize("mode,dispatched", [("uncertain", True), ("input_not_sent", False), ("deadline", False)])
def test_frame_errors_preserve_input_uncertainty_without_replay(mode, dispatched):
    result = run_frames(mode)
    assert result["result"]["ok"] is False, result
    assert result["result"]["error"]["input_dispatched"] is dispatched
    assert result["result"]["error"]["retryable"] is False
    assert len(result["inputEvents"]) == (1 if dispatched else 0)
    assert result["attached"] == result["detached"]
