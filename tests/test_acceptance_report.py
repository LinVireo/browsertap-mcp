from __future__ import annotations

import json

from scripts import acceptance_report as A
from scripts import evidence_manifest as E


def _passing_docs_report() -> dict[str, object]:
    """A documentation report with nothing wrong in it.

    `check_tool_docs.report_ok` reads every one of these keys, so a partial
    stub would fail the documentation gate for the wrong reason and quietly
    weaken any test built on it.
    """
    return {
        "registered": 55,
        "expected_registered": 55,
        "coverage_manifest": 55,
        "readme_missing": {"README.md": [], "README.zh-CN.md": []},
        "readme_extra": {"README.md": [], "README.zh-CN.md": []},
        "missing_params": {},
        "missing_defaults": {},
        "missing_descriptions": [],
        "skill_missing_files": [],
        "skill_hash_mismatch": [],
        "skill_missing_text": [],
        "version_error": None,
        "versions": {"source": "9.9.9", "pyproject": "9.9.9", "manifest": "9.9.9"},
    }


def _passing_tool_evidence() -> dict[str, object]:
    return {
        "registered": 55,
        "contract_valid_tools": 55,
        "fully_verified_tools": 55,
        "all_evidence_executed": True,
        "failed_evidence": [],
        "unclassified_evidence": [],
        "offline_execution": {"exit_code": 0},
    }


def _seal_release_evidence(monkeypatch, tmp_path, *, git_dirty: bool = False):
    """Lay out a complete, self-consistent passing evidence set.

    Everything `build_report_data` reads comes from here, so the returned score
    is produced by the real scoring code rather than by a stub of it.
    """
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    (artifacts / "coverage.json").write_text(
        json.dumps({"totals": {"percent_covered": 89.12}}), encoding="utf-8"
    )
    passing_xml = '<testsuite><testcase classname="c" name="t" /></testsuite>'
    (artifacts / "offline-junit.xml").write_text(passing_xml, encoding="utf-8")
    (artifacts / "live-junit.xml").write_text(passing_xml, encoding="utf-8")
    for name in ("tool-coverage-offline.json", "tool-coverage-live.json"):
        (artifacts / name).write_text(json.dumps(_passing_tool_evidence()), encoding="utf-8")
    wheel = dist / "browsertap_mcp-9.9.9-py3-none-any.whl"
    sdist = dist / "browsertap_mcp-9.9.9.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    manifest = {
        "schema_version": E.SCHEMA_VERSION,
        "generated": "2026-08-18T00:00:00+00:00",
        "include_live": True,
        "source": {
            "git_head": "f" * 40,
            "git_dirty": git_dirty,
            "content_sha256": "a" * 64,
            "file_count": 84,
            "missing_file_count": 0,
        },
        "artifacts": {
            path.relative_to(tmp_path).as_posix(): {"sha256": "x", "bytes": 1}
            for path in (
                artifacts / "coverage.json",
                artifacts / "offline-junit.xml",
                artifacts / "live-junit.xml",
                artifacts / "tool-coverage-offline.json",
                artifacts / "tool-coverage-live.json",
                wheel,
                sdist,
            )
        },
    }
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "validate_manifest", lambda **_kwargs: (manifest, []))
    monkeypatch.setattr(A, "build_docs_report", _passing_docs_report)
    monkeypatch.setattr(A, "validate_archive", lambda _path: [])
    return manifest


def test_complete_sealed_evidence_scores_every_gate(monkeypatch, tmp_path):
    """The 100/100 claim has to come from the scorer, not from a stub.

    The only other test of the scoring path replaces `build_report_data`
    wholesale, so nothing exercised the gate expressions that produce the
    published score.
    """
    _seal_release_evidence(monkeypatch, tmp_path)

    data = A.build_report_data()

    assert data["evidence_problems"] == []
    assert data["evidence_fresh"] is True
    assert data["gates"] == dict.fromkeys(A.GATE_WEIGHTS, True)
    assert data["objective_score"] == 100
    assert sum(A.GATE_WEIGHTS.values()) == 100
    assert data["release_ready"] is True
    assert data["version"] == "9.9.9"
    assert data["code_coverage"] == 89.12
    # A passing bound live run must promote the live tool evidence file.
    assert data["tool_coverage_source"] == "artifacts/tool-coverage-live.json"
    assert data["live"]["status"] == "pass"
    assert data["distribution_summary"] == "2 manifest-bound archive(s) validated"


