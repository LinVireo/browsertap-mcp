from .runtime_identity import capture_source_identity as _capture_source_identity

# Seal Python sources and required JavaScript assets before other modules cache
# them, including modules imported lazily later. A package reload does not reload
# all those modules, so it must not replace the original process snapshot either.
if "_PYTHON_SOURCE_IDENTITY" not in globals():
    _PYTHON_SOURCE_IDENTITY = _capture_source_identity()

from ._version import __version__
from .paths import adopt_legacy_env as _adopt_legacy_env

# Runs before any submodule is imported, which is the only point early enough to
# matter: `server` reads BROWSERTAP_BRIDGE_HOST/PORT at import time, so a
# pre-0.4.0 AGENT_BROWSER_TMWD_* setting has to be visible by then or it is lost.
_adopt_legacy_env()

__all__ = ["__version__"]
