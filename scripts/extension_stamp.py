"""Write or verify the build stamp compiled into the extension's service worker.

The measurement itself lives in the package (`browsertap_mcp.extension_build`),
because the *server* has to recompute it at runtime to answer whether the worker
Chrome is running is the code on disk. This file is only the developer-facing
half: rewrite the line after editing an extension file, or ask whether someone
forgot to.

`--check` is what makes forgetting a red gate instead of a silent one, and it
needs nothing but the tree -- unlike the derived-notice measurement, which cannot
answer without an upstream clone. `tests/test_extension_build.py` runs the same
comparison, so the offline suite already covers it and no release-chain wiring is
required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from browsertap_mcp.extension_build import (  # noqa: E402
    ExtensionStampError,
    compute_extension_stamp,
    read_extension_stamp,
    write_extension_stamp,
)

EXTENSION_DIR = ROOT / "src" / "browsertap_mcp" / "chrome_extension"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail if the stamp on disk does not match the extension sources",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="rewrite the stamp line to match the extension sources",
    )
    parser.add_argument(
        "--extension-dir",
        type=Path,
        default=EXTENSION_DIR,
        help="the unpacked extension directory (default: the one in this tree)",
    )
    args = parser.parse_args(argv)

    directory = args.extension_dir
    try:
        expected = compute_extension_stamp(directory)
    except OSError as exc:
        print(f"cannot read {directory}: {exc}", file=sys.stderr)
        return 2

    if args.write:
        try:
            stamp, changed = write_extension_stamp(directory)
        except (ExtensionStampError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{'wrote' if changed else 'already current:'} build stamp {stamp}")
        if changed:
            # Saying this here is the whole reason the message exists: the worker
            # in the browser keeps the old literal until a human reloads it, so a
            # stamp written and not reloaded reports a stale worker -- correctly,
            # and confusingly if nobody said so.
            print(
                "The running extension still reports the previous stamp. Press Reload "
                "on chrome://extensions; there is no automated path (see AGENTS.md)."
            )
        return 0

    try:
        found = read_extension_stamp(directory)
    except ExtensionStampError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if found != expected:
        print(
            f"extension build stamp is stale: background.js says {found}, the sources "
            f"hash to {expected}. Run `python -m scripts.extension_stamp --write`, then "
            "reload the unpacked extension.",
            file=sys.stderr,
        )
        return 1
    print(f"build stamp {found} matches the extension sources")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
