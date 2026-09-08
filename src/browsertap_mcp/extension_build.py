"""The stamp the extension was *loaded with*, which is a different fact from its version.

`chrome.runtime.getManifest().version` cannot answer the one question a stale
extension raises. Measured twice on this project: after a bump to 0.4.15 the
running worker still reported 0.4.14 while a reload was genuinely needed, and on
another occasion the versions agreed and every advertised capability was present
and a reload was *still* needed. So equality does not prove the worker is fresh
and inequality does not prove it is stale -- the manifest is parsed by Chrome at
load time and refreshed on its own schedule, and `background.js` does not follow
it.

A constant compiled into `background.js` has none of that ambiguity, because
there is no layer between the value and the code: whatever the worker reports for
`BTAP_BUILD` was read out of the JavaScript text it is actually running. Compare
it against this module's recomputation from the directory on disk and the answer
is decisive in both directions for the first time.

Two lines are excluded from the hash, and both are excluded because they cannot
indicate that the *code* changed:

* the stamp line itself, which is unavoidable -- a hash cannot cover the place it
  is written to.
* `manifest.json`'s version string, which `versioning bump` rewrites on every
  release. Including it would make the stamp churn whenever the release number
  moved and nothing else did, which turns the stamp into a second copy of the
  version comparison it exists to be independent of. The version question is
  still asked, separately, by the caller.

Nothing else is excluded, and that is asserted rather than described:
`tests/test_extension_build.py` fails if normalising touches any other line.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = [
    "STAMP_LENGTH",
    "STAMP_LINE_RE",
    "STAMP_PLACEHOLDER",
    "ExtensionStampError",
    "compute_extension_stamp",
    "read_extension_stamp",
    "stamp_line",
    "write_extension_stamp",
]

# 16 hex characters of sha256. The question this answers is "same code or not"
# between two builds of one project, so the full digest buys nothing and makes
# the generated line wrap.
STAMP_LENGTH = 16

STAMP_FILE = "background.js"
MANIFEST_FILE = "manifest.json"

# Anchored and exact: the writer must not be able to match a line that merely
# mentions the constant, and the reader must not accept a hand-typed value of
# some other shape -- a stamp nobody generated is worse than none, because it
# reads as a measurement.
#
# Public because there is a second reader: `scripts/check_derived_notices.py`
# has to exclude this same line from its own fingerprint, and the line's syntax
# is one fact. Two copies of the pattern would drift the moment either changed,
# and the failure would be a gate going red on every stamp regeneration -- the
# exact cry-wolf that exemption exists to prevent.
STAMP_LINE_RE = re.compile(r"^const BTAP_BUILD = '([0-9a-f]{%d})';$" % STAMP_LENGTH)
STAMP_PLACEHOLDER = "const BTAP_BUILD = '<stamp>';"

_MANIFEST_VERSION_RE = re.compile(r'^(\s*"version"\s*:\s*)"[^"]*"(.*)$')
_VERSION_PLACEHOLDER = r'\1"<version>"\2'


class ExtensionStampError(RuntimeError):
    """The extension directory holds no stamp line, or more than one."""


def stamp_line(stamp: str) -> str:
    """The exact line the generator writes, so writer and reader cannot drift."""
    return f"const BTAP_BUILD = '{stamp}';"


def _normalised(relative: str, lines: list[str]) -> list[str]:
    """Blank the two generated lines that cannot mean the code changed.

    Kept as one function so both exclusions are visible together; see the module
    docstring for why each is here and `tests/test_extension_build.py` for the
    assertion that there is no third.
    """
    if relative == STAMP_FILE:
        return [STAMP_PLACEHOLDER if STAMP_LINE_RE.match(line) else line for line in lines]
    if relative == MANIFEST_FILE:
        return [_MANIFEST_VERSION_RE.sub(_VERSION_PLACEHOLDER, line) for line in lines]
    return lines


def _entries(directory: Path) -> list[tuple[str, str]]:
    """Every file under the directory as (posix relative path, normalised text).

    Sorted by path so the digest does not depend on directory order, and the
    whole directory is walked rather than a list of names: a file added to the
    extension is code the worker loads, and a hash that had to be told about it
    would go stale exactly when it mattered. That is the enumeration defect this
    repository has already met twice -- once in the derived-notice gate and once
    in the Chrome-floor gate, both of which read `chrome_extension/*.js` and so
    could not see the directory a refactor had just created.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        entries.append((relative, "\n".join(_normalised(relative, lines))))
    return entries


