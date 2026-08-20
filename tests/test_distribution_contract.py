from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tarfile
from fnmatch import fnmatch
from io import BytesIO
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib
import zipfile

from scripts.check_distribution import (
    REQUIRED_SDIST_SUFFIXES,
    REQUIRED_WHEEL_SUFFIXES,
    validate_archive,
    validate_dist_dir,
)


def _core_metadata(version: str, *, drop: str = "", replace: tuple[str, str] | None = None) -> str:
    """Minimal but realistic RFC822 core metadata.

    Real wheels and sdists always carry this file, and the distribution check
    cross-reads its ``Version:`` and its publishing fields against the filename
    and the index requirements, so fixtures have to include them or they stop
    resembling anything the builder produces. ``drop`` removes every line
    starting with that prefix and ``replace`` rewrites one line, which is how the
    tests below build an archive that is broken in exactly one way.
    """
    lines = [
        "Metadata-Version: 2.4",
        "Name: agent-browser-mcp",
        f"Version: {version}",
        "Summary: Real-browser MCP server",
        "License-Expression: MIT",
        "Project-URL: Homepage, https://github.com/LinVireo/agent-browser-mcp",
        "Classifier: Development Status :: 4 - Beta",
        "Classifier: Intended Audience :: Developers",
        "Classifier: Programming Language :: Python :: 3.10",
        "Classifier: Operating System :: Microsoft :: Windows",
        "Classifier: Topic :: Utilities",
        "Requires-Python: >=3.10",
        "Description-Content-Type: text/markdown",
    ]
    if drop:
        lines = [line for line in lines if not line.startswith(drop)]
    if replace is not None:
        prefix, replacement = replace
        lines = [replacement if line.startswith(prefix) else line for line in lines]
    return "\n".join(lines) + "\n\nlong description body\n"


def _write_sdist(path: Path, *names: str, version: str = "0.3.4", metadata: str | None = None) -> None:
    prefix = f"agent_browser_mcp-{version}"
    members = list(names)
    pkg_info = f"{prefix}/PKG-INFO"
    text = metadata if metadata is not None else _core_metadata(version)
    contents = {pkg_info: text.encode("utf-8")}
    with tarfile.open(path, "w:gz") as archive:
        for name in (*members, pkg_info):
            data = contents.get(name, b"test")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, BytesIO(data))


def _write_wheel(path: Path, names, *, version: str = "0.3.4", metadata: str | None = None) -> None:
    metadata_name = f"agent_browser_mcp-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "")
        archive.writestr(
            metadata_name, metadata if metadata is not None else _core_metadata(version)
        )


def _wheel_names() -> list[str]:
    return [suffix.lstrip("/") for suffix in REQUIRED_WHEEL_SUFFIXES]


def _sdist_names() -> list[str]:
    prefix = "agent_browser_mcp-0.3.4"
    return [f"{prefix}{suffix}" for suffix in REQUIRED_SDIST_SUFFIXES]


def test_distribution_contract_accepts_clean_wheel_and_sdist(tmp_path):
    wheel = tmp_path / "agent_browser_mcp-0.3.4-py3-none-any.whl"
    _write_wheel(wheel, ["agent_browser_mcp/server.py", *_wheel_names()])
    sdist = tmp_path / "agent_browser_mcp-0.3.4.tar.gz"
    _write_sdist(
        sdist,
        "agent_browser_mcp-0.3.4/src/agent_browser_mcp/server.py",
        *_sdist_names(),
    )

    archives, failures = validate_dist_dir(tmp_path)

    assert set(archives) == {wheel, sdist}
    assert failures == {}


def test_distribution_contract_rejects_generated_extension_config(tmp_path):
    wheel = tmp_path / "agent_browser_mcp-0.3.4-py3-none-any.whl"
    _write_wheel(
        wheel,
        ["agent_browser_mcp/chrome_extension/config.js", *_wheel_names()],
    )

    issues = validate_archive(wheel)

    assert issues == ["agent_browser_mcp/chrome_extension/config.js: generated extension config"]


