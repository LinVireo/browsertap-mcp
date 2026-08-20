from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.evidence_manifest import write_manifest
from scripts.versioning import (
    ROOT,
    VersionError,
    bump_version,
    latest_release_tag,
    read_source_version,
    sync_versions,
    validate_version_bump,
    validate_versions,
)


def _run(*args: str) -> None:
    completed = subprocess.run(args, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _require_module(module: str) -> None:
    probe = subprocess.run(
        (sys.executable, "-c", f"import {module}"),
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        raise SystemExit(
            f"required release dependency {module!r} is unavailable; "
            "install the project development dependencies before finalizing"
        )


def _archive_previous_outputs() -> Path | None:
    """Move every previous evidence output aside before a new round runs.

    The seal binds a fixed artifact list, so anything left over from an earlier
    round is not re-validated but is still read by humans and by
    ``--check`` diagnostics. An enumerated pattern list kept missing new output
    names (a bare ``*.xml``, pre-push reports, a ``dist-`` directory from an
    interrupted build), so this now sweeps every file directly under
    ``artifacts/`` and every ``dist``-shaped directory, keeping only the archive
    tree itself in place.
    """
    artifacts = ROOT / "artifacts"
    if not artifacts.is_dir():
        return None
    previous = {path for path in artifacts.iterdir() if path.is_file()}
    stale_directories = [
        path
        for path in artifacts.iterdir()
        if path.is_dir() and (path.name == "dist" or path.name.startswith("dist-"))
    ]
    for directory in stale_directories:
        previous.update(path for path in directory.rglob("*") if path.is_file())
    if not previous:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = artifacts / "archive" / stamp
    for source in sorted(previous):
        relative = source.relative_to(artifacts)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    for directory in sorted(stale_directories, reverse=True):
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
        directory.rmdir()
    return destination


def _run_gates(skip_live: bool) -> None:
    _run(sys.executable, "-m", "compileall", "-q", "src")
    _require_module("pytest_cov")
    _require_module("build")
    (ROOT / "artifacts").mkdir(parents=True, exist_ok=True)
    _run(
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "-q",
        "--cov=agent_browser_mcp",
        "--cov-fail-under=85",
        "--cov-report=term-missing",
        "--cov-report=json:artifacts/coverage.json",
        "--junitxml=artifacts/offline-junit.xml",
    )
    _run(
        sys.executable,
        "-m",
        "scripts.tool_coverage_report",
        "--format",
        "markdown",
        "--output",
        "artifacts/tool-coverage-offline.json",
    )
    _run(sys.executable, "-m", "scripts.check_tool_docs")
    _run(sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", "artifacts/dist")
    _run(sys.executable, "-m", "scripts.check_distribution", "artifacts/dist")
    _run(sys.executable, "-m", "pip", "check")
    if not skip_live:
        _run(
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-m",
            "live",
            "--junitxml=artifacts/live-junit.xml",
        )
        _run(
            sys.executable,
            "-m",
            "scripts.tool_coverage_report",
            "--run-live",
            "--format",
            "markdown",
            "--output",
            "artifacts/tool-coverage-live.json",
        )


def _check_version_bump() -> None:
    """Refuse to finalize production changes that reuse the last release's number.

    ``.github/workflows/test.yml`` runs this check against the previous push, so
    it never fires on a repository that has not been pushed -- which is exactly
    when a round of changes is most likely to keep the version it started with.
    The baseline here is the last release tag, and the comparison covers the
    working tree because the finalizer runs before the change set is committed.
    """
    tag = latest_release_tag(ROOT)
    if tag is None:
        print("No release tag to compare against; skipping the version-increment check.")
        return
    try:
        result = validate_version_bump(ROOT, tag, include_worktree=True)
    except VersionError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Version increment checked against {tag}: base={result['base']} "
        f"target={result['current']} production_changed={result['production_changed']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize the target ABM version, then run all change-set gates against it"
    )
    parser.add_argument("--bump", choices=("none", "patch", "minor"), default="patch")
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args(argv)

    current = read_source_version(ROOT)
    target = current if args.bump == "none" else bump_version(current, args.bump)
    changed = sync_versions(ROOT, target)
    validate_versions(ROOT)
    _check_version_bump()
    archived = _archive_previous_outputs()
    _run_gates(skip_live=args.skip_live)
    _run(sys.executable, "-m", "pytest", "tests/test_versioning.py", "-q")
    write_manifest(include_live=not args.skip_live)
    if not args.skip_live:
        _run(sys.executable, "-m", "scripts.acceptance_report")
    if archived is not None:
        print(f"Archived previous evidence under {archived.relative_to(ROOT)}.")
    print(f"ABM change set finalized at {target}; synchronized {len(changed)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
