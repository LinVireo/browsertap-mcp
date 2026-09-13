"""Per-user Chrome Native Messaging registration, with owned-file cleanup.

The installer never opens a browser, starts a host, or reads a bridge token.
Only explicit, nonsecret bridge configuration is saved for Chrome's environment.
"""
from __future__ import annotations

import hashlib
import importlib
import ipaddress
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .extension_origin import origin_policy_report
from .native_host import MAX_CONFIG_BYTES
from .paths import bridge_child_environment, state_dir, validate_bridge_port

HOST_NAME = "io.browsertap.host"
CONFIG_ENVIRONMENT_KEYS = frozenset({
    "BROWSERTAP_BRIDGE_HOST", "BROWSERTAP_BRIDGE_PORT", "BROWSERTAP_STATE_DIR",
    "BROWSERTAP_BRIDGE_TOKEN_FILE", "BROWSERTAP_TRANSPORT",
})
_PATH_ENVIRONMENT_KEYS = frozenset({"BROWSERTAP_STATE_DIR", "BROWSERTAP_BRIDGE_TOKEN_FILE"})
_REGISTRY_KEY = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
_DESCRIPTION = "BrowserTap Native Messaging Host"
_EXTENSION_ID = re.compile(r"[a-p]{32}\Z")
_FILE_LIMIT = 128 * 1024


