"""Unusable remote diagnosis is a transport diagnostic, not a version verdict."""

from __future__ import annotations

import pytest

from browsertap_mcp import server as S
from tests.test_browser_bridge_coverage import driver_stub


@pytest.mark.parametrize("envelope", [
    None, [], {}, {"r": None}, {"r": []}, {"r": {}},
    {"r": {"unexpected": True}},
    {"r": {"cause": "", "ok": False}},
    {"r": {"cause": [], "ok": False}},
    {"r": {"cause": "healthy", "ok": "yes"}},
    {"r": {"cause": "healthy", "ok": 1}},
    {"r": {"cause": "healthy"}},
])
def test_empty_or_malformed_diagnosis_is_explicitly_unavailable(envelope):
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *args, **kwargs: envelope

    result = driver.diagnose(timeout=0.5)

    assert result["cause"] == "bridge_unreachable"
    assert result["ok"] is False
    assert result["error_code"] == "malformed_diagnosis"
    assert "malformed diagnosis" in result["error"]


@pytest.mark.parametrize("cause,ok", [("healthy", True), ("registering", False), ("starting", False)])
def test_minimal_legitimate_diagnosis_does_not_require_version_or_capabilities(cause, ok):
    diagnosis = {"cause": cause, "ok": ok, "advice": "synthetic advice"}
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *args, **kwargs: {"r": diagnosis}

    assert driver.diagnose() == diagnosis


def test_structured_remote_diagnosis_failure_keeps_its_reason_and_details():
    failure = {
        "error": "synthetic registry unavailable", "error_code": "internal_error",
        "diagnostics": {"retry_safe": False, "component": "synthetic-registry"},
    }
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *args, **kwargs: {"r": failure}

    result = driver.diagnose()

    assert result["ok"] is False
    assert result["cause"] == "bridge_unreachable"
    for name, value in failure.items():
        assert result[name] == value
    assert failure == {
        "error": "synthetic registry unavailable", "error_code": "internal_error",
        "diagnostics": {"retry_safe": False, "component": "synthetic-registry"},
    }, "Normalizing the diagnosis must not modify the received payload"


def test_setup_status_does_not_mislabel_an_empty_remote_diagnosis_as_stale(monkeypatch):
    driver = driver_stub(remote=True)
    driver._remote_cmd = lambda *args, **kwargs: {"r": {}}
    driver.ext_cmd = lambda *args, **kwargs: {}
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: [])

    result = S.get_setup_status()

    assert result["status"] == "bridge_unreachable"
    assert result["action"] == "restart_bridge"
    assert result["diagnosis"]["error_code"] == "malformed_diagnosis"
