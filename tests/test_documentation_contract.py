from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.check_tool_docs import SKILL_ROOT, build_report, report_ok

ROOT = Path(__file__).resolve().parents[1]

# A drive letter followed by a separator: `D:\venvs\...`, `C:/Users/...`. The
# lookbehind keeps URL schemes (`http://`, `chrome://`) out of the pattern.
_LOCAL_PATH_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]\S*")


def _shipped_skills() -> list[Path]:
    """The agent skills as the release actually carries them.

    Discovered rather than listed: they are package data now, so a skill added
    later is covered by every check below without editing this file.
    """
    return sorted(SKILL_ROOT.glob("*/SKILL.md"))


def test_tool_docs_and_caller_skill_are_synchronized():
    # No skip: the skills ship inside the package, so an absent one is a
    # packaging regression that would leave `agent-browser-mcp skill-path`
    # pointing at an empty directory.
    assert _shipped_skills(), f"no <name>/SKILL.md under {SKILL_ROOT}"
    report = build_report()
    assert report_ok(report), report


def test_documentation_contract_covers_all_registered_tools():
    report = build_report()
    assert report["registered"] == 55
    assert report["coverage_manifest"] == 55
    assert not report["readme_missing"]
    assert not report["readme_extra"]
    assert not report["missing_params"]
    assert not report["missing_defaults"]


def test_public_guides_cover_install_diagnostics_and_security_boundaries():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    contributing_zh = (ROOT / "CONTRIBUTING.zh-CN.md").read_text(encoding="utf-8")

    for text in (readme, readme_zh):
        assert "git clone https://github.com/0xlinn/agent-browser-mcp.git" in text
        assert ".venv\\Scripts\\agent-browser-mcp.exe" in text
        assert "AGENT_BROWSER_WS_ALLOWED_ORIGINS" in text
        assert "AGENT_BROWSER_WS_ALLOW_NO_ORIGIN" in text
        assert "registering" in text
        assert "bridge_error" in text
        assert "switched_session" in text
        assert "agent-browser-mcp bridge --stop" in text
        assert "pip uninstall agent-browser-mcp" in text
        assert "chrome://extensions" in text
    assert "no_response" in troubleshooting
    assert "AGENT_BROWSER_TMWD_PORT" in troubleshooting
    # The bridge port is configured on the Python side and mirrored into the
    # extension from its service-worker console. It is deliberately NOT an
    # editable field in the popup, so the guide must carry the console step.
    assert "chrome.storage.local.set({ abm_port" in troubleshooting

    assert "declarativeNetRequest" in security
    assert "three business days" in security
    assert "python -m ruff check src tests scripts" in contributing
    assert "--cov-fail-under=85" in contributing
    assert "release/*" in contributing
    assert "[简体中文](CONTRIBUTING.zh-CN.md)" in contributing
    assert "[English](CONTRIBUTING.md)" in contributing_zh
    assert "python -m scripts.finalize_change --bump none --skip-live" in contributing_zh
    for text in (contributing, contributing_zh):
        # `check_distribution` inspects the archives `build` writes, so a guide
        # that lists it without the build step sends the reader to
        # `no wheel found` and calls it a gate run.
        assert "python -m build --wheel --sdist --outdir artifacts/dist" in text
        assert text.index("python -m build --wheel --sdist") < text.index(
            "python -m scripts.check_distribution artifacts/dist"
        )
        # The tool-evidence report is a gate in both CI and the finalizer.
        assert "python -m scripts.tool_coverage_report --format markdown" in text
        # `ruff format` is not a gate and most sources are not format-clean;
        # telling contributors to run it produces unrelated reflow diffs.
        assert "ruff format --check path/to/changed.py" not in text


def test_public_docs_preserve_background_and_coordinate_semantics():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "each omitted offset axis uses the element centre" in readme
    assert "measured from the element's top-left corner" in readme
    assert "未提供 offset 的轴取元素中心" in readme_zh
    assert "从元素左上角" in readme_zh
    assert "Windows" in readme_zh
    assert "最小化窗口" in readme_zh
    assert "on_screen=false" in readme_zh


