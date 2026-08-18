from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.check_tool_docs import build_report, report_ok

ROOT = Path(__file__).resolve().parents[1]


def test_tool_docs_and_caller_skill_are_synchronized():
    if not (ROOT / "docs" / "browser-mcp-default.SKILL.md").is_file():
        pytest.skip("caller skill is a repository-only maintenance contract")
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
    path = ROOT / "docs" / "browser-mcp-default.SKILL.md"
    if not path.is_file():
        pytest.skip("caller skill is a repository-only maintenance contract")
    skill = path.read_text(encoding="utf-8")

    for local_claim in ("本机已移除", "用户本机", "本机一人使用", "本机默认"):
        assert local_claim not in skill


def test_user_facing_docs_carry_no_pre_unification_version_numbers():
    """The extension used to version itself separately (2.x).

    Everything is unified on the package version now, so a surviving `2.x.y` in
    a user-facing doc tells the reader the advice applies to a build that no
    longer exists. Dependency versions belong in CHANGELOG or CONTRIBUTING, not
    in these files, so any `2.x.y` here is a leftover ABM version.
    """
    stale = re.compile(r"(?<![\w.])2\.\d+\.\d+(?![\w.])")
    for name in (
        "README.md",
        "README.zh-CN.md",
        "docs/USAGE.md",
        "docs/USAGE.zh-CN.md",
        "docs/TROUBLESHOOTING.md",
        "docs/TROUBLESHOOTING.zh-CN.md",
        "docs/browser-mcp-default.SKILL.md",
    ):
        path = ROOT / name
        if not path.is_file():
            continue
        found = sorted(set(stale.findall(path.read_text(encoding="utf-8"))))
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
