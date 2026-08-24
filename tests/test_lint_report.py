"""Tests for the lint gate runner.

The gate's whole purpose is that a clean verdict be checkable, so the interesting
cases are the ones where ruff says nothing and that silence could be mistaken for
approval: a path that matches no files, a crash that prints no diagnostics, and
output that is not the JSON list it was asked for.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import lint_report as L


def _completed(stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=("ruff",), returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fake_ruff(monkeypatch, *, files: list[str], check: subprocess.CompletedProcess):
    """Stand in for ruff so the verdict logic is tested without the binary."""

    def run(*args: str):
        if args[:1] == ("--version",):
            return _completed("ruff 9.9.9\n")
        if "--show-files" in args:
            return _completed("".join(f"{L.ROOT / name}\n" for name in files))
        return check

    monkeypatch.setattr(L, "_ruff", run)


def test_a_clean_run_records_what_it_scanned():
    """`violation_count` alone cannot distinguish clean from unchecked."""
    pytest.importorskip("ruff")

    report = L.build_lint_report()

    assert report["status"] == "clean"
    assert report["violation_count"] == 0
    assert report["problems"] == []
    assert list(report["targets"]) == list(L.LINT_TARGETS)
    # This repository is not a handful of files, and the per-target breakdown is
    # what makes a narrowed target list visible to a reader.
    assert report["files_scanned"] > 20
    assert all(count > 0 for count in report["files_per_target"].values())


def test_violations_are_recorded_with_repository_relative_paths(monkeypatch):
    """Artifacts are uploaded by CI, so an absolute path is a leak, not detail."""
    diagnostic = [
        {
            "code": "F401",
            "filename": str(L.ROOT / "src" / "browsertap_mcp" / "server.py"),
            "location": {"row": 12, "column": 1},
            "message": "`os` imported but unused",
        }
    ]
    _fake_ruff(
        monkeypatch,
        files=["src/browsertap_mcp/server.py", "tests/test_server.py", "scripts/versioning.py"],
        check=_completed(json.dumps(diagnostic), returncode=1),
    )

    report = L.build_lint_report()

    assert report["status"] == "violations"
    assert report["violation_count"] == 1
    assert report["violations"] == [
        {
            "code": "F401",
            "file": "src/browsertap_mcp/server.py",
            "line": 12,
            "message": "`os` imported but unused",
        }
    ]
    assert str(L.ROOT) not in json.dumps(report)


def test_a_target_that_matches_no_files_is_an_error_not_a_pass(monkeypatch):
    """The vacuous pass: ruff over nothing exits 0 with an empty diagnostic list.

    Nothing about that exit code distinguishes "the code is clean" from "the
    gate stopped looking at it", which is why the file inventory is taken
    separately and a target contributing zero files is reported rather than
    averaged away.
    """
    _fake_ruff(monkeypatch, files=["src/browsertap_mcp/server.py"], check=_completed("[]"))

    report = L.build_lint_report(("src", "tests"))

    assert report["status"] == "error"
    assert report["violation_count"] == 0
    assert "lint target matched no files: tests" in report["problems"]


def test_a_crash_with_no_diagnostics_is_not_a_clean_tree(monkeypatch):
    """Exit 2 means ruff could not do the job; exit 1 means it found something."""
    _fake_ruff(
        monkeypatch,
        files=["src/browsertap_mcp/server.py"],
        check=_completed("", returncode=2, stderr="invalid rule selector"),
    )

    report = L.build_lint_report(("src",))

    assert report["status"] == "error"
    assert any("invalid rule selector" in problem for problem in report["problems"])


def test_output_that_is_not_a_diagnostic_list_is_reported(monkeypatch):
    """A changed `--output-format` must not read as zero violations."""
    _fake_ruff(
        monkeypatch,
        files=["src/browsertap_mcp/server.py"],
        check=_completed("Found 3 errors.\n", returncode=1),
    )

    report = L.build_lint_report(("src",))

    assert report["status"] == "error"
    assert "ruff did not return a JSON diagnostic list" in report["problems"]


def test_main_writes_the_artifact_and_fails_on_violations(monkeypatch, tmp_path):
    diagnostic = [
        {
            "code": "E402",
            "filename": str(L.ROOT / "scripts" / "versioning.py"),
            "location": {"row": 4, "column": 1},
            "message": "module level import not at top of file",
        }
    ]
    _fake_ruff(
        monkeypatch,
        files=["src/a.py", "tests/b.py", "scripts/c.py"],
        check=_completed(json.dumps(diagnostic), returncode=1),
    )
    output = tmp_path / "nested" / "lint.json"

    assert L.main(["--output", str(output)]) == 1

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "violations"
    assert payload["violations"][0]["code"] == "E402"


def test_main_succeeds_and_seals_a_clean_result(monkeypatch, tmp_path):
    _fake_ruff(
        monkeypatch,
        files=["src/a.py", "tests/b.py", "scripts/c.py"],
        check=_completed("[]"),
    )
    output = tmp_path / "lint.json"

    assert L.main(["--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "clean"
    assert payload["files_scanned"] == 3
    assert payload["tool_version"] == "ruff 9.9.9"


def test_a_long_violation_list_is_truncated_but_still_counted(monkeypatch):
    """The artifact is hashed into the seal; the count is the part that must be exact."""
    count = L.MAX_RECORDED_VIOLATIONS + 7
    diagnostic = [
        {
            "code": "F401",
            "filename": str(L.ROOT / "src" / f"module_{index}.py"),
            "location": {"row": index + 1, "column": 1},
            "message": "unused",
        }
        for index in range(count)
    ]
    _fake_ruff(
        monkeypatch,
        files=["src/a.py", "tests/b.py", "scripts/c.py"],
        check=_completed(json.dumps(diagnostic), returncode=1),
    )

    report = L.build_lint_report()

    assert report["violation_count"] == count
    assert len(report["violations"]) == L.MAX_RECORDED_VIOLATIONS
    assert report["violations_truncated"] is True


def test_a_path_outside_the_repository_degrades_to_its_name():
    """Never emit an absolute path, even for a file the walk cannot relativise."""
    outside = Path("/somewhere/else/module.py").resolve()

    assert L._relative(str(outside)) == "module.py"
