"""What the extension's build stamp measures, and what it must stay blind to.

The stamp exists because `chrome.runtime.getManifest().version` cannot answer
whether the worker in the browser is running the code on disk -- measured twice
here in both directions (a matching version with a reload still needed, and a
stale version reported by a worker whose code was current). A constant compiled
into `background.js` has no such layer.

That makes this file the offline half of a gate whose live half needs a browser:
`test_the_recorded_stamp_matches_the_extension_sources` is what turns "edited an
extension file and forgot to regenerate" into a red suite instead of a stamp that
silently describes a build nobody has. The rest pins the two exclusions, because
a hash blind to a line that could indicate changed code would report a stale
worker as fresh -- the failure the stamp was added to end.
"""

from __future__ import annotations

import pytest

from browsertap_mcp.extension_build import (
    MANIFEST_FILE,
    STAMP_FILE,
    STAMP_LENGTH,
    STAMP_LINE_RE,
    STAMP_PLACEHOLDER,
    ExtensionStampError,
    _entries,
    _normalised,
    compute_extension_stamp,
    read_extension_stamp,
    stamp_line,
    write_extension_stamp,
)
from browsertap_mcp.server import chrome_extension_dir


def _tree(root, files):
    """Write `{relative path: text}` under `root` and return it.

    Creates `root` itself, so an empty mapping means an empty directory rather
    than a missing one -- two cases this file has to tell apart.
    """
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    return root


def test_the_recorded_stamp_matches_the_extension_sources():
    """The gate. An extension edit without a regenerated stamp fails here.

    Without this, the stamp goes stale the first time someone edits `content.js`,
    and a stale stamp is worse than none: the server compares the worker's value
    against a fresh hash of the tree, so a stamp nobody regenerated makes a
    *current* worker report as stale and names the wrong fix.
    """
    directory = chrome_extension_dir()
    assert read_extension_stamp(directory) == compute_extension_stamp(directory), (
        "an extension file changed without regenerating the build stamp. Run "
        "`python -m scripts.extension_stamp --write`, then reload the unpacked "
        "extension on chrome://extensions."
    )


def test_normalising_touches_only_the_two_generated_lines():
    """Two lines are excluded. A third would make the stamp unable to see code.

    The exclusions are the one way this measurement could be hollowed out, so
    their reach is asserted over the real directory rather than described in a
    comment: every other file must come back byte for byte.
    """
    directory = chrome_extension_dir()
    expected = {
        STAMP_FILE: ("const BTAP_BUILD = '", STAMP_PLACEHOLDER),
        MANIFEST_FILE: ('"version"', '"<version>"'),
    }
    seen_files = set()
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        normalised = _normalised(relative, raw)
        assert len(normalised) == len(raw), f"normalising {relative} changed its line count"
        differing = [(a, b) for a, b in zip(raw, normalised, strict=True) if a != b]
        if relative not in expected:
            assert not differing, (
                f"normalising {relative} rewrote {len(differing)} line(s), and it holds "
                "neither generated line. A stamp blind to a line that could indicate "
                "changed code reports a stale worker as fresh."
            )
            continue
        seen_files.add(relative)
        marker, placeholder = expected[relative]
        assert len(differing) == 1, (
            f"normalising {relative} rewrote {len(differing)} lines, expected exactly "
            f"the one generated line: {differing}"
        )
        before, after = differing[0]
        assert marker in before, f"the excluded line is not the generated one: {before!r}"
        assert placeholder in after, f"it was not replaced by its placeholder: {after!r}"
    # Both exclusions have to actually apply to a file that is there; an exemption
    # for a file that has been renamed away is dead weight reading as a live one.
    assert seen_files == set(expected), f"no line was excluded from {set(expected) - seen_files}"