def test_distribution_contract_requires_test_and_release_sources_in_sdist(tmp_path):
    sdist = tmp_path / "agent_browser_mcp-0.3.4.tar.gz"
    names = _sdist_names()
    names.remove("agent_browser_mcp-0.3.4/tests/conftest.py")
    _write_sdist(
        sdist,
        "agent_browser_mcp-0.3.4/src/agent_browser_mcp/server.py",
        *names,
    )

    assert validate_archive(sdist) == ["missing required source file: tests/conftest.py"]


def test_distribution_contract_requires_extension_runtime_in_wheel(tmp_path):
    wheel = tmp_path / "agent_browser_mcp-0.3.4-py3-none-any.whl"
    names = _wheel_names()
    names.remove("agent_browser_mcp/chrome_extension/content.js")
    _write_wheel(wheel, names)

    assert validate_archive(wheel) == [
        "missing required wheel file: agent_browser_mcp/chrome_extension/content.js"
    ]


@pytest.mark.parametrize(
    "missing_suffix",
    (
        "/agent_browser_mcp/browser_bridge.py",
        "/agent_browser_mcp/tmwebdriver.py",
    ),
)
def test_distribution_contract_requires_bridge_modules_in_wheel(
    tmp_path, missing_suffix
):
    wheel = tmp_path / "agent_browser_mcp-0.3.4-py3-none-any.whl"
    names = _wheel_names()
    names.remove(missing_suffix.lstrip("/"))
    _write_wheel(wheel, names)

    assert validate_archive(wheel) == [
        f"missing required wheel file: {missing_suffix.lstrip('/')}"
    ]


@pytest.mark.parametrize(
    "missing_suffix",
    (
        "/src/agent_browser_mcp/browser_bridge.py",
        "/src/agent_browser_mcp/tmwebdriver.py",
    ),
)
def test_distribution_contract_requires_bridge_modules_in_sdist(
    tmp_path, missing_suffix
):
    prefix = "agent_browser_mcp-0.3.4"
    sdist = tmp_path / f"{prefix}.tar.gz"
    names = _sdist_names()
    names.remove(f"{prefix}{missing_suffix}")
    _write_sdist(sdist, *names)

    assert validate_archive(sdist) == [
        f"missing required source file: {missing_suffix.lstrip('/')}"
    ]


def test_distribution_contract_rejects_wheel_whose_metadata_version_is_stale(tmp_path):
    """A rebuild that reused an old `dist-info` must not pass silently.

    Nothing else in the release chain reads the built metadata, so without this
    check the filename would advertise the new version while every installer
    reported the old one.
    """
    wheel = tmp_path / "agent_browser_mcp-0.3.12-py3-none-any.whl"
    _write_wheel(wheel, _wheel_names(), version="0.3.4")

    assert validate_archive(wheel) == [
        "agent_browser_mcp-0.3.4.dist-info/METADATA: metadata version '0.3.4' "
        "does not match filename version '0.3.12'"
    ]


def test_distribution_contract_rejects_sdist_whose_metadata_version_is_stale(tmp_path):
    sdist = tmp_path / "agent_browser_mcp-0.3.12.tar.gz"
    _write_sdist(sdist, *_sdist_names(), version="0.3.4")

    issues = validate_archive(sdist)

    assert "metadata version '0.3.4' does not match filename version '0.3.12'" in issues[-1]


def test_distribution_contract_rejects_archive_without_core_metadata(tmp_path):
    wheel = tmp_path / "agent_browser_mcp-0.3.12-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in _wheel_names():
            archive.writestr(name, "")

    assert validate_archive(wheel) == [
        "distribution does not contain exactly one core metadata file"
    ]


def test_distribution_contract_rejects_a_long_description_the_index_cannot_render(tmp_path):
    """`Description-Content-Type` is the field that fails silently everywhere else.

    Drop it and the wheel installs perfectly while the index shows the README as
    one wall of literal Markdown. A PyPI filename can never be reused, so
    noticing after the upload costs the version number.
    """
    wheel = tmp_path / "agent_browser_mcp-0.3.4-py3-none-any.whl"
    _write_wheel(wheel, _wheel_names(), metadata=_core_metadata("0.3.4", drop="Description-"))

    assert validate_archive(wheel) == [
        "agent_browser_mcp-0.3.4.dist-info/METADATA: description-content-type is '', "
        "so the index would render README.md as plain text"
    ]


