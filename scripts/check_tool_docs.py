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
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
# Each agent skill is checked as a *group* of paths that must all hash alike:
# the canonical copy shipped with the package, plus any mirror the caller points
# at.
#
# The canonical copy lives inside the package rather than under `docs/` so that
# `browsertap skill-path` resolves it from a plain `pip install`. A
# reader who only ever installs the wheel gets the same document a contributor
# reviews.
#
# The mirrors are deliberately NOT hardcoded. Where an agent client installs its
# skills is the machine operator's business, not this project's, so naming those
# directories here would publish someone's local layout and go stale the moment
# they reorganize it. Pass `--skill-mirror DIR` (repeatable) or set
# `BROWSERTAP_SKILL_MIRRORS` to a path-separator-separated list of directories
# that each contain `<skill-name>/SKILL.md`.
#
# A mirror is worth checking because it is usually a link back to one shared
# file: if a client directory ever holds a detached *real* copy instead, it reads
# as fine for as long as the contents happen to agree and then silently stops
# receiving updates. That silent drift is the whole reason for the hash compare.
SKILL_MIRRORS_ENV = "BROWSERTAP_SKILL_MIRRORS"
SKILL_ROOT = ROOT / "src" / "browsertap_mcp" / "skills"
# `browsertap-default` tells a calling agent how to drive the tools;
# `browsertap-bridge-recovery` tells it how to get the bridge back when the transport
# itself is down. They cross-reference each other, so a reader who receives an
# update for only one of them gets pointed at advice that no longer matches.
SKILL_NAMES = ("browsertap-default", "browsertap-bridge-recovery")


def _canonical_skill(name: str) -> Path:
    return SKILL_ROOT / name / "SKILL.md"


def _skill_mirrors(explicit: list[str] | None = None) -> list[Path]:
    raw = explicit if explicit else [
        item
        for item in os.environ.get(SKILL_MIRRORS_ENV, "").split(os.pathsep)
        if item.strip()
    ]
    return [Path(item).expanduser() for item in raw]


def _skill_group(name: str, mirrors: list[Path]) -> tuple[Path, ...]:
    return (_canonical_skill(name), *(mirror / name / "SKILL.md" for mirror in mirrors))

from browsertap_mcp import server as S
from scripts.versioning import validate_versions
from tests.tool_coverage_manifest import TOOL_COVERAGE

TOOL_LINE_RE = re.compile(r"^- \*\*([a-z][a-z0-9_]*)\*\*", re.MULTILINE)
# Phrases each skill must keep. These are the claims a caller acts on blindly,
# so losing one silently changes another agent's behaviour rather than failing
# a test somewhere visible.
REQUIRED_SKILL_TEXT = {
    "browsertap-default": (
        "filter='user'",
        "network_capture_stop",
        "full_page",
        "clip",
        "quality",
        "reload_extension_required",
        "BROWSERTAP_LAB_NO_ELICIT=1",
        # The recovery skill owns transport failures; without this pointer a
        # caller retries the MCP layer against a dead bridge instead.
        "[[browsertap-bridge-recovery]]",
    ),
    "browsertap-bridge-recovery": (
        "browsertap doctor",
        # Version advice must be derived at runtime, never a literal, because
        # the extension and the package share one version now.
        "get_setup_status.package_version",
        # ...and the way back to the calling contract.
        "[[browsertap-default]]",
    ),
}
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


def _skill_hashes(paths: Iterable[Path]) -> dict[str, str]:
    return {
        str(path): hashlib.md5(path.read_bytes()).hexdigest()
        for path in paths
    }


async def _registered_tools() -> list[Any]:
    return await S.mcp.list_tools()


def build_report(
    *,
    check_installed_skills: bool = False,
    skill_mirrors: list[str] | None = None,
) -> dict[str, Any]:
    tools = asyncio.run(_registered_tools())
    registered = {tool.name: tool for tool in tools}
    readmes = {name: _read(ROOT / name) for name in ("README.md", "README.zh-CN.md")}
    mirrors = _skill_mirrors(skill_mirrors) if check_installed_skills else []
    canonical_skills = {name: _canonical_skill(name) for name in SKILL_NAMES}
    checked_groups = {name: _skill_group(name, mirrors) for name in SKILL_NAMES}
    missing_files = sorted(
        str(path)
        for group in checked_groups.values()
        for path in group
        if not path.is_file()
    )
    missing_external_files = sorted(
        str(path)
        for group in checked_groups.values()
        for path in group[1:]
        if not path.is_file()
    )
    canonical_missing = sorted(
        name for name, path in canonical_skills.items() if not path.is_file()
    )
    hashes = _skill_hashes(
        path for group in checked_groups.values() for path in group if path.is_file()
    )
    # One verdict per skill: a mismatch names the document whose copies drifted,
    # which is the only actionable part. Comparing every path in one flat set
    # would report the two skills as "different from each other", which they
    # are supposed to be.
    skill_mismatched = sorted(
        name
        for name, group in checked_groups.items()
        if len({hashes[str(path)] for path in group if path.is_file()}) > 1
    )
    skills_text = {
        name: (_read(path) if path.is_file() else "")
        for name, path in canonical_skills.items()
    }
    missing_skill_text = {
        name: missing
        for name, required in REQUIRED_SKILL_TEXT.items()
        if (missing := [item for item in required if item not in skills_text.get(name, "")])
    }
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
        "skill_mirrors": [str(mirror) for mirror in mirrors],
        # Asking for the mirror check and getting no mirrors is a failure, not a
        # pass: an empty list silently checks nothing while reading as green.
        "skill_mirrors_unset": check_installed_skills and not mirrors,
        "versions": versions,
        "version_error": version_error,
        "check_installed_skills": check_installed_skills,
    }


def report_ok(report: dict[str, Any]) -> bool:
    skill_ok = (
        not report["skill_missing_files"] and
        not report["skill_hash_mismatch"] and
        not report["skill_missing_text"] and
        not report.get("skill_mirrors_unset")
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
    parser = argparse.ArgumentParser(description="Validate BTAP tool docs and caller skill sync")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument(
        "--check-installed-skills",
        action="store_true",
        help=(
            "also require installed mirrors of the agent skills to match; "
            f"point at them with --skill-mirror or {SKILL_MIRRORS_ENV}"
        ),
    )
    parser.add_argument(
        "--skill-mirror",
        action="append",
        metavar="DIR",
        help=(
            "directory holding installed skill copies as <skill-name>/SKILL.md "
            "(repeatable; overrides the environment variable)"
        ),
    )
    args = parser.parse_args(argv)
    report = build_report(
        check_installed_skills=args.check_installed_skills,
        skill_mirrors=args.skill_mirror,
    )
    if args.format == "markdown":
        print("# BTAP Tool Documentation Contract")
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
        print(f"- skill_mirrors={len(report['skill_mirrors'])} checked")
        print(f"- versions={report['versions']}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("skill_mirrors_unset"):
        print(
            f"--check-installed-skills was requested but no mirror was given; "
            f"pass --skill-mirror DIR or set {SKILL_MIRRORS_ENV}.",
            file=sys.stderr,
        )
    return 0 if report_ok(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
