"""Public service wrappers keep typed outcomes, data, and ownership constraints."""

from __future__ import annotations

import errno
import json
from types import SimpleNamespace

import pytest

from browsertap_mcp import cli
from browsertap_mcp import server as S


@pytest.mark.parametrize("payload,expected", [
    ({"client_id": "browser"}, {"client_id": "browser"}),
    ({"generation": "g"}, {"generation": "g"}),
    ({"tab_id": 7}, {"tab_id": 7}),
    ({"session_id": "legacy"}, {"session_id": "legacy"}),
    ({"session_id": "browser:opaque"}, {"session_id": "browser:opaque", "client_id": "browser"}),
    ({"session_id": "browser:7", "browser": "chrome", "url": "https://example.test/"},
     {"session_id": "browser:7", "client_id": "browser", "tab_id": 7, "browser": "chrome", "url": "https://example.test/"}),
    ({"url": "https://example.test/"}, None),
])
def test_result_target_never_invents_missing_identity(payload, expected):
    assert S._result_target(payload) == expected


@pytest.mark.parametrize("error,code", [
    (PermissionError("denied"), "permission_denied"),
    (FileNotFoundError("absent"), "not_found"),
    (ValueError("invalid"), "invalid_request"),
    (RuntimeError("Session browser:7 not found"), "session_not_connected"),
])
def test_error_envelope_preserves_actionable_error_category(error, code):
    assert S._exception_result_metadata(error)[0] == code


def test_error_candidates_are_suggestions_not_rebound_identity():
    error = S.SessionTargetNotFoundError("old:7", [None, {}, {"id": "new:7", "url": "https://example.test/"}])
    assert error.diagnostics["replacement_candidates"] == [{"id": "new:7", "url": "https://example.test/"}]
    assert error.retry_safe is False
    assert "Possible replacement" in str(error)
    assert S.SessionTargetNotFoundError("old:7").diagnostics["replacement_candidates"] == []
    assert S._result_diagnostics(None, tool="test") == {"tool": "test"}
    assert S._result_diagnostics({"newTabs": [{"id": 7}]}, tool="test")["new_tabs_count"] == 1


def test_ownership_conflicts_do_not_overwrite_the_original_capability():
    registry = S._TabOwnershipRegistry()
    with pytest.raises(ValueError, match="empty"):
        registry.register("browser:7", "g", owner_id=" ")
    first = registry.register("browser:7", "g", owner_id="owner")
    assert registry.register("browser:7", "g", owner_id="owner") == first
    for generation, owner in [("changed", "owner"), ("g", "foreign")]:
        with pytest.raises(ValueError, match="conflict"):
            registry.register("browser:7", generation, owner_id=owner)
    assert registry.counters()["registered"] == 1
    assert registry.rebind("browser:7", "browser:7") is None
    assert registry.rebind("missing:7", "browser:8") is None
    registry.register("browser:8", "g8", owner_id="other")
    with pytest.raises(PermissionError, match="conflict"):
        registry.rebind("browser:7", "browser:8")
    for targets, owner in [(["browser:7"], None), (["browser:7"], "foreign"), (["browser:9"], "owner")]:
        with pytest.raises(PermissionError):
            registry.validate(targets, owner_id=owner)


@pytest.fixture
def service(monkeypatch):
    driver = SimpleNamespace(default_session_id="browser:1")
    calls = []
    driver.ext_cmd = lambda command, **kw: calls.append((command, kw)) or {"data": {"ok": True}}
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", lambda **kw: [{"id": "browser:7"}])
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: session_id or "browser:7")
    monkeypatch.setattr(S, "_direct_cdp", lambda *a, **kw: calls.append((a, kw)) or {"value": 7})
    return driver, calls