@pytest.mark.parametrize(
    ("drop", "expected"),
    (
        ("Summary:", "core metadata has no summary field"),
        ("Requires-Python:", "core metadata has no requires-python field"),
        ("License-Expression:", "core metadata declares no license"),
        ("Project-URL: Homepage", "core metadata has no Homepage project URL"),
        ("Classifier: Development Status", "no 'Development Status ::' classifier"),
        ("Classifier: Intended Audience", "no 'Intended Audience ::' classifier"),
        (
            "Classifier: Programming Language :: Python :: 3.10",
            "no 'Programming Language :: Python :: 3.10' classifier",
        ),
    ),
)
def test_distribution_contract_requires_publishable_metadata(tmp_path, drop, expected):
    """Each of these is a live index requirement or a search filter.

    An unclassified release is not findable and an absent `Requires-Python` is
    offered to interpreters that cannot run it, and neither shows up in any test
    that merely installs the wheel.
    """
    sdist = tmp_path / "agent_browser_mcp-0.3.4.tar.gz"
    _write_sdist(sdist, *_sdist_names(), metadata=_core_metadata("0.3.4", drop=drop))

    issues = validate_archive(sdist)

    assert len(issues) == 1
    assert expected in issues[0]


def test_distribution_contract_reads_headers_and_not_the_markdown_body(tmp_path):
    """The long description is Markdown and routinely contains header-like lines.

    `Summary: ...` inside a fenced example must not satisfy the check, and a
    folded (continued) real header must still count as present.
    """
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: agent-browser-mcp\n"
        "Version: 0.3.4\n"
        "Summary: one line\n"
        "  continued by folding\n"
        "License-Expression: MIT\n"
        "Project-URL: Homepage, https://example.invalid/abm\n"
        "Classifier: Development Status :: 4 - Beta\n"
        "Classifier: Intended Audience :: Developers\n"
        "Classifier: Programming Language :: Python :: 3.10\n"
        "Classifier: Operating System :: Microsoft :: Windows\n"
        "Classifier: Topic :: Utilities\n"
        "Requires-Python: >=3.10\n"
        "Description-Content-Type: text/markdown\n"
        "\n"
        "# README\n"
        "Classifier: Development Status :: 1 - Planning\n"
        "Requires-Python: >=9.9\n"
    )
    wheel = tmp_path / "agent_browser_mcp-0.3.4-py3-none-any.whl"
    _write_wheel(wheel, _wheel_names(), metadata=metadata)

    assert validate_archive(wheel) == []

    body_only = metadata.replace("Summary: one line\n  continued by folding\n", "")
    stripped = tmp_path / "other-0.3.4-py3-none-any.whl"
    _write_wheel(stripped, _wheel_names(), metadata=body_only)

    assert validate_archive(stripped) == [
        "agent_browser_mcp-0.3.4.dist-info/METADATA: core metadata has no summary field"
    ]


def test_shipped_distributions_carry_publishable_metadata():
    """The real pyproject must satisfy the gate, not just the fixtures.

    Guards the failure mode where a classifier is dropped from `pyproject.toml`
    and every fixture-based test keeps passing because it builds its own
    metadata.
    """
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    classifiers = project["classifiers"]
    for prefix in (
        "Development Status ::",
        "Intended Audience ::",
        "Operating System ::",
        "Topic ::",
    ):
        assert any(entry.startswith(prefix) for entry in classifiers), prefix
    # Every interpreter the package claims to support needs its own classifier;
    # `requires-python` alone is not what the index filters on.
    assert project["requires-python"] == ">=3.10"
    for minor in ("3.10", "3.11", "3.12", "3.13"):
        assert f"Programming Language :: Python :: {minor}" in classifiers
    assert project["urls"]["Homepage"].startswith("https://")
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"


