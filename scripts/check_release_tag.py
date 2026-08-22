"""Prove the release tag names the exact tree a publish would ship.

The version a release carries comes from the source tree, never from the ref
that triggered the run: ``release.yml`` can be dispatched from any branch, and
the local finalizer runs *before* the tag exists. Nothing checked that
``v<version>`` exists and points at the commit being built, so a tag left
behind on an older commit could publish a tree that was never validated under
that name -- and the reader of `pip download browsertap-mcp==X` has no way to
tell. Two separate things have to hold, so both are reported separately:

* the tag for this version exists and points at ``HEAD``, and
* the working tree has no uncommitted production changes, because a tag cannot
  describe files that are not in a commit yet.

Documentation-only dirt is reported but does not fail the check: it cannot
change what the wheel does, and blocking on it would push people to run the
gate less often.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from scripts.versioning import (
    VERSIONED_PATH_PREFIXES,
    VERSIONED_PATHS,
    read_source_version,
)


def _git(root: Path, *args: str, raw: bool = False) -> tuple[int, str, str]:
    """Run git and return (exit code, stdout, stderr).

    ``raw`` keeps stdout exactly as git wrote it. `status --porcelain` encodes
    the staged/unstaged distinction in the first two columns, so " M path"
    loses a column to a strip() and the path then reads as "rc/path" -- which
    silently reclassified unstaged production edits as documentation.
    """
    completed = subprocess.run(
        ("git", *args),
        cwd=Path(root),
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = completed.stdout if raw else completed.stdout.strip()
    return completed.returncode, stdout, completed.stderr.strip()


def is_production_path(path: str) -> bool:
    """Whether a repository-relative path is part of the shipped product.

    Same classification the version-increment gate uses, so "production change"
    cannot come to mean two different things in two different gates.
    """
    normalised = path.replace("\\", "/")
    return normalised in VERSIONED_PATHS or normalised.startswith(VERSIONED_PATH_PREFIXES)


def _porcelain_paths(output: str) -> list[str]:
    """Repository-relative paths from ``git status --porcelain`` output.

    Renames arrive as ``R  old -> new`` and both sides matter; paths with
    unusual characters arrive quoted. The path is read from column 2 and
    re-stripped rather than assumed to begin at column 3, so a line whose
    leading status column was lost to a strip() still parses.
    """
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 3:
            continue
        remainder = line[2:].strip()
        for part in remainder.split(" -> "):
            candidate = part.strip()
            if candidate.startswith('"') and candidate.endswith('"') and len(candidate) > 1:
                candidate = candidate[1:-1]
            if candidate:
                paths.append(candidate)
    return sorted(set(paths))


def validate_release_tag(
    root: Path = ROOT,
    *,
    allow_missing_tag: bool = False,
) -> dict[str, object]:
    """Report whether ``v<source version>`` names the commit that is checked out."""
    root = Path(root)
    problems: list[str] = []
    notes: list[str] = []

    version = read_source_version(root)
    tag = f"v{version}"

    code, head_commit, err = _git(root, "rev-parse", "HEAD")
    if code != 0:
        problems.append(f"cannot read HEAD: {err or 'git rev-parse failed'}")
        head_commit = ""

    tag_code, tag_commit, _ = _git(root, "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}")
    tag_exists = tag_code == 0 and bool(tag_commit)
    if not tag_exists:
        tag_commit = ""
        message = f"version {version} has no tag {tag}"
        if allow_missing_tag:
            notes.append(message + " yet; run this again after tagging the release commit")
        else:
            problems.append(message)

    aligned = bool(tag_exists and head_commit and tag_commit == head_commit)
    commits_ahead: int | None = None
    production_changes_since_tag: list[str] = []
    if tag_exists and head_commit and not aligned:
        count_code, count_out, _ = _git(root, "rev-list", "--count", f"{tag}..HEAD")
        if count_code == 0 and count_out.isdigit():
            commits_ahead = int(count_out)
        diff_code, diff_out, _ = _git(root, "diff", "--name-only", f"{tag}..HEAD")
        if diff_code == 0:
            production_changes_since_tag = sorted(
                path for path in diff_out.splitlines() if path and is_production_path(path)
            )
        detail = f"{tag} points at {tag_commit[:7]}, but HEAD is {head_commit[:7]}"
        if commits_ahead is not None:
            detail += f" ({commits_ahead} commit(s) ahead of the tag)"
        if production_changes_since_tag:
            detail += (
                "; production files differ: "
                + ", ".join(production_changes_since_tag[:5])
                + ("..." if len(production_changes_since_tag) > 5 else "")
            )
        else:
            detail += "; no production file differs, so re-tagging that commit is enough"
        problems.append(detail)

    status_code, status_out, status_err = _git(root, "status", "--porcelain", raw=True)
    if status_code != 0:
        problems.append(f"cannot read the working tree state: {status_err or 'git status failed'}")
        changed_paths: list[str] = []
    else:
        changed_paths = _porcelain_paths(status_out)
    worktree_production_changes = [path for path in changed_paths if is_production_path(path)]
    if worktree_production_changes:
        problems.append(
            "uncommitted production changes cannot be described by any tag: "
            + ", ".join(worktree_production_changes[:5])
            + ("..." if len(worktree_production_changes) > 5 else "")
        )
    elif changed_paths:
        notes.append(
            f"{len(changed_paths)} non-production path(s) are dirty, which no tag has to cover"
        )

    return {
        "ok": not problems,
        "version": version,
        "tag": tag,
        "tag_exists": tag_exists,
        "tag_commit": tag_commit or None,
        "head_commit": head_commit or None,
        "aligned": aligned,
        "commits_ahead": commits_ahead,
        "production_changes_since_tag": production_changes_since_tag,
        "worktree_dirty": bool(changed_paths),
        "worktree_production_changes": worktree_production_changes,
        "problems": problems,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that v<version> names the current commit")
    parser.add_argument(
        "--allow-missing-tag",
        action="store_true",
        help=(
            "treat a not-yet-created tag as a note instead of a failure, for the local run "
            "that happens before the release commit is tagged"
        ),
    )
    parser.add_argument("--output", type=Path, default=None, help="also write the report as JSON")
    args = parser.parse_args(argv)

    report = validate_release_tag(ROOT, allow_missing_tag=args.allow_missing_tag)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.output is not None:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
