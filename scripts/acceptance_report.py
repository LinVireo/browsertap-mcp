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

from scripts.check_distribution import validate_archive
from scripts.check_tool_docs import build_report as build_docs_report
from scripts.check_tool_docs import report_ok as docs_ok
from scripts.evidence_manifest import validate_manifest

GATE_WEIGHTS = {
    "tool_contract": 15,
    "offline_evidence": 15,
    "live_evidence": 20,
    "code_coverage": 20,
    "documentation": 10,
    "versions": 5,
    "distributions": 5,
    "live_suite": 10,
}


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
    if failures:
        issue_count = sum(len(issues) for issues in failures.values())
        return False, f"{issue_count} archive contract violation(s)"
    return True, f"{len(relative_paths)} manifest-bound archive(s) validated"


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


def _sealed_source(manifest: dict[str, object] | None) -> dict[str, object]:
    source = manifest.get("source") if isinstance(manifest, dict) else None
    return source if isinstance(source, dict) else {}


def build_report_data() -> dict[str, object]:
    evidence_manifest, evidence_problems = validate_manifest(require_live=False)
    offline = _offline_junit(evidence_manifest)
    live = _live_junit(evidence_manifest)
    live, live_bound = _bind_live_status(live, evidence_manifest)
    live_passed = live["status"] == "pass" and live_bound
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
    docs = build_docs_report()
    versions = docs.get("versions") or {}
    distributions_ok, distribution_summary = _distribution_status(evidence_manifest)

    registered = int(tool_coverage.get("registered", 0))
    contract_valid = int(tool_coverage.get("contract_valid_tools", 0))
    offline_execution = tool_coverage.get("offline_execution") or {}
    if not isinstance(offline_execution, dict):
        offline_execution = {}
    gates = {
        "tool_contract": evidence_fresh and registered == 55 and contract_valid == registered,
        "offline_evidence": (
            evidence_fresh
            and offline.get("status") == "pass"
            and offline_execution.get("exit_code") == 0
            and not tool_coverage.get("failed_evidence")
            and not tool_coverage.get("unclassified_evidence")
        ),
        "live_evidence": (
            evidence_fresh
            and live_passed
            and tool_coverage.get("all_evidence_executed") is True
            and tool_coverage.get("fully_verified_tools") == registered == 55
        ),
        "code_coverage": evidence_fresh and code_coverage is not None and code_coverage >= 85.0,
        "documentation": docs_ok(docs),
        "versions": (
            not docs.get("version_error") and bool(versions) and len(set(versions.values())) == 1
        ),
        "distributions": evidence_fresh and distributions_ok,
        "live_suite": evidence_fresh and live_passed,
    }
    score = sum(weight for name, weight in GATE_WEIGHTS.items() if gates[name])
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "version": versions.get("source", "unknown"),
        "gates": gates,
        "gate_weights": GATE_WEIGHTS,
        "objective_score": score,
        "release_ready": all(gates.values()),
        "tool_coverage": tool_coverage,
        "tool_coverage_source": tool_coverage_source,
        "code_coverage": code_coverage,
        "code_coverage_source": code_coverage_source,
        "versions": versions,
        "live": live,
        "offline": offline,
        "distribution_summary": distribution_summary,
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
            f"({float(code_coverage):.2f}% from `{data['code_coverage_source']}`, gate 85.00%)"
            if code_coverage is not None
            else "- Code coverage: `FAIL` (coverage artifact missing, gate 85.00%)"
        ),
        f"- Documentation contract: `{_status(bool(gates['documentation']))}`",
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
        "| Gate | Weight | Result |",
        "|---|---:|---|",
    ]
    weights = data["gate_weights"]
    assert isinstance(weights, dict)
    for name, weight in weights.items():
        lines.append(f"| `{name}` | {weight} | {_status(bool(gates[name]))} |")
    lines.extend(
        [
            "",
            f"**Score: {data['objective_score']}/100**",
            f"**Release ready: {str(data['release_ready']).lower()}**",
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