def test_build_and_runtime_dependencies_have_compatible_bounds():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)

    assert project["build-system"]["requires"] == ["setuptools>=77"]
    dependencies = project["project"]["dependencies"]
    assert all(
        any(operator in dependency for operator in (">=", "==", "~="))
        for dependency in dependencies
    )
    assert "mcp>=1.27,<2" in dependencies
    assert not any(
        name in dependency.lower()
        for dependency in dependencies
        for name in ("pyautogui", "mss", "pillow")
    )
    assert project["project"]["optional-dependencies"]["desktop"] == [
        "pyautogui>=0.9.54",
        "mss>=10.1.0",
        "pillow>=12.2.0",
    ]
    assert "chrome_extension/_locales/*/*.json" in project["tool"]["setuptools"]["package-data"][
        "agent_browser_mcp"
    ]


# Distribution name for every third-party top-level module the package imports.
# Optional ones live behind the `desktop` extra and are imported lazily.
_IMPORT_TO_DISTRIBUTION = {
    "anyio": "anyio",
    "bottle": "bottle",
    "bs4": "beautifulsoup4",
    "mcp": "mcp",
    "pydantic": "pydantic",
    "requests": "requests",
    "simple_websocket_server": "simple-websocket-server",
}
_OPTIONAL_IMPORTS = {"PIL": "pillow", "mss": "mss", "pyautogui": "pyautogui"}


def _third_party_imports() -> set[str]:
    root = Path(__file__).resolve().parents[1] / "src" / "agent_browser_mcp"
    modules: set[str] = set()
    for source in sorted(root.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return {
        module
        for module in modules
        if module not in sys.stdlib_module_names and module != "agent_browser_mcp"
    }


def test_every_module_the_package_imports_is_a_declared_dependency():
    """An undeclared import that happens to be a transitive works until it isn't.

    `anyio` and `pydantic` reached the package only through `mcp`, which is free
    to drop or repin either one in any release; the install would then succeed
    and `import agent_browser_mcp.server` would fail. This test fails on the next
    undeclared import instead of waiting for that.
    """
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    declared = {
        re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].strip().lower()
        for group in (
            project["project"]["dependencies"],
            *project["project"]["optional-dependencies"].values(),
        )
        for requirement in group
    }

    imported = _third_party_imports()
    unmapped = imported - set(_IMPORT_TO_DISTRIBUTION) - set(_OPTIONAL_IMPORTS)
    assert not unmapped, f"new third-party import(s) with no distribution mapping: {unmapped}"

    missing = {
        module: distribution
        for module, distribution in {**_IMPORT_TO_DISTRIBUTION, **_OPTIONAL_IMPORTS}.items()
        if module in imported and distribution not in declared
    }
    assert not missing, f"imported but undeclared: {missing}"

    runtime = {
        re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].strip().lower()
        for requirement in project["project"]["dependencies"]
    }
    assert "anyio" in runtime and "pydantic" in runtime
    # Optional imports must stay out of the mandatory set.
    assert not (runtime & set(_OPTIONAL_IMPORTS.values()))
    # Only the release tooling parses pyproject.toml, so `tomli` is dev-only.
    assert "tomli" not in runtime
    assert any(
        requirement.startswith("tomli")
        for requirement in project["project"]["optional-dependencies"]["dev"]
    )



def test_distribution_contract_ships_packaged_skills_but_rejects_stray_copies(tmp_path):
    """The skills ship *from one place only*.

    `agent-browser-mcp skill-path` points a caller's skill manager at
    `agent_browser_mcp/skills/`, so that copy is required. A second copy
    somewhere else in the archive is a caller's own installed mirror or a
    leftover, and it drifts from the shipped one without anything failing.
    """
    sdist = tmp_path / "agent_browser_mcp-0.3.4.tar.gz"
    _write_sdist(
        sdist,
        *_sdist_names(),
        "agent_browser_mcp-0.3.4/docs/browser-mcp-default.SKILL.md",
        "agent_browser_mcp-0.3.4/SKILL.md",
    )

    assert validate_archive(sdist) == [
        "agent_browser_mcp-0.3.4/docs/browser-mcp-default.SKILL.md: "
        "agent skill outside the packaged skills directory",
        "agent_browser_mcp-0.3.4/SKILL.md: "
        "agent skill outside the packaged skills directory",
    ]


