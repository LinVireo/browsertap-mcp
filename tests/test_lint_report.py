"""Tests for the lint gate runner.

The gate's whole purpose is that a clean verdict be checkable, so the interesting
cases are the ones where a linter says nothing and that silence could be mistaken
for approval: a path that matches no files, a crash that prints no diagnostics,
and output that is not the JSON list it was asked for.

The gate has two halves and the second one has a third way to be vacuous that
ruff does not: eslint reads its rules from a flat config, so a `files:` pattern
that no longer matches walks the whole directory, exits 0 and reports every file
clean while enforcing **nothing**. That is not hypothetical -- it is what the
first version of this half did when handed a file outside the pattern. So
`rules_applied` is interrogated separately and an empty rule set is an error.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import lint_report as L

_EXT = "src/browsertap_mcp/chrome_extension"
# The second JS target. Two directories with two config blocks, so the
# tests that exercise the real `JS_LINT_TARGETS` have to feed both -- the
# ones below that only care about the verdict logic pass `targets=(_EXT,)`
# instead, which keeps them working when a third target is added.
_PAGE = "src/browsertap_mcp/page_scripts"


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
    monkeypatch.setattr(L, "build_type_lint_report", _passing_types)


def _passing_types():
    return {
        "tool": "mypy", "tool_version": "mypy 1.11.2", "available": True,
        "enforced": True, "unavailable_reason": None, "check_untyped_defs": True,
        "targets": list(L.TYPE_LINT_TARGETS), "files_scanned": 1, "files_expected": 1,
        "files": ["src/browsertap_mcp/server.py"], "exit_code": 0, "status": "clean",
        "violation_count": 0, "violations": [], "violations_truncated": False, "problems": [],
    }


def _eslint_run(
    *files: tuple[str, list[dict[str, object]]], returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Build eslint's `--format json` shape: one entry per file it opened."""
    return _completed(
        json.dumps(
            [
                {"filePath": str(L.ROOT / name), "messages": list(messages)}
                for name, messages in files
            ]
        ),
        returncode=returncode,
    )


def _fake_node(
    monkeypatch,
    *,
    lint: subprocess.CompletedProcess,
    print_config: subprocess.CompletedProcess | None = None,
    node_probe: subprocess.CompletedProcess | None = None,
):
    """Stand in for node so the JavaScript verdict is tested without a toolchain.

    `ESLINT_BIN` is pointed at a file that is certainly present rather than at
    the real install: `npm ci` runs on one CI job out of nine, so a test that
    needed `node_modules` would pass on a developer machine and quietly not run
    everywhere else -- the exact shape the gate under test exists to refuse.

    The sentinel has to be a file the *sdist* also carries. `package.json` was
    the obvious pick and was wrong: MANIFEST.in deliberately leaves the three
    JavaScript dev files out of the archive while shipping `tests/` so the gates
    stay runnable from it, so nine tests here failed only when run from an
    unpacked sdist -- and the report blamed `package.json is absent`, which reads
    like a missing eslint. `pyproject.toml` is packaged by definition.
    """
    monkeypatch.setattr(L, "ESLINT_BIN", Path("pyproject.toml"))
    if print_config is None:
        print_config = _completed(json.dumps({"rules": {"no-unused-vars": ["error"]}}))

    def resolve_config(path: str) -> subprocess.CompletedProcess:
        # A callable so a test can answer differently per target: the rule count
        # is asked once per lint target now, and one covered target may not
        # vouch for an uncovered one.
        return print_config(path) if callable(print_config) else print_config

    def run(*args: str):
        # Order matters: the node probe is a bare `--version`, while asking
        # eslint its version passes the same flag after the script path.
        if args[:1] == ("--version",):
            return node_probe if node_probe is not None else _completed("v22.11.0\n")
        if "--print-config" in args:
            return resolve_config(args[-1].replace("\\", "/"))
        if "--format" in args:
            return lint
        return _completed("10.9.0\n")

    monkeypatch.setattr(L, "_node", run)


