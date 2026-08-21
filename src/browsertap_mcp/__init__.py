from ._version import __version__
from .paths import adopt_legacy_env as _adopt_legacy_env

# Runs before any submodule is imported, which is the only point early enough to
# matter: `server` reads BROWSERTAP_BRIDGE_HOST/PORT at import time, so a
# pre-0.4.0 AGENT_BROWSER_TMWD_* setting has to be visible by then or it is lost.
_adopt_legacy_env()

__all__ = ["__version__"]
