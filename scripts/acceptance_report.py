"""Generate an evidence-derived BTAP release acceptance report."""

from __future__ import annotations

import argparse
import json
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from scripts.check_distribution import runtime_package_mismatch, validate_archive
from scripts.check_tool_docs import build_report as build_docs_report
from scripts.check_tool_docs import report_ok as docs_ok
from scripts.evidence_manifest import validate_manifest

# A single global percentage is an average, and an average hides a module that
# has stopped being tested: `bridge.py` holds most of the platform-specific
# daemon code and is the least covered file in the package, while the total sat
# comfortably above its gate the whole time. coverage.py has no per-file
# threshold, so the floor is enforced here, from the same sealed artifact the
# total is read from. It is a rot detector rather than a target: the weakest
# module today is around 63%, so this passes now and fails when a file falls
# away. Raise it when the weakest module improves; do not lower it to turn a
# red gate green.
PER_FILE_COVERAGE_FLOOR = 60.0

GATE_WEIGHTS = {
    "tool_contract": 15,
    "offline_evidence": 15,
    "live_evidence": 20,
    "code_coverage": 20,
    "documentation": 10,
    "versions": 5,
    "distributions": 5,
    "live_suite": 10,
    "lint": 5,
}
# Derived, never typed twice. This used to be a literal `100` in the rendered
# line beside a table anyone could edit, so adding a gate here would have shipped
# a report scoring 105 out of a hardcoded 100 -- a mutable numerator over a
# frozen denominator, which is the same defect the per-file coverage floor and
# the licence table each had to be rescued from.
TOTAL_GATE_WEIGHT = sum(GATE_WEIGHTS.values())


def _status(value: bool) -> str:
    return "PASS" if value else "FAIL"


def _recorded(manifest: dict[str, object] | None, relative: str) -> bool:
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    return isinstance(artifacts, dict) and relative in artifacts


