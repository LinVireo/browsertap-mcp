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

The gate has two halves because the deliverable does. Roughly 4.9k lines of the
wheel are the Chrome extension, and no Python linter can see any of it -- which
is how `background.js` came to carry an unreachable 'equalmany' branch, a
`MAX_CDP_TIMEOUT_MS` that capped nothing, and two copies of the tab-generation
refusal, the shipped one untested and the tested one called by nothing. eslint
covers that half, and its result is recorded next to ruff's under `javascript`.

Its absence is reported, not passed over. A machine without `node_modules` gets
`status: "unavailable"` and `enforced: false` rather than a silent clean, the same
distinction `own_tabs.enforced` and `input_quiet.enforced` exist to make: this
artifact must be able to say "nothing was wrong" and "nothing was measured" in
different words. `acceptance_report.py` refuses to seal on the second one.
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
# The JavaScript that ships inside the wheel. `build/` and `.superpowers/` hold
# older copies of these same files and are excluded by eslint.config.mjs.
# Two directories, two different environments, two config blocks: the extension
# runs as an MV3 worker and content scripts, while `page_scripts` is injected
# into the page the user is looking at and has no `chrome.*` at all. Each target
# is rule-counted separately below, because a config block that stops matching
# one of them is invisible in a combined count.
JS_LINT_TARGETS = (
    "src/browsertap_mcp/chrome_extension",
    "src/browsertap_mcp/page_scripts",
)
# Invoked through node directly rather than through npx: npx will reach for the
# network when the package is missing, and "missing" is a state this needs to
# report rather than repair.
ESLINT_BIN = Path("node_modules") / "eslint" / "bin" / "eslint.js"
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


