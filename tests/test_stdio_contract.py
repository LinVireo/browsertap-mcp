from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = (
    ROOT / "src" / "agent_browser_mcp" / "server.py",
    ROOT / "src" / "agent_browser_mcp" / "simphtml.py",
    ROOT / "src" / "agent_browser_mcp" / "browser_bridge.py",
    ROOT / "src" / "agent_browser_mcp" / "tmwebdriver.py",
)


def test_mcp_runtime_modules_never_print_to_stdout() -> None:
    violations: list[str] = []
    for path in RUNTIME_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == [], (
        "MCP stdio reserves stdout for JSON-RPC; use module logging instead: "
        + ", ".join(violations)
    )