def test_distribution_contract_requires_every_packaged_skill_in_the_wheel(tmp_path):
    wheel = tmp_path / "agent_browser_mcp-0.3.4-py3-none-any.whl"
    dropped = "agent_browser_mcp/skills/abm-bridge-recovery/SKILL.md"
    assert dropped in _wheel_names(), "the recovery skill is no longer a required wheel member"
    _write_wheel(
        wheel,
        [
            "agent_browser_mcp/server.py",
            *(name for name in _wheel_names() if name != dropped),
        ],
    )

    failures = validate_archive(wheel)

    assert [failure for failure in failures if dropped in failure], failures


def test_manifest_ships_the_packaged_agent_skills():
    root = Path(__file__).resolve().parents[1]
    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    # A bare root-level `SKILL.md` is still refused: that one is a caller's own
    # copy dropped in the checkout, not part of the release.
    assert "exclude SKILL.md" in manifest
    assert "recursive-include src/agent_browser_mcp/skills SKILL.md" in manifest
    # sdist and wheel take different routes into the archive, so shipping needs
    # both the MANIFEST rule above and the package-data glob below. Getting only
    # one produces a source archive that carries the skills and a wheel that
    # does not, which is the half that `pip install` actually uses.
    assert '"skills/*/SKILL.md"' in pyproject
    # Discovered rather than listed: a skill added later has to be reachable
    # through those two rules without editing this test.
    shipped_skills = sorted(
        path.parent.name
        for path in (root / "src" / "agent_browser_mcp" / "skills").glob("*/SKILL.md")
    )
    assert shipped_skills, "expected at least one agent skill under src/agent_browser_mcp/skills/"
    assert "include CONTRIBUTING.zh-CN.md" in manifest


def test_repository_hygiene_rules_do_not_hide_python_sources_or_mutate_import_paths():
    root = Path(__file__).resolve().parents[1]
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    server = (root / "src" / "agent_browser_mcp" / "server.py").read_text(encoding="utf-8")
    content = (
        root / "src" / "agent_browser_mcp" / "chrome_extension" / "content.js"
    ).read_text(encoding="utf-8")

    # Rules only, never the prose. `_*.py` used to be an actual rule here and it
    # hid `_version.py`, the single source of the release version, so the file
    # now documents the ban -- and a comment naming the pattern must not read as
    # the pattern. Generalised past that one case: no rule may match any shipped
    # Python source, whatever it is called.
    rules = [
        line.strip()
        for line in gitignore.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "!"))
    ]
    sources = sorted((*(root / "src").rglob("*.py"), *(root / "scripts").rglob("*.py")))
    assert sources, "the source scan found nothing, so this assertion proves nothing"
    hidden = sorted(
        f"{rule!r} hides {relative}"
        for rule in rules
        for path in sources
        if (relative := path.relative_to(root).as_posix())
        and (fnmatch(path.name, rule.lstrip("/")) or fnmatch(relative, rule.lstrip("/")))
    )
    assert not hidden

    assert "sys.path.insert" not in server
    assert "streamlit" not in content.lower()