def _absent_eslint(monkeypatch):
    monkeypatch.setattr(L, "ESLINT_BIN", Path("node_modules/eslint/bin/absent.js"))


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
    # The only test here that runs both linters for real, so the assertion has to
    # hold with or without `npm ci` having been run: eslint being absent is a
    # legitimate outcome on a machine with no JavaScript toolchain, and a clean
    # verdict that enforced nothing is not an outcome at all.
    javascript = report["javascript"]
    assert javascript["enforced"] == (javascript["status"] in ("clean", "violations"))
    if javascript["status"] == "clean":
        assert javascript["files_scanned"] > 0
        assert javascript["rules_applied"] > 0
    types = report["types"]
    assert types["enforced"] == (types["status"] in ("clean", "violations"))
    if types["status"] == "clean":
        assert types["files_scanned"] == types["files_expected"] > 0
        assert len(types["files"]) == types["files_scanned"]


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
    _absent_eslint(monkeypatch)

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
    _absent_eslint(monkeypatch)

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
    _absent_eslint(monkeypatch)

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
    _absent_eslint(monkeypatch)

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
    _fake_node(monkeypatch, lint=_eslint_run((f"{_EXT}/background.js", [])))
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
    # Both JS targets must contribute a file, or the per-target floor reports the
    # empty one -- which is the check working, not the gate failing.
    _fake_node(
        monkeypatch,
        lint=_eslint_run((f"{_EXT}/background.js", []), (f"{_PAGE}/page_outline.js", [])),
    )
    output = tmp_path / "lint.json"

    assert L.main(["--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "clean"
    assert payload["files_scanned"] == 3
    assert payload["tool_version"] == "ruff 9.9.9"
    assert payload["javascript"]["status"] == "clean"
    assert payload["javascript"]["enforced"] is True


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
    _absent_eslint(monkeypatch)

    report = L.build_lint_report()

    assert report["violation_count"] == count
    assert len(report["violations"]) == L.MAX_RECORDED_VIOLATIONS
    assert report["violations_truncated"] is True


def test_a_path_outside_the_repository_degrades_to_its_name():
    """Never emit an absolute path, even for a file the walk cannot relativise."""
    outside = Path("/somewhere/else/module.py").resolve()

    assert L._relative(str(outside)) == "module.py"


# --- the JavaScript half ------------------------------------------------------
#
# It exists because roughly 4.9k lines of the shipped extension had no linter at
# all, and its own failure modes are not ruff's: eslint resolves its rules from a
# flat config, so the interesting question is not only "did it read files" but
# "did the config give it anything to enforce on them".


def test_a_javascript_run_records_the_rules_it_would_enforce(monkeypatch):
    """`files_scanned > 0` is half the proof; the rule count is the other half."""
    _fake_node(
        monkeypatch,
        lint=_eslint_run((f"{_EXT}/background.js", []), (f"{_EXT}/content.js", [])),
        print_config=_completed(json.dumps({"rules": {"no-empty": ["error"], "eqeqeq": "off"}})),
    )

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "clean"
    assert report["available"] is True
    assert report["enforced"] is True
    assert report["files_scanned"] == 2
    assert report["rules_applied"] == 1
    assert report["problems"] == []


def test_a_config_that_matches_nothing_is_an_error_not_a_clean_tree(monkeypatch):
    """The vacuous pass this half was written for, and it is not hypothetical.

    Measured while building the gate: a file outside the config's `files:`
    pattern came back with an empty message list and exit 0, which is byte for
    byte what a genuinely clean file looks like. Only `--print-config` can tell
    them apart, so an empty rule set is an error even though eslint was happy.
    """
    _fake_node(
        monkeypatch,
        lint=_eslint_run((f"{_EXT}/background.js", [])),
        print_config=_completed(json.dumps({"rules": {}})),
    )

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "error"
    assert report["enforced"] is False
    assert report["violation_count"] == 0
    assert any("resolves no rules" in problem for problem in report["problems"])
    # The message has to name the thing to go and look at, or the next reader
    # re-derives this from scratch.
    assert any("eslint.config.mjs" in problem for problem in report["problems"])


def test_one_covered_target_does_not_vouch_for_an_uncovered_one(monkeypatch):
    """The rule count is a floor across targets, not a sample of the first one.

    `_rules_applied` used to take the first target's first `.js` file and stop.
    That was sound while there was one target and became a vacuous pass the
    moment `page_scripts` was added: eslint reports a file matched by no
    `files:` pattern as clean, so a second directory nobody wrote a config
    block for would have borrowed the extension's 64 rules and read as
    enforced while enforcing nothing at all.

    Verified against the real toolchain by mutation before this was written:
    changing the page_scripts block to `*.mjs`, so its pattern stops matching,
    turned the gate from `clean` / `enforced: true` into `error` /
    `enforced: false` naming that one target. This pins the same verdict
    without needing `node_modules`.
    """
    covered = json.dumps({"rules": {"no-empty": ["error"], "eqeqeq": "error"}})
    bare = json.dumps({"rules": {}})

    _fake_node(
        monkeypatch,
        lint=_eslint_run((f"{_EXT}/background.js", []), (f"{_PAGE}/page_outline.js", [])),
        # The second target is the one no config block matches.
        print_config=lambda path: _completed(bare if _PAGE in path else covered),
    )

    report = L.build_js_lint_report(targets=(_EXT, _PAGE))

    assert report["status"] == "error"
    assert report["enforced"] is False
    # Both files really were opened, so this is not the "scanned nothing"
    # shape -- which is what makes it the interesting one.
    assert report["files_scanned"] == 2
    assert report["rules_applied_by_target"] == {_EXT: 2, _PAGE: 0}
    # The floor, not the maximum and not the average.
    assert report["rules_applied"] == 0
    assert any(
        _PAGE in problem and "resolves no rules" in problem for problem in report["problems"]
    )
    # And it must not smear the blame onto the target that is fine.
    assert not any(_EXT in problem for problem in report["problems"])


def test_a_config_that_cannot_be_printed_is_not_a_pass(monkeypatch):
    """A crashing `--print-config` leaves the rule count unknown, not zero-risk."""
    _fake_node(
        monkeypatch,
        lint=_eslint_run((f"{_EXT}/background.js", [])),
        print_config=_completed("Oops: could not resolve config\n", returncode=2),
    )

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "error"
    assert report["enforced"] is False
    assert any("gave no rule set" in problem for problem in report["problems"])


def test_javascript_violations_are_recorded_with_relative_paths(monkeypatch):
    """Same rule as the Python half: the artifact is uploaded, so no host paths."""
    _fake_node(
        monkeypatch,
        lint=_eslint_run(
            (
                f"{_EXT}/background.js",
                [
                    {
                        "ruleId": "no-unused-vars",
                        "line": 412,
                        "message": "'attachedTabId' is assigned a value but never used.",
                    }
                ],
            ),
            returncode=1,
        ),
    )

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "violations"
    # A finding is proof the gate ran, so this stays enforced.
    assert report["enforced"] is True
    assert report["violations"] == [
        {
            "code": "no-unused-vars",
            "file": f"{_EXT}/background.js",
            "line": 412,
            "message": "'attachedTabId' is assigned a value but never used.",
        }
    ]
    assert str(L.ROOT) not in json.dumps(report)


def test_scanning_no_files_is_an_error_even_with_rules_resolved(monkeypatch):
    """eslint over a path that matches nothing also exits 0 with `[]`."""
    _fake_node(monkeypatch, lint=_eslint_run())

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "error"
    assert report["enforced"] is False
    # Which target came back empty, not just that one did. This per-target
    # message now fires before the whole-report `files_scanned <= 0` fallback
    # (the loop appends it first), and it is the more precise of the two -- the
    # fallback only ever spoke when no target had already been named.
    assert f"eslint opened no file under lint target: {_EXT}" in report["problems"]


def test_output_that_is_not_a_result_list_is_reported(monkeypatch):
    """A changed `--format` must not read as zero violations."""
    _fake_node(
        monkeypatch,
        lint=_completed("Oops\n", returncode=2, stderr="Invalid option '--format json'"),
    )

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "error"
    assert report["enforced"] is False
    assert any("did not return a JSON result list" in problem for problem in report["problems"])
    assert any("Invalid option" in problem for problem in report["problems"])


def test_a_missing_eslint_says_which_command_installs_it(monkeypatch):
    """Unavailable is a real state, not a failure -- but it must not read clean."""
    _absent_eslint(monkeypatch)

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "unavailable"
    assert report["available"] is False
    assert report["enforced"] is False
    assert report["files_scanned"] == 0
    assert "npm ci" in str(report["unavailable_reason"])


def test_a_missing_node_is_told_apart_from_a_missing_eslint(monkeypatch):
    """Two different fixes, so they may not collapse into one message."""
    _fake_node(
        monkeypatch,
        lint=_eslint_run(),
        node_probe=_completed("", returncode=127, stderr="node: command not found"),
    )

    report = L.build_js_lint_report(targets=(_EXT,))

    assert report["status"] == "unavailable"
    assert "node is not runnable here" in str(report["unavailable_reason"])
    assert "command not found" in str(report["unavailable_reason"])


def test_an_unavailable_javascript_half_does_not_fail_the_command(monkeypatch, tmp_path):
    """A contributor with no toolchain still gets the Python half.

    Refusing here would take the Python gate away from them to punish a missing
    optional dependency. `acceptance_report.py` is the layer that will not seal a
    release over it, and the printed line names the fix either way.
    """
    _fake_ruff(
        monkeypatch, files=["src/a.py", "tests/b.py", "scripts/c.py"], check=_completed("[]")
    )
    _absent_eslint(monkeypatch)
    output = tmp_path / "lint.json"

    assert L.main(["--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "clean"
    assert payload["javascript"]["status"] == "unavailable"
    assert payload["javascript"]["enforced"] is False


def test_javascript_violations_fail_the_command_even_when_python_is_clean(monkeypatch, tmp_path):
    """One exit code covers both linters; a green ruff may not mask a red eslint."""
    _fake_ruff(
        monkeypatch, files=["src/a.py", "tests/b.py", "scripts/c.py"], check=_completed("[]")
    )
    _fake_node(
        monkeypatch,
        lint=_eslint_run(
            (
                f"{_EXT}/popup.js",
                [{"ruleId": "no-undef", "line": 9, "message": "'x' is not defined."}],
            ),
            (f"{_PAGE}/page_outline.js", []),
            returncode=1,
        ),
    )
    output = tmp_path / "lint.json"

    assert L.main(["--output", str(output)]) == 1

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "clean"
    assert payload["javascript"]["status"] == "violations"


@pytest.mark.parametrize("status", ["violations", "error", "unavailable"])
def test_type_check_failures_fail_the_command(monkeypatch, tmp_path, status):
    _fake_ruff(monkeypatch, files=["src/a.py", "tests/b.py", "scripts/c.py"], check=_completed("[]"))
    _absent_eslint(monkeypatch)
    types = _passing_types()
    types.update(status=status, enforced=status == "violations")
    monkeypatch.setattr(L, "build_type_lint_report", lambda: types)
    output = tmp_path / "lint.json"
    assert L.main(["--output", str(output)]) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["types"]["status"] == status


@pytest.mark.parametrize("stdout,exit_code", [("[]", 2), ("[]", 1), ("", 0), ("[null]", 0)])
def test_ruff_cannot_pass_on_invalid_or_inconsistent_output(monkeypatch, stdout, exit_code):
    _fake_ruff(monkeypatch, files=["src/a.py"], check=_completed(stdout, returncode=exit_code))
    _absent_eslint(monkeypatch)
    assert L.build_lint_report(("src",))["status"] == "error"


@pytest.mark.parametrize("exit_code", [1, 2])
def test_eslint_failure_with_a_result_list_is_not_clean(monkeypatch, exit_code):
    _fake_node(monkeypatch, lint=_eslint_run((f"{_EXT}/background.js", []), returncode=exit_code))
    assert L.build_js_lint_report((_EXT,))["status"] == "error"


def test_disabled_eslint_rules_do_not_count_as_enforcement(monkeypatch):
    _fake_node(
        monkeypatch, lint=_eslint_run((f"{_EXT}/background.js", [])),
        print_config=_completed(json.dumps({"rules": {"a": [0], "b": "off", "c": ["off"]}})),
    )
    report = L.build_js_lint_report((_EXT,))
    assert report["status"] == "error" and report["enforced"] is False
    assert report["rules_applied"] == 0


def test_failed_print_config_does_not_certify_rules(monkeypatch):
    _fake_node(
        monkeypatch, lint=_eslint_run((f"{_EXT}/background.js", [])),
        print_config=_completed(json.dumps({"rules": {"no-undef": [2]}}), returncode=2),
    )
    assert L.build_js_lint_report((_EXT,))["status"] == "error"


def test_missing_node_executable_reports_unavailable(monkeypatch):
    monkeypatch.setattr(L, "ESLINT_BIN", Path("pyproject.toml"))
    def absent(*args):
        raise FileNotFoundError("node")
    monkeypatch.setattr(L, "_node", absent)
    report = L.build_js_lint_report((_EXT,))
    assert report["status"] == "unavailable" and report["enforced"] is False
