"""Native registration tests use temporary state and a synthetic Windows registry."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from browsertap_mcp import extension_origin, native_host
from browsertap_mcp import native_installer as installer

EXTENSION_ID = "abcdefghijklmnop" * 2


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_WRITE = 2
    KEY_WOW64_32KEY = 256
    KEY_WOW64_64KEY = 512
    REG_SZ = 1

    def __init__(self):
        self.entries = {}
        self.writes = []
        self.fail_read = False
        self.fail_write = False
        self.alias_views = False

    def _view(self, access):
        return 256 if self.alias_views else access & (self.KEY_WOW64_32KEY | self.KEY_WOW64_64KEY)

    @contextmanager
    def OpenKey(self, hive, path, reserved, access):
        assert hive == self.HKEY_CURRENT_USER
        assert path == installer._REGISTRY_KEY
        if self.fail_read:
            raise PermissionError("synthetic registry refusal")
        view = self._view(access)
        if view not in self.entries:
            raise FileNotFoundError(path)
        yield view

    @contextmanager
    def CreateKeyEx(self, hive, path, reserved, access):
        assert hive == self.HKEY_CURRENT_USER
        assert path == installer._REGISTRY_KEY
        view = self._view(access)
        self.entries.setdefault(view, {})
        yield view

    def QueryInfoKey(self, key):
        return 0, len(self.entries[key]), 0

    def QueryValueEx(self, key, name):
        if name not in self.entries[key]:
            raise FileNotFoundError(name)
        return self.entries[key][name]

    def SetValueEx(self, key, name, reserved, kind, value):
        if self.fail_write:
            raise PermissionError("synthetic registry write refusal")
        self.writes.append(("set", key, value))
        self.entries[key][name] = (value, kind)

    def DeleteKeyEx(self, hive, path, view, reserved):
        assert hive == self.HKEY_CURRENT_USER
        assert path == installer._REGISTRY_KEY
        view = self._view(view)
        if view not in self.entries:
            raise FileNotFoundError(path)
        self.writes.append(("delete", view))
        del self.entries[view]


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("BROWSERTAP_", "AGENT_BROWSER_")) or name == "XDG_CONFIG_HOME":
            monkeypatch.delenv(name)
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(installer, "sys", SimpleNamespace(platform=sys.platform, executable=sys.executable))
    monkeypatch.setattr(installer, "origin_policy_report", lambda: {
        "default_origin": f"chrome-extension://{EXTENSION_ID}", "identity_source": "manifest_key",
    })
    registry = FakeRegistry()
    monkeypatch.setitem(sys.modules, "winreg", registry)
    return SimpleNamespace(home=tmp_path, registry=registry, monkeypatch=monkeypatch)


def _platform(sandbox, platform):
    installer.sys.platform = platform
    return installer._layout()


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_install_register_readback_reinstall_and_uninstall(sandbox, platform):
    layout = _platform(sandbox, platform)
    assert installer.native_host_status()["status"] == "missing"
    assert not layout.directory.exists()
    result = installer.install_native_host()
    assert result["status"] == "installed"
    assert result["extension_id"] == EXTENSION_ID
    manifest = json.loads(layout.manifest.read_bytes())
    assert manifest == {"name": "io.browsertap.host", "description": "BrowserTap Native Messaging Host",
                        "type": "stdio", "path": str(layout.launcher),
                        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"]}
    if platform == "win32":
        assert sandbox.registry.entries == {
            256: {"": (str(layout.manifest), sandbox.registry.REG_SZ)},
        }
    else:
        assert layout.registration.read_bytes() == layout.manifest.read_bytes()
    assert installer.native_host_status()["status"] == "installed"
    mtimes = {path: path.stat().st_mtime_ns for path in layout.owned_files}
    assert installer.install_native_host()["installed"]
    assert mtimes == {path: path.stat().st_mtime_ns for path in layout.owned_files}
    assert installer.uninstall_native_host()["status"] == "removed"
    assert not layout.directory.exists()
    assert not sandbox.registry.entries
    assert installer.uninstall_native_host()["status"] == "not_installed"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_registration_uses_chrome_per_user_directory(sandbox, platform):
    layout = _platform(sandbox, platform)
    expected = (sandbox.home / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
                if platform == "darwin" else sandbox.home / ".config/google-chrome/NativeMessagingHosts")
    assert layout.registration == expected / "io.browsertap.host.json"
    assert not expected.exists()


def test_xdg_registration_and_relative_xdg_rejection(sandbox):
    sandbox.monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox.home / "custom config"))
    layout = _platform(sandbox, "linux")
    assert layout.registration == sandbox.home / "custom config/google-chrome/NativeMessagingHosts/io.browsertap.host.json"
    sandbox.monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    with pytest.raises(installer.NativeInstallError, match="absolute"):
        installer.install_native_host()
    assert not layout.directory.exists()


def test_default_identity_comes_from_manifest_key(sandbox):
    directory = sandbox.home / "extension"
    directory.mkdir()
    key = base64.b64encode(b"synthetic native messaging public key").decode()
    (directory / "manifest.json").write_text(json.dumps({"key": key}), encoding="utf-8")
    origin, source = extension_origin._directory_identity(directory)
    sandbox.monkeypatch.setattr(installer, "origin_policy_report", lambda: {
        "default_origin": origin, "identity_source": source,
    })
    result = installer.install_native_host()
    assert result["allowed_origins"] == [origin + "/"]


@pytest.mark.parametrize("policy", [
    {"default_origin": None, "identity_source": "unavailable"},
    {"default_origin": f"chrome-extension://{EXTENSION_ID}", "identity_source": "unpacked_path"},
    {"default_origin": "chrome-extension://invalid", "identity_source": "manifest_key"},
])
def test_missing_or_invalid_pinned_identity_fails_before_writes(sandbox, policy):
    sandbox.monkeypatch.setattr(installer, "origin_policy_report", lambda: policy)
    with pytest.raises(installer.NativeInstallError):
        installer.install_native_host()
    assert not installer._layout().directory.exists()


@pytest.mark.parametrize("identifier", ["", "a" * 31, "a" * 33, "z" * 32, "A" * 32, EXTENSION_ID + "\n", 17])
def test_invalid_extension_override_is_rejected(sandbox, identifier):
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host(identifier)
    assert error.value.code == "invalid_extension_id"
    assert not installer._layout().directory.exists()


def test_explicit_fork_override_does_not_require_packaged_key(sandbox):
    sandbox.monkeypatch.setattr(installer, "origin_policy_report", lambda: pytest.fail("unused identity"))
    assert installer.install_native_host("p" * 32)["extension_id"] == "p" * 32


def test_only_explicit_nonsecret_overrides_are_captured_and_paths_resolved(sandbox):
    sandbox.monkeypatch.chdir(sandbox.home)
    sandbox.monkeypatch.setenv("BROWSERTAP_STATE_DIR", "relative state")
    sandbox.monkeypatch.setenv("BROWSERTAP_BRIDGE_TOKEN_FILE", "tokens/bridge token")
    sandbox.monkeypatch.setenv("BROWSERTAP_BRIDGE_HOST", "127.0.0.2")
    sandbox.monkeypatch.setenv("BROWSERTAP_BRIDGE_PORT", "19765")
    sandbox.monkeypatch.setenv("BROWSERTAP_TRANSPORT", "websocket")
    sandbox.monkeypatch.setenv("BROWSERTAP_BRIDGE_TOKEN", "synthetic-do-not-persist")
    sandbox.monkeypatch.setenv("BROWSERTAP_OTHER_CONFIG", "unrelated")
    installer.install_native_host()
    layout = installer._layout()
    config = json.loads(layout.config.read_bytes())
    assert config == {"schema": 1, "extension_id": EXTENSION_ID, "environment": {
        "BROWSERTAP_STATE_DIR": str((sandbox.home / "relative state").resolve()),
        "BROWSERTAP_BRIDGE_TOKEN_FILE": str((sandbox.home / "tokens/bridge token").resolve()),
        "BROWSERTAP_BRIDGE_HOST": "127.0.0.2", "BROWSERTAP_BRIDGE_PORT": "19765",
        "BROWSERTAP_TRANSPORT": "websocket",
    }}
    for path in layout.owned_files:
        assert b"synthetic-do-not-persist" not in path.read_bytes()
    assert not (sandbox.home / "tokens").exists()


def test_defaults_and_empty_paths_are_not_saved_as_overrides(sandbox):
    sandbox.monkeypatch.delenv("BROWSERTAP_STATE_DIR")
    sandbox.monkeypatch.setenv("BROWSERTAP_BRIDGE_TOKEN_FILE", "  ")
    installer.install_native_host()
    assert json.loads(installer._layout().config.read_bytes())["environment"] == {}


def test_configuration_too_large_for_host_is_rejected_before_installation(sandbox):
    sandbox.monkeypatch.setenv("BROWSERTAP_BRIDGE_TOKEN_FILE", str(sandbox.home / ("中" * 12_000)))
    config = installer._json_bytes(installer._configuration(EXTENSION_ID))
    assert native_host.MAX_CONFIG_BYTES < len(config) < installer._FILE_LIMIT
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "invalid_config"
    assert not installer._layout().directory.exists()
    assert not sandbox.registry.writes


@pytest.mark.parametrize(("name", "value"), [
    ("BROWSERTAP_BRIDGE_HOST", "192.0.2.4"), ("BROWSERTAP_BRIDGE_HOST", "host.example"),
    ("BROWSERTAP_BRIDGE_HOST", ""), ("BROWSERTAP_BRIDGE_HOST", "localhost\n"),
    ("BROWSERTAP_BRIDGE_PORT", "0"), ("BROWSERTAP_BRIDGE_PORT", "65534"),
    ("BROWSERTAP_BRIDGE_PORT", "not-a-port"), ("BROWSERTAP_TRANSPORT", "auto"),
])
def test_invalid_configuration_prevents_any_registration_or_write(sandbox, name, value):
    sandbox.monkeypatch.setenv(name, value)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "invalid_config"
    assert not installer._layout().directory.exists()
    assert not sandbox.registry.writes


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.12.34.56", "::1"])
def test_loopback_configuration_is_accepted(host):
    assert installer.validate_config_environment({"BROWSERTAP_BRIDGE_HOST": host})


@pytest.mark.parametrize("environment", [None, [], {"UNKNOWN": "value"},
    {"BROWSERTAP_BRIDGE_TOKEN": "synthetic"}, {"BROWSERTAP_BRIDGE_PORT": 18765},
    {"BROWSERTAP_STATE_DIR": "relative"}, {"BROWSERTAP_TRANSPORT": "native\0"}])
def test_config_schema_rejects_unknown_keys_secrets_and_invalid_types(environment):
    with pytest.raises(installer.NativeInstallError):
        installer.validate_config_environment(environment)


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_foreign_registration_is_preserved(sandbox, platform):
    layout = _platform(sandbox, platform)
    if platform == "win32":
        sandbox.registry.entries[512] = {"": (str(sandbox.home / "foreign.json"), 1)}
    else:
        layout.registration.parent.mkdir(parents=True)
        layout.registration.write_bytes(b'{"name":"foreign"}')
    for operation in (installer.install_native_host, installer.uninstall_native_host):
        with pytest.raises(installer.NativeInstallError) as error:
            operation()
        assert error.value.code == "foreign_registration"
    assert installer.native_host_status()["reason"] == "foreign_registration"
    assert not layout.directory.exists()
    if platform != "win32":
        assert layout.registration.read_bytes() == b'{"name":"foreign"}'


@pytest.mark.parametrize("entries", [{"": ("bad", 2)}, {"extra": ("data", 1)}, {}])
def test_foreign_registry_values_fail_closed(sandbox, entries):
    _platform(sandbox, "win32")
    sandbox.registry.entries[256] = entries
    assert installer.native_host_status()["status"] == "invalid"
    assert not sandbox.registry.writes


@pytest.mark.parametrize("filename", ["config.json", "launch.cmd", "io.browsertap.host.json", "install.json"])
def test_unowned_file_is_not_overwritten(sandbox, filename):
    layout = _platform(sandbox, "win32")
    layout.directory.mkdir(parents=True)
    path = layout.directory / filename
    path.write_bytes(b"unrelated user file")
    with pytest.raises(installer.NativeInstallError):
        installer.install_native_host()
    assert path.read_bytes() == b"unrelated user file"
    assert not sandbox.registry.writes


@pytest.mark.parametrize("member", ["config", "launcher", "manifest", "receipt"])
def test_modified_owned_files_block_update_and_uninstall(sandbox, member):
    layout = _platform(sandbox, "win32")
    installer.install_native_host()
    target = getattr(layout, member)
    target.write_bytes(target.read_bytes() + b"foreign edit")
    snapshot = {path: path.read_bytes() for path in layout.owned_files}
    writes = list(sandbox.registry.writes)
    for operation in (installer.install_native_host, installer.uninstall_native_host):
        with pytest.raises(installer.NativeInstallError):
            operation()
    assert installer.native_host_status()["status"] == "invalid"
    assert {path: path.read_bytes() for path in layout.owned_files} == snapshot
    assert sandbox.registry.writes == writes


def test_hardlinked_owned_file_is_refused_without_touching_target(sandbox):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    duplicate = sandbox.home / "linked-config"
    os.link(layout.config, duplicate)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.uninstall_native_host()
    assert error.value.code == "unsafe_file"
    assert duplicate.read_bytes() == layout.config.read_bytes()
    assert layout.registration.exists()


def test_symlink_checks_reject_file_and_parent_reparse_points(sandbox):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    original = Path.lstat
    target = layout.launcher

    def lstat(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=1024)
        return metadata

    sandbox.monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.uninstall_native_host()
    assert error.value.code == "unsafe_file"
    target = layout.directory
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "unsafe_path"


def test_uninstall_keeps_user_state_and_unknown_files(sandbox):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    token = layout.directory.parent / "bridge-token"
    token.write_bytes(b"synthetic preserved token")
    keep = layout.directory / "notes.txt"
    keep.write_bytes(b"user notes")
    installer.uninstall_native_host()
    assert keep.read_bytes() == b"user notes"
    assert token.read_bytes() == b"synthetic preserved token"
    assert list(layout.directory.iterdir()) == [keep]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_missing_registration_can_be_repaired_or_uninstalled(sandbox, platform):
    layout = _platform(sandbox, platform)
    installer.install_native_host()
    if platform == "win32":
        sandbox.registry.entries.clear()
    else:
        layout.registration.unlink()
    assert installer.native_host_status()["reason"] == "registration_missing"
    installer.install_native_host()
    assert installer.native_host_status()["installed"]
    if platform == "win32":
        sandbox.registry.entries.clear()
    else:
        layout.registration.unlink()
    assert installer.uninstall_native_host()["status"] == "removed"


def test_both_windows_registry_views_are_removed_when_owned(sandbox):
    layout = _platform(sandbox, "win32")
    installer.install_native_host()
    sandbox.registry.entries[512] = {"": (str(layout.manifest), 1)}
    installer.uninstall_native_host()
    assert not sandbox.registry.entries


def test_32bit_windows_registry_view_aliases_are_safe_to_uninstall(sandbox):
    _platform(sandbox, "win32")
    sandbox.registry.alias_views = True
    installer.install_native_host()
    installer.uninstall_native_host()
    assert not sandbox.registry.entries


def test_status_is_nonmutating_and_reports_unreadable_registry(sandbox):
    layout = _platform(sandbox, "win32")
    sandbox.registry.fail_read = True
    result = installer.native_host_status()
    assert result["status"] == "unreadable"
    assert result["error_type"] == "PermissionError"
    assert not layout.directory.exists()
    assert not sandbox.registry.writes


def test_status_invalid_configuration_does_not_leak_file_content(sandbox):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    layout.receipt.write_bytes(b"\xffsynthetic-sensitive-json")
    result = installer.native_host_status()
    assert result["status"] == "invalid"
    assert "synthetic-sensitive-json" not in json.dumps(result)


def test_windows_pythonw_is_replaced_with_console_interpreter(sandbox):
    _platform(sandbox, "win32")
    console = sandbox.home / "python.exe"
    console.write_bytes(b"synthetic console interpreter")
    installer.sys.executable = str(console.with_name("pythonw.exe"))
    assert installer._python_executable("win32") == console
    installer.install_native_host()
    assert "pythonw.exe" not in installer._layout().launcher.read_text(encoding="utf-8")


def test_missing_console_python_prevents_registration(sandbox):
    layout = _platform(sandbox, "win32")
    installer.sys.executable = str(sandbox.home / "pythonw.exe")
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "python_unavailable"
    assert not layout.directory.exists()


def test_empty_interpreter_path_prevents_registration(sandbox):
    installer.sys.executable = ""
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "python_unavailable"
    assert not installer._layout().directory.exists()


def test_posix_launcher_quotes_absolute_paths_and_forwards_arguments(sandbox):
    sandbox.monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(sandbox.home / "中文 state ' %!&"))
    layout = _platform(sandbox, "linux")
    executable = sandbox.home / "Python runtime" / "python3"
    data = installer._launcher_bytes(layout, executable).decode()
    command = shlex.split(data.splitlines()[1])
    assert command == ["exec", str(executable), "-m", "browsertap_mcp.native_host", "--config",
                       str(layout.config), "$@"]
    assert data.startswith("#!/bin/sh\n")


def test_generated_launcher_preserves_chrome_arguments_and_unicode_paths(sandbox):
    sandbox.monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(sandbox.home / "中文 state 50% !& (native)'"))
    layout = installer._layout()
    installer.install_native_host()
    modules = sandbox.home / "stub modules"
    package = modules / "browsertap_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (package / "native_host.py").write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['NATIVE_TEST_OUTPUT']).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    output = sandbox.home / "argv.json"
    args = [f"chrome-extension://{EXTENSION_ID}/", "--parent-window=1042"]
    if sys.platform == "win32":
        # cmd /s /c needs an outer quote pair around the complete command. A
        # subprocess argument list applies C-runtime escaping, which cmd lacks.
        command = (f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c '
                   f'""{layout.launcher}" "{args[0]}" "{args[1]}""')
    else:
        command = [str(layout.launcher), *args]
    completed = subprocess.run(command, env={**os.environ, "PYTHONPATH": str(modules),
                               "NATIVE_TEST_OUTPUT": str(output)}, cwd=sandbox.home,
                               check=False, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b""
    assert json.loads(output.read_text(encoding="utf-8")) == ["--config", str(layout.config), *args]


def test_install_write_failure_restores_previous_owned_installation(sandbox):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    before = {path: path.read_bytes() for path in (*layout.owned_files, layout.registration)}
    original = installer._write_file

    def fail(path, content, previous, mode):
        if path == layout.manifest:
            raise PermissionError("synthetic write failure")
        return original(path, content, previous, mode)

    sandbox.monkeypatch.setattr(installer, "_write_file", fail)
    with pytest.raises(PermissionError):
        installer.install_native_host("p" * 32)
    assert {path: path.read_bytes() for path in before} == before
    assert installer.native_host_status()["installed"]


def test_fresh_install_file_failure_removes_only_new_files(sandbox):
    layout = _platform(sandbox, "linux")
    original = installer._write_file

    def fail(path, content, previous, mode):
        if path == layout.manifest:
            raise PermissionError("synthetic write failure")
        return original(path, content, previous, mode)

    sandbox.monkeypatch.setattr(installer, "_write_file", fail)
    with pytest.raises(PermissionError):
        installer.install_native_host()
    assert not layout.directory.exists()
    assert not layout.registration.exists()


def test_registry_write_failure_removes_its_empty_key_and_new_files(sandbox):
    layout = _platform(sandbox, "win32")
    sandbox.registry.fail_write = True
    with pytest.raises(PermissionError):
        installer.install_native_host()
    assert not sandbox.registry.entries
    assert not layout.directory.exists()


def test_unsupported_platform_diagnostic_is_safe(sandbox):
    installer.sys.platform = "freebsd"
    assert installer.native_host_status()["reason"] == "unsupported_platform"
    with pytest.raises(installer.NativeInstallError):
        installer.install_native_host()


@pytest.mark.parametrize("tail", ["newline\npath", 'quoted"path'])
def test_windows_launcher_rejects_paths_with_shell_control_characters(sandbox, tail):
    layout = installer._Layout("win32", sandbox.home / tail, None)
    with pytest.raises(installer.NativeInstallError) as error:
        installer._launcher_bytes(layout, Path(sys.executable))
    assert error.value.code == "unsafe_path"


@pytest.mark.parametrize("grows_after_stat", [False, True])
def test_oversized_installation_file_is_bounded(sandbox, grows_after_stat):
    target = sandbox.home / "large.json"
    target.write_bytes(b"x" * (installer._FILE_LIMIT + 1))
    if grows_after_stat:
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            metadata = original(path, *args, **kwargs)
            if path == target:
                fields = {name: getattr(metadata, name) for name in (
                    "st_mode", "st_nlink", "st_dev", "st_ino", "st_uid",
                )}
                return SimpleNamespace(**fields, st_size=0)
            return metadata

        sandbox.monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(installer.NativeInstallError, match="size limit"):
        installer._read_file(target)


def test_file_replaced_between_stat_and_open_is_refused(sandbox):
    target = sandbox.home / "owned.json"
    target.write_bytes(b"safe")
    original = os.fstat

    def fstat(descriptor):
        metadata = original(descriptor)
        return SimpleNamespace(st_dev=metadata.st_dev, st_ino=metadata.st_ino + 1)

    sandbox.monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(installer.NativeInstallError) as error:
        installer._read_file(target)
    assert error.value.code == "installation_changed"


def test_owned_file_from_another_user_is_refused(sandbox):
    target = sandbox.home / "owned.json"
    target.write_bytes(b"safe")
    sandbox.monkeypatch.setattr(os, "getuid", lambda: target.stat().st_uid + 1, raising=False)
    with pytest.raises(installer.NativeInstallError) as error:
        installer._read_file(target)
    assert error.value.code == "unsafe_file"
    assert target.read_bytes() == b"safe"


@pytest.mark.parametrize("stage", ["before_write", "before_publish", "before_remove"])
def test_file_race_preserves_replacement_and_removes_temporary_file(sandbox, stage):
    target = sandbox.home / "owned.json"
    target.write_bytes(b"original")
    if stage == "before_publish":
        original = os.fsync

        def fsync(descriptor):
            target.write_bytes(b"replacement")
            return original(descriptor)

        sandbox.monkeypatch.setattr(os, "fsync", fsync)
    else:
        target.write_bytes(b"replacement")
    with pytest.raises(installer.NativeInstallError) as error:
        if stage == "before_remove":
            installer._remove_file(target, b"original")
        else:
            installer._write_file(target, b"new", b"original", 0o600)
    assert error.value.code == "installation_changed"
    assert target.read_bytes() == b"replacement"
    assert not list(sandbox.home.glob(".btap-native-*"))


def _rewrite_recorded_file(layout, target, contents):
    target.write_bytes(contents)
    receipt = json.loads(layout.receipt.read_bytes())
    receipt["files"][target.name] = hashlib.sha256(contents).hexdigest()
    layout.receipt.write_text(json.dumps(receipt), encoding="utf-8")


@pytest.mark.parametrize(("field", "value"), [
    ("schema", True), ("schema", 2), ("host_name", "foreign.host"), ("platform", "foreign"),
    ("registration", "foreign.json"), ("files", []), ("files", {}), ("python", "relative-python"),
])
def test_invalid_ownership_record_prevents_removal(sandbox, field, value):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    receipt = json.loads(layout.receipt.read_bytes())
    receipt[field] = value
    layout.receipt.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(installer.NativeInstallError):
        installer.uninstall_native_host()
    assert layout.registration.exists()
    assert all(path.exists() for path in layout.owned_files)


@pytest.mark.parametrize("config", [[], {"schema": True}, {"schema": 2},
    {"schema": 1, "extension_id": None},
    {"schema": 1, "extension_id": EXTENSION_ID, "environment": {"BROWSERTAP_BRIDGE_TOKEN": "fake"}},
    {"schema": 1, "extension_id": EXTENSION_ID,
     "environment": {"BROWSERTAP_BRIDGE_TOKEN_FILE": "/" + "x" * native_host.MAX_CONFIG_BYTES}},
])
def test_config_contract_is_checked_even_when_hash_matches(sandbox, config):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    _rewrite_recorded_file(layout, layout.config, json.dumps(config).encode())
    with pytest.raises(installer.NativeInstallError):
        installer.uninstall_native_host()
    assert installer.native_host_status()["status"] == "invalid"
    assert layout.registration.exists()


@pytest.mark.parametrize("target", ["manifest", "launcher", "registration"])
def test_launch_contract_and_external_manifest_are_checked(sandbox, target):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    if target == "registration":
        layout.registration.write_bytes(b'{"name":"foreign"}')
    else:
        contents = (json.dumps({"name": installer.HOST_NAME, "path": "foreign"}).encode()
                    if target == "manifest" else b"#!/bin/sh\necho foreign\n")
        _rewrite_recorded_file(layout, getattr(layout, target), contents)
    with pytest.raises(installer.NativeInstallError):
        installer.uninstall_native_host()
    assert all(path.exists() for path in layout.owned_files)


def test_removed_python_is_diagnosed_without_preventing_owned_uninstall(sandbox):
    _platform(sandbox, "win32")
    executable = sandbox.home / "python.exe"
    executable.write_bytes(b"synthetic")
    installer.sys.executable = str(executable)
    installer.install_native_host()
    executable.unlink()
    assert installer.native_host_status()["reason"] == "python_unavailable"
    assert installer.uninstall_native_host()["status"] == "removed"


def test_nonexecutable_launcher_is_diagnosed_without_chmod(sandbox):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    original = os.access
    sandbox.monkeypatch.setattr(os, "access", lambda path, mode: False if path == layout.launcher else original(path, mode))
    before = layout.launcher.stat().st_mtime_ns
    assert installer.native_host_status()["reason"] == "launcher_not_executable"
    assert layout.launcher.stat().st_mtime_ns == before


def test_registry_change_before_install_commit_is_preserved(sandbox):
    layout = _platform(sandbox, "win32")
    original = installer._write_file
    foreign = str(sandbox.home / "foreign.json")

    def replace_registration(path, contents, previous, mode):
        original(path, contents, previous, mode)
        if path == layout.receipt:
            sandbox.registry.entries[256] = {"": (foreign, 1)}

    sandbox.monkeypatch.setattr(installer, "_write_file", replace_registration)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "installation_changed"
    assert sandbox.registry.entries[256][""][0] == foreign
    assert not layout.directory.exists()


def test_registry_change_before_uninstall_does_not_remove_files(sandbox):
    layout = _platform(sandbox, "win32")
    installer.install_native_host()
    original = installer._verify_owned
    foreign = str(sandbox.home / "foreign.json")

    def replace_registration(*args):
        result = original(*args)
        sandbox.registry.entries[256] = {"": (foreign, 1)}
        return result

    sandbox.monkeypatch.setattr(installer, "_verify_owned", replace_registration)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.uninstall_native_host()
    assert error.value.code == "installation_changed"
    assert sandbox.registry.entries[256][""][0] == foreign
    assert all(path.exists() for path in layout.owned_files)


def test_registry_readback_failure_rolls_back_new_registration_and_files(sandbox):
    layout = _platform(sandbox, "win32")
    original = sandbox.registry.SetValueEx

    def written_then_failed(*args):
        original(*args)
        raise PermissionError("synthetic failure after write")

    sandbox.monkeypatch.setattr(sandbox.registry, "SetValueEx", written_then_failed)
    with pytest.raises(PermissionError):
        installer.install_native_host()
    assert not sandbox.registry.entries
    assert not layout.directory.exists()


def test_silent_registry_write_failure_is_not_a_success(sandbox):
    layout = _platform(sandbox, "win32")

    def lost_write(view, value):
        pass

    sandbox.monkeypatch.setattr(installer, "_set_registry", lost_write)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "registration_failed"
    assert not layout.directory.exists()


def test_rollback_refuses_to_delete_a_concurrent_file_edit(sandbox):
    layout = _platform(sandbox, "linux")
    original = installer._write_file

    def fail_with_foreign_edit(path, content, previous, mode):
        if path == layout.manifest:
            layout.config.write_bytes(b"concurrent edit")
            raise PermissionError("synthetic write failure")
        return original(path, content, previous, mode)

    sandbox.monkeypatch.setattr(installer, "_write_file", fail_with_foreign_edit)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "rollback_incomplete"
    assert layout.config.read_bytes() == b"concurrent edit"
    assert not layout.launcher.exists()
    assert not layout.registration.exists()


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_uninstall_busy_launcher_restores_files_and_registration(sandbox, platform):
    layout = _platform(sandbox, platform)
    installer.install_native_host()
    before = {path: path.read_bytes() for path in layout.owned_files}
    original = installer._remove_file

    def busy_launcher(path, expected):
        if path == layout.launcher:
            raise PermissionError("synthetic busy launcher")
        return original(path, expected)

    with sandbox.monkeypatch.context() as patch:
        patch.setattr(installer, "_remove_file", busy_launcher)
        with pytest.raises(PermissionError):
            installer.uninstall_native_host()
    assert {path: path.read_bytes() for path in layout.owned_files} == before
    assert installer.native_host_status()["status"] == "installed"
    assert installer.uninstall_native_host()["status"] == "removed"


def test_uninstall_rollback_preserves_concurrent_replacement(sandbox):
    layout = _platform(sandbox, "linux")
    installer.install_native_host()
    original = installer._remove_file

    def replace_config_then_fail(path, expected):
        if path == layout.launcher:
            layout.config.write_bytes(b"user replacement")
            raise PermissionError("synthetic busy launcher")
        return original(path, expected)

    sandbox.monkeypatch.setattr(installer, "_remove_file", replace_config_then_fail)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.uninstall_native_host()
    assert error.value.code == "rollback_incomplete"
    assert layout.config.read_bytes() == b"user replacement"
    assert layout.launcher.exists()
    assert not layout.registration.exists()


def test_install_metadata_limit_is_checked_before_any_write(sandbox):
    layout = _platform(sandbox, "linux")
    sandbox.monkeypatch.setattr(installer, "_FILE_LIMIT", 100)
    with pytest.raises(installer.NativeInstallError) as error:
        installer.install_native_host()
    assert error.value.code == "invalid_config"
    assert not layout.directory.exists()
    assert not layout.registration.exists()


def test_posix_publish_uses_private_modes_and_complete_files(sandbox):
    layout = _platform(sandbox, "linux")
    real_os = os
    modes = []

    def fchmod(descriptor, mode):
        modes.append(mode)
        real_fchmod = getattr(real_os, "fchmod", None)
        if real_fchmod is not None:
            real_fchmod(descriptor, mode)

    # Keep pathlib and the host OS intact while exercising the POSIX publication
    # operations on the real temporary filesystem (hard links included).
    posix_os = SimpleNamespace(**vars(os))
    posix_os.name = "posix"
    posix_os.fchmod = fchmod
    sandbox.monkeypatch.setattr(installer, "os", posix_os)
    installer.install_native_host()
    assert modes.count(0o700) == 1
    assert modes.count(0o600) == 4
    assert layout.registration.read_bytes() == layout.manifest.read_bytes()
    assert all(path.stat().st_nlink == 1 for path in layout.owned_files)
    assert not list(layout.directory.glob(".btap-native-*"))
