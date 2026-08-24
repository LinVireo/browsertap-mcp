"""Run the lint gate and record what it saw as sealable evidence.

Lint was the one gate outside the evidence chain. `.github/workflows/test.yml`
ran `ruff check src tests scripts` and the release finalizer did not run it at
all, so `acceptance_report.py` could seal `release_ready: true` over a tree that
CI was about to fail -- two verdicts on the same commit with nothing to reconcile
them, and the sealed one was the one quoted in the release notes.

Running ruff in both places would have fixed the coverage and left the drift: two
copies of the target list, and a green report either way if one of them narrowed.
So the gate lives here, both callers invoke this module, and the artifact it
writes is bound by the evidence manifest like every other measurement.

The artifact records the file count on purpose. `ruff check` over a path that
matches nothing reports zero violations and exits 0, which is the shape of every
vacuous pass this repository has had to fix -- so "clean" here means each target
really did contribute files, and the count is written down for a reader to sanity
check rather than inferred from the exit code.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# One list, read by the finalizer and by CI. Both used to spell it out.
LINT_TARGETS = ("src", "tests", "scripts")
DEFAULT_OUTPUT = Path("artifacts/lint.json")
# The whole diagnostic list can be arbitrarily long, and the artifact is read by
# humans and hashed into the seal. The count stays exact either way.
MAX_RECORDED_VIOLATIONS = 50


def _relative(filename: str) -> str:
    """Machine-local absolute paths do not belong in an uploaded artifact."""
    try:
        return Path(filename).resolve().relative_to(ROOT).as_posix()
    except (OSError, ValueError):
        return Path(filename).name


def _ruff(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "ruff", *args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _scanned_files(targets: tuple[str, ...]) -> tuple[dict[str, int], list[str]]:
    """Ask ruff which files it would check, per target.

    This is the anti-vacuity half. A target that resolves to nothing is reported
    as such instead of contributing a silent zero to a clean verdict.
    """
    listing = _ruff("check", "--show-files", *targets)
    if listing.returncode != 0:
        return {}, [f"ruff --show-files failed with exit {listing.returncode}"]
    per_target = {target: 0 for target in targets}
    for line in listing.stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        relative = _relative(candidate)
        for target in targets:
            if relative == target or relative.startswith(f"{target}/"):
                per_target[target] += 1
                break
    empty = [target for target, count in per_target.items() if count == 0]
    return per_target, [f"lint target matched no files: {target}" for target in empty]


def build_lint_report(targets: tuple[str, ...] = LINT_TARGETS) -> dict[str, object]:
    version = _ruff("--version")
    tool_version = version.stdout.strip() or "unknown"
    per_target, problems = _scanned_files(targets)
    checked = _ruff("check", "--output-format", "json", *targets)
    violations: list[dict[str, object]] = []
    if checked.stdout.strip():
        try:
            payload = json.loads(checked.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                location = item.get("location")
                row = location.get("row") if isinstance(location, dict) else None
                violations.append(
                    {
                        "code": item.get("code"),
                        "file": _relative(str(item.get("filename", ""))),
                        "line": row,
                        "message": item.get("message"),
                    }
                )
        else:
            problems.append("ruff did not return a JSON diagnostic list")
    elif checked.returncode not in (0, 1):
        problems.append(
            f"ruff check failed with exit {checked.returncode}: "
            f"{checked.stderr.strip()[:400] or 'no stderr'}"
        )

    if problems:
        status = "error"
    elif violations:
        status = "violations"
    else:
        status = "clean"
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "tool": "ruff",
        "tool_version": tool_version,
        "targets": list(targets),
        "files_scanned": sum(per_target.values()),
        "files_per_target": per_target,
        "exit_code": checked.returncode,
        "status": status,
        "violation_count": len(violations),
        "violations": violations[:MAX_RECORDED_VIOLATIONS],
        "violations_truncated": len(violations) > MAX_RECORDED_VIOLATIONS,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ruff and record the result as evidence")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    report = build_lint_report()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    relative = output.relative_to(ROOT).as_posix() if output.is_relative_to(ROOT) else str(output)
    for problem in report["problems"]:
        print(f"lint gate problem: {problem}")
    for violation in report["violations"]:
        print(f"{violation['file']}:{violation['line']}: {violation['code']} {violation['message']}")
    if report["violations_truncated"]:
        print(f"... {int(report['violation_count']) - MAX_RECORDED_VIOLATIONS} more not recorded")
    print(
        f"lint_status={report['status']} violations={report['violation_count']} "
        f"files_scanned={report['files_scanned']} tool={report['tool_version']} -> {relative}"
    )
    return 0 if report["status"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