def test_every_file_the_release_contract_requires_is_tracked_by_the_repository():
    """Archives are built from the working tree; a clone gets only what is tracked.

    Both required-file lists are checked against archives that `build` produced
    from this same tree, so they cannot notice a file that exists on a
    maintainer's disk and nowhere in the repository. That gap ships two distinct
    broken installs: a clone missing `browser_bridge.py` cannot import the
    package at all, and a clone missing `_locales/` cannot load the unpacked
    extension, because Chrome rejects a `__MSG_extensionName__` it cannot
    resolve. Only git can answer the question, so only git is asked.
    """
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("git tracking is a repository-only contract")
    try:
        listed = subprocess.run(
            ("git", "ls-files", "-z"),
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - no git
        pytest.skip(f"git is unavailable: {exc}")
    tracked = {name for name in listed.split("\0") if name}
    # Wheel members drop the `src/` layout prefix; sdist members are already
    # repository-relative once the leading separator is gone.
    required = sorted(
        {"src" + suffix for suffix in REQUIRED_WHEEL_SUFFIXES}
        | {suffix.lstrip("/") for suffix in REQUIRED_SDIST_SUFFIXES}
    )
    missing = [name for name in required if name not in tracked]
    assert not missing, (
        "these files are required in a release archive but absent from the "
        f"repository, so a clone cannot reproduce this build: {missing}"
    )


def test_extension_locales_define_every_key_the_extension_asks_for():
    """A missing key degrades silently, so nothing reports it at runtime.

    `popup.js` falls back to the raw key name and `content.js` to an English
    literal, so a locale that lost a key renders `cookieViewerTitle` at the user
    instead of failing. The manifest is worse: Chrome refuses to load the
    extension when a `__MSG_*` name cannot be resolved in `default_locale`.
    """
    extension = Path(__file__).resolve().parents[1] / "src" / "agent_browser_mcp"
    extension = extension / "chrome_extension"
    locales = {
        path.parent.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(extension.glob("_locales/*/messages.json"))
    }
    manifest = (extension / "manifest.json").read_text(encoding="utf-8")
    assert json.loads(manifest)["default_locale"] in locales

    used = set(re.findall(r"__MSG_(\w+)__", manifest))
    used |= set(
        re.findall(
            r"""data-i18n=["'](\w+)["']""",
            (extension / "popup.html").read_text(encoding="utf-8"),
        )
    )
    for name in ("popup.js", "content.js", "background.js"):
        used |= set(
            re.findall(
                r"""(?:getMessage|message)\(\s*['"](\w+)['"]""",
                (extension / name).read_text(encoding="utf-8"),
            )
        )
    assert "extensionName" in used, "the key scan stopped matching the extension sources"

    for locale, messages in locales.items():
        undefined = sorted(used - set(messages))
        assert not undefined, f"_locales/{locale} does not define {undefined}"
        assert all(
            isinstance(entry.get("message"), str) and entry["message"]
            for entry in messages.values()
        ), f"_locales/{locale} carries an empty message"


def test_live_runner_is_scoped_to_canonical_repository_and_environment():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "live.yml").read_text(encoding="utf-8")
    assert "if: github.repository == 'LinVireo/agent-browser-mcp'" in workflow
    assert "environment: abm-live" in workflow
    assert "sys.path.insert" not in workflow


def test_publish_workflow_cannot_fire_by_accident_and_stores_no_upload_token():
    """An upload to PyPI is irreversible: the filename can never be reused.

    So the publish path must not be reachable from a push, must go through an
    environment (that is where a required reviewer is configured), and must get
    its credential from Trusted Publishing rather than a stored secret.
    """
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    triggers = workflow.split("permissions:", 1)[0]
    assert "workflow_dispatch:" in triggers
    assert "\n  push:" not in triggers
    assert "pull_request:" not in triggers

    assert "if: github.repository == 'LinVireo/agent-browser-mcp'" in workflow
    assert "needs: build" in workflow
    assert "environment: ${{" in workflow
    assert "id-token: write" in workflow
    # A stored API token is the thing Trusted Publishing exists to remove; if one
    # appears here it is a leak waiting to happen and a rotation nobody will do.
    assert "password:" not in workflow
    assert "PYPI_API_TOKEN" not in workflow
    assert "secrets." not in workflow
    # The gates run against the archives that will actually be uploaded, in the
    # only window where a metadata or rendering fault is still fixable.
    build_stage = workflow.split("publish:", 1)[0]
    assert "python -m scripts.check_distribution dist" in build_stage
    assert "python -m twine check --strict dist/*" in build_stage
    assert "--cov-fail-under=85" in build_stage
    assert build_stage.index("python -m build --wheel --sdist") < build_stage.index(
        "python -m scripts.check_distribution dist"
    )


def test_publish_workflow_defaults_to_the_index_that_can_be_undone():
    """TestPyPI is the default because a mistake there costs nothing.

    Reaching the real index takes either an explicit choice of `pypi` or a
    published GitHub Release, both of which are deliberate acts.
    """
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    inputs = workflow.split("options:", 1)[0]
    assert "default: testpypi" in inputs
    assert "repository-url: https://test.pypi.org/legacy/" in workflow
    assert "if: github.event_name == 'release' || inputs.index == 'pypi'" in workflow