@pytest.mark.parametrize("arguments", [{"session_id": "7"}, {"tab_id": "browser:7"}, {"tab_id": 7}])
def test_cdp_target_forms_resolve_to_the_same_explicit_native_tab(service, arguments):
    driver, calls = service
    result = S.cdp_command("DOM.getDocument", **arguments)
    assert result["session_id"] == "browser:7" and result["tab_id"] == 7
    assert calls[0][1]["client_id"] == "browser"
    assert driver.default_session_id == "browser:1"


def test_cdp_conflicting_target_constraints_never_dispatch(service, monkeypatch):
    _, calls = service
    with pytest.raises(ValueError, match="does not match"):
        S.cdp_command("DOM.getDocument", session_id="browser:7", tab_id=8)
    monkeypatch.setattr(S, "_normalize_tab_targets", lambda *a, **kw: ([7], "foreign"))
    with pytest.raises(ValueError, match="different browser"):
        S.cdp_command("DOM.getDocument", session_id="browser:7", tab_id=7)
    monkeypatch.setattr(S, "_normalize_tab_targets", lambda *a, **kw: ([7], None))
    with pytest.raises(ValueError, match="infer browser"):
        S.cdp_command("DOM.getDocument", session_id="7")
    assert calls == []


def test_non_tab_cdp_targets_stay_in_the_selected_browser(service):
    _, calls = service
    S.cdp_command("Runtime.evaluate", extension_id="extension", target_id="worker", session_id="browser:7")
    assert calls[0][0]["extensionId"] == "extension"
    assert calls[0][0]["targetId"] == "worker"
    assert calls[0][1]["client_id"] == "browser"


@pytest.mark.parametrize("reply,message", [
    ({"ok": True, "data": []}, "invalid extension response"),
    ({"status": "completed"}, "no local path"),
    ({"status": "completed", "path": "ABSENT"}, "does not exist"),
])
def test_download_completion_requires_an_existing_file(service, tmp_path, reply, message):
    driver, _ = service
    if reply.get("path") == "ABSENT":
        reply = {**reply, "path": str(tmp_path / "absent")}
    driver.ext_cmd = lambda *a, **kw: {"data": reply}
    with pytest.raises(RuntimeError, match=message):
        S.download_file("https://example.test/file", session_id="browser:7")


def test_download_error_keeps_the_extension_reason_and_hint(service):
    driver, _ = service
    driver.ext_cmd = lambda *a, **kw: {"data": {
        "status": "failed", "code": "interrupted", "error": "connection lost", "hint": "retry download",
    }}
    result = S.download_file("https://example.test/file", session_id="browser:7")
    assert result["status"] == "failed"
    assert result["code"] == "interrupted" and result["hint"] == "retry download"


def test_download_move_fallback_preserves_data_across_volumes(monkeypatch, tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"payload")
    destination.write_bytes(b"old")
    replace = S.os.replace
    calls = []
    def cross_volume(src, dst):
        calls.append((src, dst))
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "different volume")
        return replace(src, dst)
    monkeypatch.setattr(S.os, "replace", cross_volume)
    S._move_download(source, destination, overwrite=True)
    assert not source.exists()
    assert destination.read_bytes() == b"payload"
    assert list(tmp_path.iterdir()) == [destination]
    S._move_download(destination, destination, overwrite=False)
    assert destination.read_bytes() == b"payload"


@pytest.mark.parametrize("present", [False, True])
def test_skill_path_reports_packaged_skills_without_affecting_stdout(monkeypatch, tmp_path, capsys, present):
    if present:
        skill = tmp_path / "browsertap-default" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("skill", encoding="ascii")
    monkeypatch.setattr(cli, "agent_skills_dir", lambda: tmp_path)
    assert cli.cmd_skill_path() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == str(tmp_path)
    assert ("browsertap-default" if present else "no <name>/SKILL.md") in captured.err


