"""Execute and report the 55-tool behavior-evidence contract."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

from agent_browser_mcp import server as S
from tests.tool_coverage_manifest import TOOL_COVERAGE


def registered_tools() -> set[str]:
    return {tool.name for tool in asyncio.run(S.mcp.list_tools())}


def collected_node_ids(marker_expression: str | None = None) -> set[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "--collect-only",
        "-q",
        "-o",
        "addopts=",
    ]
    if marker_expression:
        command.extend(("-m", marker_expression))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"pytest collection failed: {detail}")
    return {
        line.strip().replace("\\", "/")
        for line in completed.stdout.splitlines()
        if line.strip().replace("\\", "/").startswith("tests/") and "::" in line
    }


def _item_evidence(item: Any) -> tuple[str, ...]:
    values = [item.success_case, item.boundary_case]
    if item.mutates_state and item.cleanup_case:
        values.append(item.cleanup_case)
    return tuple(values)


def _evidence_owners() -> dict[str, list[tuple[str, str]]]:
    owners: dict[str, list[tuple[str, str]]] = {}
    for tool, item in TOOL_COVERAGE.items():
        for role in ("success_case", "boundary_case", "cleanup_case"):
            node_id = getattr(item, role)
            if node_id:
                owners.setdefault(node_id, []).append((tool, role))
    return owners


def _node_mentions_tool(node_id: str, tool: str) -> bool:
    compact_node = re.sub(r"[^a-z0-9]", "", node_id.lower())
    compact_tool = re.sub(r"[^a-z0-9]", "", tool.lower())
    return compact_tool in compact_node


def _junit_identity(node_id: str) -> tuple[str, str]:
    path, *parts = node_id.replace("\\", "/").split("::")
    module = Path(path).with_suffix("").as_posix().replace("/", ".")
    classname = ".".join((module, *parts[:-1])) if len(parts) > 1 else module
    return classname, parts[-1]


def _run_evidence(node_ids: Iterable[str], *, timeout: int) -> dict[str, Any]:
    requested = sorted(set(node_ids))
    if not requested:
        return {
            "requested": 0,
            "passed": [],
            "failed": [],
            "skipped": [],
            "missing_results": [],
            "exit_code": 0,
            "output": "",
        }
    with tempfile.TemporaryDirectory(prefix="abm-tool-evidence-") as temporary:
        junit_path = Path(temporary) / "evidence.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            *requested,
            "-q",
            "-o",
            "addopts=",
            f"--junitxml={junit_path}",
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        results: dict[tuple[str, str], str] = {}
        if junit_path.is_file():
            try:
                root = ET.parse(junit_path).getroot()
            except (ET.ParseError, OSError):
                root = None
            if root is not None:
                for case in root.iter("testcase"):
                    identity = (case.attrib.get("classname", ""), case.attrib.get("name", ""))
                    if case.find("failure") is not None or case.find("error") is not None:
                        results[identity] = "failed"
                    elif case.find("skipped") is not None:
                        results[identity] = "skipped"
                    else:
                        results[identity] = "passed"
        by_status = {"passed": [], "failed": [], "skipped": [], "missing_results": []}
        for node_id in requested:
            status = results.get(_junit_identity(node_id))
            if status in {"passed", "failed", "skipped"}:
                by_status[status].append(node_id)
            else:
                by_status["missing_results"].append(node_id)
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        return {
            "requested": len(requested),
            **by_status,
            "exit_code": completed.returncode,
            "output": output[-12000:],
        }


def build_report(*, run_live: bool = False, execute: bool = True) -> dict[str, Any]:
    registered = registered_tools()
    collected = collected_node_ids()
    live_collected = collected_node_ids("live")
    offline_collected = collected_node_ids("not live")
    manifest_names = set(TOOL_COVERAGE)

    missing_success = sorted(
        name for name, item in TOOL_COVERAGE.items()
        if not item.success_case or item.success_case not in collected
    )
    missing_boundary = sorted(
        name for name, item in TOOL_COVERAGE.items()
        if not item.boundary_case or item.boundary_case not in collected
    )
    missing_cleanup = sorted(
        name for name, item in TOOL_COVERAGE.items()
        if item.mutates_state and (
            not item.cleanup_case or item.cleanup_case not in collected
        )
    )
    duplicate_evidence = sorted(
        name for name, item in TOOL_COVERAGE.items()
        if item.success_case == item.boundary_case
        or (
            item.cleanup_case is not None
            and item.cleanup_case in {item.success_case, item.boundary_case}
        )
    )
    evidence_owners = _evidence_owners()
    shared_evidence_nodes = {
        node_id: [f"{tool}:{role}" for tool, role in owners]
        for node_id, owners in sorted(evidence_owners.items())
        if len(owners) > 1
    }
    evidence_without_tool_name = sorted(
        f"{tool}:{role}:{node_id}"
        for node_id, owners in evidence_owners.items()
        for tool, role in owners
        if not _node_mentions_tool(node_id, tool)
    )
    missing_harness_reasons = sorted(
        name for name, item in TOOL_COVERAGE.items()
        if item.layer == "harness" and not (item.harness_reason or "").strip()
    )
    misclassified_success = sorted(
        name for name, item in TOOL_COVERAGE.items()
        if (
            item.layer == "live" and item.success_case not in live_collected
        ) or (
            item.layer != "live" and item.success_case not in offline_collected
        ) or (
            item.layer == "harness"
            and not item.success_case.startswith("tests/test_all_tools_behavior.py::")
        )
    )
    manifest_only = sorted(manifest_names - registered)
    registered_only = sorted(registered - manifest_names)
    shared_tools = {
        tool
        for owners in evidence_owners.values()
        if len(owners) > 1
        for tool, _role in owners
    }
    unnamed_tools = {
        tool
        for node_id, owners in evidence_owners.items()
        for tool, _role in owners
        if not _node_mentions_tool(node_id, tool)
    }
    structural_failures = set(
        missing_success + missing_boundary + missing_cleanup + duplicate_evidence
        + missing_harness_reasons + misclassified_success + manifest_only
    )
    structural_failures.update(shared_tools)
    structural_failures.update(unnamed_tools)
    contract_valid_names = manifest_names - structural_failures

    evidence_by_tool = {
        name: _item_evidence(item) for name, item in TOOL_COVERAGE.items()
    }
    all_evidence = set().union(*(set(values) for values in evidence_by_tool.values()))
    offline_evidence = sorted(all_evidence & offline_collected)
    live_evidence = sorted(all_evidence & live_collected)
    unclassified_evidence = sorted(all_evidence - offline_collected - live_collected)

    offline_execution = _run_evidence(offline_evidence, timeout=600) if execute else None
    live_execution = (
        _run_evidence(live_evidence, timeout=900) if execute and run_live else None
    )
    passed_evidence = set()
    failed_evidence = set()
    for execution in (offline_execution, live_execution):
        if not execution:
            continue
        passed_evidence.update(execution["passed"])
        failed_evidence.update(execution["failed"])
        failed_evidence.update(execution["skipped"])
        failed_evidence.update(execution["missing_results"])
        if execution["exit_code"] != 0 and not (
            execution["failed"] or execution["skipped"] or execution["missing_results"]
        ):
            failed_evidence.update(
                offline_evidence if execution is offline_execution else live_evidence
            )
    deferred_live = live_evidence if not run_live else []
    fully_verified_tools = sorted(
        name for name, evidence in evidence_by_tool.items()
        if name in contract_valid_names and set(evidence) <= passed_evidence
    )
    offline_verified_tools = sorted(
        name for name, evidence in evidence_by_tool.items()
        if set(evidence) & offline_collected <= passed_evidence
    )

    return {
        "registered": len(registered),
        "contract_valid_tools": len(contract_valid_names & registered),
        "expected_registered": 55,
        "manifest_entries": len(manifest_names),
        "mode": "live" if run_live else "offline",
        "execution_enabled": execute,
        "all_evidence_executed": (
            execute
            and len(contract_valid_names & registered) == len(registered) == 55
            and not deferred_live
            and not failed_evidence
            and not unclassified_evidence
        ),
        "fully_verified_tools": len(fully_verified_tools),
        "fully_verified_tool_names": fully_verified_tools,
        "offline_verified_tools": len(offline_verified_tools),
        "offline_evidence_nodes": len(offline_evidence),
        "live_evidence_nodes": len(live_evidence),
        "deferred_live_evidence": deferred_live,
        "failed_evidence": sorted(failed_evidence),
        "unclassified_evidence": unclassified_evidence,
        "offline_execution": offline_execution,
        "live_execution": live_execution,
        "missing_success_evidence": missing_success,
        "missing_boundary_evidence": missing_boundary,
        "missing_cleanup_evidence": missing_cleanup,
        "duplicate_evidence": duplicate_evidence,
        "shared_evidence_nodes": shared_evidence_nodes,
        "evidence_without_tool_name": evidence_without_tool_name,
        "missing_harness_reasons": missing_harness_reasons,
        "misclassified_success_evidence": misclassified_success,
        "registered_not_in_manifest": registered_only,
        "manifest_not_registered": manifest_only,
        "tools": {name: asdict(TOOL_COVERAGE[name]) for name in sorted(TOOL_COVERAGE)},
    }


def report_ok(report: dict[str, Any]) -> bool:
    execution_ok = True
    if report["execution_enabled"]:
        offline = report["offline_execution"] or {}
        execution_ok = (
            offline.get("exit_code") == 0
            and not report["failed_evidence"]
            and not report["unclassified_evidence"]
        )
        if report["mode"] == "live":
            live = report["live_execution"] or {}
            execution_ok = execution_ok and live.get("exit_code") == 0
            execution_ok = execution_ok and report["all_evidence_executed"]
    return (
        report["registered"] == report["expected_registered"] == 55
        and report["manifest_entries"] == 55
        and report["contract_valid_tools"] == 55
        and execution_ok
        and not any(
            report[key]
            for key in (
                "missing_success_evidence",
                "missing_boundary_evidence",
                "missing_cleanup_evidence",
                "duplicate_evidence",
                "shared_evidence_nodes",
                "evidence_without_tool_name",
                "missing_harness_reasons",
                "misclassified_success_evidence",
                "registered_not_in_manifest",
                "manifest_not_registered",
            )
        )
    )


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MCP Tool Behavior Coverage",
        "",
        f"- mode={report['mode']}",
        f"- registered={report['registered']}",
        f"- contract_valid_tools={report['contract_valid_tools']}",
        f"- fully_verified_tools={report['fully_verified_tools']}",
        f"- offline_evidence_nodes={report['offline_evidence_nodes']}",
        f"- live_evidence_nodes={report['live_evidence_nodes']}",
        f"- deferred_live_evidence={len(report['deferred_live_evidence'])}",
        f"- failed_evidence={len(report['failed_evidence'])}",
        "",
    ]
    for key, title in (
        ("missing_success_evidence", "Missing success evidence"),
        ("missing_boundary_evidence", "Missing boundary evidence"),
        ("missing_cleanup_evidence", "Missing cleanup evidence"),
        ("duplicate_evidence", "Invalid duplicate evidence"),
        ("shared_evidence_nodes", "Evidence nodes claimed by multiple roles or tools"),
        ("evidence_without_tool_name", "Evidence nodes not bound to the tool name"),
        ("misclassified_success_evidence", "Layer/marker mismatches"),
        ("failed_evidence", "Failed or skipped evidence"),
        ("deferred_live_evidence", "Deferred live evidence"),
        ("registered_not_in_manifest", "Registered but absent from manifest"),
        ("manifest_not_registered", "Manifest-only tools"),
    ):
        values = report[key]
        lines.extend([f"## {title}", "", "\n".join(f"- `{value}`" for value in values) if values else "None", ""])
    lines.extend([
        "## Coverage",
        "",
        "| Tool | Layer | Success | Boundary | Cleanup |",
        "|---|---|---|---|---|",
    ])
    for name, item in report["tools"].items():
        cleanup = f"`{item['cleanup_case']}`" if item["cleanup_case"] else "-"
        lines.append(
            f"| `{name}` | {item['layer']} | `{item['success_case']}` | "
            f"`{item['boundary_case']}` | {cleanup} |"
        )
    lines.extend([
        "",
        f"**Structurally valid contract: {report['contract_valid_tools']}/"
        f"{report['registered']} tools; fully verified: "
        f"{report['fully_verified_tools']}/{report['registered']}**",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--no-execute", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = build_report(run_live=args.run_live, execute=not args.no_execute)
    except Exception as exc:
        print(f"tool coverage report failed: {exc}", file=sys.stderr)
        return 2
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(markdown(report))
    return 0 if report_ok(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
