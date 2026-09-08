from __future__ import annotations

import json

import pytest

from scripts import acceptance_report as A
from scripts import evidence_manifest as E


def _passing_docs_report() -> dict[str, object]:
    """A documentation report with nothing wrong in it.

    `check_tool_docs.report_ok` reads every one of these keys, so a partial
    stub would fail the documentation gate for the wrong reason and quietly
    weaken any test built on it.
    """
    return {
        "registered": 49,
        "expected_registered": 49,
        "coverage_manifest": 49,
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
        "registered": 49,
        "contract_valid_tools": 49,
        "fully_verified_tools": 49,
        "all_evidence_executed": True,
        "failed_evidence": [],
        "unclassified_evidence": [],
        "offline_execution": {"exit_code": 0},
    }


_EXTENSION_BUILD_STAMP = "a" * 64


def _passing_live_preflight() -> dict[str, object]:
    return {
        "components": {
            "extension_build_verdict": "matches_tree",
            "extension_build_enforced": True,
            "extension_build_stamp": _EXTENSION_BUILD_STAMP,
            "expected_extension_build_stamp": _EXTENSION_BUILD_STAMP,
        }
    }


def _passing_lint_report():
    """A clean ruff run as `scripts/lint_report.py` records it.

    `files_scanned` is part of the verdict, not decoration: ruff over a path that
    matches nothing exits 0 with an empty diagnostic list, so a fixture with a
    zero count here would let the gate pass on the shape it exists to reject.
    """
    return {
        "tool": "ruff",
        "tool_version": "ruff 9.9.9",
        "targets": ["src", "tests", "scripts"],
        "files_scanned": 64,
        "files_per_target": {"src": 10, "tests": 43, "scripts": 11},
        "exit_code": 0,
        "status": "clean",
        "violation_count": 0,
        "violations": [],
        "violations_truncated": False,
        "problems": [],
        "javascript": _passing_js_lint_report(),
        "types": _passing_type_lint_report(),
    }


def _passing_type_lint_report():
    return {
        "tool": "mypy", "tool_version": "mypy 1.11.2", "available": True,
        "enforced": True, "unavailable_reason": None, "check_untyped_defs": True,
        "targets": ["src/browsertap_mcp"], "files_scanned": 2, "files_expected": 2,
        "files": ["src/browsertap_mcp/server.py", "src/browsertap_mcp/browser_bridge.py"],
        "exit_code": 0, "status": "clean", "violation_count": 0, "violations": [],
        "violations_truncated": False, "problems": [],
    }


def _passing_js_lint_report():
    """A clean eslint run over the extension, as the same module records it.

    `enforced` carries the distinction the count alone cannot: eslint exits 0
    both when it checked four files and found nothing and when the config
    matched no rules at all, and only one of those is a clean tree.
    """
    return {
        "tool": "eslint",
        "tool_version": "9.9.9",
        "available": True,
        "enforced": True,
        "unavailable_reason": None,
        "targets": ["src/browsertap_mcp/chrome_extension"],
        "files_scanned": 4,
        "rules_applied": 64,
        "exit_code": 0,
        "status": "clean",
        "violation_count": 0,
        "violations": [],
        "violations_truncated": False,
        "problems": [],
    }


def _seal_release_evidence(monkeypatch, tmp_path, *, git_dirty: bool = False):
    """Lay out a complete, self-consistent passing evidence set.

    Everything `build_report_data` reads comes from here, so the returned score
    is produced by the real scoring code rather than by a stub of it.
    """
    artifacts = tmp_path / "artifacts"
    dist = artifacts / "dist"
    dist.mkdir(parents=True)
    # Per-file percentages as coverage.py writes them, Windows separators and
    # all: the floor is scored from this section, so a payload with only
    # `totals` would make every test built on this fixture prove less than it
    # looks like it does.
    (artifacts / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 96.12},
                "files": {
                    "src\\browsertap_mcp\\server.py": {
                        "summary": {"percent_covered": 83.56}
                    },
                    "src\\browsertap_mcp\\bridge.py": {
                        "summary": {"percent_covered": 63.24}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    passing_xml = '<testsuite><testcase classname="c" name="t" /></testsuite>'
    (artifacts / "offline-junit.xml").write_text(passing_xml, encoding="utf-8")
    (artifacts / "live-junit.xml").write_text(passing_xml, encoding="utf-8")
    (artifacts / "live-preflight.json").write_text(
        json.dumps(_passing_live_preflight()), encoding="utf-8"
    )
    for name in ("tool-coverage-offline.json", "tool-coverage-live.json"):
        (artifacts / name).write_text(json.dumps(_passing_tool_evidence()), encoding="utf-8")
    (artifacts / "lint.json").write_text(json.dumps(_passing_lint_report()), encoding="utf-8")
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
                artifacts / "live-preflight.json",
                artifacts / "tool-coverage-offline.json",
                artifacts / "tool-coverage-live.json",
                artifacts / "lint.json",
                wheel,
                sdist,
            )
        },
    }
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(A, "validate_manifest", lambda **_kwargs: (manifest, []))
    monkeypatch.setattr(A, "build_docs_report", _passing_docs_report)
    monkeypatch.setattr(A, "validate_archive", lambda _path: [])
    monkeypatch.setattr(A, "runtime_package_mismatch", lambda _wheel, _sdist: [])
    return manifest


