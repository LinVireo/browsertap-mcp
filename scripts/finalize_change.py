from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from versioning import ROOT, bump_version, read_source_version, sync_versions, validate_versions


def _run(*args: str) -> None:
    completed = subprocess.run(args, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _run_gates(skip_live: bool) -> None:
    _run(sys.executable, "-m", "compileall", "-q", "src")
    _run(sys.executable, "-m", "pytest", "tests", "-q")
    for script in ("scripts/tool_coverage_report.py", "scripts/check_tool_docs.py"):
        if (ROOT / script).is_file():
            _run(sys.executable, script)
    if not skip_live:
        _run(sys.executable, "-m", "pytest", "tests", "-q", "-m", "live")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run ABM change-set gates and bump the unified version only after success"
    )
    parser.add_argument("--bump", choices=("none", "patch", "minor"), default="patch")
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args(argv)

    validate_versions(ROOT)
    _run_gates(skip_live=args.skip_live)
    current = read_source_version(ROOT)
    target = current if args.bump == "none" else bump_version(current, args.bump)
    changed = sync_versions(ROOT, target)
    _run(sys.executable, "-m", "pytest", "tests/test_versioning.py", "-q")
    print(f"ABM change set finalized at {target}; synchronized {len(changed)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