def test_a_version_bump_alone_does_not_move_the_stamp(tmp_path):
    """Excluded, and for a reason that has to hold in both directions.

    `versioning bump` rewrites the manifest's version on every release. If that
    moved the stamp, every release would report the whole extension as a new
    build and ask for a reload that changes nothing -- and the stamp would become
    a second copy of the version comparison it exists to be independent of.
    """
    before = _tree(
        tmp_path / "before",
        {
            STAMP_FILE: stamp_line("0" * STAMP_LENGTH) + "\n",
            MANIFEST_FILE: '{\n  "version": "0.4.15",\n  "minimum_chrome_version": "121"\n}\n',
        },
    )
    bumped = _tree(
        tmp_path / "bumped",
        {
            STAMP_FILE: stamp_line("0" * STAMP_LENGTH) + "\n",
            MANIFEST_FILE: '{\n  "version": "0.4.16",\n  "minimum_chrome_version": "121"\n}\n',
        },
    )
    edited = _tree(
        tmp_path / "edited",
        {
            STAMP_FILE: stamp_line("0" * STAMP_LENGTH) + "\n",
            MANIFEST_FILE: '{\n  "version": "0.4.15",\n  "minimum_chrome_version": "111"\n}\n',
        },
    )
    assert compute_extension_stamp(bumped) == compute_extension_stamp(before), (
        "a version bump moved the stamp, so every release reports a fresh build"
    )
    assert compute_extension_stamp(edited) != compute_extension_stamp(before), (
        "a real manifest edit left the stamp alone, so the manifest exclusion is "
        "wider than the version line and the stamp cannot see a changed floor"
    )


def test_the_stamp_line_itself_cannot_move_the_stamp(tmp_path):
    """A hash cannot cover the place it is written to, so writing is a no-op.

    This is what makes `write_extension_stamp` idempotent rather than a fixed
    point that has to be iterated to, and it is why `--write` twice in a row says
    `already current` the second time.
    """
    directory = _tree(
        tmp_path / "ext",
        {STAMP_FILE: "// code\n" + stamp_line("0" * STAMP_LENGTH) + "\n"},
    )
    first, changed = write_extension_stamp(directory)
    assert changed, "the placeholder stamp should have been rewritten"
    again, changed_again = write_extension_stamp(directory)
    assert (again, changed_again) == (first, False), (
        f"writing twice was not idempotent: {first} then {again}"
    )
    assert read_extension_stamp(directory) == compute_extension_stamp(directory)


def test_the_stamp_survives_a_crlf_checkout(tmp_path):
    """Line endings must not move it, or the same code reads as two builds.

    Every tracked path is pinned to LF by `.gitattributes`, but a tree that
    arrives another way is not -- an export without that file, an editor set to
    CRLF, or a script that round-trips through Python's text mode on Windows,
    which is how 21 tracked files here came to hold CRLF at once. Hashing bytes
    would then report a fresh worker as stale on someone else's machine, with no
    edit anywhere.
    """
    body = "// first\n// second\n" + stamp_line("0" * STAMP_LENGTH) + "\n"
    lf = _tree(tmp_path / "lf", {STAMP_FILE: body, "sub/helper.js": "const a = 1;\n"})
    crlf = _tree(
        tmp_path / "crlf",
        {
            STAMP_FILE: body.replace("\n", "\r\n"),
            "sub/helper.js": "const a = 1;\r\n",
        },
    )
    assert (crlf / STAMP_FILE).read_bytes().count(b"\r\n"), "the CRLF fixture is not CRLF"
    assert compute_extension_stamp(crlf) == compute_extension_stamp(lf)


def test_writing_the_stamp_does_not_introduce_crlf(tmp_path):
    """The generator must not do to `background.js` what it guards against.

    `check_derived_notices.py --write` did exactly this to its own source, and
    `ruff format` with `line-ending = auto` did it to 21 files, so a writer that
    goes through Python's text mode on Windows is the measured failure and not a
    hypothetical one.
    """
    directory = _tree(
        tmp_path / "ext",
        {STAMP_FILE: "// code\n" + stamp_line("0" * STAMP_LENGTH) + "\n"},
    )
    write_extension_stamp(directory)
    assert b"\r\n" not in (directory / STAMP_FILE).read_bytes()


def test_a_new_file_anywhere_under_the_directory_moves_the_stamp(tmp_path):
    """The walk is the whole directory, not a list of names it has to be told.

    Two gates in this repository read `chrome_extension/*.js` and went blind the
    moment a refactor created a sibling directory. A stamp with that defect would
    report a worker as running the tree's code while the worker had loaded a file
    the hash never saw.
    """
    directory = _tree(tmp_path / "ext", {STAMP_FILE: stamp_line("0" * STAMP_LENGTH) + "\n"})
    baseline = compute_extension_stamp(directory)

    (directory / "added.js").write_text("const b = 2;\n", encoding="utf-8", newline="")
    with_sibling = compute_extension_stamp(directory)
    assert with_sibling != baseline, "a new file at the top level did not move the stamp"

    nested = directory / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "more.js").write_text("const c = 3;\n", encoding="utf-8", newline="")
    assert compute_extension_stamp(directory) != with_sibling, (
        "a file in a subdirectory did not move the stamp, so the walk is not recursive"
    )