def test_complete_sealed_evidence_scores_every_gate(monkeypatch, tmp_path):
    """The full-marks claim has to come from the scorer, not from a stub.

    The only other test of the scoring path replaces `build_report_data`
    wholesale, so nothing exercised the gate expressions that produce the
    published score. The total is computed from the weight table rather than
    written here: a literal would have to be re-typed for every gate added, and
    a score compared against a stale literal is the failure this whole file is
    about.
    """
    _seal_release_evidence(monkeypatch, tmp_path)

    data = A.build_report_data()

    assert data["evidence_problems"] == []
    assert data["evidence_fresh"] is True
    assert data["gates"] == dict.fromkeys(A.GATE_WEIGHTS, True)
    assert data["objective_score"] == sum(A.GATE_WEIGHTS.values())
    assert data["objective_score_total"] == sum(A.GATE_WEIGHTS.values())
    assert data["release_ready"] is True
    assert data["version"] == "9.9.9"
    assert data["code_coverage"] == 96.12
    assert data["per_file_coverage"]["status"] == "ok"
    assert data["per_file_coverage"]["below"] == []
    assert data["per_file_coverage"]["weakest"] == {
        "file": "src/browsertap_mcp/bridge.py",
        "percent": 63.24,
    }
    # A passing bound live run must promote the live tool evidence file.
    assert data["tool_coverage_source"] == "artifacts/tool-coverage-live.json"
    assert data["live"]["status"] == "pass"
    assert data["distribution_summary"] == "2 manifest-bound archive(s) validated"


def test_distribution_gate_checks_the_manifest_bound_pair(monkeypatch, tmp_path):
    """Per-archive success must not hide a wheel/sdist package-set mismatch."""
    _seal_release_evidence(monkeypatch, tmp_path)
    monkeypatch.setattr(
        A,
        "runtime_package_mismatch",
        lambda _wheel, _sdist: [
            "wheel contains package files absent from sdist: browsertap_mcp/retired.py"
        ],
    )

    data = A.build_report_data()

    assert data["gates"]["distributions"] is False
    assert data["release_ready"] is False
    assert "archive contract violation" in data["distribution_summary"]


def test_every_weighted_gate_names_what_it_measured(monkeypatch, tmp_path):
    """A verdict with no measurement is the shape four of these gates shipped as.

    ruff over a path that matched nothing, eslint whose `files:` pattern had
    stopped matching, a coverage payload with no per-file section, a distribution
    check with no archives bound -- each reported zero problems and each was
    measuring nothing. The fixes were local, so the tenth gate would have started
    out unprotected all over again. This asserts the structural half: every gate
    in the weight table produces a non-empty description of what it read, and the
    report publishes it next to the verdict so it has a reader.
    """
    _seal_release_evidence(monkeypatch, tmp_path)

    data = A.build_report_data()
    measurements = data["gate_measurements"]

    assert data["gate_structure_problems"] == []
    # Keyed off the weight table, not a list typed here: a gate added to the
    # table without a measurement has to fail this rather than be forgotten.
    assert set(measurements) == set(A.GATE_WEIGHTS)
    for name in A.GATE_WEIGHTS:
        assert measurements[name].strip(), name
        assert measurements[name] not in {"nothing measured", "gate not evaluated"}, name

    text = A.render_report(data)
    for name in A.GATE_WEIGHTS:
        assert f"| `{name}` | {A.GATE_WEIGHTS[name]} | PASS | {measurements[name]} |" in text


