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

sys.path.insert(0, str(ROOT / "src"))

from browsertap_mcp.extension_build import (  # noqa: E402
    STAMP_LINE_RE,
    STAMP_PLACEHOLDER,
)

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
    "src/browsertap_mcp/browser_bridge.py": "9e3427f2afcbdfa121a20d501f5cb5fb01e4955636bfa1cd9ff0bea6aa3722cd",
    "src/browsertap_mcp/chrome_extension/background.js": "28df5f788a47f004f9a823a3a3973ffe4e66aaa96474e77d89eda17760677d7b",
    "src/browsertap_mcp/chrome_extension/content.js": "942d5df35bedba224c13db6930f2d07bccf554f7713c28885fd5f5b51f8a644d",
    "src/browsertap_mcp/chrome_extension/disable_dialogs.js": "1edc19d8a6a5bf0850cc0e8f2123fe799d80fb5b7afa578fa014586d7181970a",
    "src/browsertap_mcp/chrome_extension/manifest.json": "a7394272189f6b0acd168dfbbc0196a624df8395c802df3e1cde8611df3d8758",
    "src/browsertap_mcp/chrome_extension/popup.html": "5fd4ea0d2351bd244d95c1ef9fc592864e4b839f8d7a70bc914ddf7bb71c95af",
    "src/browsertap_mcp/chrome_extension/popup.js": "82e0aca6b52141c31af9e7a0c09ed123cae8230e1743046384b4bd602849d5dd",
    "src/browsertap_mcp/simphtml.py": "a7c6bafb2b06e2e6107ec2cdb185f665a7eb9347c9faa9cc295d6ca98d4ac24d",
}

_ROW_RE = re.compile(
    r"^\|\s*`(?P<ours>src/[^`]+)`\s*\|\s*`(?P<theirs>[^`]+)`\s*\|\s*"
    r"(?P<identical>\d+) of (?P<total>\d+) \((?P<percent>\d+)%\)"
)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


MANIFEST = f"{EXT}/manifest.json"
BACKGROUND = f"{EXT}/background.js"

_MANIFEST_VERSION_RE = re.compile(r'^(\s*"version"\s*:\s*)"[^"]*"(.*)$')

# Every line this fingerprint is deliberately blind to, and the only ones. An
# entry has to satisfy two things at once: the line is *generated* by a script
# in this repository, and it cannot change how many of upstream's lines survive
# -- which both of these settle the same way, by not existing upstream at all.
#
# The values are `re.sub` templates, so the manifest's can keep the rest of the
# line via backreferences while the stamp's replaces the whole of it.
# `tests/test_documentation_contract.py` asserts this table is exactly these two
# entries and that each rewrites exactly one line of its own file, because
# widening it is the one way this gate could be made vacuous again.
GENERATED_LINES: dict[str, tuple[re.Pattern[str], str]] = {
    MANIFEST: (_MANIFEST_VERSION_RE, r'\1"<version>"\2'),
    BACKGROUND: (STAMP_LINE_RE, STAMP_PLACEHOLDER),
}


def _normalised(relative: str, lines: list[str]) -> list[str]:
    """Blank the two generated lines that provably cannot move a measured count.

    `versioning bump` rewrites `manifest.json`'s own version string on every
    release, so a byte-faithful fingerprint went red on every release and asked
    for a re-measure that could not possibly return a different number. Upstream
    declares `"version": "2.0"` and every value this package can hold is a
    `MAJOR.MINOR.PATCH` triple, so the line is not identical to upstream's before
    the bump and not identical after it: `identical` is invariant. Measured at
    0.4.14 and again at 0.4.15 -- `29 of 40 (72%)` both times.

    `background.js`'s `BTAP_BUILD` stamp is the same argument one step further.
    It is rewritten whenever *any* extension file changes, so without the
    exclusion an edit to `content.js` would demand a re-measure of `background.js`
    as well -- a file nobody touched -- and upstream has no such line for it to be
    identical to.

    Nothing else is excluded, and that restraint is the point. A fingerprint
    blind to a line that *could* move the count is exactly the self-consistent
    vacuous pass this file was written to end, so
    `tests/test_documentation_contract.py` pins the blindness to these two lines
    rather than trusting the comment.
    """
    generated = GENERATED_LINES.get(relative)
    if generated is None:
        return lines
    pattern, placeholder = generated
    return [pattern.sub(placeholder, line) for line in lines]


def file_digest(path: Path, relative: str = "") -> str:
    """Hash the file's *lines*, so a checkout's line endings do not change it.

    `.gitattributes` pins every tracked path to LF, so a clone is LF
    -- but a tree that arrives another way is not. An export without the
    attributes file, an editor configured for CRLF, or a script that round-trips
    a file through Python's text mode on Windows all produce CRLF, and the last
    one happened to 21 tracked files in this repository. Hashing bytes would
    report all ten derived files as edited on such a tree and say the notice
    table needs re-measuring, which is a false alarm with a real cost: the
    re-measure needs an upstream checkout that is not in the tree.
    """
    return hashlib.sha256(
        "\n".join(_normalised(relative, _lines(path))).encode("utf-8")
    ).hexdigest()


def fingerprints(root: Path = ROOT) -> dict[str, str]:
    """Current hash of every derived file, keyed by repo-relative path."""
    return {ours: file_digest(root / ours, ours) for ours, _ in DERIVED_PAIRS}


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
    # newline="\n" is not cosmetic. Python's text mode translates on Windows, so
    # this rewrite used to hand its own source back with CRLF, and
    # `evidence_manifest._sha256` hashes raw working-tree bytes -- meaning the one
    # command a maintainer runs to make the notice table honest silently made the
    # release fingerprint unreproducible from a clone of the commit it describes.
    source.write_text(new, encoding="utf-8", newline="\n")
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
