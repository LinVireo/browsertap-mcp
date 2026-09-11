"""Run and verify complete offline/live pytest collections for release evidence."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from scripts import evidence_manifest as E

ROOT = E.ROOT


def receipt_relative(mode: str, phase: str) -> str:
    return f"artifacts/{mode}-{phase}.json"


def pytest_command(mode: str, phase: str, executable: str = sys.executable) -> list[str]:
    command = [
        executable, "-m", "pytest", "tests", "-q", "-o", "addopts=",
        "-m", "live" if mode == "live" else "not live",
        "-p", "scripts.pytest_evidence",
        f"--btap-evidence-mode={mode}", f"--btap-evidence-phase={phase}",
        f"--btap-evidence-output={receipt_relative(mode, phase)}",
    ]
    if phase == "collection":
        command.append("--collect-only")
    else:
        command.extend([f"--junitxml=artifacts/{mode}-junit.xml", "-o", "junit_family=xunit2"])
        if mode == "offline":
            command.extend([
                "--cov=browsertap_mcp", "--cov-fail-under=95", "--cov-report=term-missing",
                "--cov-report=xml", "--cov-report=json:artifacts/coverage.json",
            ])
    return command


def _nodeids(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(
        not isinstance(nodeid, str) or not nodeid.startswith("tests/") or "::" not in nodeid
        for nodeid in value
    ):
        return None
    return value


def _read_receipt(root: Path, mode: str, phase: str, problems: list[str]) -> dict[str, Any]:
    relative = receipt_relative(mode, phase)
    try:
        value = json.loads((root / relative).read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("receipt must be an object")
        return value
    except (OSError, UnicodeError, ValueError):
        problems.append(f"{phase} receipt unavailable or malformed: {relative}")
        return {}


def _check_receipt(
    record: dict[str, Any], mode: str, phase: str, source: dict[str, Any], problems: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    prefix = f"{mode} {phase}"
    if (
        type(record.get("schema_version")) is not int
        or (record.get("schema_version"), record.get("mode"), record.get("phase")) != (1, mode, phase)
    ):
        problems.append(f"{prefix}: unsupported receipt identity")
    command = record.get("command")
    if (
        not isinstance(command, list) or not command or not isinstance(command[0], str)
        or not command[0] or command != pytest_command(mode, phase, command[0])
    ):
        problems.append(f"{prefix}: command is not the canonical complete-suite invocation")
    if (
        record.get("targets") != ["tests"] or record.get("keyword") != ""
        or record.get("markexpr") != ("live" if mode == "live" else "not live")
        or record.get("invoked_from_root") is not True
    ):
        problems.append(f"{prefix}: effective pytest selection is not the complete suite")
    if type(record.get("exit_code")) is not int or record["exit_code"] != 0:
        problems.append(f"{prefix}: pytest did not finish successfully")
    if record.get("source_before") != source or record.get("source_after") != source:
        problems.append(f"{prefix}: source changed or does not match the sealed tree")
    collection_reports = record.get("collection_reports")
    if not isinstance(collection_reports, list) or not collection_reports or any(
        not isinstance(row, dict) or not isinstance(row.get("nodeid"), str)
        or row.get("outcome") != "passed" for row in collection_reports
    ):
        problems.append(f"{prefix}: collection contains missing, failing or skipped reports")
    roster = record.get("all_tests")
    if not isinstance(roster, list) or any(
        not isinstance(row, dict) or _nodeids([row.get("nodeid")]) is None
        or type(row.get("live")) is not bool for row in roster
    ):
        roster = []
        problems.append(f"{prefix}: full collection is missing or malformed")
    all_ids = [row["nodeid"] for row in roster]
    if not all_ids or len(set(all_ids)) != len(all_ids):
        problems.append(f"{prefix}: full collection is empty or contains duplicate nodeids")
    expected = [row["nodeid"] for row in roster if row["live"] == (mode == "live")]
    deselected = [row["nodeid"] for row in roster if row["live"] != (mode == "live")]
    selected = _nodeids(record.get("selected_nodeids"))
    dropped = _nodeids(record.get("deselected_nodeids"))
    if not expected or selected is None or Counter(selected) != Counter(expected):
        problems.append(f"{prefix}: selected collection omits or duplicates expected tests")
    if dropped is None or Counter(dropped) != Counter(deselected):
        problems.append(f"{prefix}: deselected collection does not match the suite marker")
    return roster, expected


def _junit_cases(path: Path, problems: list[str]) -> tuple[list[str], dict[str, int], bool]:
    counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - local pytest JUnit artifact
    except (OSError, ET.ParseError, ValueError):
        problems.append("JUnit is unavailable or malformed")
        return [], counts, False
    if root.tag not in {"testsuite", "testsuites"}:
        problems.append("JUnit root is not a pytest test suite")
    suites = list(root.iter("testsuite"))
    if not suites:
        problems.append("JUnit contains no test suite")
    for suite in suites:
        if suite.findall("testsuite"):
            problems.append("JUnit contains nested suites instead of pytest's flat suite output")
        cases = list(suite.findall("testcase"))
        actual = {
            "tests": len(cases),
            "failures": sum(case.find("failure") is not None for case in cases),
            "errors": sum(case.find("error") is not None for case in cases),
            "skipped": sum(case.find("skipped") is not None for case in cases),
        }
        for name, count in actual.items():
            try:
                declared = int(suite.attrib[name])
            except (KeyError, ValueError):
                declared = -1
            if declared != count:
                problems.append(f"JUnit {name} counter does not match its testcase elements")
            counts[name] += count
    if root.tag == "testsuites":
        for name, count in counts.items():
            if name not in root.attrib:
                continue
            try:
                declared = int(root.attrib[name])
            except ValueError:
                declared = -1
            if declared != count:
                problems.append(f"JUnit aggregate {name} counter does not match its suites")
    nodeids = []
    for case in root.iter("testcase"):
        properties = [
            prop.get("value") for prop in case.findall("properties/property")
            if prop.get("name") == "btap_nodeid"
        ]
        if len(properties) != 1 or _nodeids(properties) is None:
            problems.append("JUnit testcase is missing a unique BTAP nodeid")
        else:
            nodeids.extend(properties)
    if len(set(nodeids)) != len(nodeids):
        problems.append("JUnit contains duplicate test nodeids")
    if not counts["tests"] or any(counts[name] for name in ("failures", "errors", "skipped")):
        problems.append("JUnit does not describe a nonempty passing suite with no skips")
    return nodeids, counts, True


def validate_test_run(
    root: Path, mode: str, *, source: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Join independent collection, execution, source identity and JUnit evidence."""
    problems: list[str] = []
    if mode not in {"offline", "live"}:
        raise ValueError("mode must be offline or live")
    if manifest is not None:
        artifacts = manifest.get("artifacts", {})
        for relative in (
            receipt_relative(mode, "collection"), receipt_relative(mode, "execution"),
            f"artifacts/{mode}-junit.xml",
        ):
            if not isinstance(artifacts, dict) or relative not in artifacts:
                problems.append(f"test evidence is not bound by the manifest: {relative}")
        source = manifest.get("source")
    if not isinstance(source, dict) or not source.get("content_sha256"):
        problems.append("complete test evidence has no source identity")
        source = {}
    collection = _read_receipt(root, mode, "collection", problems)
    execution = _read_receipt(root, mode, "execution", problems)
    collected_roster, expected = _check_receipt(collection, mode, "collection", source, problems)
    executed_roster, selected = _check_receipt(execution, mode, "execution", source, problems)
    if collected_roster != executed_roster or expected != selected:
        problems.append("collection changed between independent collection and execution")
    collect_command, execute_command = collection.get("command"), execution.get("command")
    if (
        isinstance(collect_command, list) and isinstance(execute_command, list)
        and collect_command[:1] != execute_command[:1]
    ):
        problems.append("collection and execution used different Python interpreters")
    if collection.get("reports") != []:
        problems.append("collect-only receipt unexpectedly contains test executions")
    reports = execution.get("reports")
    if not isinstance(reports, list) or any(
        not isinstance(row, dict) or _nodeids([row.get("nodeid")]) is None
        or row.get("when") not in ("setup", "call", "teardown")
        or row.get("outcome") != "passed" for row in reports
    ):
        problems.append("execution reports contain missing, failing or skipped phases")
        reports = []
    actual_phases = Counter((row["nodeid"], row["when"]) for row in reports)
    expected_phases = Counter(
        (nodeid, phase) for nodeid in expected for phase in ("setup", "call", "teardown")
    )
    if actual_phases != expected_phases:
        problems.append("execution did not complete every expected test exactly once")
    nodeids, counts, available = _junit_cases(root / f"artifacts/{mode}-junit.xml", problems)
    if Counter(nodeids) != Counter(expected) or counts["tests"] != len(expected):
        problems.append("JUnit omits, duplicates or adds tests relative to the expected collection")
    return {
        "ok": not problems, "mode": mode, "expected_tests": len(expected),
        "junit_available": available, **counts, "problems": problems,
    }


def run_suite(mode: str) -> dict[str, Any]:
    paths = [
        ROOT / receipt_relative(mode, phase) for phase in ("collection", "execution")
    ] + [ROOT / f"artifacts/{mode}-junit.xml"]
    if any(path.exists() for path in paths):
        raise RuntimeError("test evidence already exists; archive the previous run before replacing it")
    (ROOT / "artifacts").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Only this subprocess environment changes. Release selection must not be
    # narrowed by a shell's -k/--ignore options or injected pytest plugin list.
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    for phase in ("collection", "execution"):
        completed = subprocess.run(
            pytest_command(mode, phase), cwd=ROOT, env=env, check=False,
            capture_output=phase == "collection", text=True,
        )
        if completed.returncode:
            if completed.stdout:
                print(completed.stdout)
            if completed.stderr:
                print(completed.stderr, file=sys.stderr)
            return {"ok": False, "problems": [f"{phase} pytest exited {completed.returncode}"]}
    return validate_test_run(ROOT, mode, source=E.source_identity())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = (
            validate_test_run(ROOT, args.mode, source=E.source_identity())
            if args.check else run_suite(args.mode)
        )
    except (OSError, RuntimeError) as exc:
        result = {"ok": False, "problems": [str(exc)]}
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