def _code_coverage(manifest: dict[str, object] | None) -> tuple[float | None, str]:
    relative = "artifacts/coverage.json"
    if not _recorded(manifest, relative):
        return None, "coverage artifact is not bound by the evidence manifest"
    try:
        payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        percent = float(payload["totals"]["percent_covered"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, "coverage artifact unavailable"
    return percent, relative


def _per_file_coverage(
    manifest: dict[str, object] | None, *, floor: float = PER_FILE_COVERAGE_FLOOR
) -> tuple[dict[str, object], str]:
    """Report every measured file under `floor`, and say when nothing was measured.

    A coverage payload with no per-file section must not read as "no file is
    below the floor" -- that is a pass produced by absence, the same shape as a
    quiet-input gate with nothing to compare. `status` carries it explicitly and
    the gate requires `ok`.
    """
    relative = "artifacts/coverage.json"
    empty: dict[str, object] = {
        "floor": floor,
        "measured": 0,
        "below": [],
        "weakest": None,
        "status": "not-bound",
    }
    if not _recorded(manifest, relative):
        return empty, "coverage artifact is not bound by the evidence manifest"
    try:
        payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        files = payload["files"]
        if not isinstance(files, dict) or not files:
            raise KeyError("files")
        measured = {
            str(name).replace("\\", "/"): float(entry["summary"]["percent_covered"])
            for name, entry in files.items()
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {**empty, "status": "unavailable"}, "per-file coverage unavailable"
    below = sorted(
        (
            {"file": name, "percent": round(percent, 2)}
            for name, percent in measured.items()
            if percent < floor
        ),
        key=lambda row: (row["percent"], row["file"]),
    )
    weakest_file, weakest_percent = min(measured.items(), key=lambda row: (row[1], row[0]))
    return {
        "floor": floor,
        "measured": len(measured),
        "below": below,
        "weakest": {"file": weakest_file, "percent": round(weakest_percent, 2)},
        "status": "ok",
    }, relative


def _junit(relative: str, manifest: dict[str, object] | None) -> dict[str, object]:
    if not _recorded(manifest, relative):
        return {
            "status": "not-bound",
            "source": relative,
            "summary": "not recorded in evidence manifest",
            "tests": 0,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        }
    path = ROOT / relative
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        return {
            "status": "not-run",
            "source": relative,
            "summary": f"unavailable ({type(exc).__name__})",
            "tests": 0,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        }

    cases = list(root.iter("testcase"))
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    tests = len(cases)
    status = "pass" if tests and not (failures or errors or skipped) else "fail"
    return {
        "status": status,
        "source": relative,
        "summary": (f"tests={tests}, failures={failures}, errors={errors}, skipped={skipped}"),
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }


def _live_junit(manifest: dict[str, object] | None) -> dict[str, object]:
    return _junit("artifacts/live-junit.xml", manifest)


def _offline_junit(manifest: dict[str, object] | None) -> dict[str, object]:
    return _junit("artifacts/offline-junit.xml", manifest)


def _tool_coverage(
    manifest: dict[str, object] | None, live_passed: bool
) -> tuple[dict[str, object], str]:
    candidates = ["artifacts/tool-coverage-live.json"] if live_passed else []
    candidates.append("artifacts/tool-coverage-offline.json")
    for relative in candidates:
        if not _recorded(manifest, relative):
            continue
        try:
            payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload, relative
    return {}, "tool evidence artifact missing"


def _distribution_status(manifest: dict[str, object] | None) -> tuple[bool, str]:
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, dict):
        return False, "distribution artifacts are not bound by the evidence manifest"
    relative_paths = sorted(
        name for name in artifacts if name.startswith("artifacts/dist/")
    )
    if not relative_paths:
        return False, "distribution artifacts are not bound by the evidence manifest"
    failures = {}
    for relative in relative_paths:
        path = ROOT / relative
        try:
            issues = validate_archive(path)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            issues = [str(exc)]
        if issues:
            failures[relative] = issues
    wheels = [relative for relative in relative_paths if relative.endswith(".whl")]
    sdists = [
        relative
        for relative in relative_paths
        if relative.endswith((".tar.gz", ".tgz"))
    ]
    # ``validate_archive`` is intentionally per-file.  The release contract
    # also requires the one wheel and one sdist to carry the same installable
    # package set; otherwise a stale build directory can make each archive look
    # valid while the pair is irreproducible.  The manifest already enforces the
    # one-of-each shape, but keep the guard explicit so a malformed/legacy
    # manifest cannot turn a missing pair into a vacuous pass.
    if len(wheels) == 1 and len(sdists) == 1:
        wheel, sdist = (ROOT / wheels[0], ROOT / sdists[0])
        try:
            issues = runtime_package_mismatch(wheel, sdist)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
            issues = [f"cross-archive comparison failed: {type(exc).__name__}: {exc}"]
        if issues:
            failures.setdefault(wheels[0], []).extend(issues)
    else:
        failures["distribution-pair"] = [
            "manifest-bound distributions must contain exactly one wheel and one source archive"
        ]
    if failures:
        issue_count = sum(len(issues) for issues in failures.values())
        return False, f"{issue_count} archive contract violation(s)"
    return True, f"{len(relative_paths)} manifest-bound archive(s) validated"


def _lint_status(manifest: dict[str, object] | None) -> tuple[bool, str]:
    """Read the sealed lint result, and refuse a pass produced by absence.

    `ruff check` over a path that matches nothing exits 0 with an empty
    diagnostic list, so "zero violations" is only meaningful together with what
    was scanned. `scripts/lint_report.py` records both and marks a target that
    matched no files as an error rather than letting it contribute a silent zero;
    this reads that verdict instead of re-deriving it, so the two cannot drift.
    """
    relative = "artifacts/lint.json"
    if not _recorded(manifest, relative):
        return False, "lint artifact is not bound by the evidence manifest"
    try:
        payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        status = str(payload["status"])
        violations = int(payload["violation_count"])
        files_scanned = int(payload["files_scanned"])
        targets = payload["targets"]
        tool_version = str(payload["tool_version"])
        problems = payload["problems"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False, "lint artifact unavailable or malformed"
    if not isinstance(targets, list) or not targets:
        return False, "lint artifact records no target paths"
    if isinstance(problems, list) and problems:
        return False, "; ".join(str(problem) for problem in problems)
    if status != "clean" or violations:
        return False, f"{violations} lint violation(s) from {tool_version}"
    if files_scanned <= 0:
        return False, "lint reported no violations because it scanned no files"

    # The second half of the same gate. Roughly 4.9k lines of the wheel are the
    # Chrome extension, so a report that said "lint PASS" while nothing had ever
    # read that JavaScript was claiming more than it had measured.
    js = payload.get("javascript")
    if not isinstance(js, dict):
        return False, "lint artifact records no JavaScript half"
    try:
        js_status = str(js["status"])
        js_violations = int(js["violation_count"])
        js_enforced = bool(js["enforced"])
        js_files = int(js["files_scanned"])
        js_version = js["tool_version"]
        js_problems = js["problems"]
        js_reason = js["unavailable_reason"]
    except (KeyError, TypeError, ValueError):
        return False, "lint artifact's JavaScript half is malformed"
    if js_status == "unavailable":
        # Deliberately a refusal here and only here. `scripts.lint_report` exits 0
        # so a contributor without node still gets the Python half; a release is
        # the one verdict that may not be silent about half the shipped code.
        return False, f"JavaScript lint was not enforced: {js_reason}"
    if isinstance(js_problems, list) and js_problems:
        return False, "; ".join(str(problem) for problem in js_problems)
    if js_status != "clean" or js_violations:
        return False, f"{js_violations} JavaScript lint violation(s) from eslint {js_version}"
    if not js_enforced or js_files <= 0:
        return False, "eslint reported no violations without enforcing anything"
    return True, (
        f"{tool_version} clean over {files_scanned} file(s) in "
        f"{', '.join(map(str, targets))}; eslint {js_version} clean over "
        f"{js_files} extension file(s)"
    )


def _bind_live_status(
    live: dict[str, object], manifest: dict[str, object] | None
) -> tuple[dict[str, object], bool]:
    """Treat a passing live XML as current only when its manifest includes live."""
    live_bound = bool(
        manifest
        and manifest.get("include_live") is True
        and _recorded(manifest, "artifacts/live-junit.xml")
    )
    if live.get("status") == "pass" and not live_bound:
        stale = dict(live)
        stale["status"] = "stale"
        stale["summary"] = (
            f"{live.get('summary', 'live result')} (not bound to current source tree)"
        )
        return stale, False
    return live, live_bound


def _extension_build_status(
    manifest: dict[str, object] | None,
) -> tuple[bool, str]:
    """Read the one sealed record that can bind the running worker to this tree."""
    relative = "artifacts/live-preflight.json"
    if not _recorded(manifest, relative):
        return False, f"unknown: `{relative}` is not bound by the evidence manifest"
    try:
        payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        components = payload["components"]
    except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError):
        return False, f"unknown: `{relative}` is unavailable or malformed"
    if not isinstance(components, dict):
        return False, f"unknown: `{relative}` has no component record"

    missing = [
        field
        for field in ("extension_build_verdict", "extension_build_enforced")
        if field not in components
    ]
    if missing:
        return False, f"unknown: `{relative}` is missing {', '.join(missing)}"

    verdict = components["extension_build_verdict"]
    enforced = components["extension_build_enforced"]
    if verdict == "stale_worker":
        return False, (
            "stale_worker: the live run was answered by extension code outside "
            f"the sealed source tree, recorded in `{relative}`"
        )
    if verdict == "stamp_not_regenerated":
        return False, (
            "stamp_not_regenerated: regenerate the extension stamp with "
            "`python -m scripts.extension_stamp --write`"
        )
    if enforced is not True:
        return False, (
            "unknown: extension build comparison was not enforced "
            f"(verdict={verdict!r}) in `{relative}`"
        )
    if verdict != "matches_tree":
        return False, f"unknown: extension build verdict is {verdict!r} in `{relative}`"

    reported_stamp = components.get("extension_build_stamp")
    expected_stamp = components.get("expected_extension_build_stamp")
    if not isinstance(reported_stamp, str) or not isinstance(expected_stamp, str):
        return False, f"unknown: `{relative}` is missing the extension build stamps"
    if reported_stamp != expected_stamp:
        return False, (
            "unknown: extension build stamps disagree despite a matches_tree verdict "
            f"in `{relative}`"
        )
    return True, (
        f"extension build matches_tree with enforcement from `{relative}` "
        f"(stamp {reported_stamp})"
    )


def _sealed_source(manifest: dict[str, object] | None) -> dict[str, object]:
    source = manifest.get("source") if isinstance(manifest, dict) else None
    return source if isinstance(source, dict) else {}


def _per_file_summary(per_file: dict[str, object]) -> str:
    """One line for the per-file coverage result, read by the gate and the report.

    Both need it, and deriving it in two places is how the two come to disagree
    about the same artifact.
    """
    below = per_file.get("below") or []
    weakest = per_file.get("weakest")
    if below:
        return "below the per-file floor: " + ", ".join(
            f"{row['file']} {float(row['percent']):.2f}%" for row in below
        )
    if per_file.get("status") == "ok" and isinstance(weakest, dict):
        return (
            f"{per_file['measured']} files measured, weakest "
            f"{weakest['file']} {float(weakest['percent']):.2f}%"
        )
    return f"per-file coverage {per_file.get('status')}"


def _finalize_gates(
    measured: dict[str, tuple[bool, str]],
) -> tuple[dict[str, bool], dict[str, str], list[str]]:
    """Pair every gate with what it read, and refuse a verdict that names nothing.

    Four of the nine gates here have already had to be rescued from a pass
    produced by absence: ruff over a path that matched nothing, eslint with a
    `files:` pattern that had stopped matching, a coverage payload with no
    per-file section, a distribution check with no archives bound. Each fix was
    local to the gate that had already failed, which leaves the *next* gate
    starting out unprotected -- so the shape has to be structural or it simply
    recurs. A gate that cannot say what it looked at is scored FAIL and the
    reason recorded, rather than believed because it happens to be written True.

    The weight table stays the authority on what exists, in both directions. A
    weighted gate nobody evaluated is a hole in the score; an evaluated gate with
    no weight is a gate with no reader -- it runs, it reports, and the score is
    identical whether it passed or failed, which is the other failure this
    repository keeps meeting.
    """
    problems: list[str] = []
    gates: dict[str, bool] = {}
    measurements: dict[str, str] = {}
    for name in GATE_WEIGHTS:
        if name not in measured:
            gates[name] = False
            measurements[name] = "gate not evaluated"
            problems.append(f"gate `{name}` carries weight but was never evaluated")
            continue
        verdict, description = measured[name]
        summary = str(description).strip()
        if not summary:
            gates[name] = False
            measurements[name] = "nothing measured"
            problems.append(
                f"gate `{name}` reported {_status(bool(verdict))} without naming what it measured"
            )
            continue
        gates[name] = bool(verdict)
        measurements[name] = summary
    for name in measured:
        if name not in GATE_WEIGHTS:
            problems.append(
                f"gate `{name}` was evaluated but carries no weight, so nothing reads it"
            )
    return gates, measurements, problems


def build_report_data() -> dict[str, object]:
    evidence_manifest, evidence_problems = validate_manifest(require_live=False)
    offline = _offline_junit(evidence_manifest)
    live = _live_junit(evidence_manifest)
    live, live_bound = _bind_live_status(live, evidence_manifest)
    live_passed = live["status"] == "pass" and live_bound
    extension_build_ok, extension_build_summary = _extension_build_status(evidence_manifest)
    sealed_source = _sealed_source(evidence_manifest)
    if sealed_source.get("git_dirty") is True:
        # A seal taken over an uncommitted worktree cannot be reproduced from Git:
        # `git_head` alone does not identify the code that produced the artifacts.
        evidence_problems = [
            *evidence_problems,
            "sealed source tree was dirty (uncommitted or untracked files); "
            "commit the release surface and re-seal",
        ]
    evidence_fresh = not evidence_problems
    tool_coverage, tool_coverage_source = _tool_coverage(evidence_manifest, live_passed)
    code_coverage, code_coverage_source = _code_coverage(evidence_manifest)
    per_file_coverage, per_file_coverage_source = _per_file_coverage(evidence_manifest)
    docs = build_docs_report()
    versions = docs.get("versions") or {}
    distributions_ok, distribution_summary = _distribution_status(evidence_manifest)
    lint_ok, lint_summary = _lint_status(evidence_manifest)

    registered = int(tool_coverage.get("registered", 0))
    contract_valid = int(tool_coverage.get("contract_valid_tools", 0))
    offline_execution = tool_coverage.get("offline_execution") or {}
    if not isinstance(offline_execution, dict):
        offline_execution = {}
    per_file_summary = _per_file_summary(per_file_coverage)
    coverage_text = (
        f"{code_coverage:.2f}% total from `{code_coverage_source}` (gate 85.00%); "
        f"per-file floor {PER_FILE_COVERAGE_FLOOR:.2f}%, {per_file_summary}"
        if code_coverage is not None
        else f"no total coverage: {code_coverage_source}; {per_file_summary}"
    )
    tool_contract_ok = evidence_fresh and registered == 49 and contract_valid == registered
    offline_evidence_ok = (
        evidence_fresh
        and offline.get("status") == "pass"
        and offline_execution.get("exit_code") == 0
        and not tool_coverage.get("failed_evidence")
        and not tool_coverage.get("unclassified_evidence")
    )
    live_evidence_ok = (
        evidence_fresh
        and live_passed
        and extension_build_ok
        and tool_coverage.get("all_evidence_executed") is True
        and tool_coverage.get("fully_verified_tools") == registered == 49
    )
    code_coverage_ok = (
        evidence_fresh
        and code_coverage is not None
        and code_coverage >= 85.0
        and per_file_coverage["status"] == "ok"
        and not per_file_coverage["below"]
    )
    versions_ok = (
        not docs.get("version_error") and bool(versions) and len(set(versions.values())) == 1
    )
    # (verdict, what was actually read). The second half is not decoration: a
    # verdict whose measurement is empty is refused by `_finalize_gates`, because
    # a check over nothing and a check over everything both report zero problems.
    measured: dict[str, tuple[bool, str]] = {
        "tool_contract": (
            tool_contract_ok,
            f"{contract_valid}/{registered} tools structurally valid from `{tool_coverage_source}`",
        ),
        "offline_evidence": (
            offline_evidence_ok,
            f"{offline.get('summary')} from `{offline.get('source')}`; evidence run exit "
            f"{offline_execution.get('exit_code')}, "
            f"{len(tool_coverage.get('failed_evidence') or [])} failed / "
            f"{len(tool_coverage.get('unclassified_evidence') or [])} unclassified",
        ),
        "live_evidence": (
            live_evidence_ok,
            f"{tool_coverage.get('fully_verified_tools', 0)}/{registered} tools fully "
            f"verified from `{tool_coverage_source}`; all_evidence_executed="
            f"{tool_coverage.get('all_evidence_executed')}; {extension_build_summary}",
        ),
        "code_coverage": (code_coverage_ok, coverage_text),
        "documentation": (
            docs_ok(docs),
            f"{docs.get('registered')} registered tools vs "
            f"{docs.get('coverage_manifest')} in the coverage manifest, both README "
            f"tables, and {len(docs.get('skill_hashes') or {})} shipped skill file(s)",
        ),
        "versions": (
            versions_ok,
            f"{len(versions)} version source(s): "
            f"{sorted(set(versions.values())) or 'none found'}"
            + (f"; {docs.get('version_error')}" if docs.get("version_error") else ""),
        ),
        "distributions": (evidence_fresh and distributions_ok, distribution_summary),
        "live_suite": (
            evidence_fresh and live_passed,
            f"{live.get('summary')} from `{live.get('source')}`",
        ),
        "lint": (evidence_fresh and lint_ok, lint_summary),
    }
    gates, gate_measurements, gate_structure_problems = _finalize_gates(measured)
    score = sum(weight for name, weight in GATE_WEIGHTS.items() if gates[name])
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "version": versions.get("source", "unknown"),
        "gates": gates,
        "gate_measurements": gate_measurements,
        "gate_structure_problems": gate_structure_problems,
        "gate_weights": GATE_WEIGHTS,
        "objective_score": score,
        "objective_score_total": TOTAL_GATE_WEIGHT,
        "release_ready": all(gates.values()) and not gate_structure_problems,
        "tool_coverage": tool_coverage,
        "tool_coverage_source": tool_coverage_source,
        "code_coverage": code_coverage,
        "code_coverage_source": code_coverage_source,
        "per_file_coverage": per_file_coverage,
        "per_file_coverage_source": per_file_coverage_source,
        "versions": versions,
        "live": live,
        "offline": offline,
        "distribution_summary": distribution_summary,
        "lint_summary": lint_summary,
        "evidence_fresh": evidence_fresh,
        "evidence_manifest": evidence_manifest,
        "evidence_problems": evidence_problems,
        "sealed_source": sealed_source,
    }


def render_report(data: dict[str, object]) -> str:
    gates = data["gates"]
    assert isinstance(gates, dict)
    tool_coverage = data["tool_coverage"]
    assert isinstance(tool_coverage, dict)
    live = data["live"]
    assert isinstance(live, dict)
    offline = data["offline"]
    assert isinstance(offline, dict)
    code_coverage = data["code_coverage"]
    per_file = data["per_file_coverage"]
    assert isinstance(per_file, dict)
    per_file_summary = _per_file_summary(per_file)
    registered = int(tool_coverage.get("registered", 0))
    contract_valid = int(tool_coverage.get("contract_valid_tools", 0))
    lines = [
        "# BTAP Release Acceptance Report",
        "",
        f"Generated: {data['generated']}",
        f"Version: `{data['version']}`",
        "",
        "## Gate Evidence",
        "",
        (
            f"- Tool evidence contract: `{_status(bool(gates['tool_contract']))}` "
            f"({contract_valid}/{registered} structurally valid; "
            f"{tool_coverage.get('fully_verified_tools', 0)}/{registered} fully verified "
            f"from `{data['tool_coverage_source']}`)"
        ),
        (f"- Offline evidence execution: `{_status(bool(gates['offline_evidence']))}`"),
        (
            f"- Offline Python suite: `{str(offline['status']).upper()}` "
            f"({offline['summary']} from `{offline['source']}`)"
        ),
        (
            f"- Code coverage: `{_status(bool(gates['code_coverage']))}` "
            f"({float(code_coverage):.2f}% from `{data['code_coverage_source']}`, gate 85.00%; "
            f"per-file floor {float(per_file['floor']):.2f}%, {per_file_summary})"
            if code_coverage is not None
            else (
                "- Code coverage: `FAIL` (coverage artifact missing, gate 85.00%; "
                f"per-file floor {float(per_file['floor']):.2f}%, {per_file_summary})"
            )
        ),
        f"- Documentation contract: `{_status(bool(gates['documentation']))}`",
        f"- Lint (Python + extension JS): `{_status(bool(gates['lint']))}` "
        f"({data['lint_summary']})",
        f"- Unified versions: `{_status(bool(gates['versions']))}` ({data['versions']})",
        (
            f"- Evidence/source binding: `{_status(bool(data['evidence_fresh']))}`"
            + (
                f" ({'; '.join(str(item) for item in data['evidence_problems'])})"
                if data["evidence_problems"]
                else (
                    " (recorded HEAD, worktree content fingerprint, and artifact"
                    " hashes all match; the seal was taken over a clean tree)"
                )
            )
        ),
        (
            f"- Distribution contents: `{_status(bool(gates['distributions']))}` "
            f"({data['distribution_summary']})"
        ),
        (
            f"- Live Chrome suite: `{str(live['status']).upper()}` "
            f"({live['summary']} from `{live['source']}`)"
        ),
        f"- Live tool evidence: `{_status(bool(gates['live_evidence']))}`",
        "",
        "## Objective Gate Score",
        "",
        "| Gate | Weight | Result | Measured |",
        "|---|---:|---|---|",
    ]
    weights = data["gate_weights"]
    assert isinstance(weights, dict)
    # The measurement column is what keeps the verdict from being the whole
    # story: `PASS` beside an empty cell is the shape four of these gates
    # shipped as, and it reads identically to a real one.
    measurements = data.get("gate_measurements") or {}
    assert isinstance(measurements, dict)
    for name, weight in weights.items():
        cell = str(measurements.get(name, "nothing measured")).replace("|", "\\|")
        lines.append(f"| `{name}` | {weight} | {_status(bool(gates[name]))} | {cell} |")
    total = data.get("objective_score_total", sum(int(weight) for weight in weights.values()))
    lines.extend(
        [
            "",
            f"**Score: {data['objective_score']}/{total}**",
            f"**Release ready: {str(data['release_ready']).lower()}**",
            "",
        ]
    )
    structure_problems = data.get("gate_structure_problems") or []
    if structure_problems:
        lines.extend(
            [
                "> **The gate table cannot vouch for itself.** A gate that does not name",
                "> what it measured is scored FAIL, because a check over nothing and a",
                "> check over everything both report zero problems:",
                *(f"> - {problem}" for problem in structure_problems),
                "",
            ]
        )
    lines.extend(_render_scored_source(data))
    return "\n".join(lines)


def _render_scored_source(data: dict[str, object]) -> list[str]:
    """Stamp the source state this score was computed over.

    The acceptance report is generated after the evidence manifest is sealed, so
    it cannot be bound by that manifest itself. Recording the sealed fingerprint
    inside the report lets a reader detect a stale report mechanically: re-run
    `python -m scripts.evidence_manifest --check` and compare.
    """
    sealed = data.get("sealed_source")
    if not isinstance(sealed, dict) or not sealed:
        return [
            "## Scored Source",
            "",
            "- Sealed source fingerprint: `unavailable` (no evidence manifest source record)",
            "",
        ]
    return [
        "## Scored Source",
        "",
        f"- `git_head`: `{sealed.get('git_head', 'unknown')}`",
        f"- `git_dirty`: `{str(sealed.get('git_dirty', 'unknown')).lower()}`",
        f"- `content_sha256`: `{sealed.get('content_sha256', 'unknown')}`",
        f"- `file_count`: `{sealed.get('file_count', 'unknown')}`",
        "",
        "Verify this report is current with"
        " `python -m scripts.evidence_manifest --check`; a differing"
        " `content_sha256` means the report predates the current worktree.",
        "",
    ]


def build_report() -> str:
    return render_report(build_report_data())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/acceptance-report.md"))
    args = parser.parse_args(argv)
    data = build_report_data()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(data), encoding="utf-8")
    print(output)
    return 0 if data["release_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