class NativeInstallError(RuntimeError):
    """A safe, actionable failure that does not include untrusted file contents."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Layout:
    platform: str
    directory: Path
    registration: Path | None

    @property
    def manifest(self) -> Path:
        return self.directory / f"{HOST_NAME}.json"

    @property
    def launcher(self) -> Path:
        return self.directory / ("launch.cmd" if self.platform == "win32" else "launch.sh")

    @property
    def config(self) -> Path:
        return self.directory / "config.json"

    @property
    def receipt(self) -> Path:
        return self.directory / "install.json"

    @property
    def registration_name(self) -> str:
        return str(self.registration) if self.registration else "HKCU\\" + _REGISTRY_KEY

    @property
    def owned_files(self) -> tuple[Path, ...]:
        return (self.config, self.launcher, self.manifest, self.receipt)


def _layout() -> _Layout:
    platform = sys.platform
    directory = state_dir(create=False).resolve() / "native-messaging"
    if platform == "win32":
        registration = None
    elif platform == "darwin":
        registration = (Path.home() / "Library" / "Application Support" / "Google" /
                        "Chrome" / "NativeMessagingHosts" / f"{HOST_NAME}.json")
    elif platform.startswith("linux"):
        platform = "linux"
        configured = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(configured).expanduser() if configured else Path.home() / ".config"
        if not base.is_absolute():
            raise NativeInstallError("invalid_config", "XDG_CONFIG_HOME must be an absolute path")
        registration = base / "google-chrome" / "NativeMessagingHosts" / f"{HOST_NAME}.json"
    else:
        raise NativeInstallError("unsupported_platform", "Native host installation is unsupported")
    return _Layout(platform, directory, registration)


def _extension_identifier(override: str | None) -> str:
    if override is None:
        policy = origin_policy_report()
        origin = policy.get("default_origin")
        if policy.get("identity_source") != "manifest_key" or not isinstance(origin, str):
            raise NativeInstallError(
                "extension_identity_unavailable", "The packaged extension needs a valid pinned key",
            )
        override = origin.removeprefix("chrome-extension://")
    if not isinstance(override, str) or not _EXTENSION_ID.fullmatch(override):
        raise NativeInstallError("invalid_extension_id", "Extension ID must contain 32 letters a-p")
    return override


def validate_config_environment(environment: object) -> dict[str, str]:
    """Validate the nonsecret native launcher configuration without effects."""
    if not isinstance(environment, dict) or any(
        key not in CONFIG_ENVIRONMENT_KEYS or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise NativeInstallError("invalid_config", "Native configuration contains unsupported values")
    result: dict[str, str] = dict(environment)
    for name, value in result.items():
        if not value or any(char in value for char in ("\0", "\r", "\n")):
            raise NativeInstallError("invalid_config", "Native configuration has an invalid value")
        if name in _PATH_ENVIRONMENT_KEYS and not Path(value).is_absolute():
            raise NativeInstallError("invalid_config", "Native configuration paths must be absolute")
    host = result.get("BROWSERTAP_BRIDGE_HOST", "127.0.0.1")
    if host.lower() != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise NativeInstallError("invalid_config", "Native bridge host must be loopback")
    try:
        validate_bridge_port(result.get("BROWSERTAP_BRIDGE_PORT", "18765"))
    except ValueError:
        raise NativeInstallError("invalid_config", "Native bridge port must be between 1 and 65533") from None
    if result.get("BROWSERTAP_TRANSPORT", "native") not in {"native", "websocket"}:
        raise NativeInstallError("invalid_config", "Native transport must be native or websocket")
    return result


def _configuration(extension_id: str) -> dict[str, Any]:
    environment = bridge_child_environment()
    selected = {key: environment[key] for key in CONFIG_ENVIRONMENT_KEYS if key in environment}
    # Blank path overrides have the same default meaning as paths.state_dir.
    for key in _PATH_ENVIRONMENT_KEYS:
        if key in selected and not selected[key].strip():
            del selected[key]
    return {"schema": 1, "environment": validate_config_environment(selected),
            "extension_id": extension_id}


def _python_executable(platform: str) -> Path:
    if not sys.executable:
        raise NativeInstallError("python_unavailable", "The current Python executable is unavailable")
    # Do not resolve POSIX venv symlinks: invoking the base interpreter loses the venv.
    executable = Path(os.path.abspath(sys.executable))
    if platform == "win32" and executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    if not executable.is_file() or (platform != "win32" and not os.access(executable, os.X_OK)):
        raise NativeInstallError("python_unavailable", "A console Python executable is required")
    return executable


def _launcher_bytes(layout: _Layout, executable: Path) -> bytes:
    args = (str(executable), "-m", "browsertap_mcp.native_host", "--config", str(layout.config))
    if any(any(char in arg for char in ("\0", "\r", "\n")) for arg in args):
        raise NativeInstallError("unsafe_path", "Native launcher paths contain control characters")
    if layout.platform == "win32":
        if any('"' in arg for arg in args):
            raise NativeInstallError("unsafe_path", "Native launcher paths contain a quote")
        quoted = [f'"{arg.replace("%", "%%")}"' for arg in args]
        # The ASCII prologue selects UTF-8 before cmd reads Unicode paths. Disable
        # delayed expansion so literal ! characters in installed paths survive.
        return ("@echo off\r\nsetlocal DisableDelayedExpansion\r\nchcp 65001 >nul\r\n" +
                " ".join(quoted) + " %*\r\n").encode("utf-8")
    return ("#!/bin/sh\nexec " + " ".join(shlex.quote(arg) for arg in args) + ' "$@"\n').encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii")


def _linked(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _check_directories(path: Path) -> None:
    for directory in reversed((path, *path.parents)):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            continue
        if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise NativeInstallError("unsafe_path", "Native installation directory is not a plain directory")


def _read_file(path: Path) -> bytes | None:
    _check_directories(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if _linked(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise NativeInstallError("unsafe_file", "Native installation file is not a single-link regular file")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and metadata.st_uid != getuid():
        raise NativeInstallError("unsafe_file", "Native installation file belongs to another user")
    if metadata.st_size > _FILE_LIMIT:
        raise NativeInstallError("invalid_installation", "Native installation file exceeds its size limit")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) |
             getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise NativeInstallError("installation_changed", "Native installation changed during inspection")
        contents = stream.read(_FILE_LIMIT + 1)
    if len(contents) > _FILE_LIMIT:
        raise NativeInstallError("invalid_installation", "Native installation file exceeds its size limit")
    return contents


def _object(contents: bytes) -> dict[str, Any]:
    try:
        value = json.loads(contents.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise NativeInstallError("invalid_installation", "Native installation contains invalid JSON") from None
    if not isinstance(value, dict):
        raise NativeInstallError("invalid_installation", "Native installation JSON must be an object")
    return value


def _registry_entries() -> dict[int, str | None]:
    registry = importlib.import_module("winreg")
    entries: dict[int, str | None] = {}
    for view in (registry.KEY_WOW64_32KEY, registry.KEY_WOW64_64KEY):
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, _REGISTRY_KEY, 0,
                                  registry.KEY_READ | view) as key:
                subkeys, values, _ = registry.QueryInfoKey(key)
                if subkeys or values != 1:
                    raise NativeInstallError("foreign_registration", "Chrome native registration has foreign values")
                try:
                    value, kind = registry.QueryValueEx(key, "")
                except FileNotFoundError:
                    raise NativeInstallError("foreign_registration", "Chrome native registration has no default path") from None
                if kind != registry.REG_SZ or not isinstance(value, str):
                    raise NativeInstallError("foreign_registration", "Chrome native registration has an invalid path")
                entries[view] = value
        except FileNotFoundError:
            entries[view] = None
    return entries


def _registration_snapshot(layout: _Layout) -> dict[int, str | None] | bytes | None:
    if layout.platform == "win32":
        return _registry_entries()
    if layout.registration is None:
        raise NativeInstallError("invalid_installation", "Chrome native registration path is missing")
    return _read_file(layout.registration)


def _snapshot(layout: _Layout) -> tuple[dict[Path, bytes | None], dict[int, str | None] | bytes | None]:
    files = {path: _read_file(path) for path in layout.owned_files}
    registration = _registration_snapshot(layout)
    if isinstance(registration, dict) and any(
        value is not None and os.path.normcase(value) != os.path.normcase(str(layout.manifest))
        for value in registration.values()
    ):
        raise NativeInstallError("foreign_registration", "Chrome native host is registered to another installation")
    return files, registration


def _verify_owned(layout: _Layout, files: dict[Path, bytes | None],
                  registration: dict[int, str | None] | bytes | None) -> dict[str, Any] | None:
    receipt_bytes = files[layout.receipt]
    if receipt_bytes is None:
        registered = any(registration.values()) if isinstance(registration, dict) else registration is not None
        if any(data is not None for data in files.values()) or registered:
            raise NativeInstallError("foreign_registration", "Installation ownership cannot be established")
        return None
    receipt = _object(receipt_bytes)
    expected_names = {layout.config.name, layout.launcher.name, layout.manifest.name}
    hashes = receipt.get("files")
    if (type(receipt.get("schema")) is not int or receipt["schema"] != 1 or
            receipt.get("host_name") != HOST_NAME or receipt.get("platform") != layout.platform or
            receipt.get("registration") != layout.registration_name or not isinstance(hashes, dict) or
            set(hashes) != expected_names):
        raise NativeInstallError("invalid_installation", "Native installation ownership record is invalid")
    for path in (layout.config, layout.launcher, layout.manifest):
        content = files[path]
        if content is None or hashlib.sha256(content).hexdigest() != hashes[path.name]:
            raise NativeInstallError("modified_installation", "Native installation files are missing or modified")
    config_bytes = files[layout.config] or b""
    if len(config_bytes) > MAX_CONFIG_BYTES:
        raise NativeInstallError("invalid_config", "Native launcher configuration exceeds its size limit")
    config = _object(config_bytes)
    if (type(config.get("schema")) is not int or config["schema"] != 1 or
            not isinstance(config.get("extension_id"), str)):
        raise NativeInstallError("invalid_config", "Native launcher configuration schema is invalid")
    identifier = _extension_identifier(config["extension_id"])
    validate_config_environment(config.get("environment"))
    executable = receipt.get("python")
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise NativeInstallError("invalid_installation", "Native installation interpreter path is invalid")
    expected_manifest = {"name": HOST_NAME, "description": _DESCRIPTION, "type": "stdio",
                         "path": str(layout.launcher),
                         "allowed_origins": [f"chrome-extension://{identifier}/"]}
    if (_object(files[layout.manifest] or b"") != expected_manifest or
            files[layout.launcher] != _launcher_bytes(layout, Path(executable))):
        raise NativeInstallError("invalid_installation", "Native installation launch contract is invalid")
    if isinstance(registration, bytes) and registration != files[layout.manifest]:
        raise NativeInstallError("foreign_registration", "Chrome native manifest belongs to another installation")
    return receipt


def _ensure_directory(path: Path, created: list[Path]) -> None:
    _check_directories(path)
    if path.exists():
        return
    _ensure_directory(path.parent, created)
    path.mkdir(mode=0o700)
    created.append(path)


def _write_file(path: Path, content: bytes, previous: bytes | None, mode: int) -> None:
    if _read_file(path) != previous:
        raise NativeInstallError("installation_changed", "Native installation changed before replacement")
    descriptor, candidate = tempfile.mkstemp(prefix=".btap-native-", dir=path.parent)
    temporary = Path(candidate)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_file(path) != previous:
            raise NativeInstallError("installation_changed", "Native installation changed before replacement")
        if previous is None:
            if os.name == "nt":
                # Windows rename fails if the destination exists; unlike POSIX
                # rename, this is atomic no-replace and also supports FAT volumes.
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_file(path: Path, expected: bytes) -> None:
    if _read_file(path) != expected:
        raise NativeInstallError("installation_changed", "Native installation changed before removal")
    path.unlink()


def _set_registry(view: int, value: str | None) -> None:
    registry = importlib.import_module("winreg")
    if value is None:
        registry.DeleteKeyEx(registry.HKEY_CURRENT_USER, _REGISTRY_KEY, view, 0)
    else:
        absent = _registry_entries().get(view) is None
        try:
            with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, _REGISTRY_KEY, 0,
                                      registry.KEY_WRITE | view) as key:
                registry.SetValueEx(key, "", 0, registry.REG_SZ, value)
        except OSError:
            if absent:
                # CreateKeyEx can succeed before SetValueEx fails. Only its empty
                # key is ours to remove; a newly populated key must be preserved.
                try:
                    with registry.OpenKey(registry.HKEY_CURRENT_USER, _REGISTRY_KEY, 0,
                                          registry.KEY_READ | view) as key:
                        subkeys, values, _ = registry.QueryInfoKey(key)
                    if not subkeys and not values:
                        registry.DeleteKeyEx(registry.HKEY_CURRENT_USER, _REGISTRY_KEY, view, 0)
                except FileNotFoundError:
                    pass
            raise


def _base_report(layout: _Layout) -> dict[str, Any]:
    return {"host_name": HOST_NAME, "platform": layout.platform,
            "manifest_path": str(layout.manifest), "launcher_path": str(layout.launcher),
            "config_path": str(layout.config), "registration": layout.registration_name}


def _restore_files(layout: _Layout, previous: Mapping[Path, bytes | None],
                   applied: Mapping[Path, bytes | None], changed: list[Path]) -> bool:
    failed = False
    for path in reversed(changed):
        try:
            before, after = previous[path], applied[path]
            if _read_file(path) == before:
                continue
            if before is None and after is not None:
                _remove_file(path, after)
            elif before is not None:
                _write_file(path, before, after, 0o700 if path == layout.launcher else 0o600)
        except (OSError, NativeInstallError):
            failed = True
    return not failed


def _restore_registry(previous: dict[int, str | None], changed: list[int],
                      applied: str | None) -> bool:
    failed = False
    for view in reversed(changed):
        try:
            current = _registry_entries()[view]
            if current == previous[view]:
                continue
            if current != applied:
                raise NativeInstallError("installation_changed", "Chrome registration changed during rollback")
            _set_registry(view, previous[view])
        except (OSError, NativeInstallError):
            failed = True
    return not failed


def install_native_host(extension_id: str | None = None) -> dict[str, Any]:
    """Install for the current user, refusing foreign or modified registrations."""
    identifier = _extension_identifier(extension_id)
    config = _configuration(identifier)
    layout = _layout()
    executable = _python_executable(layout.platform)
    files, registration = _snapshot(layout)
    _verify_owned(layout, files, registration)
    origins = [f"chrome-extension://{identifier}/"]
    manifest = {"name": HOST_NAME, "description": _DESCRIPTION, "type": "stdio",
                "path": str(layout.launcher), "allowed_origins": origins}
    payloads = {
        layout.config: _json_bytes(config), layout.launcher: _launcher_bytes(layout, executable),
        layout.manifest: _json_bytes(manifest),
    }
    receipt = {"schema": 1, "host_name": HOST_NAME, "platform": layout.platform,
               "registration": layout.registration_name, "python": str(executable),
               "files": {path.name: hashlib.sha256(data).hexdigest() for path, data in payloads.items()}}
    payloads[layout.receipt] = _json_bytes(receipt)
    if (len(payloads[layout.config]) > MAX_CONFIG_BYTES or
            any(len(data) > _FILE_LIMIT for data in payloads.values())):
        raise NativeInstallError("invalid_config", "Native installation metadata exceeds its size limit")
    if layout.registration is not None:
        payloads[layout.registration] = payloads[layout.manifest]
        files[layout.registration] = registration if isinstance(registration, bytes) else None
    created: list[Path] = []
    written: list[Path] = []
    changed_views: list[int] = []
    try:
        for path, data in payloads.items():
            _ensure_directory(path.parent, created)
            needs_execute = (path == layout.launcher and layout.platform != "win32" and
                             files[path] is not None and not os.access(path, os.X_OK))
            if files[path] != data or needs_execute:
                written.append(path)
                _write_file(path, data, files[path], 0o700 if path == layout.launcher else 0o600)
        if isinstance(registration, dict):
            if _registry_entries() != registration:
                raise NativeInstallError("installation_changed", "Chrome registration changed during installation")
            registry = importlib.import_module("winreg")
            view = registry.KEY_WOW64_32KEY
            if registration[view] != str(layout.manifest):
                changed_views.append(view)
                _set_registry(view, str(layout.manifest))
            if _registry_entries()[view] != str(layout.manifest):
                raise NativeInstallError("registration_failed", "Chrome registration could not be verified")
        observed, observed_registration = _snapshot(layout)
        if (any(observed[path] != payloads[path] for path in layout.owned_files) or
                (layout.registration is not None and observed_registration != payloads[layout.manifest])):
            raise NativeInstallError("installation_changed", "Native installation readback did not match")
        _verify_owned(layout, observed, observed_registration)
    except (OSError, NativeInstallError):
        # Roll back only bytes this call wrote. Never overwrite a concurrent edit.
        rollback_failed = (isinstance(registration, dict) and
                           not _restore_registry(registration, changed_views, str(layout.manifest)))
        if not _restore_files(layout, files, payloads, written):
            rollback_failed = True
        for directory in reversed(created):
            try:
                if not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise NativeInstallError("rollback_incomplete", "Native installation failed; rollback needs inspection") from None
        raise
    return {**_base_report(layout), "status": "installed", "installed": True,
            "extension_id": identifier, "allowed_origins": origins}


def uninstall_native_host() -> dict[str, Any]:
    """Remove only a verified installation and its own Chrome registration."""
    layout = _layout()
    files, registration = _snapshot(layout)
    if _verify_owned(layout, files, registration) is None:
        return {**_base_report(layout), "status": "not_installed", "installed": False}
    removed: list[Path] = []
    changed_views: list[int] = []
    registration_removed = False
    try:
        # Validate all files before deleting any registration or launcher.
        if isinstance(registration, dict):
            if _registry_entries() != registration:
                raise NativeInstallError("installation_changed", "Chrome registration changed before removal")
            for view, value in registration.items():
                if value is not None:
                    # WOW64 flags alias the same key on a 32-bit Windows system.
                    current = _registry_entries()[view]
                    if current is None:
                        continue
                    if current != value:
                        raise NativeInstallError("installation_changed", "Chrome registration changed before removal")
                    changed_views.append(view)
                    _set_registry(view, None)
        elif isinstance(registration, bytes) and layout.registration is not None:
            registration_removed = True
            _remove_file(layout.registration, registration)
        for path in layout.owned_files:
            removed.append(path)
            _remove_file(path, files[path] or b"")
    except (OSError, NativeInstallError):
        restored = _restore_files(layout, files, dict.fromkeys(removed), removed)
        # Re-enable Chrome startup only after all owned launch files are restored.
        if restored:
            if isinstance(registration, dict):
                restored = _restore_registry(registration, changed_views, None)
            elif registration_removed and layout.registration is not None:
                restored = _restore_files(layout, {layout.registration: registration},
                                           {layout.registration: None}, [layout.registration])
        if not restored:
            raise NativeInstallError("rollback_incomplete", "Native uninstall failed; rollback needs inspection") from None
        raise
    if not any(layout.directory.iterdir()):
        layout.directory.rmdir()
    return {**_base_report(layout), "status": "removed", "installed": False}


def native_host_status() -> dict[str, Any]:
    """Inspect installation without creating files, changing registry or spawning."""
    report: dict[str, Any] = {"host_name": HOST_NAME, "installed": False}
    try:
        layout = _layout()
        report.update(_base_report(layout))
        files, registration = _snapshot(layout)
        receipt = _verify_owned(layout, files, registration)
        if receipt is None:
            return {**report, "status": "missing"}
        registered = any(registration.values()) if isinstance(registration, dict) else registration is not None
        if not registered:
            raise NativeInstallError("registration_missing", "Chrome native host registration is missing")
        config = _object(files[layout.config] or b"")
        identifier = config["extension_id"]
        executable = receipt.get("python")
        if not isinstance(executable, str) or not Path(executable).is_absolute() or not Path(executable).is_file():
            raise NativeInstallError("python_unavailable", "Installed Python executable is unavailable")
        if layout.platform != "win32" and not os.access(layout.launcher, os.X_OK):
            raise NativeInstallError("launcher_not_executable", "Native launcher is not executable")
        return {**report, "status": "installed", "installed": True,
                "extension_id": identifier, "allowed_origins": [f"chrome-extension://{identifier}/"]}
    except NativeInstallError as exc:
        return {**report, "status": "invalid", "reason": exc.code, "error_type": type(exc).__name__}
    except (OSError, UnicodeError) as exc:
        return {**report, "status": "unreadable", "reason": "installation_unreadable",
                "error_type": type(exc).__name__}
