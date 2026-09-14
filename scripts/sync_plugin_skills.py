"""Copy the packaged caller Skills into the hosts' plugin discovery directory."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sync_plugin_skills(root: Path = ROOT, *, check: bool = False) -> list[str]:
    root = root.resolve()
    source = root / "src" / "browsertap_mcp" / "skills"
    target = root / "skills"
    sources = sorted(source.glob("*/SKILL.md"))
    if not sources:
        raise ValueError("No packaged caller Skills found")
    expected = {path.relative_to(source) for path in sources}
    extra = {path.relative_to(target) for path in target.glob("*/SKILL.md")} - expected
    if extra:
        raise ValueError(f"Unexpected plugin Skills: {', '.join(map(str, sorted(extra)))}")
    changed = []
    for path in sources:
        destination = target / path.relative_to(source)
        if not destination.resolve().is_relative_to(root):
            raise ValueError(f"Plugin Skill destination escapes the project: {destination}")
        content = path.read_bytes()
        if destination.is_file() and destination.read_bytes() == content:
            continue
        changed.append(destination.relative_to(root).as_posix())
        if not check:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail on missing or stale copies")
    args = parser.parse_args(argv)
    try:
        changed = sync_plugin_skills(check=args.check)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    if args.check and changed:
        print("Plugin Skills need synchronization: " + ", ".join(changed))
        print("Run python -m scripts.sync_plugin_skills")
        return 1
    print("Plugin Skills match packaged Skills" if not changed else "Updated: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
