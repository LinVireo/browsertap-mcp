"""Complete-suite evidence exercised with actual pytest in a tiny local repository."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts import test_run_evidence as T

ROOT = Path(__file__).resolve().parents[1]


def _invoke(root, *arguments, extra_env=None):
    env = dict(os.environ)
    # Nested pytest must not overwrite the caller's coverage database.
    for name in tuple(env):
        if name.startswith(("COV_CORE_", "COVERAGE_")):
            env.pop(name)
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "src")))
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, *arguments], cwd=root, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )


def _repository(root, *, extra_files=None):
    files = {
        ".gitignore": "artifacts/\n__pycache__/\n.pytest_cache/\n.coverage*\ncoverage.xml\n",
        "pyproject.toml": (
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
            "addopts = '-k nonexistent_default_selection'\n"
            "markers = ['live: synthetic local test for evidence validation']\n"
            "[tool.coverage.run]\nbranch = true\nsource = ['browsertap_mcp', 'scripts']\n"
        ),
        "src/browsertap_mcp/__init__.py": "def twice(number):\n    return number * 2\n",
        "tests/test_numbers.py": (
            "import pytest\nfrom browsertap_mcp import twice\n\n"
            "@pytest.mark.parametrize('number', [1, 2])\n"
            "def test_double(number):\n    assert twice(number) == number * 2\n"
        ),
        "tests/test_other.py": (
            "import pytest\nfrom browsertap_mcp import twice\n\n"
            "def test_zero():\n    assert twice(0) == 0\n\n"
            "@pytest.mark.live\ndef test_synthetic_live():\n    assert twice(3) == 6\n"
        ),
    }
    files.update(extra_files or {})
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    for name in ("__init__.py", "evidence_manifest.py", "pytest_evidence.py", "test_run_evidence.py"):
        target = root / "scripts" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "scripts" / name, target)
    for args in (
        ("init", "-q"), ("config", "core.autocrlf", "false"), ("add", "."),
        ("-c", "user.name=BTAP Test", "-c", "user.email=btap-test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "fixture"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=20)
    return root


@pytest.fixture(scope="module")
def complete_suite(tmp_path_factory):
    root = _repository(tmp_path_factory.mktemp("pytest-evidence-repository"))
    for mode in ("offline", "live"):
        result = _invoke(
            root, "-m", "scripts.test_run_evidence", "--mode", mode,
            extra_env={"PYTEST_ADDOPTS": "-k test_zero", "PYTEST_PLUGINS": "missing_btap_plugin"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
    return root


@pytest.fixture
def suite_evidence(complete_suite, tmp_path):
    shutil.copytree(complete_suite / "artifacts", tmp_path / "artifacts")
    receipt = json.loads((tmp_path / T.receipt_relative("offline", "collection")).read_bytes())
    return tmp_path, receipt["source_before"]


def _receipt(root, mode="offline", phase="execution"):
    return json.loads((root / T.receipt_relative(mode, phase)).read_bytes())


def _write_receipt(root, record, *, mode="offline", phase="execution"):
    (root / T.receipt_relative(mode, phase)).write_bytes(json.dumps(record).encode())


def _xml(root, mode="offline"):
    return ET.parse(root / f"artifacts/{mode}-junit.xml")  # noqa: S314 - local pytest fixture output


def _write_xml(root, tree, mode="offline"):
    tree.write(root / f"artifacts/{mode}-junit.xml", encoding="utf-8")


@pytest.mark.parametrize("mode,expected", [("offline", 3), ("live", 1)])
def test_real_pytest_collection_execution_and_junit_are_joined(complete_suite, mode, expected):
    source = _receipt(complete_suite, mode)["source_before"]
    result = T.validate_test_run(complete_suite, mode, source=source)
    assert result["ok"] is True, result
    assert result["tests"] == result["expected_tests"] == expected
    assert len(_receipt(complete_suite, mode)["all_tests"]) == 4
    assert len(_receipt(complete_suite, mode)["reports"]) == expected * 3
    check = _invoke(complete_suite, "-m", "scripts.test_run_evidence", "--mode", mode, "--check")
    assert check.returncode == 0, check.stdout + check.stderr
    # An inherited -k, an injected plugin, and pyproject addopts were all present
    # in the invocation environment; none narrowed the canonical run.
    if mode == "offline":
        coverage = json.loads((complete_suite / "artifacts/coverage.json").read_bytes())
        assert coverage["totals"]["percent_covered"] == 100
        assert all("scripts" not in path for path in coverage["files"])


def test_runner_refuses_to_overwrite_a_previous_evidence_set(complete_suite):
    before = {path: path.read_bytes() for path in (complete_suite / "artifacts").iterdir()}
    result = _invoke(complete_suite, "-m", "scripts.test_run_evidence", "--mode", "offline")
    assert result.returncode == 1
    assert "archive the previous run" in result.stdout
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("tamper", [
    "truncate", "truncate_and_recount", "duplicate_and_recount", "replace_with_duplicate",
    "missing_nodeid", "duplicate_nodeid_property", "different_nodeid", "aggregate_counter",
    "skipped", "failure", "error",
])
def test_successful_or_recounted_junit_cannot_hide_missing_or_duplicate_tests(suite_evidence, tamper):
    root, source = suite_evidence
    tree = _xml(root)
    suite = next(tree.getroot().iter("testsuite"))
    cases = suite.findall("testcase")
    if tamper in {"truncate", "truncate_and_recount"}:
        suite.remove(cases[-1])
        if tamper == "truncate_and_recount":
            suite.set("tests", str(len(cases) - 1))
    elif tamper == "duplicate_and_recount":
        suite.append(copy.deepcopy(cases[0]))
        suite.set("tests", str(len(cases) + 1))
    elif tamper == "replace_with_duplicate":
        suite.remove(cases[-1])
        suite.append(copy.deepcopy(cases[0]))
    elif tamper == "aggregate_counter":
        tree.getroot().set("tests", "99")
    elif tamper in {"skipped", "failure", "error"}:
        ET.SubElement(cases[0], tamper)
        suite.set({"skipped": "skipped", "failure": "failures", "error": "errors"}[tamper], "1")
    else:
        properties = cases[0].find("properties")
        prop = properties.find("property")
        if tamper == "missing_nodeid":
            properties.remove(prop)
        elif tamper == "duplicate_nodeid_property":
            properties.append(copy.deepcopy(prop))
        else:
            prop.set("value", "tests/test_unknown.py::test_invented")
    _write_xml(root, tree)

    result = T.validate_test_run(root, "offline", source=source)
    assert result["ok"] is False
    assert any("JUnit" in problem for problem in result["problems"])


@pytest.mark.parametrize("field,value", [
    ("command", None), ("command", "pytest tests"), ("command", []), ("command", [None]),
    ("keyword", "test_zero"), ("targets", ["tests/test_other.py"]), ("markexpr", "live"),
    ("invoked_from_root", False), ("exit_code", 1), ("exit_code", True),
    ("schema_version", 0), ("schema_version", True), ("mode", "live"), ("phase", "collection"),
    ("all_tests", None), ("all_tests", []), ("all_tests", [{"nodeid": [], "live": False}]),
    ("selected_nodeids", []), ("selected_nodeids", [None]), ("deselected_nodeids", []),
    ("reports", None), ("reports", []),
    ("reports", [{"nodeid": "tests/test_other.py::test_zero", "when": [], "outcome": "passed"}]),
    ("source_before", {}), ("source_after", {}), ("collection_reports", []),
    ("collection_reports", [{"nodeid": "tests/test_other.py", "outcome": "skipped"}]),
])
def test_malformed_or_incomplete_execution_receipts_fail_closed(suite_evidence, field, value):
    root, source = suite_evidence
    record = _receipt(root)
    record[field] = value
    _write_receipt(root, record)
    assert T.validate_test_run(root, "offline", source=source)["ok"] is False


@pytest.mark.parametrize("phase", ["collection", "execution"])
@pytest.mark.parametrize("tamper", ["missing", "invalid_json", "non_object", "unbound"])
def test_each_receipt_must_exist_parse_and_be_bound(suite_evidence, phase, tamper):
    root, source = suite_evidence
    path = root / T.receipt_relative("offline", phase)
    manifest = {"source": source, "artifacts": {
        T.receipt_relative("offline", "collection"): {},
        T.receipt_relative("offline", "execution"): {},
        "artifacts/offline-junit.xml": {},
    }}
    if tamper == "missing":
        path.unlink()
    elif tamper == "unbound":
        manifest["artifacts"].pop(T.receipt_relative("offline", phase))
    else:
        path.write_bytes(b"{" if tamper == "invalid_json" else b"null")
    assert T.validate_test_run(root, "offline", manifest=manifest)["ok"] is False


@pytest.mark.parametrize("tamper", ["missing_phase", "duplicate_phase", "failed", "skipped"])
def test_every_test_must_complete_each_execution_phase_exactly_once(suite_evidence, tamper):
    root, source = suite_evidence
    record = _receipt(root)
    if tamper == "missing_phase":
        record["reports"].pop()
    elif tamper == "duplicate_phase":
        record["reports"].append(copy.deepcopy(record["reports"][-1]))
    else:
        record["reports"][-1]["outcome"] = tamper
    _write_receipt(root, record)
    assert T.validate_test_run(root, "offline", source=source)["ok"] is False


def test_a_consistent_subset_of_passes_cannot_redefine_the_expected_collection(suite_evidence):
    root, source = suite_evidence
    for phase in ("collection", "execution"):
        record = _receipt(root, phase=phase)
        dropped = record["selected_nodeids"].pop()
        record["deselected_nodeids"].append(dropped)
        record["reports"] = [row for row in record["reports"] if row["nodeid"] != dropped]
        _write_receipt(root, record, phase=phase)
    tree = _xml(root)
    suite = next(tree.getroot().iter("testsuite"))
    for case in suite.findall("testcase"):
        if case.find("properties/property").get("value") == dropped:
            suite.remove(case)
    suite.set("tests", "2")
    _write_xml(root, tree)

    result = T.validate_test_run(root, "offline", source=source)
    assert result["ok"] is False
    assert result["expected_tests"] == 3
    assert any("selected collection omits" in problem for problem in result["problems"])


def test_real_subset_invocations_cannot_be_presented_as_full_suites(tmp_path):
    root = _repository(tmp_path)
    for phase in ("collection", "execution"):
        command = T.pytest_command("offline", phase)
        result = _invoke(root, *command[1:], "-k", "test_zero")
        assert result.returncode == 0, result.stdout + result.stderr
    record = _receipt(root)
    assert len(record["selected_nodeids"]) == 1
    assert len(record["all_tests"]) == 4
    result = T.validate_test_run(root, "offline", source=record["source_before"])
    assert result["ok"] is False
    assert any("canonical complete-suite invocation" in problem for problem in result["problems"])


def test_actual_source_changes_between_collection_and_execution_are_rejected(tmp_path):
    root = _repository(tmp_path)
    result = _invoke(root, *T.pytest_command("offline", "collection")[1:])
    assert result.returncode == 0, result.stdout + result.stderr
    (root / "tests/test_added.py").write_bytes(b"def test_added():\n    assert True\n")
    result = _invoke(root, *T.pytest_command("offline", "execution")[1:])
    assert result.returncode == 0, result.stdout + result.stderr

    result = T.validate_test_run(root, "offline", source=_receipt(root)["source_after"])
    assert result["ok"] is False
    assert any("collection changed" in problem for problem in result["problems"])


def test_a_skipped_module_cannot_silently_shrink_the_complete_suite(tmp_path):
    root = _repository(tmp_path, extra_files={
        "tests/test_skipped.py": "import pytest\npytest.skip('fixture skip', allow_module_level=True)\n",
    })
    result = _invoke(root, "-m", "scripts.test_run_evidence", "--mode", "offline")
    assert result.returncode == 1
    assert "collection contains missing, failing or skipped reports" in result.stdout
