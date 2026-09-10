"""Wait timeouts must leave no page timer and must retain pending operation IDs."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


@pytest.fixture
def waiting(monkeypatch):
    now = [0.0]
    driver = SimpleNamespace(default_session_id="chrome:7")
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "ensure_sessions", lambda: [{"id": "chrome:7"}])
    monkeypatch.setattr(S, "switch_session", lambda **kw: "chrome:7")
    monkeypatch.setattr(S.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(S.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    return driver, now


def _wait(kind, **kwargs):
    return S.wait_for(js="false", **kwargs) if kind == "condition" else S.wait_for_url("missing", **kwargs)


@pytest.mark.parametrize("kind", ["condition", "url"])
def test_wait_probes_resolve_without_page_timer_dispatch(waiting, monkeypatch, kind):
    _, now = waiting
    scripts = []
    def execute(script, **kw):
        scripts.append(script)
        now[0] += 1
        return {"data": {"met": False}}
    monkeypatch.setattr(S, "exec_js", execute)
    assert _wait(kind, timeout=1)["status"] == "timeout"
    harness = """
const location = { href: 'https://example.test/' };
const document = { title: 'test', readyState: 'complete' };
function setTimeout() { throw new Error('page timers are throttled'); }
const result = new Function(SOURCE)();
if (result && typeof result.then === 'function') throw new Error('wait left a pending page promise');
process.stdout.write(JSON.stringify(JSON.parse(result)));
""".replace("SOURCE", json.dumps(scripts[0]))
    completed = subprocess.run(["node", "-"], input=harness, text=True, capture_output=True,
                               timeout=5, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["met"] is False


@pytest.mark.parametrize("kind", ["condition", "url"])
def test_tail_budget_is_not_spent_on_an_unreturnable_dispatch(waiting, monkeypatch, kind):
    _, now = waiting
    calls = []
    def execute(script, **kw):
        calls.append(kw["timeout"])
        now[0] += 0.8
        return {"data": {"met": False}}
    monkeypatch.setattr(S, "exec_js", execute)
    assert _wait(kind, timeout=1)["status"] == "timeout"
    assert len(calls) == 1
    assert now[0] == pytest.approx(1)


@pytest.mark.parametrize("kind", ["condition", "url"])
@pytest.mark.parametrize("completes", [False, True])
def test_wait_queries_the_same_pending_operation_without_replaying(waiting, monkeypatch, kind, completes):
    driver, now = waiting
    dispatched, queried = [], []
    def execute(script, **kw):
        dispatched.append(script)
        now[0] += 0.4
        raise S.BridgeNoResponseError(
            "response pending", delivery_state="delivered_no_result", retry_safe=False,
            operation_id="wait-operation", reservation_held=True,
        )
    def result(operation_id, timeout):
        queried.append(operation_id)
        now[0] += timeout if not completes else 0.1
        return ({"status": "success", "data": {"met": True}} if completes else
                {"status": "in_progress", "operation_id": operation_id, "reservation_held": True})
    monkeypatch.setattr(S, "exec_js", execute)
    driver.get_execute_js_result = result
    outcome = _wait(kind, timeout=2)
    assert len(dispatched) == 1
    assert queried == ["wait-operation"]
    assert outcome["status"] == ("success" if completes else "timeout")
    if not completes:
        assert outcome["operation_id"] == "wait-operation"
        assert outcome["reservation_held"] is True
        assert outcome["retry_safe"] is False
        assert outcome["poll_with"] == "get_execute_js_result"
