"""The one place that decides where ABM keeps its per-user state.

Everything ABM writes outside the package lives in this single directory:
``bridge.pid``, ``bridge.log``, the persistent bridge token, and the spawn and
physical-input lock files.

The directory literal used to be spelled out at five call sites and only
:func:`bridge.bridge_state_dir` consulted ``AGENT_BROWSER_STATE_DIR``, so
pointing that variable at a scratch directory relocated the pid file and left
the other four behind in the real home directory -- a state dir that was only
one-fifth redirected. Build the path through :func:`state_dir` instead of
repeating the literal, and a rename or a redirect stays a one-line change.

This module deliberately imports nothing from the package: ``browser_bridge``
sits below ``bridge`` in the import graph, so a shared helper can only live in a
leaf.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Overrides the state directory wholesale. Absolute path; ``~`` is expanded.
STATE_DIR_ENV = "AGENT_BROWSER_STATE_DIR"

#: Directory name used under the home directory when nothing overrides it.
DEFAULT_STATE_DIR_NAME = ".agent-browser-mcp"


def state_dir(*, create: bool = False) -> Path:
    """Return the per-user state directory.

    ``create`` is opt-in so that merely asking where a file *would* go never has
    a side effect. The two lock files are created through an atomic
    ``O_EXCL`` open whose caller already builds the parent, so they ask for the
    path only.
    """
    configured = (os.environ.get(STATE_DIR_ENV) or "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / DEFAULT_STATE_DIR_NAME
    )
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
