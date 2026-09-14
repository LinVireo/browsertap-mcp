"""Plugin distribution must keep discovery copies and bundled code in agreement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.sync_plugin_skills import sync_plugin_skills

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_skills_match_the_packaged_caller_guidance():
    assert sync_plugin_skills(ROOT, check=True) == []


def test_sync_reports_drift_without_writing_then_repairs_it(tmp_path):
    source = tmp_path / "src/browsertap_mcp/skills/example/SKILL.md"
    target = tmp_path / "skills/example/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"canonical\n")
    expected = ["skills/example/SKILL.md"]

    assert sync_plugin_skills(tmp_path, check=True) == expected
    assert not target.exists()
    assert sync_plugin_skills(tmp_path) == expected
    source.write_bytes(b"updated\n")
    assert sync_plugin_skills(tmp_path, check=True) == expected
    assert target.read_bytes() == b"canonical\n"
    assert sync_plugin_skills(tmp_path) == expected
    assert target.read_bytes() == source.read_bytes()
    assert sync_plugin_skills(tmp_path, check=True) == []


def test_sync_refuses_an_unexpected_private_skill(tmp_path):
    source = tmp_path / "src/browsertap_mcp/skills/public/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"public\n")
    private = tmp_path / "skills/private/SKILL.md"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"private\n")

    with pytest.raises(ValueError, match="Unexpected plugin Skills"):
        sync_plugin_skills(tmp_path)
    assert not (tmp_path / "skills/public").exists()
    assert private.read_bytes() == b"private\n"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_plugin_launch_uses_its_bundled_source_in_an_isolated_environment(host):
    plugin = json.loads((ROOT / f".{host}-plugin/plugin.json").read_text(encoding="utf-8"))
    server = plugin["mcpServers"]["browsertap"]
    args = server["args"]
    assert server["command"] == "uv"
    assert args[0] == "run"
    # Use uv's selected Python, including for the detached bridge. On Windows
    # a relocatable console-script stub can resolve a different interpreter.
    assert args[-3:] == ["python", "-m", "browsertap_mcp.cli"]
    assert {"--isolated", "--no-project", "--no-env-file"}.issubset(args)
    bundled_source = args[args.index("--with-editable") + 1]
    if host == "claude":
        assert bundled_source == "${CLAUDE_PLUGIN_ROOT}[desktop]"
    else:
        # Codex resolves a relative cwd against the installed plugin root;
        # unlike Claude, it does not interpolate this variable in MCP args.
        assert server["cwd"] == "."
        assert bundled_source == ".[desktop]"
    assert plugin["skills"] == "./skills/"
    assert "hooks" not in plugin


def test_both_marketplaces_resolve_to_the_shared_plugin_root():
    claude = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    assert claude["name"] == codex["name"] == "browsertap"
    assert claude["plugins"][0]["name"] == codex["plugins"][0]["name"] == "browsertap-mcp"
    assert (ROOT / claude["plugins"][0]["source"]).resolve() == ROOT
    assert (ROOT / codex["plugins"][0]["source"]["path"]).resolve() == ROOT