def test_troubleshooting_quotes_the_literals_the_code_actually_emits():
    """A guide that paraphrases an error message cannot be searched for.

    These three strings are what a caller sees verbatim: the plain-text 401
    body, the refusal that fires when a named tab is gone, and the field that
    says an unnamed target was re-picked. All three previously existed only in
    the source, so the operator hitting one had nothing to look up.
    """
    bridge = (ROOT / "src" / "agent_browser_mcp" / "browser_bridge.py").read_text(
        encoding="utf-8"
    )
    # Adjacent string fragments are one value at runtime, so collapse the source
    # line breaks between them before searching for what the caller receives.
    emitted = re.sub(r'"\s*\n\s*f?"', "", bridge)
    guides = [
        (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "TROUBLESHOOTING.zh-CN.md").read_text(encoding="utf-8"),
    ]

    literals = (
        "unauthorized: missing or bad bridge token",
        "is not connected. ABM refused to execute on a different tab",
        "switched_session",
    )
    for literal in literals:
        assert literal in emitted, f"{literal!r} is no longer emitted by the bridge"
        for guide in guides:
            assert literal in guide
    for guide in guides:
        # The 401 body is not JSON. A client that assumes it is reports a parse
        # error instead of the missing token, so both guides have to say so.
        assert "JSON" in guide
        assert "switched_from" in guide


def test_public_maintenance_commands_use_module_invocation():
    paths = (
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.zh-CN.md",
        ROOT / "docs" / "superpowers" / "specs" / "2026-08-14-abm-095-stability-design.md",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "python scripts/" not in text
        assert "python scripts\\" not in text


def test_canonical_caller_skill_is_not_machine_specific():
    paths = _shipped_skills()
    assert paths, f"no <name>/SKILL.md under {SKILL_ROOT}"

    for path in paths:
        skill = path.read_text(encoding="utf-8")
        label = f"{path.parent.name}/{path.name}"

        for local_claim in ("本机已移除", "用户本机", "本机一人使用", "本机默认"):
            assert local_claim not in skill, f"{label} states a machine-local fact"
        # These skills are written from the maintainer's machine, where the
        # absolute paths happen to work. A reader on any other machine follows
        # them into a directory that does not exist.
        drive_paths = sorted(set(_LOCAL_PATH_RE.findall(skill)))
        assert not drive_paths, f"{label} carries absolute local path(s) {drive_paths}"


def test_user_facing_docs_carry_no_pre_unification_version_numbers():
    """The extension used to version itself separately (2.x).

    Everything is unified on the package version now, so a surviving `2.x.y` in
    a user-facing doc tells the reader the advice applies to a build that no
    longer exists. Dependency versions belong in CHANGELOG or CONTRIBUTING, not
    in these files, so any `2.x.y` here is a leftover ABM version.
    """
    stale = re.compile(r"(?<![\w.])2\.\d+\.\d+(?![\w.])")
    paths = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "USAGE.md",
        ROOT / "docs" / "USAGE.zh-CN.md",
        ROOT / "docs" / "TROUBLESHOOTING.md",
        ROOT / "docs" / "TROUBLESHOOTING.zh-CN.md",
    ]
    # The agent skills are the most likely place for a stale extension version
    # to survive, because they tell a reader which build to compare against.
    paths += _shipped_skills()
    for path in paths:
        if not path.is_file():
            continue
        found = sorted(set(stale.findall(path.read_text(encoding="utf-8"))))
        name = path.relative_to(ROOT).as_posix()
        assert not found, f"{name} still references pre-unification version(s) {found}"


def test_example_client_names_match_the_standard_config():
    for name in ("claude-desktop-config.json", "cursor-mcp.json"):
        payload = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
        assert payload == {
            "mcpServers": {
                "agent-browser-mcp": {
                    "type": "stdio",
                    "command": "agent-browser-mcp",
                    "args": [],
                }
            }
        }
    hermes = (ROOT / "examples" / "hermes-config.yaml").read_text(encoding="utf-8")
    assert "  agent-browser-mcp:\n" in hermes
    assert "agent_browser" not in hermes
