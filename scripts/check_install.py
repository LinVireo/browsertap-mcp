"""Install the built wheel the way a stranger would, then prove it runs.

`check_distribution` reads what is *inside* an archive: required members, no
machine-local paths, publishing metadata. That is not the same question as
"does `pip install browsertap-mcp` leave you with something that works" --
package data can be excluded by a build backend, a console script can point at
a module that no longer exists, and an editable development checkout hides both
because the repository is already importable. This gate answers the install-side
question the only way that is not a guess: a throwaway virtual environment, the
freshly built wheel, and no repository on the path.

Two modes, and the report always says which one ran:

* default -- a full `pip install`, so the console script and the imports are
  actually executed;
* ``--no-deps`` -- for a host with no index access. It proves the *layout* only
  (dist metadata, console script, packaged skills and extension) and marks the
  behaviour probes ``skipped-no-deps`` rather than reporting a pass it did not
  earn.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from scripts.versioning import read_source_version

#: Wheel filenames are `<name>-<version>-<python tag>-...`, and the name is
#: normalised to underscores by the build backend.
WHEEL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>[^-]+)-.*\.whl$")

DIST_NAME = "browsertap-mcp"
IMPORT_NAME = "browsertap_mcp"


def find_wheel(dist_dir: Path, version: str) -> tuple[Path | None, list[str]]:
    """The wheel in ``dist_dir`` built from the current source version."""
    problems: list[str] = []
    if not dist_dir.is_dir():
        return None, [f"distribution directory does not exist: {dist_dir}"]
    matches: list[Path] = []
    for candidate in sorted(dist_dir.glob("*.whl")):
        parsed = WHEEL_RE.match(candidate.name)
        if parsed is None:
            problems.append(f"cannot read a version out of {candidate.name}")
            continue
        if parsed.group("version") == version:
            matches.append(candidate)
    if not matches:
        built = ", ".join(path.name for path in sorted(dist_dir.glob("*.whl"))) or "none"
        problems.append(f"no wheel in {dist_dir} carries version {version} (found: {built})")
        return None, problems
    if len(matches) > 1:
        problems.append(
            "more than one wheel carries this version, so the install target is ambiguous: "
            + ", ".join(path.name for path in matches)
        )
        return None, problems
    return matches[0], problems


def _clean_env() -> dict[str, str]:
    """An environment that cannot reach a development checkout by accident."""
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP"):
        env.pop(name, None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _run(command: list[str], *, cwd: Path, timeout: int) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=_clean_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": f"timed out after {timeout}s",
            "status": "timeout",
        }
    except OSError as exc:
        return {
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "status": "error",
        }
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "status": "ran",
    }


def _venv_paths(venv: Path) -> tuple[Path, Path]:
    """The interpreter and the console-script directory inside a virtual env."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe", venv / "Scripts"
    return venv / "bin" / "python", venv / "bin"


