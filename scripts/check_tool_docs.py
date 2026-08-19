"""Check the public tool contract against the shipped documentation.

The MCP schema is the source of truth for tool names and parameters. Every
registered input property must appear in that tool's English and Chinese
README block; prose remains free-form and is not treated as a second schema.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
USER_HOME = Path.home()
# Only the first entry ships with the repository and is the canonical source;
# the rest are the maintainer's *installed* caller-skill copies, which is why
# checking them is opt-in (`--check-installed-skills`) rather than part of the
# release gate — a contributor's machine has no reason to have them.
#
# Those copies come from a skill manager that keeps one real file per skill and
# links each client directory to it, so on a healthy install the three entries
# below are aliases of the same file. They are still listed separately on
# purpose: a client directory holding a detached *real* copy instead of a link
# is exactly the failure this check exists to catch, because it reads as fine
# for as long as the contents happen to agree and then silently stops receiving
# updates. The store path is machine configuration and has moved before, so
# treat a `skill_missing_external_files` entry as "re-read where the store is",
# not as "the file was deleted".
SKILL_PATHS = (
    ROOT / "docs" / "browser-mcp-default.SKILL.md",
    USER_HOME / ".cc-switch" / "skills" / "browser-mcp-default" / "SKILL.md",
    USER_HOME / ".codex" / "skills" / "browser-mcp-default" / "SKILL.md",
    USER_HOME / ".claude" / "skills" / "browser-mcp-default" / "SKILL.md",
)

from agent_browser_mcp import server as S
from scripts.versioning import validate_versions
from tests.tool_coverage_manifest import TOOL_COVERAGE

TOOL_LINE_RE = re.compile(r"^- \*\*([a-z][a-z0-9_]*)\*\*", re.MULTILINE)
REQUIRED_SKILL_TEXT = (
    "filter='user'",
    "network_capture_stop",
    "full_page",
    "clip",
    "quality",
    "reload_extension_required",
    "AGENT_BROWSER_LAB_NO_ELICIT=1",
)
REQUIRED_DEFAULT_PARAMETERS = {
    "scan_page": {"cutlist", "maxchars", "timeout"},
    "scroll_page": {"to", "timeout"},
    # The five physical-input tools move the real cursor and keyboard, so a
    # caller that guesses their pacing guesses wrong at the user's expense.
    # `activate_session` is here because its default is the safety property
    # (raise the target tab before acting); a README that only says the value
    # exists lets a reader assume the opposite.
    "mouse_move": {"duration", "activate_session"},
    "mouse_click": {"button", "clicks", "interval", "activate_session"},
    "mouse_drag": {"duration", "button", "activate_session"},
    "type_text": {"interval", "activate_session"},
    "hotkey": {"activate_session"},
    "capture_desktop_screenshot": {"return_base64"},
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _documented_tools(path: Path) -> set[str]:
    return set(TOOL_LINE_RE.findall(_read(path)))


def _tool_block(text: str, tool: str) -> str:
    marker = f"- **{tool}**"
    start = text.find(marker)
    if start < 0:
        return ""
    next_tool = re.search(
        r"^- \*\*[a-z][a-z0-9_]*\*\*",
        text[start + len(marker):],
        re.MULTILINE,
    )
    details_end = text.find("</details>", start + len(marker))
    candidates = [
        start + len(marker) + next_tool.start() if next_tool else len(text),
        details_end if details_end >= 0 else len(text),
    ]
    return text[start:min(candidates)]


def _mentions_parameter(block: str, parameter: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_])`?{re.escape(parameter)}`?(?![A-Za-z0-9_])",
            block,
        )
    )


def _display_default(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _mentions_default(block: str, parameter: str, value: Any) -> bool:
    marker = f"`{parameter}`"
    expected = re.escape(_display_default(value))
    for occurrence in re.finditer(re.escape(marker), block):
        # A tool's prose may mention the parameter before the compact schema
        # line. Check each occurrence while keeping the window local enough
        # that another parameter's default cannot satisfy this one.
        segment = block[occurrence.start():occurrence.start() + 140]
        if re.search(
            rf"(?:default|默认)\s*`?{expected}`?(?![0-9.])",
            segment,
            re.IGNORECASE,
        ):
            return True
    return False


def _skill_hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {
        str(path): hashlib.md5(path.read_bytes()).hexdigest()
        for path in paths
    }


async def _registered_tools() -> list[Any]:
    return await S.mcp.list_tools()


def build_report(*, check_installed_skills: bool = False) -> dict[str, Any]:
    tools = asyncio.run(_registered_tools())
    registered = {tool.name: tool for tool in tools}
    readmes = {name: _read(ROOT / name) for name in ("README.md", "README.zh-CN.md")}
    canonical_skill = SKILL_PATHS[0]
    external_skill_paths = SKILL_PATHS[1:]
    checked_skill_paths = SKILL_PATHS if check_installed_skills else SKILL_PATHS[:1]
    missing_files = [str(path) for path in checked_skill_paths if not path.is_file()]
    missing_external_files = [str(path) for path in external_skill_paths if not path.is_file()]
    canonical_missing = not canonical_skill.is_file()
    existing_skill_paths = tuple(path for path in checked_skill_paths if path.is_file())
    hashes = _skill_hashes(existing_skill_paths)
    skills_text = _read(canonical_skill) if not canonical_missing else ""
    missing_params = {
        readme_name: {
            tool_name: sorted(
                parameter
                for parameter in tool.inputSchema.get("properties", {})
                if not _mentions_parameter(_tool_block(text, tool_name), parameter)
            )
            for tool_name, tool in registered.items()
            if any(
                not _mentions_parameter(_tool_block(text, tool_name), parameter)
                for parameter in tool.inputSchema.get("properties", {})
            )
        }
        for readme_name, text in readmes.items()
    }
    missing_descriptions = [
        name for name, tool in registered.items() if not (tool.description or "").strip()
    ]
    missing_defaults = {
        readme_name: {
            tool_name: sorted(
                parameter
                for parameter, schema in tool.inputSchema.get("properties", {}).items()
                if (
                    parameter == "timeout"
                    or parameter in REQUIRED_DEFAULT_PARAMETERS.get(tool_name, set())
                )
                and "default" in schema
                and schema["default"] is not None
                and not _mentions_default(_tool_block(text, tool_name), parameter, schema["default"])
            )
            for tool_name, tool in registered.items()
            if any(
                (
                    parameter == "timeout"
                    or parameter in REQUIRED_DEFAULT_PARAMETERS.get(tool_name, set())
                )
                and "default" in schema
                and schema["default"] is not None
                and not _mentions_default(_tool_block(text, tool_name), parameter, schema["default"])
                for parameter, schema in tool.inputSchema.get("properties", {}).items()
            )
        }
        for readme_name, text in readmes.items()
    }
    documented = {name: _documented_tools(ROOT / name) for name in readmes}
    registered_set = set(registered)
    skill_mismatched = bool(hashes) and len(set(hashes.values())) != 1
    missing_skill_text = [item for item in REQUIRED_SKILL_TEXT if item not in skills_text]
    try:
        versions = validate_versions(ROOT)
        version_error = None
    except Exception as exc:  # pragma: no cover - exercised by the CLI gate
        versions = {}
        version_error = str(exc)
    return {
        "registered": len(registered_set),
        "expected_registered": 55,
        "coverage_manifest": len(TOOL_COVERAGE),
        "readme_missing": {
            name: sorted(registered_set - documented[name])
            for name in documented
            if registered_set - documented[name]
        },
        "readme_extra": {
            name: sorted(documented[name] - registered_set)
            for name in documented
            if documented[name] - registered_set
        },
        "missing_params": {
            name: values for name, values in missing_params.items() if values
        },
        "missing_defaults": {
            name: values for name, values in missing_defaults.items() if values
        },
        "missing_descriptions": sorted(missing_descriptions),
        "skill_missing_files": missing_files,
        "skill_missing_external_files": missing_external_files,
        "canonical_skill_missing": canonical_missing,
        "skill_hashes": hashes,
        "skill_hash_mismatch": skill_mismatched,
        "skill_missing_text": missing_skill_text,
        "versions": versions,
        "version_error": version_error,
        "check_installed_skills": check_installed_skills,
    }


def report_ok(report: dict[str, Any]) -> bool:
    skill_ok = (
        not report["skill_missing_files"] and
        not report["skill_hash_mismatch"] and
        not report["skill_missing_text"]
    )
    return (
        report["registered"] == report["expected_registered"] == 55
        and report["coverage_manifest"] == 55
        and all(not values for values in report["readme_missing"].values())
        and all(not values for values in report["readme_extra"].values())
        and not report["missing_params"]
        and not report["missing_defaults"]
        and not report["missing_descriptions"]
        and skill_ok
        and not report["version_error"]
        and len(set(report["versions"].values())) == 1
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate ABM tool docs and caller skill sync")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument(
        "--check-installed-skills",
        action="store_true",
        help="also require the maintainer's installed Agent/Codex/Claude skill copies to match",
    )
    args = parser.parse_args(argv)
    report = build_report(check_installed_skills=args.check_installed_skills)
    if args.format == "markdown":
        print("# ABM Tool Documentation Contract")
        print()
        print(f"- registered={report['registered']}")
        print(f"- coverage_manifest={report['coverage_manifest']}")
        print(f"- docs_ok={report_ok(report)}")
        print(f"- readme_missing={report['readme_missing']}")
        print(f"- readme_extra={report['readme_extra']}")
        print(f"- missing_params={report['missing_params']}")
        print(f"- missing_defaults={report['missing_defaults']}")
        print(f"- skill_missing_files={report['skill_missing_files']}")
        print(f"- skill_hash_mismatch={report['skill_hash_mismatch']}")
        print(f"- skill_missing_text={report['skill_missing_text']}")
        print(f"- versions={report['versions']}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report_ok(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