def test_doctor_preserves_failures_of_fallback_diagnostics(monkeypatch, capsys):
    def failed():
        raise RuntimeError("bridge unavailable")
    monkeypatch.setattr(cli, "get_driver", lambda: SimpleNamespace(get_all_sessions=failed, diagnose=failed))
    monkeypatch.setattr(cli, "get_setup_status", lambda: {"status": "bridge_unreachable"})
    monkeypatch.setattr(cli, "_port_open", lambda *a: False)
    assert cli.cmd_doctor() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "bridge unavailable"
    assert result["diagnosis"]["cause"] == "diagnose_failed"


@pytest.mark.parametrize("field,value", [("scale", 0), ("timeout", 121)])
def test_pdf_rejects_invalid_print_budgets_without_dispatch(monkeypatch, tmp_path, field, value):
    monkeypatch.setattr(S.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(S, "cdp_command", lambda *a, **kw: pytest.fail("invalid print dispatched"))
    with pytest.raises(ValueError, match=field):
        S.save_pdf("page.pdf", **{field: value})


@pytest.mark.parametrize("activation", ["disabled", "success", "failure"])
def test_switch_tab_publishes_replacement_and_reports_activation_failure(service, monkeypatch, activation):
    driver, _ = service
    published = []
    driver.publish_default_session_id = published.append
    monkeypatch.setattr(S, "switch_session", lambda **kw: "browser:8")
    monkeypatch.setattr(S, "active_sessions", lambda **kw: [{"id": "browser:8", "tab_identity": "stable"}])
    monkeypatch.setattr(S, "compact_tabs", lambda: [{"id": "browser:8"}])
    monkeypatch.setattr(S.time, "sleep", lambda seconds: None)
    def activate(sid):
        if activation == "failure":
            raise RuntimeError("window unavailable")
        return {"on_screen": True}
    monkeypatch.setattr(S, "_activate", activate)
    result = S.switch_tab(session_id="browser:7", activate=activation != "disabled")
    assert published == ["browser:8"]
    assert result["replacement_session_id"] == "browser:8"
    assert result["tab_identity"] == "stable"
    if activation == "failure":
        assert result["activation_failed"] == "window unavailable"
    elif activation == "success":
        assert result["activated"]["on_screen"] is True
    else:
        assert "activated" not in result


@pytest.mark.parametrize("reply,expected", [(None, None), ({"r": {"onScreen": False}}, False), ({"onScreen": True}, True)])
def test_activation_reports_legacy_or_unknown_visibility_honestly(service, reply, expected):
    driver, _ = service
    driver.ext_cmd = lambda *a, **kw: reply
    assert S._activate("browser:7")["on_screen"] is expected
    driver.default_session_id = None
    with pytest.raises(RuntimeError, match="no target"):
        S._activate()


def test_beforeunload_auto_policy_reads_only_a_valid_matching_host(service, monkeypatch):
    monkeypatch.setattr(S, "_automation_mode", lambda: "lab")
    monkeypatch.setattr(S, "_auto_beforeunload_hosts", lambda: ["shell."])
    assert S._lab_auto_accepts_beforeunload("missing:7") is False
    assert S._lab_auto_accepts_beforeunload("browser:7", "https://[invalid") is False
    assert S._lab_auto_accepts_beforeunload("browser:7", "https://shell.example.test/") is True
    monkeypatch.setattr(S, "_automation_mode", lambda: "safe")
    assert S._lab_auto_accepts_beforeunload("browser:7", "https://shell.example.test/") is False


@pytest.mark.parametrize("origin", [None, "", "https://[broken", "https://example.test:invalid", "https://user:password@example.test/"])
def test_site_permission_rejects_malformed_or_credentialed_origins(origin):
    with pytest.raises(ValueError, match="origin"):
        S._normalize_site_permission_origin(origin)


def test_site_permission_preserves_ipv6_origin_and_validates_permission_type():
    assert S._normalize_site_permission_origin("https://[::1]:8443/path") == "https://[::1]:8443"
    assert S._site_permission_spec("clipboard") == {"kind": "clipboard", "setting": "clipboard"}
    with pytest.raises(ValueError):
        S._site_permission_spec(None)
