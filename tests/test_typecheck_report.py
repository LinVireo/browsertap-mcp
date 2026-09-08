"""The type gate must prove which files were checked, including on failures."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import lint_report as L


@pytest.fixture
def source_tree(monkeypatch, tmp_path):
    package = tmp_path / "src" / "sample"
    package.mkdir(parents=True)
    for name in ("first.py", "second.py"):
        (package / name).write_text("value: int = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.10"\n', encoding="utf-8",
    )
    monkeypatch.setattr(L, "ROOT", tmp_path)
    return ("src/sample",)


def _fake_mypy(monkeypatch, *, stdout="", stderr="", exit_code=0, inventory=True, probe=0):
    def run(*args):
        if args == ("--version",):
            return subprocess.CompletedProcess(args, probe, "mypy 1.11.2\n" if not probe else "", "")
        assert "--no-incremental" in args
        assert "--check-untyped-defs" in args
        assert "--output" in args and args[args.index("--output") + 1] == "json"
        report_dir = Path(args[args.index("--linecoverage-report") + 1])
        if inventory is not None:
            files = list((L.ROOT / "src" / "sample").glob("*.py"))
            data = {"lines": {str(path): [1] for path in files}} if inventory is True else inventory
            (report_dir / "coverage.json").write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(args, exit_code, stdout, stderr)

    monkeypatch.setattr(L, "_mypy", run, raising=False)


def _diagnostic(**changes):
    return {
        "file": str(L.ROOT / "src/sample/first.py"), "line": 1,
        "message": "Incompatible types in assignment", "code": "assignment",
        "severity": "error", **changes,
    }


def test_clean_types_records_the_actual_source_inventory(monkeypatch, source_tree):
    _fake_mypy(monkeypatch)
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "clean"
    assert report["available"] is True and report["enforced"] is True
    assert report["files_scanned"] == report["files_expected"] == 2
    assert report["files"] == ["src/sample/first.py", "src/sample/second.py"]
    assert report["problems"] == []
    assert str(L.ROOT) not in json.dumps(report)


def test_type_errors_preserve_codes_and_relative_paths(monkeypatch, source_tree):
    _fake_mypy(monkeypatch, stdout=json.dumps(_diagnostic()), exit_code=1)
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "violations" and report["enforced"] is True
    assert report["violation_count"] == 1
    assert report["violations"] == [{
        "code": "assignment", "file": "src/sample/first.py", "line": 1,
        "message": "Incompatible types in assignment",
    }]
    assert str(L.ROOT) not in json.dumps(report)


@pytest.mark.parametrize("inventory", [None, {}, [], {"lines": {}}, {"lines": []}])
def test_missing_or_malformed_inventory_is_not_clean(monkeypatch, source_tree, inventory):
    _fake_mypy(monkeypatch, inventory=inventory)
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "error" and report["enforced"] is False
    assert report["problems"]


def test_a_single_checked_file_cannot_vouch_for_the_package(monkeypatch, source_tree):
    _fake_mypy(monkeypatch, inventory={"lines": {str(L.ROOT / "src/sample/first.py"): []}})
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "error" and report["enforced"] is False
    assert report["files_scanned"] == 1
    assert any("second.py" in problem for problem in report["problems"])


def test_an_empty_target_cannot_borrow_another_targets_files(monkeypatch, source_tree):
    _fake_mypy(monkeypatch)
    report = L.build_type_lint_report((*source_tree, "src/missing"))
    assert report["status"] == "error" and report["enforced"] is False
    assert any("src/missing" in problem for problem in report["problems"])


@pytest.mark.parametrize("stdout", ["mypy crashed", "[]", "{}", "null", '{"severity":"error"}'])
def test_invalid_diagnostics_never_mean_zero_errors(monkeypatch, source_tree, stdout):
    _fake_mypy(monkeypatch, stdout=stdout)
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "error" and report["enforced"] is False


@pytest.mark.parametrize("exit_code", [1, 2, 127])
def test_nonzero_exit_without_errors_is_not_clean(monkeypatch, source_tree, exit_code):
    _fake_mypy(monkeypatch, exit_code=exit_code, stderr="type checker failed")
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "error" and report["enforced"] is False


def test_notes_are_not_type_errors(monkeypatch, source_tree):
    _fake_mypy(monkeypatch, stdout=json.dumps(_diagnostic(severity="note", code=None)))
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "clean" and report["violation_count"] == 0


def test_missing_mypy_names_the_dev_extra(monkeypatch, source_tree):
    _fake_mypy(monkeypatch, probe=1)
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "unavailable"
    assert report["enforced"] is False and report["available"] is False
    assert 'pip install -e ".[dev]"' in report["unavailable_reason"]


def test_a_hung_type_checker_fails_closed(monkeypatch, source_tree):
    def run(*args):
        if args == ("--version",):
            return subprocess.CompletedProcess(args, 0, "mypy 1.11.2\n", "")
        raise subprocess.TimeoutExpired("mypy", 120)

    monkeypatch.setattr(L, "_mypy", run, raising=False)
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "error" and report["enforced"] is False
    assert any("TimeoutExpired" in problem for problem in report["problems"])


def test_real_checker_catches_errors_in_unannotated_functions(source_tree):
    pytest.importorskip("mypy")
    (L.ROOT / "src/sample/first.py").write_text(
        'def broken():\n    value: int = "bad"\n    return value\n', encoding="utf-8",
    )
    report = L.build_type_lint_report(source_tree)
    assert report["status"] == "violations"
    assert report["files_scanned"] == 2 and report["enforced"] is True
    assert report["violation_count"] == 1
    assert report["violations"][0]["code"] == "assignment"
    assert report["violations"][0]["file"] == "src/sample/first.py"
