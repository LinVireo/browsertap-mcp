"""Accidental-destruction guard for the two public raw-CDP tools.

This is deliberately not a sandbox: Runtime.evaluate and other allowed methods
can still change page state. Internal, scoped tools retain their own policies.
"""

from __future__ import annotations

import re
from typing import Any

_METHOD = re.compile(r"[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*\Z")
_BROWSER_READS = frozenset({
    "Browser.getVersion", "Browser.getBrowserCommandLine", "Browser.getHistogram",
    "Browser.getHistograms", "Browser.getWindowBounds", "Browser.getWindowForTarget",
})
_BLOCKED_METHODS = frozenset({
    "Network.clearBrowserCookies", "Network.clearBrowserCache",
    "Network.setUserAgentOverride", "Emulation.setUserAgentOverride",
    "Page.setDownloadBehavior",
    "Page.close", "Target.closeTarget", "Target.disposeBrowserContext",
    # This opaque inner protocol message would hide a blocked method from the
    # check on the outer command. Direct target addressing remains available.
    "Target.sendMessageToTarget",
})


class RawCdpPolicyError(PermissionError):
    """A raw command was refused before any browser lookup or dispatch."""

    error_code = "raw_cdp_blocked"
    delivery_state = "undelivered"
    # Repeating the same request cannot change a deterministic policy refusal.
    retry_safe = False


def validate_raw_cdp_method(method: Any, *, allow_unsafe: bool = False) -> None:
    if not isinstance(method, str) or _METHOD.fullmatch(method) is None:
        raise ValueError("method must be a CDP Domain.method name")
    blocked = (
        (method.startswith("Browser.") and method not in _BROWSER_READS)
        or method.startswith("Storage.clear")
        or method in _BLOCKED_METHODS
    )
    if blocked and not allow_unsafe:
        raise RawCdpPolicyError(
            f"raw_cdp_blocked: {method} can bypass scoped state or tab-ownership checks. "
            "Use the dedicated BTAP tool where available. For intentional raw access, "
            "the operator must set BROWSERTAP_ALLOW_UNSAFE_CDP=1 and use lab mode."
        )


def validate_raw_cdp_batch(payload: dict[str, Any], *, allow_unsafe: bool = False) -> None:
    """Preflight every member, so a refused tail cannot leave a half-run batch."""
    commands = payload.get("commands")
    if not isinstance(commands, list):
        raise ValueError("batch commands must be a JSON array")
    for command in commands:
        if not isinstance(command, dict):
            raise ValueError("each batch command must be a JSON object")
        kind = command.get("cmd")
        if kind == "cdp":
            validate_raw_cdp_method(command.get("method"), allow_unsafe=allow_unsafe)
            if not isinstance(command.get("params", {}), dict):
                raise ValueError("CDP batch params must be a JSON object")
        elif kind not in ("tabs", "cookies"):
            raise ValueError("batch commands support only cdp, tabs, and cookies")
