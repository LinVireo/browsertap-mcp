"""Compatibility imports for the pre-0.3.6 bridge module name.

New code should import :class:`BrowserBridge` from ``browser_bridge``.  This
module intentionally emits no deprecation warning because ABM may be started
over MCP stdio, where unsolicited output can corrupt the protocol stream.
"""

from .browser_bridge import (
    TOKEN_AUTH_ENV,
    TOKEN_ENV,
    TOKEN_FILE_ENV,
    BrowserBridge,
    ExtensionNotConnectedError,
    Session,
    SessionDisconnectedError,
    SessionNotConnectedError,
    TMWebDriver,
    _error_payload,
    _persist_token,
    _read_token_file,
    bridge_token,
    bridge_token_path,
    check_link_token,
    header_token,
    require_link_token,
)

__all__ = [
    "BrowserBridge",
    "ExtensionNotConnectedError",
    "Session",
    "SessionDisconnectedError",
    "SessionNotConnectedError",
    "TMWebDriver",
    "TOKEN_AUTH_ENV",
    "TOKEN_ENV",
    "TOKEN_FILE_ENV",
    "_error_payload",
    "_persist_token",
    "_read_token_file",
    "bridge_token",
    "bridge_token_path",
    "check_link_token",
    "header_token",
    "require_link_token",
]
