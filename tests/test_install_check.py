"""The clean-install gate: what a stranger gets from `pip install`, not what is in the zip.

`check_distribution` inspects archive members. This gate installs the wheel into
a throwaway virtual environment and exercises it, which is the only way the
answers to "did the package data ship", "does the console script resolve" and
"is the import coming from site-packages rather than ./src" stop being guesses.

The subprocess layer is stubbed here: a test that really built a venv and hit an
index would be slow, network-dependent, and would test pip. What these tests pin
is the gate's own reasoning -- which probes run, what each failure is called, and
that a run which could not prove the CLI never claims it did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_install as C

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.9"
WHEEL_NAME = f"browsertap_mcp-{VERSION}-py3-none-any.whl"


def _dist_with_wheel(tmp_path: Path, *names: str) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for name in names or (WHEEL_NAME,):
        (dist / name).write_bytes(b"not really a zip")
    return dist


class _FakeInstall:
    """Stands in for `_run`, materialising the layout a real install leaves behind.

    Knobs exist for each way an install can be broken while every command still
    exits 0 -- that is the interesting class of failure, because a green pip run
    is exactly what hid these problems before the gate existed.
    """

    def __init__(self, workdir: Path, **knobs: object) -> None:
        self.workdir = workdir
        self.venv = workdir / "venv"
        self.purelib = self.venv / "Lib" / "site-packages"
        self.package = self.purelib / C.IMPORT_NAME
        self.calls: list[dict[str, object]] = []

        self.install_exit = knobs.get("install_exit", 0)
        self.metadata_version = knobs.get("metadata_version", VERSION)
        self.cli_version_output = knobs.get("cli_version_output", f"browsertap {VERSION}")
        self.import_location = knobs.get("import_location", self.package)
        self.make_skills = knobs.get("make_skills", True)
        self.make_manifest = knobs.get("make_manifest", True)
        self.leak_config = knobs.get("leak_config", False)
        self.make_console_script = knobs.get("make_console_script", True)
        self.skill_path = knobs.get("skill_path", self.package / "skills" / "browsertap")
        self.extension_path = knobs.get("extension_path", self.package / "chrome_extension")

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _ok(stdout: str) -> dict[str, object]:
        return {
            "command": [],
            "exit_code": 0,
            "stdout": stdout,
            "stderr": "",
            "status": "ran",
        }

    def _console_script(self) -> Path:
        _, scripts_dir = C._venv_paths(self.venv)
        return scripts_dir / ("browsertap.exe" if C.os.name == "nt" else "browsertap")

    def _materialise(self) -> None:
        if self.make_skills:
            skill = self.package / "skills" / "browsertap" / "SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text("---\nname: browsertap\n---\n", encoding="utf-8")
        if self.make_manifest:
            manifest = self.package / "chrome_extension" / "manifest.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}\n", encoding="utf-8")
        if self.leak_config:
            leaked = self.package / "chrome_extension" / "config.js"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            leaked.write_text("// machine-local\n", encoding="utf-8")
        if self.make_console_script:
            script = self._console_script()
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_bytes(b"launcher")

    # -- the stub itself -------------------------------------------------
    def __call__(self, command: list[str], *, cwd: Path, timeout: int) -> dict[str, object]:
        self.calls.append({"command": [str(part) for part in command], "cwd": Path(cwd)})
        joined = " ".join(str(part) for part in command)
        if "-m venv" in joined:
            python, _ = C._venv_paths(Path(command[-1]))
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"interpreter")
            return self._ok("")
        if "-m pip install" in joined:
            if self.install_exit != 0:
                return {
                    "command": [str(part) for part in command],
                    "exit_code": self.install_exit,
                    "stdout": "",
                    "stderr": "ERROR: Could not find a version that satisfies the requirement",
                    "status": "ran",
                }
            self._materialise()
            return self._ok("")
        if "sysconfig.get_paths" in joined:
            return self._ok(str(self.purelib))
        if "importlib.metadata" in joined:
            return self._ok(str(self.metadata_version))
        if "pkg.__file__" in joined:
            return self._ok(str(self.import_location))
        if joined.endswith("--version"):
            return self._ok(str(self.cli_version_output))
        if joined.endswith("skill-path"):
            return self._ok(str(self.skill_path))
        if joined.endswith("extension-path"):
            return self._ok(str(self.extension_path))
        raise AssertionError(f"the gate ran a command these tests do not model: {joined}")


@pytest.fixture
def install_run(monkeypatch, tmp_path):
    """Run the gate against a stubbed subprocess layer."""

    def run(*, install_deps: bool = True, **knobs: object):
        workdir = tmp_path / "work"
        fake = _FakeInstall(workdir, **knobs)
        monkeypatch.setattr(C, "_run", fake)
        report = C.run_install_check(
            _dist_with_wheel(tmp_path),
            version=VERSION,
            install_deps=install_deps,
            workdir=workdir,
        )
        return report, fake

    return run


def _probe_names(report: dict[str, object]) -> list[str]:
    return [str(probe["name"]) for probe in report["probes"]]


def test_a_working_install_reports_ok_and_says_it_proved_the_cli(install_run):
    report, fake = install_run()

    assert report["ok"] is True
    assert report["problems"] == []
    assert report["mode"] == "full"
    assert report["proves_cli"] is True
    assert report["wheel"] == WHEEL_NAME
    assert _probe_names(report) == [
        "create-venv",
        "pip-install",
        "site-packages",
        "dist-metadata",
        "console-script",
        "package-data",
        "cli-version",
        "import-location",
        "cli-skill-path",
        "cli-extension-path",
    ]
    # The wheel is what gets installed -- never `pip install .` or the checkout.
    install = next(call for call in fake.calls if "install" in call["command"])
    assert WHEEL_NAME in install["command"][-1]
    assert "--no-deps" not in install["command"]


def test_no_probe_runs_from_inside_the_repository(install_run):
    """A cwd inside the checkout would let ./src answer for the install."""
    _, fake = install_run()

    for call in fake.calls:
        cwd = Path(call["cwd"]).resolve()
        assert ROOT != cwd and ROOT not in cwd.parents, call["command"]


def test_layout_only_mode_skips_the_behaviour_probes_and_admits_it(install_run):
    report, fake = install_run(install_deps=False)

    assert report["ok"] is True
    assert report["mode"] == "layout-only"
    # The point of the mode: it must not be mistaken for a proof that it runs.
    assert report["proves_cli"] is False
    skipped = [
        probe["name"] for probe in report["probes"] if probe.get("status") == "skipped-no-deps"
    ]
    assert skipped == ["cli-version", "import-location", "cli-skill-path", "cli-extension-path"]
    assert any("--no-deps" in note for note in report["notes"])
    install = next(call for call in fake.calls if "install" in call["command"])
    assert "--no-deps" in install["command"]


def test_a_failed_install_stops_instead_of_deriving_eight_more_failures(install_run):
    report, _ = install_run(install_exit=1)

    assert report["ok"] is False
    assert report["proves_cli"] is False
    assert len(report["problems"]) == 1
    assert "pip-install failed" in report["problems"][0]
    assert "Could not find a version" in report["problems"][0]
    assert _probe_names(report) == ["create-venv", "pip-install"]
    assert any("stopped after the install failed" in note for note in report["notes"])


def test_metadata_carrying_a_different_version_is_a_problem(install_run):
    """Installing a stale wheel is silent otherwise: every command still exits 0."""
    report, _ = install_run(metadata_version="0.0.1")

    assert report["ok"] is False
    assert "dist-metadata printed '0.0.1', expected '0.9.9'" in report["problems"]


def test_a_cli_reporting_the_wrong_version_is_a_problem(install_run):
    report, _ = install_run(cli_version_output="browsertap 0.0.1")

    assert report["ok"] is False
    assert "cli-version printed 'browsertap 0.0.1'" in "\n".join(report["problems"])


def test_an_import_from_outside_site_packages_invalidates_the_run(install_run, tmp_path):
    report, _ = install_run(import_location=ROOT / "src" / C.IMPORT_NAME)

    assert report["ok"] is False
    problem = "\n".join(report["problems"])
    assert "outside" in problem and "not measuring the install" in problem


def test_missing_package_data_is_reported_one_cause_at_a_time(install_run):
    report, _ = install_run(make_skills=False, make_manifest=False, leak_config=True)

    assert report["ok"] is False
    problems = "\n".join(report["problems"])
    assert "no skills/<name>/SKILL.md landed in site-packages" in problems
    assert "chrome_extension/manifest.json is missing" in problems
    assert "config.js shipped even though it is excluded package data" in problems
    data = next(probe for probe in report["probes"] if probe["name"] == "package-data")
    assert data["skills"] == []
    assert data["extension_manifest"] is False
    assert data["excluded_config_js_absent"] is False


def test_a_missing_console_script_is_reported_with_the_path_it_looked_at(install_run):
    report, _ = install_run(make_console_script=False)

    assert report["ok"] is False
    assert any("console script was not installed at" in problem for problem in report["problems"])
    probe = next(probe for probe in report["probes"] if probe["name"] == "console-script")
    assert probe["exists"] is False


def test_a_cli_path_pointing_at_nothing_is_a_problem(install_run, tmp_path):
    report, _ = install_run(skill_path=tmp_path / "gone" / "skills")

    assert report["ok"] is False
    assert any("which does not exist" in problem for problem in report["problems"])


def test_find_wheel_picks_the_wheel_carrying_the_source_version(tmp_path):
    dist = _dist_with_wheel(tmp_path, WHEEL_NAME, "browsertap_mcp-0.1.0-py3-none-any.whl")

    wheel, problems = C.find_wheel(dist, VERSION)

    assert wheel is not None and wheel.name == WHEEL_NAME
    assert problems == []


def test_find_wheel_names_what_it_found_when_the_version_is_absent(tmp_path):
    dist = _dist_with_wheel(tmp_path, "browsertap_mcp-0.1.0-py3-none-any.whl")

    wheel, problems = C.find_wheel(dist, VERSION)

    assert wheel is None
    assert "no wheel" in problems[0] and "browsertap_mcp-0.1.0-py3-none-any.whl" in problems[0]


def test_find_wheel_refuses_to_guess_between_two_wheels_of_one_version(tmp_path):
    dist = _dist_with_wheel(
        tmp_path,
        WHEEL_NAME,
        f"browsertap_mcp-{VERSION}-cp313-cp313-win_amd64.whl",
    )

    wheel, problems = C.find_wheel(dist, VERSION)

    assert wheel is None
    assert "ambiguous" in problems[0]


def test_find_wheel_reports_a_missing_directory_rather_than_an_empty_pass(tmp_path):
    wheel, problems = C.find_wheel(tmp_path / "nope", VERSION)

    assert wheel is None
    assert problems == [f"distribution directory does not exist: {tmp_path / 'nope'}"]


def test_an_unreadable_wheel_name_is_surfaced_even_when_a_match_exists(tmp_path):
    dist = _dist_with_wheel(tmp_path, WHEEL_NAME, "mystery.whl")

    wheel, problems = C.find_wheel(dist, VERSION)

    assert wheel is not None and wheel.name == WHEEL_NAME
    assert problems == ["cannot read a version out of mystery.whl"]


def test_the_probe_environment_cannot_reach_a_development_checkout(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    monkeypatch.setenv("VIRTUAL_ENV", str(ROOT / ".venv"))
    monkeypatch.setenv("PYTHONSTARTUP", str(ROOT / "sitecustomize.py"))

    env = C._clean_env()

    assert "PYTHONPATH" not in env
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONSTARTUP" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"


@pytest.mark.parametrize(
    ("os_name", "interpreter", "scripts"),
    [("nt", "Scripts/python.exe", "Scripts"), ("posix", "bin/python", "bin")],
)
def test_venv_layout_follows_the_platform(tmp_path, os_name, interpreter, scripts):
    # os_name is passed in rather than monkeypatched onto the os module: on
    # Python 3.10/3.11 `pathlib.Path` reads that global to choose its concrete
    # class, so patching it makes Path() raise NotImplementedError everywhere in
    # the process -- pytest's own report formatting included, which ends the run
    # with an INTERNALERROR instead of a test failure.
    python, scripts_dir = C._venv_paths(tmp_path / "venv", os_name=os_name)

    assert python == tmp_path / "venv" / Path(interpreter)
    assert scripts_dir == tmp_path / "venv" / scripts


def test_the_finalizer_proves_the_layout_and_ci_proves_the_cli():
    """The split is what keeps a local pass honest.

    The finalizer has to work on a machine with no index access, so locally the
    gate installs with --no-deps and says it proved the layout only. Every CI run
    installs for real and executes the console script.
    """
    finalizer = (ROOT / "scripts" / "finalize_change.py").read_text(encoding="utf-8")
    assert '"scripts.check_install", "artifacts/dist", "--no-deps"' in finalizer

    offline = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "python -m scripts.check_install artifacts/dist" in offline
    assert "--no-deps" not in offline


def test_publish_workflow_installs_the_built_wheel_before_it_uploads():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build_stage, publish_stage = workflow.split("publish:", 1)

    assert "python -m scripts.check_install dist" in build_stage
    # Archive contents first, then the install: the second is only worth running
    # once the first says the archive is shaped correctly.
    assert build_stage.index("python -m scripts.check_distribution dist") < build_stage.index(
        "python -m scripts.check_install dist"
    )
    # And both before anything leaves the runner. A PyPI filename is spent forever.
    assert "check_install" not in publish_stage
    assert "pypa/gh-action-pypi-publish" not in build_stage