def _node(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("node", *args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _unavailable(reason: str, targets: tuple[str, ...]) -> dict[str, object]:
    """Say which of the two it is: no linter, or a linter that found nothing."""
    return {
        "tool": "eslint",
        "tool_version": None,
        "available": False,
        "enforced": False,
        "unavailable_reason": reason,
        "targets": list(targets),
        "files_scanned": 0,
        "status": "unavailable",
        "violation_count": 0,
        "violations": [],
        "violations_truncated": False,
        "problems": [],
    }


def _rules_applied(
    eslint: str, targets: tuple[str, ...], problems: list[str]
) -> tuple[int, dict[str, int]]:
    """How many rules the resolved config enforces, asked once per target.

    Returns ``(floor, per_target)`` where the floor is the **minimum** across
    targets, so ``rules_applied > 0`` means every target is covered rather than
    at least one. That distinction is the whole point: this used to sample the
    first target's first file and stop, which was sound while there was one
    target and became a vacuous pass the moment a second one arrived. eslint
    reports files outside a config's `files:` pattern as clean, so a second
    directory nobody wrote a config block for would have inherited the first
    one's rule count and read as enforced while enforcing nothing.
    """
    per_target: dict[str, int] = {}
    for target in targets:
        candidate = ROOT / target
        sample = None
        if candidate.is_file() and candidate.suffix == ".js":
            sample = candidate
        elif candidate.is_dir():
            found = sorted(candidate.glob("*.js"))
            if found:
                sample = found[0]
        if sample is None:
            problems.append(f"no .js file found under lint target: {target}")
            per_target[target] = 0
            continue
        relative = sample.relative_to(ROOT).as_posix()
        printed = _node(eslint, "--print-config", relative)
        try:
            rules = json.loads(printed.stdout)["rules"]
        except (json.JSONDecodeError, KeyError, TypeError):
            problems.append(
                f"eslint --print-config gave no rule set for {relative} "
                f"(exit {printed.returncode})"
            )
            per_target[target] = 0
            continue
        count = len(rules) if isinstance(rules, dict) else 0
        if count == 0:
            problems.append(
                f"eslint resolves no rules for {relative}, so a clean verdict "
                "for that target would mean nothing; check the `files:` pattern "
                "in eslint.config.mjs"
            )
        per_target[target] = count
    floor = min(per_target.values()) if per_target else 0
    return floor, per_target


def build_js_lint_report(targets: tuple[str, ...] = JS_LINT_TARGETS) -> dict[str, object]:
    """Lint the extension, or explain precisely why it was not linted."""
    if not (ROOT / ESLINT_BIN).is_file():
        return _unavailable(
            f"{ESLINT_BIN.as_posix()} is absent; run `npm ci` to install the "
            "JavaScript half of the lint gate",
            targets,
        )
    probe = _node("--version")
    if probe.returncode != 0:
        return _unavailable(
            f"node is not runnable here: {probe.stderr.strip()[:200] or 'no stderr'}",
            targets,
        )

    eslint = ESLINT_BIN.as_posix()
    version = _node(eslint, "--version")
    tool_version = version.stdout.strip().lstrip("v") or "unknown"
    checked = _node(eslint, "--format", "json", *targets)

    problems: list[str] = []
    # Opening the files is not the same as checking them. eslint walks a
    # directory happily, and a flat config whose `files:` pattern no longer
    # matches (the extension moved, someone wrote `*.mjs`) applies **zero**
    # rules and reports every file clean -- measured: a `let unusedThing = 1;`
    # outside the pattern came back `status: clean`. So ask the resolved config
    # what it would enforce, and treat an empty rule set as an error.
    rules_applied, rules_by_target = _rules_applied(eslint, targets, problems)
    violations: list[dict[str, object]] = []
    files_scanned = 0
    payload: object = None
    if checked.stdout.strip():
        try:
            payload = json.loads(checked.stdout)
        except json.JSONDecodeError:
            payload = None
    files_by_target = {target: 0 for target in targets}
    if isinstance(payload, list):
        # One entry per file eslint actually opened. This is the anti-vacuity
        # half: `eslint` over a path matching nothing also exits 0.
        files_scanned = len(payload)
        for item in payload:
            opened = _relative(str(item.get("filePath", ""))) if isinstance(item, dict) else ""
            for target in targets:
                if opened == target or opened.startswith(f"{target}/"):
                    files_by_target[target] += 1
                    break
        for target, count in files_by_target.items():
            if count == 0:
                problems.append(f"eslint opened no file under lint target: {target}")
        for item in payload:
            if not isinstance(item, dict):
                continue
            for message in item.get("messages") or ():
                if not isinstance(message, dict):
                    continue
                violations.append(
                    {
                        "code": message.get("ruleId"),
                        "file": _relative(str(item.get("filePath", ""))),
                        "line": message.get("line"),
                        "message": message.get("message"),
                    }
                )
    else:
        problems.append(
            f"eslint did not return a JSON result list (exit {checked.returncode}): "
            f"{checked.stderr.strip()[:400] or 'no stderr'}"
        )

    if problems:
        status = "error"
    elif violations:
        status = "violations"
    elif files_scanned <= 0:
        status = "error"
        problems.append("eslint reported no violations because it scanned no files")
    else:
        status = "clean"
    return {
        "tool": "eslint",
        "tool_version": tool_version,
        "available": True,
        # Available, it really did read files, and the config really does apply
        # rules to them. A clean verdict missing any of the three is the shape
        # this whole module exists to refuse.
        #
        # The last two terms are deliberately redundant today: both zero-cases
        # already append a problem, which forces `status` to "error", so removing
        # them changes no verdict -- verified by mutation, the whole suite still
        # passes without them. They are kept as a second lock on the same door,
        # because the failure being prevented is a *silent* one: an edit that
        # stops `_rules_applied` reporting, or a new early return that skips it,
        # would otherwise turn "nothing was checked" back into "nothing was
        # wrong" with no test able to tell.
        "enforced": (
            status in ("clean", "violations") and files_scanned > 0 and rules_applied > 0
        ),
        "unavailable_reason": None,
        "targets": list(targets),
        "files_scanned": files_scanned,
        # Per target, because the totals above cannot say *which* target went
        # bare -- and a second target that resolves no rules is exactly the
        # failure the floor was introduced to catch.
        "files_scanned_by_target": files_by_target,
        "rules_applied": rules_applied,
        "rules_applied_by_target": rules_by_target,
        "exit_code": checked.returncode,
        "status": status,
        "violation_count": len(violations),
        "violations": violations[:MAX_RECORDED_VIOLATIONS],
        "violations_truncated": len(violations) > MAX_RECORDED_VIOLATIONS,
        "problems": problems,
    }


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
        # The top level stays ruff's, so every existing reader of this artifact
        # keeps working; the second half is recorded beside it under a name of
        # its own rather than averaged into a single verdict.
        "javascript": build_js_lint_report(),
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
    javascript = report["javascript"]
    assert isinstance(javascript, dict)
    for half in (report, javascript):
        for problem in half["problems"]:
            print(f"{half['tool']} gate problem: {problem}")
        for violation in half["violations"]:
            print(
                f"{violation['file']}:{violation['line']}: "
                f"{violation['code']} {violation['message']}"
            )
        if half["violations_truncated"]:
            extra = int(half["violation_count"]) - MAX_RECORDED_VIOLATIONS
            print(f"... {extra} more not recorded")
    print(
        f"lint_status={report['status']} violations={report['violation_count']} "
        f"files_scanned={report['files_scanned']} tool={report['tool_version']} -> {relative}"
    )
    # Printed even when unavailable, and it names the fix. A gate nobody can see
    # skipping is the failure mode this half was added to close.
    print(
        f"js_lint_status={javascript['status']} "
        f"violations={javascript['violation_count']} "
        f"files_scanned={javascript['files_scanned']} "
        f"enforced={str(javascript['enforced']).lower()} "
        f"tool=eslint {javascript['tool_version'] or '(absent)'}"
        + (f" -- {javascript['unavailable_reason']}" if javascript["unavailable_reason"] else "")
    )
    # `unavailable` does not fail this command: a contributor without node still
    # gets the Python half, and `acceptance_report.py` is what refuses to seal a
    # release over an unenforced gate. Anything eslint actually found does fail.
    if javascript["status"] in ("violations", "error"):
        return 1
    return 0 if report["status"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