def test_a_gate_that_passes_without_naming_a_measurement_is_scored_fail():
    """The guard itself, driven directly, because the real gates all comply.

    Only a mutation can prove this one fires, and mutating a real gate would
    prove it for that gate alone. Calling the guard with a blank measurement is
    the whole point: `True` is not enough, and the reason has to be published
    rather than swallowed.
    """
    passing = {name: (True, f"read {name}") for name in A.GATE_WEIGHTS}
    victim = next(iter(A.GATE_WEIGHTS))

    gates, measurements, problems = A._finalize_gates({**passing, victim: (True, "   ")})

    assert gates[victim] is False
    assert measurements[victim] == "nothing measured"
    assert any(victim in problem and "without naming" in problem for problem in problems)
    # Every other gate is untouched, so the guard is not a blanket refusal.
    assert all(gates[name] for name in A.GATE_WEIGHTS if name != victim)


def test_a_weighted_gate_nobody_evaluated_is_a_hole_not_a_pass():
    """A gate absent from the results used to be a KeyError or a silent skip.

    Neither is right: the weight is still in the denominator, so the score has
    to lose those points and say why.
    """
    victim = next(iter(A.GATE_WEIGHTS))
    partial = {name: (True, f"read {name}") for name in A.GATE_WEIGHTS if name != victim}

    gates, measurements, problems = A._finalize_gates(partial)

    assert set(gates) == set(A.GATE_WEIGHTS)
    assert gates[victim] is False
    assert measurements[victim] == "gate not evaluated"
    assert any(victim in problem and "never evaluated" in problem for problem in problems)


def test_a_gate_with_no_weight_is_reported_as_having_no_reader():
    """The other direction, and the one this repository has met four times.

    A check that runs, reports, and carries no weight leaves the score identical
    whether it passed or failed -- so the weight table is the authority on what
    exists, and an extra result is a defect in the wiring rather than a bonus.
    """
    passing = {name: (True, f"read {name}") for name in A.GATE_WEIGHTS}

    gates, measurements, problems = A._finalize_gates(
        {**passing, "supply_chain": (True, "audited 41 dependencies")}
    )

    assert set(gates) == set(A.GATE_WEIGHTS)
    assert "supply_chain" not in measurements
    assert any("supply_chain" in problem and "no weight" in problem for problem in problems)


def test_a_structural_problem_forfeits_the_release_even_with_every_gate_green(
    monkeypatch, tmp_path
):
    """`release_ready` cannot be true over a table that cannot vouch for itself.

    Scoring the points and refusing the release are different questions: a gate
    whose measurement is missing may still have passed, so the honest answer is
    that the report does not know -- and the reason belongs in the rendered
    document, not only in the JSON nobody opens.
    """
    _seal_release_evidence(monkeypatch, tmp_path)
    real = A._finalize_gates
    monkeypatch.setattr(
        A,
        "_finalize_gates",
        lambda measured: (
            real(measured)[0],
            real(measured)[1],
            ["gate `supply_chain` was evaluated but carries no weight, so nothing reads it"],
        ),
    )

    data = A.build_report_data()
    text = A.render_report(data)

    assert data["gates"] == dict.fromkeys(A.GATE_WEIGHTS, True)
    assert data["objective_score"] == sum(A.GATE_WEIGHTS.values())
    assert data["release_ready"] is False
    assert "cannot vouch for itself" in text
    assert "supply_chain" in text
    assert "Release ready: false" in text