def run_install_check(
    dist_dir: Path,
    *,
    version: str | None = None,
    base_python: str | None = None,
    install_deps: bool = True,
    timeout: int = 900,
    workdir: Path | None = None,
) -> dict[str, object]:
    """Install ``dist_dir``'s wheel into a fresh venv and exercise it."""
    version = version or read_source_version(ROOT)
    dist_dir = Path(dist_dir)
    problems: list[str] = []
    notes: list[str] = []
    probes: list[dict[str, object]] = []

    wheel, wheel_problems = find_wheel(dist_dir, version)
    problems.extend(wheel_problems)
    if wheel is None:
        return {
            "ok": False,
            "mode": "full" if install_deps else "layout-only",
            "version": version,
            "wheel": None,
            "probes": probes,
            "problems": problems,
            "notes": notes,
            "proves_cli": False,
        }

    def _record(name: str, result: dict[str, object], *, expect: str | None = None) -> str:
        """Keep one probe's evidence and return its stdout."""
        entry = {"name": name, **result}
        probes.append(entry)
        if result["status"] != "ran" or result["exit_code"] != 0:
            detail = result["stderr"] or result["stdout"] or result["status"]
            problems.append(f"{name} failed: {str(detail).splitlines()[-1] if detail else 'no output'}")
            return ""
        stdout = str(result["stdout"])
        if expect is not None and stdout != expect:
            problems.append(f"{name} printed {stdout!r}, expected {expect!r}")
        return stdout

    with tempfile.TemporaryDirectory(prefix="btap-install-") as temporary:
        scratch = Path(workdir) if workdir is not None else Path(temporary)
        scratch.mkdir(parents=True, exist_ok=True)
        venv = scratch / "venv"
        # cwd for every probe: anywhere but the repository, so nothing can import
        # `browsertap_mcp` out of ./src and call that a working install.
        outside = scratch / "cwd"
        outside.mkdir(parents=True, exist_ok=True)

        creator = base_python or sys.executable
        create = _run([creator, "-m", "venv", str(venv)], cwd=outside, timeout=timeout)
        _record("create-venv", create)
        python, scripts_dir = _venv_paths(venv)
        if not python.exists():
            problems.append(f"virtual environment has no interpreter at {python}")
            return {
                "ok": False,
                "mode": "full" if install_deps else "layout-only",
                "version": version,
                "wheel": wheel.name,
                "probes": probes,
                "problems": problems,
                "notes": notes,
                "proves_cli": False,
            }

        install = [str(python), "-m", "pip", "install", "--no-input", str(wheel)]
        if not install_deps:
            install.insert(4, "--no-deps")
            notes.append(
                "installed with --no-deps: the console script and imports cannot run, so this "
                "run proves the packaged layout only"
            )
        before_install = len(problems)
        _record("pip-install", _run(install, cwd=outside, timeout=timeout))
        if len(problems) > before_install:
            # Every later probe would fail as a consequence, and eight derived
            # failures bury the one that explains them.
            notes.append("stopped after the install failed; later probes would only echo it")
            return {
                "ok": False,
                "mode": "full" if install_deps else "layout-only",
                "version": version,
                "wheel": wheel.name,
                "probes": probes,
                "problems": problems,
                "notes": notes,
                "proves_cli": False,
            }

        purelib = _record(
            "site-packages",
            _run(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                cwd=outside,
                timeout=timeout,
            ),
        )
        _record(
            "dist-metadata",
            _run(
                [
                    str(python),
                    "-c",
                    "import importlib.metadata as m; print(m.version('%s'))" % DIST_NAME,
                ],
                cwd=outside,
                timeout=timeout,
            ),
            expect=version,
        )

        console_script = scripts_dir / ("browsertap.exe" if os.name == "nt" else "browsertap")
        probes.append(
            {
                "name": "console-script",
                "command": [str(console_script)],
                "status": "checked",
                "exists": console_script.exists(),
            }
        )
        if not console_script.exists():
            problems.append(f"the `browsertap` console script was not installed at {console_script}")

        if purelib:
            package = Path(purelib) / IMPORT_NAME
            skills = sorted(package.glob("skills/*/SKILL.md"))
            extension_manifest = package / "chrome_extension" / "manifest.json"
            leaked_config = package / "chrome_extension" / "config.js"
            probes.append(
                {
                    "name": "package-data",
                    "status": "checked",
                    "skills": [str(path.relative_to(package)) for path in skills],
                    "extension_manifest": extension_manifest.exists(),
                    "excluded_config_js_absent": not leaked_config.exists(),
                }
            )
            if not skills:
                problems.append(
                    "no skills/<name>/SKILL.md landed in site-packages, so `browsertap skill-path` "
                    "has nothing to point at after a plain pip install"
                )
            if not extension_manifest.exists():
                problems.append("chrome_extension/manifest.json is missing from the install")
            if leaked_config.exists():
                problems.append(
                    "chrome_extension/config.js shipped even though it is excluded package data"
                )

        if install_deps:
            _record(
                "cli-version",
                _run([str(console_script), "--version"], cwd=outside, timeout=timeout),
                expect=f"browsertap {version}",
            )
            import_location = _record(
                "import-location",
                _run(
                    [
                        str(python),
                        "-c",
                        "import pathlib, %s as pkg; print(pathlib.Path(pkg.__file__).parent)"
                        % IMPORT_NAME,
                    ],
                    cwd=outside,
                    timeout=timeout,
                ),
            )
            if import_location and purelib:
                try:
                    inside = Path(import_location).resolve().is_relative_to(
                        Path(purelib).resolve()
                    )
                except ValueError:  # pragma: no cover - only on unrelated drives
                    inside = False
                if not inside:
                    problems.append(
                        f"the installed package imported from {import_location}, which is outside "
                        f"{purelib} -- the probe was not measuring the install"
                    )
            for name, subcommand in (("cli-skill-path", "skill-path"), ("cli-extension-path", "extension-path")):
                printed = _record(
                    name,
                    _run([str(console_script), subcommand], cwd=outside, timeout=timeout),
                )
                if printed and not Path(printed).exists():
                    problems.append(f"`browsertap {subcommand}` printed {printed}, which does not exist")
        else:
            for name in ("cli-version", "import-location", "cli-skill-path", "cli-extension-path"):
                probes.append({"name": name, "status": "skipped-no-deps"})

    return {
        "ok": not problems,
        "mode": "full" if install_deps else "layout-only",
        "version": version,
        "wheel": wheel.name,
        "probes": probes,
        "problems": problems,
        "notes": notes,
        "proves_cli": install_deps and not problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the built wheel into a fresh venv")
    parser.add_argument(
        "dist",
        nargs="?",
        default="dist",
        type=Path,
        help="directory holding the built wheel (default: dist)",
    )
    parser.add_argument(
        "--no-deps",
        action="store_true",
        help="skip dependency downloads; proves the packaged layout only",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="interpreter used to create the virtual environment (default: this one)",
    )
    parser.add_argument("--timeout", type=int, default=900, help="per-command timeout in seconds")
    parser.add_argument("--output", type=Path, default=None, help="also write the report as JSON")
    args = parser.parse_args(argv)

    dist_dir = args.dist if args.dist.is_absolute() else ROOT / args.dist
    report = run_install_check(
        dist_dir,
        base_python=args.python,
        install_deps=not args.no_deps,
        timeout=args.timeout,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.output is not None:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
