"""Pin browser Origins without changing an existing unpacked extension's ID.

Chromium derives an ID from a manifest public key, or otherwise from the native
absolute installation path. See components/crx_file/id_util.cc in Chromium.
An Origin is a browser boundary, not authentication for arbitrary local clients.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path


def _extension_id(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()[:32]
    return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def unpacked_extension_id(path: str, *, windows: bool | None = None) -> str:
    """Hash the native path spelling, with Chromium's Windows drive casing."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        if len(path) >= 2 and "a" <= path[0] <= "z" and path[1] == ":":
            path = path[0].upper() + path[1:]
        encoded = path.encode("utf-16-le", errors="surrogatepass")
    else:
        encoded = os.fsencode(path)
    return _extension_id(encoded)


def _directory_identity(directory: Path) -> tuple[str, str]:
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("extension manifest must be an object")
    if "key" in manifest:
        key = manifest["key"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("extension manifest key must be nonempty base64")
        public_key = _manifest_key_bytes(key)
        if not public_key:
            raise ValueError("extension manifest key must be nonempty base64")
        identifier, source = _extension_id(public_key), "manifest_key"
    else:
        identifier, source = unpacked_extension_id(str(directory)), "unpacked_path"
    return f"chrome-extension://{identifier}", source


_PEM_KEY = re.compile(
    r"\A-----BEGIN PUBLIC KEY-----\r?\n"
    r"(?P<body>(?:[^\r\n]*\r?\n)+)"
    r"-----END PUBLIC KEY-----\Z"
)
_BASE64 = re.compile(r"\A[A-Za-z0-9+/]*={0,2}\Z")


def _manifest_key_bytes(key: str) -> bytes:
    """Parse the two key spellings accepted by Chromium's manifest loader."""
    if len(key.encode("utf-8", errors="surrogatepass")) > 100 * 1024:
        raise ValueError("extension manifest key exceeds Chromium's input limit")

    pem = _PEM_KEY.fullmatch(key)
    if pem is not None:
        lines = pem.group("body").splitlines()
        if not lines:
            raise ValueError("extension manifest PEM key is empty")
        normalized: list[str] = []
        for line in lines:
            # PEM permits indentation around complete base64 lines, but not
            # spaces inside a base64 quartet.
            line = line.strip(" \t")
            if not line or _BASE64.fullmatch(line) is None:
                raise ValueError("extension manifest PEM key is not valid base64")
            normalized.append(line)
        encoded = "".join(normalized)
    else:
        # Raw manifest keys are strict: Chromium does not accept whitespace or
        # PEM markers in this form.
        if _BASE64.fullmatch(key) is None:
            raise ValueError("extension manifest key is not valid base64")
        encoded = key

    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("extension manifest key is not valid base64") from exc


@lru_cache(maxsize=1)
def _packaged_identity() -> tuple[str | None, str]:
    # Cache the process's package identity; changing a key/path requires a fresh
    # bridge just like other authentication configuration. Never learn trust from
    # ext_ready, a clientId, or the first socket to connect.
    try:
        return _directory_identity(Path(__file__).resolve().parent / "chrome_extension")
    except (OSError, ValueError, UnicodeError):
        return None, "unavailable"


def default_extension_origin() -> str | None:
    return _packaged_identity()[0]


def _extra_origins() -> set[str]:
    return {origin.strip() for origin in
            os.environ.get("BROWSERTAP_WS_ALLOWED_ORIGINS", "").split(",") if origin.strip()}


def origin_is_allowed(origin: str) -> bool:
    return bool(origin) and (origin == default_extension_origin() or origin in _extra_origins())


def origin_policy_report() -> dict:
    origin, source = _packaged_identity()
    return {
        "default_origin": origin,
        "identity_source": source,
        "additional_origins_count": len(_extra_origins()),
        "allow_no_origin": os.environ.get("BROWSERTAP_WS_ALLOW_NO_ORIGIN", "") == "1",
    }