def test_a_module_rotting_away_fails_the_coverage_gate(monkeypatch, tmp_path):
    """The total gate is an average, and an average hides a dead module.

    `bridge.py` is where the platform-specific daemon code lives and the least
    covered file in the package; the total stayed comfortably above the line the
    whole time it did. The floor has to fail on one file falling away even while
    the total still passes, which is the case a global threshold cannot see.
    """
    _seal_release_evidence(monkeypatch, tmp_path)
    (tmp_path / "artifacts" / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {"percent_covered": 96.12},
                "files": {
                    "src\\browsertap_mcp\\server.py": {
                        "summary": {"percent_covered": 97.0}
                    },
                    "src\\browsertap_mcp\\bridge.py": {
                        "summary": {"percent_covered": 11.5}
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    data = A.build_report_data()

    assert data["code_coverage"] == 96.12
    per_file = data["per_file_coverage"]
    assert per_file["status"] == "ok"
    assert per_file["floor"] == A.PER_FILE_COVERAGE_FLOOR
    assert per_file["below"] == [{"file": "src/browsertap_mcp/bridge.py", "percent": 11.5}]
    assert data["gates"]["code_coverage"] is False
    assert data["release_ready"] is False
    # The reader has to be told which file, or the failure is unactionable.
    assert "src/browsertap_mcp/bridge.py 11.50%" in A.render_report(data)


def test_coverage_without_per_file_data_is_not_a_pass(monkeypatch, tmp_path):
    """A floor with nothing to measure must not read as "nothing is below it".

    This is the shape of every silent pass this repository has had to fix: a
    missing input makes the predicate vacuously true, so the gate reports
    success for the reason it should be reporting failure.
    """
    _seal_release_evidence(monkeypatch, tmp_path)
    (tmp_path / "artifacts" / "coverage.json").write_text(
        json.dumps({"totals": {"percent_covered": 99.0}}), encoding="utf-8"
    )

    data = A.build_report_data()

    assert data["code_coverage"] == 99.0
    assert data["per_file_coverage"]["status"] == "unavailable"
    assert data["per_file_coverage"]["measured"] == 0
    assert data["gates"]["code_coverage"] is False
    assert "per-file coverage unavailable" in A.render_report(data)


@pytest.mark.parametrize("percent,passes", [(94.99, False), (95.0, True), (98.0, True)])
def test_total_coverage_requires_ninety_five_percent(monkeypatch, tmp_path, percent, passes):
    _seal_release_evidence(monkeypatch, tmp_path)
    path = tmp_path / "artifacts" / "coverage.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["totals"]["percent_covered"] = percent
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = A.build_report_data()
    assert report["gates"]["code_coverage"] is passes
    assert "gate 95.00%" in A.render_report(report)


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
        "lint": False,
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


def test_live_evidence_gate_reads_the_extension_tree_binding(monkeypatch, tmp_path):
    """A passing live XML cannot vouch for a worker running code outside the tree."""
    _seal_release_evidence(monkeypatch, tmp_path)
    preflight = _passing_live_preflight()
    preflight["components"]["extension_build_verdict"] = "stale_worker"
    (tmp_path / "artifacts" / "live-preflight.json").write_text(
        json.dumps(preflight), encoding="utf-8"
    )

    data = A.build_report_data()

    assert data["gates"]["live_suite"] is True
    assert data["gates"]["live_evidence"] is False
    assert data["release_ready"] is False
    measurement = data["gate_measurements"]["live_evidence"]
    assert "stale_worker" in measurement
    assert "outside the sealed source tree" in measurement


def test_unknown_extension_tree_binding_is_not_a_pass(monkeypatch, tmp_path):
    """An absent or unenforced comparison is unknown, not a successful comparison."""
    _seal_release_evidence(monkeypatch, tmp_path)
    path = tmp_path / "artifacts" / "live-preflight.json"
    variants = []

    unverifiable = _passing_live_preflight()
    unverifiable["components"].update(
        extension_build_verdict="unverifiable", extension_build_enforced=False
    )
    variants.append(unverifiable)

    missing = _passing_live_preflight()
    missing["components"].pop("extension_build_verdict")
    variants.append(missing)

    for preflight in variants:
        path.write_text(json.dumps(preflight), encoding="utf-8")
        data = A.build_report_data()
        measurement = data["gate_measurements"]["live_evidence"]

        assert data["gates"]["live_evidence"] is False
        assert "unknown:" in measurement


def test_an_unregenerated_extension_stamp_names_its_fix(monkeypatch, tmp_path):
    _seal_release_evidence(monkeypatch, tmp_path)
    preflight = _passing_live_preflight()
    preflight["components"].update(
        extension_build_verdict="stamp_not_regenerated", extension_build_enforced=False
    )
    (tmp_path / "artifacts" / "live-preflight.json").write_text(
        json.dumps(preflight), encoding="utf-8"
    )

    data = A.build_report_data()
    measurement = data["gate_measurements"]["live_evidence"]

    assert data["gates"]["live_evidence"] is False
    assert "stamp_not_regenerated" in measurement
    assert "python -m scripts.extension_stamp --write" in measurement


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
        "per_file_coverage": {
            "floor": A.PER_FILE_COVERAGE_FLOOR,
            "measured": 0,
            "below": [],
            "weakest": None,
            "status": "not-bound",
        },
        "per_file_coverage_source": "missing",
        "versions": {},
        "live": {
            "status": "not-run",
            "summary": "unavailable",
            "source": "artifacts/live-junit.xml",
        },
        "distribution_summary": "missing",
        "lint_summary": "missing",
        "evidence_fresh": False,
        "evidence_problems": ["evidence manifest unavailable"],
    }
    monkeypatch.setattr(A, "build_report_data", lambda: data)
    output = tmp_path / "acceptance.md"

    assert A.main(["--output", str(output)]) == 1
    text = output.read_text(encoding="utf-8")
    # Denominator computed here too. A literal on both sides of the comparison
    # agrees with itself while disagreeing with the weight table, which is the
    # only thing either of them is meant to describe.
    assert f"Score: 0/{sum(A.GATE_WEIGHTS.values())}" in text
    assert "Release ready: false" in text
    assert "95-Point" not in text


def test_the_rendered_denominator_tracks_the_weight_table(monkeypatch, tmp_path):
    """A score is a fraction, and its denominator used to be hardcoded.

    `render_report` wrote `/100` as a literal beside a weight table anyone could
    add a gate to, so the first added gate would have published `105/100`. Both
    numbers now come from the table; this fails if either goes back to a literal.
    """
    _seal_release_evidence(monkeypatch, tmp_path)

    data = A.build_report_data()
    text = A.render_report(data)
    total = sum(A.GATE_WEIGHTS.values())

    assert A.TOTAL_GATE_WEIGHT == total
    assert f"**Score: {total}/{total}**" in text
    # Every gate in the table is rendered as its own row, so a gate can be added
    # to the scorer without appearing in the report only if this also fails.
    for name in A.GATE_WEIGHTS:
        assert f"| `{name}` |" in text


def test_lint_violations_fail_the_gate(monkeypatch, tmp_path):
    """CI ran ruff, the seal recorded nothing, and both verdicts were correct.

    `release_ready: true` over a tree that `.github/workflows/test.yml` was about
    to fail is the state this gate exists to make impossible, and the sealed
    report was the one quoted in release notes.
    """
    _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    report.update(
        status="violations",
        exit_code=1,
        violation_count=2,
        violations=[
            {"code": "F401", "file": "src/browsertap_mcp/server.py", "line": 3, "message": "unused"},
            {"code": "E402", "file": "scripts/versioning.py", "line": 9, "message": "import"},
        ],
    )
    (tmp_path / "artifacts" / "lint.json").write_text(json.dumps(report), encoding="utf-8")

    data = A.build_report_data()

    assert data["gates"]["lint"] is False
    assert data["release_ready"] is False
    assert data["objective_score"] == sum(A.GATE_WEIGHTS.values()) - A.GATE_WEIGHTS["lint"]
    assert "2 lint violation(s) from ruff 9.9.9" in A.render_report(data)


def test_a_lint_run_that_scanned_nothing_is_not_a_pass(monkeypatch, tmp_path):
    """Zero violations over zero files is the vacuous pass, not a clean tree.

    `ruff check` against a path that matches nothing exits 0 with an empty
    diagnostic list. Narrowing the target list would then turn the gate green
    while checking less and less, which is the same shape as a skills mirror
    check with no directory or a quiet-input gate with nothing to compare.
    """
    _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    report.update(files_scanned=0, files_per_target={"src": 0, "tests": 0, "scripts": 0})
    (tmp_path / "artifacts" / "lint.json").write_text(json.dumps(report), encoding="utf-8")

    data = A.build_report_data()

    assert data["gates"]["lint"] is False
    assert "scanned no files" in A.render_report(data)


def test_an_unbound_lint_artifact_fails_the_gate(monkeypatch, tmp_path):
    """A lint result the seal does not cover proves nothing about this tree."""
    manifest = _seal_release_evidence(monkeypatch, tmp_path)
    manifest["artifacts"].pop("artifacts/lint.json")

    data = A.build_report_data()

    assert data["gates"]["lint"] is False
    assert "not bound by the evidence manifest" in data["lint_summary"]


def test_javascript_lint_violations_fail_the_same_gate(monkeypatch, tmp_path):
    """The extension is ~4.9k lines of the wheel; ruff cannot see one of them."""
    _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    report["javascript"].update(
        status="violations",
        violation_count=3,
        violations=[
            {
                "code": "no-unused-vars",
                "file": "src/browsertap_mcp/chrome_extension/background.js",
                "line": 56,
                "message": "'MAX_CDP_TIMEOUT_MS' is assigned a value but never used.",
            }
        ],
    )
    (tmp_path / "artifacts" / "lint.json").write_text(json.dumps(report), encoding="utf-8")

    data = A.build_report_data()

    assert data["gates"]["lint"] is False
    assert data["release_ready"] is False
    assert "3 JavaScript lint violation(s) from eslint 9.9.9" in A.render_report(data)


def test_a_release_may_not_seal_over_a_javascript_lint_that_never_ran(
    monkeypatch, tmp_path
):
    """`unavailable` is honest, and it is still not a pass.

    `scripts.lint_report` exits 0 without node so a contributor keeps the Python
    half. A release is the one verdict that may not be quiet about half the
    shipped code, so the refusal lives here and names the fix.
    """
    _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    report["javascript"] = {
        "tool": "eslint",
        "tool_version": None,
        "available": False,
        "enforced": False,
        "unavailable_reason": "node_modules/eslint/bin/eslint.js is absent; run `npm ci`",
        "targets": ["src/browsertap_mcp/chrome_extension"],
        "files_scanned": 0,
        "status": "unavailable",
        "violation_count": 0,
        "violations": [],
        "violations_truncated": False,
        "problems": [],
    }
    (tmp_path / "artifacts" / "lint.json").write_text(json.dumps(report), encoding="utf-8")

    data = A.build_report_data()

    assert data["gates"]["lint"] is False
    assert "was not enforced" in data["lint_summary"]
    assert "npm ci" in data["lint_summary"]


def test_a_javascript_lint_that_enforced_no_rules_is_not_a_pass(monkeypatch, tmp_path):
    """Zero violations over four files it applied no rules to.

    eslint walks a directory and reports every file clean when the flat config's
    `files:` pattern no longer matches them -- measured on a real run, a file
    with an obvious unused variable came back `clean`. So the artifact records
    what the config would enforce, and a clean verdict without it is refused
    here for the same reason `own_tabs.enforced` exists.
    """
    _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    report["javascript"].update(enforced=False, rules_applied=0)
    (tmp_path / "artifacts" / "lint.json").write_text(json.dumps(report), encoding="utf-8")

    data = A.build_report_data()

    assert data["gates"]["lint"] is False
    assert "without enforcing anything" in data["lint_summary"]


def test_a_lint_artifact_with_no_javascript_half_fails_the_gate(monkeypatch, tmp_path):
    """An artifact predating the JavaScript half proves nothing about it."""
    _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    report.pop("javascript")
    (tmp_path / "artifacts" / "lint.json").write_text(json.dumps(report), encoding="utf-8")

    data = A.build_report_data()

    assert data["gates"]["lint"] is False
    assert "records no JavaScript half" in data["lint_summary"]


@pytest.mark.parametrize("types", [
    None,
    {"status": "clean"},
    {"status": "unavailable", "unavailable_reason": 'pip install -e ".[dev]"'},
])
def test_a_release_requires_type_check_evidence(monkeypatch, tmp_path, types):
    manifest = _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    if types is not None:
        report["types"] = types
    else:
        report.pop("types", None)
    (tmp_path / "artifacts/lint.json").write_text(json.dumps(report), encoding="utf-8")
    ok, reason = A._lint_status(manifest)
    assert ok is False
    assert "type" in reason.lower()


@pytest.mark.parametrize("changes", [
    {"status": "violations", "violation_count": 1, "exit_code": 1},
    {"exit_code": 2}, {"enforced": False}, {"enforced": "true"},
    {"available": False}, {"check_untyped_defs": False},
    {"files_scanned": 0}, {"files_expected": 3}, {"files": []},
    {"files": ["src/a.py", "src/a.py"]}, {"violation_count": "0"},
    {"problems": ["type check did not finish"]},
])
def test_type_check_result_cannot_be_a_vacuous_pass(monkeypatch, tmp_path, changes):
    manifest = _seal_release_evidence(monkeypatch, tmp_path)
    report = _passing_lint_report()
    report["types"].update(changes)
    (tmp_path / "artifacts/lint.json").write_text(json.dumps(report), encoding="utf-8")
    assert A._lint_status(manifest)[0] is False
