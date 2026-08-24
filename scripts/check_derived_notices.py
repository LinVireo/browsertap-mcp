"""Keep `THIRD-PARTY-NOTICES.md`'s derived-line table true to the tree.

The table states how much of each upstream file survives here. Those figures can
only be measured against an upstream checkout, which is not in this tree, so for
three releases the only automated question asked about them was whether
`identical / total` matched the stated percentage. That is self-consistency, not
truth, and it passed while two rows were wrong: `7efc604` edited `simphtml.py`
and `popup.js` without re-measuring, and the resulting notice was sealed at
`105/105` stating `780 of 873` for a file that by then matched 757.

So there are two halves here, and the offline one is what catches that:

* `measure()` needs upstream and produces the real figures. It is a maintainer
  command -- `--upstream DIR` is mandatory rather than defaulted, because a
  measurement against a directory that happens not to exist is worse than no
  measurement at all.
* `MEASURED_AGAINST` records what each derived file hashed to when the table was
  last measured. Comparing that to the tree needs nothing but the tree, so
  editing a derived file without re-measuring is a red gate instead of a silent
  drift. `tests/test_documentation_contract.py` is what reads it.

Re-measuring, after editing any derived file:

    git clone https://github.com/lsdefine/GenericAgent <dir>
    python -m scripts.check_derived_notices --upstream <dir> --check
    python -m scripts.check_derived_notices --upstream <dir> --write
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "src/browsertap_mcp"
EXT = f"{PKG}/chrome_extension"
PAGE_SCRIPTS = f"{PKG}/page_scripts"
NOTICES_NAME = "THIRD-PARTY-NOTICES.md"

# Ours -> upstream. The two `page_scripts/*.js` files were replaced outright
# in 0.4.15 (the page analysis now asks the engine instead of re-deriving it),
# so upstream's `simphtml.py` has exactly one heir here again. `family_total()`
# still exists because a future split would recreate the overlap.
DERIVED_PAIRS: tuple[tuple[str, str], ...] = (
    (f"{PKG}/simphtml.py", "simphtml.py"),
    (f"{PKG}/browser_bridge.py", "TMWebDriver.py"),
    (f"{EXT}/background.js", "assets/tmwd_cdp_bridge/background.js"),
    (f"{EXT}/manifest.json", "assets/tmwd_cdp_bridge/manifest.json"),
    (f"{EXT}/content.js", "assets/tmwd_cdp_bridge/content.js"),
    (f"{EXT}/popup.html", "assets/tmwd_cdp_bridge/popup.html"),
    (f"{EXT}/popup.js", "assets/tmwd_cdp_bridge/popup.js"),
    (f"{EXT}/disable_dialogs.js", "assets/tmwd_cdp_bridge/disable_dialogs.js"),
)

# The upstream file the page scripts were carved out of, used for the family
# figure below.
FAMILY_UPSTREAM = "simphtml.py"

# sha256 of each derived file as of the measurement the table states. Regenerate
# with `--write`; never hand-edit an entry to silence the check, because the whole
# point is that a changed derived file and a stale table are the same event.
MEASURED_AGAINST: dict[str, str] = {
    "src/browsertap_mcp/browser_bridge.py": "5b9ee46e38bb3e3a4df0551ae096fdb52ba570523569ce0c4e7d9e7e14940e0b",
    "src/browsertap_mcp/chrome_extension/background.js": "6a76936176acbc9ceb6c8484db1bc35f158b81000a719ae976a0adc9ff8a3050",
    "src/browsertap_mcp/chrome_extension/content.js": "942d5df35bedba224c13db6930f2d07bccf554f7713c28885fd5f5b51f8a644d",
    "src/browsertap_mcp/chrome_extension/disable_dialogs.js": "1edc19d8a6a5bf0850cc0e8f2123fe799d80fb5b7afa578fa014586d7181970a",
    "src/browsertap_mcp/chrome_extension/manifest.json": "7a48f3208badd3f90c33f2e56ff90ce0afde94959fad2489fc90b83ced7b3564",
    "src/browsertap_mcp/chrome_extension/popup.html": "6957d4ee2b058edafd3e704ee496c1e6c232919d447ff5dcff32c76b8ca6ae2a",
    "src/browsertap_mcp/chrome_extension/popup.js": "b8295038ad083c13529ce3668bded40b8047cdf143f70986201d470944d42cce",
    "src/browsertap_mcp/simphtml.py": "f27253df366691a755f7d81adae0db74faf2b1705815933393698659715d6019",
}

_ROW_RE = re.compile(
    r"^\|\s*`(?P<ours>src/[^`]+)`\s*\|\s*`(?P<theirs>[^`]+)`\s*\|\s*"
    r"(?P<identical>\d+) of (?P<total>\d+) \((?P<percent>\d+)%\)"
)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def file_digest(path: Path) -> str:
    """Hash the file's *lines*, so a checkout's line endings do not change it.

    `.gitattributes` pins `*.py` and `*.js` to LF, but `manifest.json` and
    `popup.html` are not covered by it, and a CRLF checkout would otherwise
    report every derived file as edited.
    """
    return hashlib.sha256("\n".join(_lines(path)).encode("utf-8")).hexdigest()


def fingerprints(root: Path = ROOT) -> dict[str, str]:
    """Current hash of every derived file, keyed by repo-relative path."""
    return {ours: file_digest(root / ours) for ours, _ in DERIVED_PAIRS}


def measure(upstream: Path, root: Path = ROOT) -> list[dict[str, object]]:
    """Line-for-line measurement of each pair. Needs the upstream checkout."""
    rows: list[dict[str, object]] = []
    for ours, theirs in DERIVED_PAIRS:
        up_path = upstream / theirs
        if not up_path.is_file():
            raise SystemExit(f"upstream file missing: {up_path}")
        up, our = _lines(up_path), _lines(root / ours)
        matcher = difflib.SequenceMatcher(None, up, our, autojunk=False)
        blocks = [b for b in matcher.get_matching_blocks() if b.size]
        identical = sum(b.size for b in blocks)
        rows.append(
            {
                "ours": ours,
                "theirs": theirs,
                "identical": identical,
                "total": len(up),
                "percent": round(100 * identical / len(up)),
                "run": max((b.size for b in blocks), default=0),
                "our_lines": len(our),
            }
        )
    return rows


def family_total(upstream: Path, root: Path = ROOT) -> dict[str, int]:
    """How many of upstream `simphtml.py`'s lines survive across all its heirs.

    Only one file descends from it as of 0.4.15, so this agrees with that row --
    but three did in 0.4.14, and a line present in two heirs is counted twice by
    `measure()`. This walks the upstream line numbers instead, so each is counted
    once and the total cannot exceed the file, which is what keeps the figure safe
    to publish after the next split.
    """
    up = _lines(upstream / FAMILY_UPSTREAM)
    heirs = [ours for ours, theirs in DERIVED_PAIRS if theirs == FAMILY_UPSTREAM]
    covered: set[int] = set()
    for ours in heirs:
        matcher = difflib.SequenceMatcher(None, up, _lines(root / ours), autojunk=False)
        for block in matcher.get_matching_blocks():
            covered.update(range(block.a, block.a + block.size))
    return {"identical": len(covered), "total": len(up), "heirs": len(heirs)}


def table_rows(root: Path = ROOT) -> dict[str, dict[str, int | str]]:
    """Parse the table out of the notice, keyed the same way as `fingerprints`."""
    text = (root / NOTICES_NAME).read_text(encoding="utf-8")
    rows: dict[str, dict[str, int | str]] = {}
    for line in text.splitlines():
        match = _ROW_RE.match(line)
        if match is None:
            continue
        rows[match.group("ours")] = {
            "upstream": match.group("theirs"),
            "identical": int(match.group("identical")),
            "total": int(match.group("total")),
            "percent": int(match.group("percent")),
        }
    return rows


def render(rows: list[dict[str, object]]) -> str:
    """The markdown rows, so the published numbers are generated not typed."""
    out = []
    for row in rows:
        run = f", longest run {row['run']}" if int(row["run"]) >= 100 else ""
        out.append(
            f"| `{row['ours']}` | `{row['theirs']}` | "
            f"{row['identical']} of {row['total']} ({row['percent']}%){run} |"
        )
    return "\n".join(out)


def problems(rows: list[dict[str, object]], root: Path = ROOT) -> list[str]:
    """Every disagreement between the measurement, the table and the tree."""
    stated = table_rows(root)
    measured = {str(row["ours"]): row for row in rows}
    found: list[str] = []
    for ours, row in measured.items():
        claim = stated.get(ours)
        if claim is None:
            found.append(f"{ours}: derived but absent from the table")
        elif claim["identical"] != row["identical"] or claim["total"] != row["total"]:
            found.append(
                f"{ours}: table says {claim['identical']} of {claim['total']}, "
                f"measured {row['identical']} of {row['total']}"
            )
    found.extend(
        f"{ours}: credited but not a pair this script knows about"
        for ours in stated
        if ours not in measured
    )
    if MEASURED_AGAINST:
        found.extend(
            f"{ours}: changed since MEASURED_AGAINST was written"
            for ours, digest in fingerprints(root).items()
            if MEASURED_AGAINST.get(ours) != digest
        )
    return found


def _rewrite_fingerprints(root: Path = ROOT) -> int:
    body = "\n".join(f'    "{k}": "{v}",' for k, v in sorted(fingerprints(root).items()))
    source = Path(__file__)
    text = source.read_text(encoding="utf-8")
    new, count = re.subn(
        r"MEASURED_AGAINST: dict\[str, str\] = \{.*?\}\n",
        "MEASURED_AGAINST: dict[str, str] = {\n" + body + "\n}\n",
        text,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise SystemExit("could not find MEASURED_AGAINST to rewrite")
    source.write_text(new, encoding="utf-8")
    return len(fingerprints(root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-measure the derived-line table.")
    parser.add_argument(
        "--upstream",
        type=Path,
        required=True,
        help="checkout of https://github.com/lsdefine/GenericAgent; mandatory on purpose",
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the table disagrees")
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite MEASURED_AGAINST from the tree, after updating the table",
    )
    args = parser.parse_args(argv)

    if not args.upstream.is_dir():
        raise SystemExit(f"not a directory: {args.upstream}")
    rows = measure(args.upstream)
    family = family_total(args.upstream)

    print(render(rows))
    share = round(100 * family["identical"] / family["total"])
    heirs = family["heirs"]
    where = "the one file here that descends" if heirs == 1 else (
        f"the {heirs} files here that descend"
    )
    print(
        f"\nupstream {FAMILY_UPSTREAM}: {family['identical']} of {family['total']} lines "
        f"({share}%) survive across {where} from it"
    )

    if args.write:
        print(f"\nMEASURED_AGAINST rewritten with {_rewrite_fingerprints()} entries")

    if args.check:
        found = problems(rows)
        for problem in found:
            print(f"FAIL {problem}", file=sys.stderr)
        if found:
            return 1
        print("\nthe table matches the tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