def test_dirty_sealed_tree_forfeits_every_evidence_bound_gate(monkeypatch, tmp_path):
    """A seal taken over uncommitted work is not reproducible from Git.

    `git_head` names a commit that does not contain the code under test, so the
    artifacts cannot be tied to any reviewable source state. Only the two gates
    that read the live worktree instead of the seal may still pass.
    """
    _seal_release_evidence(monkeypatch, tmp_path, git_dirty=True)

    data = A.build_report_data()

    assert data["evidence_fresh"] is False
    assert any("sealed source tree was dirty" in problem for problem in data["evidence_problems"])
    assert data["gates"] == {
        "tool_contract": False,
        "offline_evidence": False,
        "live_evidence": False,
        "code_coverage": False,
        "documentation": True,
        "versions": True,
        "distributions": False,
        "live_suite": False,
    }
    assert data["objective_score"] == A.GATE_WEIGHTS["documentation"] + A.GATE_WEIGHTS["versions"]
    assert data["release_ready"] is False


def test_report_stamps_the_source_fingerprint_it_scored(monkeypatch, tmp_path):
    """The report is written after the seal, so it cannot be bound by it.

    Without the fingerprint in the body, a report left over from an earlier
    round is indistinguishable from a current one.
    """
    _seal_release_evidence(monkeypatch, tmp_path)

    text = A.render_report(A.build_report_data())

    assert "## Scored Source" in text
    assert f"`content_sha256`: `{'a' * 64}`" in text
    assert f"`git_head`: `{'f' * 40}`" in text
    assert "`git_dirty`: `false`" in text
    assert "the seal was taken over a clean tree" in text


def test_report_without_a_source_record_says_so_instead_of_inventing_one(monkeypatch, tmp_path):
    manifest = _seal_release_evidence(monkeypatch, tmp_path)
    manifest.pop("source")

    text = A.render_report(A.build_report_data())

    assert "Sealed source fingerprint: `unavailable`" in text
    assert "content_sha256" not in text.split("## Scored Source")[1]


def test_live_junit_is_the_only_live_status_source(monkeypatch, tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "live-junit.xml").write_text(
        '<testsuites><testsuite><testcase classname="live" name="ok" />'
        '<testcase classname="live" name="also_ok" /></testsuite></testsuites>',
        encoding="utf-8",
    )
    monkeypatch.setattr(A, "ROOT", tmp_path)

    result = A._live_junit(
        {"include_live": True, "artifacts": {"artifacts/live-junit.xml": {}}}
    )

    assert result["status"] == "pass"
    assert result["summary"] == "tests=2, failures=0, errors=0, skipped=0"


def test_live_junit_fails_on_skips(monkeypatch, tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "live-junit.xml").write_text(
        '<testsuite><testcase classname="live" name="skipped"><skipped /></testcase></testsuite>',
        encoding="utf-8",
    )
    monkeypatch.setattr(A, "ROOT", tmp_path)

    result = A._live_junit(
        {"include_live": True, "artifacts": {"artifacts/live-junit.xml": {}}}
    )

    assert result["status"] == "fail"
    assert result["skipped"] == 1


def test_unbound_passing_live_result_is_stale_but_does_not_fail_offline_gates():
    live, bound = A._bind_live_status(
        {"status": "pass", "summary": "tests=2"},
        {"include_live": False},
    )

    assert bound is False
    assert live["status"] == "stale"
    assert "not bound to current source tree" in live["summary"]


def test_main_returns_nonzero_when_release_gates_fail(monkeypatch, tmp_path):
    data = {
        "generated": "2026-08-16T00:00:00+00:00",
        "version": "0.3.4",
        "gates": {name: False for name in A.GATE_WEIGHTS},
        "gate_weights": A.GATE_WEIGHTS,
        "objective_score": 0,
        "release_ready": False,
        "tool_coverage": {},
        "tool_coverage_source": "missing",
        "offline": {
            "status": "not-run",
            "summary": "unavailable",
            "source": "artifacts/offline-junit.xml",
        },
        "code_coverage": None,
        "code_coverage_source": "missing",
        "versions": {},
        "live": {
            "status": "not-run",
            "summary": "unavailable",
            "source": "artifacts/live-junit.xml",
        },
        "distribution_summary": "missing",
        "evidence_fresh": False,
        "evidence_problems": ["evidence manifest unavailable"],
    }
    monkeypatch.setattr(A, "build_report_data", lambda: data)
    output = tmp_path / "acceptance.md"

    assert A.main(["--output", str(output)]) == 1
    text = output.read_text(encoding="utf-8")
    assert "Score: 0/100" in text
    assert "Release ready: false" in text
    assert "95-Point" not in text