def test_a_path_and_a_line_of_content_cannot_be_confused(tmp_path):
    """The length delimiter is load-bearing, not decoration.

    Concatenating paths and contents lets a file whose content happens to equal
    another file's name shift the boundary silently: two different directories
    hash the same, and the stamp says "same code" about code that differs.
    """
    one = _tree(tmp_path / "one", {"ab": "c"})
    two = _tree(tmp_path / "two", {"a": "bc"})
    assert compute_extension_stamp(one) != compute_extension_stamp(two), (
        "two directories whose naive concatenation is identical hashed the same"
    )


def test_directory_order_does_not_change_the_stamp(tmp_path):
    """Sorted, so two clones of one tree agree.

    `rglob` order follows the filesystem, so without the sort the same files
    written in a different order would hash differently and the server would
    report a stale worker on a fresh checkout.
    """
    forward = _tree(tmp_path / "a", {})
    for name, text in (("one.js", "1\n"), ("two.js", "2\n"), ("three.js", "3\n")):
        (forward / name).write_text(text, encoding="utf-8", newline="")
    backward = _tree(tmp_path / "b", {})
    for name, text in (("three.js", "3\n"), ("two.js", "2\n"), ("one.js", "1\n")):
        (backward / name).write_text(text, encoding="utf-8", newline="")
    assert compute_extension_stamp(forward) == compute_extension_stamp(backward)
    assert [name for name, _ in _entries(forward)] == ["one.js", "three.js", "two.js"]


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("missing", "// no stamp here\n"),
        ("doubled", stamp_line("0" * STAMP_LENGTH) + "\n" + stamp_line("1" * STAMP_LENGTH) + "\n"),
        ("hand-typed", "const BTAP_BUILD = 'not-a-hash';\n"),
        ("commented out", "// " + stamp_line("0" * STAMP_LENGTH) + "\n"),
    ],
)
def test_reading_an_unusable_stamp_raises_instead_of_returning_a_sentinel(tmp_path, label, body):
    """A `None` here would be indistinguishable from a stale worker.

    The caller's next move is to compare this value against a fresh hash of the
    tree. A sentinel that compared unequal would produce the `stale_worker`
    verdict, which names a browser reload as the fix for a problem a reload
    cannot touch -- so every unusable shape has to be an error, including the two
    that look like a stamp and are not: a hand-typed value nobody generated, and
    one left commented out.
    """
    directory = _tree(tmp_path / label.replace(" ", "-"), {STAMP_FILE: body})
    with pytest.raises(ExtensionStampError):
        read_extension_stamp(directory)
    # Rewriting is refused for the same reason rather than guessing which line
    # was meant -- except where there is exactly nothing to guess between.
    if label != "missing":
        with pytest.raises(ExtensionStampError):
            write_extension_stamp(directory)


def test_the_stamp_is_sixteen_hex_characters_and_the_reader_agrees(tmp_path):
    """Writer and reader share one pattern, so they cannot drift apart.

    `scripts/check_derived_notices.py` is a third reader of the same line, which
    is why the pattern is public: two copies would drift and the failure would be
    that gate going red on every stamp regeneration.
    """
    directory = _tree(tmp_path / "ext", {STAMP_FILE: stamp_line("0" * STAMP_LENGTH) + "\n"})
    stamp, _ = write_extension_stamp(directory)
    assert len(stamp) == STAMP_LENGTH and set(stamp) <= set("0123456789abcdef")
    written = (directory / STAMP_FILE).read_text(encoding="utf-8").splitlines()
    assert [line for line in written if STAMP_LINE_RE.match(line)] == [stamp_line(stamp)]


def test_a_missing_directory_is_an_error_the_caller_can_name(tmp_path):
    """`get_setup_status` catches `OSError` here and reports it, so it must raise.

    A silently empty walk would hash to a constant and let a worker match a tree
    that is not there.
    """
    absent = tmp_path / "not-installed"
    assert compute_extension_stamp(absent) == compute_extension_stamp(_tree(tmp_path / "empty", {}))
    with pytest.raises(ExtensionStampError):
        read_extension_stamp(absent)
