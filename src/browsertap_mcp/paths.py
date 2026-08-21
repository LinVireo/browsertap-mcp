"""The one place that decides where BTAP keeps its per-user state.

Everything BTAP writes outside the package lives in this single directory:
``bridge.pid``, ``bridge.log``, the persistent bridge token, and the spawn and
physical-input lock files.

The directory literal used to be spelled out at five call sites and only
:func:`bridge.bridge_state_dir` consulted ``BROWSERTAP_STATE_DIR``, so
pointing that variable at a scratch directory relocated the pid file and left
the other four behind in the real home directory -- a state dir that was only
one-fifth redirected. Build the path through :func:`state_dir` instead of
repeating the literal, and a rename or a redirect stays a one-line change.

0.4.0 renamed the project from ``agent-browser-mcp`` to ``browsertap-mcp``, which
moved both the directory and every environment variable. Both old spellings are
still honoured here -- see :func:`state_dir` and :func:`adopt_legacy_env` -- so an
install that predates the rename keeps its bridge token instead of silently
handing out a new one to half the processes.

This module deliberately imports nothing from the package: ``browser_bridge``
sits below ``bridge`` in the import graph, so a shared helper can only live in a
leaf.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Overrides the state directory wholesale. Absolute path; ``~`` is expanded.
STATE_DIR_ENV = "BROWSERTAP_STATE_DIR"

#: Directory name used under the home directory when nothing overrides it.
DEFAULT_STATE_DIR_NAME = ".browsertap"

#: Pre-0.4.0 directory name, still used when it is the only one that exists.
LEGACY_STATE_DIR_NAME = ".agent-browser-mcp"

#: Environment prefix for every variable this package reads.
ENV_PREFIX = "BROWSERTAP_"

#: Pre-0.4.0 environment prefix, accepted as an alias.
LEGACY_ENV_PREFIX = "AGENT_BROWSER_"

#: Aliases that are not a straight prefix swap. The bridge host and port carried
#: ``TMWD`` from the driver class that 0.4.0 deleted, so their new names drop it.
LEGACY_ENV_NAMES = {
    "BROWSERTAP_BRIDGE_HOST": "AGENT_BROWSER_TMWD_HOST",
    "BROWSERTAP_BRIDGE_PORT": "AGENT_BROWSER_TMWD_PORT",
}


def state_dir(*, create: bool = False) -> Path:
    """Return the per-user state directory.

    ``create`` is opt-in so that merely asking where a file *would* go never has
    a side effect. The two lock files are created through an atomic
    ``O_EXCL`` open whose caller already builds the parent, so they ask for the
    path only.

    An install that predates the 0.4.0 rename keeps writing to
    ``~/.agent-browser-mcp`` as long as that directory exists and the new one
    does not. The old directory is *used*, never moved: a running bridge holds
    ``bridge.log`` open, and Windows refuses to rename a directory containing an
    open handle -- so a migration-by-rename would fail exactly when the daemon
    the token belongs to is alive.
    """
    configured = (os.environ.get(STATE_DIR_ENV) or "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        home = Path.home()
        path = home / DEFAULT_STATE_DIR_NAME
        legacy = home / LEGACY_STATE_DIR_NAME
        if not path.exists() and legacy.is_dir():
            path = legacy
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def adopt_legacy_env(environ: dict[str, str] | None = None) -> list[str]:
    """Fill in ``BROWSERTAP_*`` variables from their pre-0.4.0 spellings.

    Called once from the package ``__init__``, because ``server`` reads its
    bridge host and port at *import* time: an entry-point-level call would run
    after the value it is meant to supply had already been read.

    Only unset names are filled in, so a caller that sets both spellings gets the
    new one. Returns the new names that were populated, which is what makes the
    behaviour testable without mutating the real environment.
    """
    env = os.environ if environ is None else environ
    adopted: list[str] = []
    renamed_legacy = set(LEGACY_ENV_NAMES.values())

    for new, old in LEGACY_ENV_NAMES.items():
        if not env.get(new) and env.get(old):
            env[new] = env[old]
            adopted.append(new)

    for old in [key for key in env if key.startswith(LEGACY_ENV_PREFIX)]:
        if old in renamed_legacy:
            continue
        new = ENV_PREFIX + old[len(LEGACY_ENV_PREFIX):]
        if not env.get(new) and env.get(old):
            env[new] = env[old]
            adopted.append(new)

    return adopted