def compute_extension_stamp(directory: Path) -> str:
    """Hash the extension sources as they are on disk.

    Line endings must not move the stamp, for the reason `.gitattributes` exists
    here: every tracked path is pinned to LF, but a tree that arrives another way
    is not, and a stamp that changed with the checkout's line endings would report
    a fresh worker as stale on someone else's machine.

    Two layers in `_entries` each achieve that on their own, which is worth knowing
    before simplifying either: reading in text mode turns CRLF and a lone CR into
    `\\n` on the way in, and `str.splitlines()` treats `\\r\\n` as one separator
    and drops it. Measured -- `b'a\\r\\nb'` comes back `['a', 'b']` through either
    path alone. The redundancy is not deliberate; splitting exists because
    `_normalised` works a line at a time. But it does mean no single-line change
    here can make `test_the_stamp_survives_a_crlf_checkout` go red, so do not read
    that test as cover for the text-mode read specifically.

    Each entry is length-delimited so a path and a line of content cannot be
    confused for one another -- concatenating them would let a file whose content
    happened to equal another file's name shift the boundary silently.
    """
    digest = hashlib.sha256()
    for relative, text in _entries(Path(directory)):
        digest.update(f"{relative}\0{len(text)}\0".encode("utf-8"))
        digest.update(text.encode("utf-8"))
    return digest.hexdigest()[:STAMP_LENGTH]


def read_extension_stamp(directory: Path) -> str:
    """The stamp currently written into `background.js`.

    Raises rather than returning a sentinel when the line is missing or doubled:
    the caller's next move is to compare this to a freshly computed value, and a
    None that compared unequal would be indistinguishable from a stale worker --
    which is the wrong verdict and names the wrong fix.
    """
    path = Path(directory) / STAMP_FILE
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ExtensionStampError(f"cannot read {path}: {exc}") from exc
    found = [match.group(1) for match in map(STAMP_LINE_RE.match, lines) if match]
    if len(found) != 1:
        raise ExtensionStampError(
            f"{path} must hold exactly one `{stamp_line('<16 hex>')}` line, found "
            f"{len(found)}; run `python -m scripts.extension_stamp --write`"
        )
    return found[0]


def write_extension_stamp(directory: Path) -> tuple[str, bool]:
    """Rewrite the stamp line to match the directory. Returns (stamp, changed).

    Idempotent by construction: the value it writes is computed with the stamp
    line already normalised away, so writing twice in a row is a no-op rather
    than a fixed point that has to be iterated to.
    """
    directory = Path(directory)
    path = directory / STAMP_FILE
    stamp = compute_extension_stamp(directory)
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    replaced = 0
    for index, line in enumerate(lines):
        if STAMP_LINE_RE.match(line):
            lines[index] = stamp_line(stamp)
            replaced += 1
    if replaced != 1:
        raise ExtensionStampError(
            f"{path} must hold exactly one stamp line to rewrite, found {replaced}"
        )
    updated = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    if updated == original:
        return stamp, False
    # newline="" so Python's text layer does not translate to CRLF on Windows,
    # which is how 21 tracked files in this repository came to hold it at once
    # and what every line-ending gate here exists to prevent.
    path.write_text(updated, encoding="utf-8", newline="")
    return stamp, True
