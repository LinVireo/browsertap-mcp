"""Process build checks must compare frozen imports with current package bytes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from browsertap_mcp import runtime_identity as I

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "browsertap_mcp"
IMPORTED_JAVASCRIPT = (
    "chrome_extension/result_serialization.js",
    "chrome_extension/guarded_eval.js",
    "page_scripts/page_outline.js",
    "page_scripts/list_groups.js",
)

_BRIDGE_SCRIPT = r'''
import json
import sys
from pathlib import Path
import browsertap_mcp as package
from browsertap_mcp.browser_bridge import BrowserBridge
from browsertap_mcp.extension_build import compute_extension_stamp

root = Path(package.__file__).parent
assert root == Path(sys.argv[1]), root
if sys.argv[2] == "edit":
    source = root / "server.py"
    source.write_bytes(source.read_bytes() + b"\n# changed after bridge import\n")
bridge = BrowserBridge.__new__(BrowserBridge)
bridge.is_remote = False
bridge.sessions = {}
bridge.last_ext_seen = None
bridge.client_last_seen = {}
bridge.rejected_client_takeovers = 0
bridge.last_rejected_takeover = None
bridge.clean_sessions = lambda: None
bridge.ext_cmd = lambda *args, **kwargs: {"data": {
    "extension_version": package.__version__, "protocol_version": 3,
    "capabilities": {
        "content_command_channel_removed": True, "batch_result_guard": True,
    },
    "build_stamp": compute_extension_stamp(root / "chrome_extension"),
}}
print(json.dumps(bridge.diagnose()))
'''


def _package_copy(tmp_path: Path) -> Path:
    package = tmp_path / "site-packages" / "browsertap_mcp"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return package


def _run_package_script(package: Path, script: str, *args: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package.parent)
    env["BROWSERTAP_STATE_DIR"] = str(package.parent.parent / "state")
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(package), *args],
        cwd=package.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(completed.stdout)


def _subprocess_status(
    package: Path, edit_at: str, bridge_diagnosis: dict | None = None, *, fresh_bridge=False
) -> dict:
    script = r'''
import json
import subprocess
import sys
from pathlib import Path
import browsertap_mcp as package

root = Path(package.__file__).parent
assert root == Path(sys.argv[1]), root

def edit():
    source = root / "server.py"
    source.write_bytes(source.read_bytes() + b"\n# same-version source edit\n")

if sys.argv[2] == "before_server_import":
    edit()
from browsertap_mcp import server as S
if sys.argv[2] == "after_server_import":
    edit()
bridge_diagnosis = json.loads(sys.argv[3])
if sys.argv[4]:
    reply = subprocess.run(
        [sys.executable, "-B", "-c", sys.argv[4], str(root), "none"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    bridge_diagnosis = json.loads(reply.stdout)

class Driver:
    is_remote = True
    default_session_id = None

    def diagnose(self, timeout=None):
        if bridge_diagnosis is not None:
            return bridge_diagnosis
        result = {
            "cause": "healthy", "ok": True,
            "bridge_version": package.__version__,
            "extension_version": package.__version__,
            "protocol_version": 3,
            "extension_capabilities": {
                "content_command_channel_removed": True,
                "batch_result_guard": True,
            },
            "extension_build_stamp": S.compute_extension_stamp(S.chrome_extension_dir()),
        }
        return result

S.get_driver = lambda: Driver()
S.compact_tabs = lambda **kwargs: []
status = S.get_setup_status()
print(json.dumps({key: status.get(key) for key in (
    "status", "action", "package_version", "bridge_version",
    "restart_mcp_session_required", "restart_bridge_required", "reload_extension_required",
    "mcp_source_identity", "bridge_source_identity", "expected_python_source_identity",
    "mcp_build_verdict", "mcp_build_enforced",
    "bridge_build_verdict", "bridge_build_enforced", "diagnosis",
)}))
'''
    return _run_package_script(
        package, script, edit_at, json.dumps(bridge_diagnosis),
        _BRIDGE_SCRIPT if fresh_bridge else "",
    )


@pytest.mark.parametrize("edit_at", ["before_server_import", "after_server_import"])
def test_same_version_source_edit_after_package_import_requires_mcp_restart(tmp_path, edit_at):
    package = _package_copy(tmp_path)
    status = _subprocess_status(package, edit_at)

    assert status["package_version"] == status["bridge_version"]
    assert status["status"] == "stale_package"
    assert status["action"] == "restart_mcp_session"
    assert status["restart_mcp_session_required"] is True
    assert status["reload_extension_required"] is False
    assert status["mcp_build_verdict"] == "stale_process"
    assert status["mcp_build_enforced"] is True
    assert status["mcp_source_identity"]["sha256"] != status["expected_python_source_identity"]["sha256"]

    fresh = _subprocess_status(package, "none")
    assert fresh["status"] == "healthy"
    assert fresh["action"] == "none"
    assert fresh["restart_mcp_session_required"] is False
    assert fresh["mcp_build_verdict"] == "matches_tree"
    assert fresh["mcp_build_enforced"] is True


def test_old_bridge_and_fresh_mcp_identify_only_the_bridge_for_restart(tmp_path):
    package = _package_copy(tmp_path)
    bridge = _run_package_script(package, _BRIDGE_SCRIPT, "edit")
    assert bridge["bridge_build_verdict"] == "stale_process"
    assert bridge["bridge_build_enforced"] is True
    assert bridge["bridge_source_identity"] != bridge["bridge_expected_source_identity"]

    status = _subprocess_status(package, "none", bridge)
    assert status["package_version"] == status["bridge_version"]
    assert status["status"] == "stale_bridge"
    assert status["action"] == "restart_bridge"
    assert status["mcp_build_verdict"] == "matches_tree"
    assert status["bridge_build_verdict"] == "stale_process"
    assert status["bridge_source_identity"] == bridge["bridge_source_identity"]
    assert status["restart_bridge_required"] is True
    assert status["restart_mcp_session_required"] is False
    assert status["reload_extension_required"] is False


def test_old_mcp_and_fresh_bridge_identify_only_the_mcp_for_restart(tmp_path):
    package = _package_copy(tmp_path)
    status = _subprocess_status(package, "after_server_import", fresh_bridge=True)

    assert status["package_version"] == status["bridge_version"]
    assert status["status"] == "stale_package"
    assert status["action"] == "restart_mcp_session"
    assert status["mcp_build_verdict"] == "stale_process"
    assert status["bridge_build_verdict"] == "matches_tree"
    assert status["diagnosis"]["bridge_build_verdict"] == "matches_tree"
    assert status["restart_mcp_session_required"] is True
    assert status["restart_bridge_required"] is False
    assert status["reload_extension_required"] is False


def test_fresh_installed_layout_needs_no_checkout_for_a_verified_identity(tmp_path):
    package = _package_copy(tmp_path)
    status = _subprocess_status(package, "none", fresh_bridge=True)

    assert not (package.parent / ".git").exists()
    assert status["status"] == "healthy"
    assert status["action"] == "none"
    assert status["mcp_build_verdict"] == status["bridge_build_verdict"] == "matches_tree"
    assert status["mcp_build_enforced"] is status["bridge_build_enforced"] is True
    assert status["mcp_source_identity"] == status["bridge_source_identity"]
    assert status["mcp_source_identity"] == status["expected_python_source_identity"]


def test_old_bridge_without_identity_is_unknown_even_when_versions_match(tmp_path):
    status = _subprocess_status(_package_copy(tmp_path), "none")

    assert status["status"] == "healthy"
    assert status["restart_bridge_required"] is False
    assert status["bridge_source_identity"] is None
    assert status["bridge_build_verdict"] == "unverifiable"
    assert status["bridge_build_enforced"] is False


def _tiny_package(tmp_path: Path) -> Path:
    root = tmp_path / "package"
    root.mkdir()
    (root / "__init__.py").write_bytes(b"VALUE = 1\n")
    for name in IMPORTED_JAVASCRIPT:
        asset = root / name
        asset.parent.mkdir(exist_ok=True)
        asset.write_bytes(b"function syntheticAsset() {}\n")
    return root


def test_manifest_covers_python_and_imported_javascript_without_installation_paths(tmp_path):
    root = _tiny_package(tmp_path)
    (root / "subpackage").mkdir()
    source = root / "subpackage" / "lazy.py"
    source.write_bytes(b"VALUE = 2\n")
    identity = I.capture_source_identity(root)
    assert dict(identity.manifest).keys() == {
        "__init__.py", "subpackage/lazy.py", *IMPORTED_JAVASCRIPT,
    }
    assert identity.as_dict()["complete"] is True
    assert identity.as_dict()["file_count"] == 6
    copy = tmp_path / "different-installation"
    shutil.copytree(root, copy)
    assert I.capture_source_identity(copy) == identity
    (root / "README.md").write_bytes(b"changed asset")
    (root / "lazy.pyc").write_bytes(b"not a source file")
    assert I.capture_source_identity(root) == identity
    source.write_bytes(b"VALUE = 3\n")
    assert I.capture_source_identity(root).sha256 != identity.sha256
    source.write_bytes(b"VALUE = 2\n")
    source.rename(source.with_name("renamed.py"))
    assert I.capture_source_identity(root).sha256 != identity.sha256
    exposed = identity.as_dict()
    exposed["sha256"] = "cannot mutate the frozen snapshot"
    assert identity.as_dict()["sha256"] == identity.sha256


@pytest.mark.parametrize("kind", ["missing", "no_sources", "no_init"])
def test_incomplete_source_layout_is_unverifiable(tmp_path, kind):
    root = tmp_path / "package"
    if kind != "missing":
        root.mkdir()
    if kind == "no_init":
        (root / "server.py").write_bytes(b"VALUE = 1\n")
    identity = I.capture_source_identity(root).as_dict()
    assert identity["complete"] is False
    assert identity["sha256"] is None
    assert I.compare_source_identities(identity, identity) == "unverifiable"


def test_unreadable_source_does_not_certify_a_partial_manifest(tmp_path, monkeypatch):
    root = _tiny_package(tmp_path)
    (root / "unreadable.py").write_bytes(b"VALUE = 2\n")
    original = Path.open

    def unreadable(path, *args, **kwargs):
        if path.name == "unreadable.py":
            raise PermissionError("private path must not be published")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    identity = I.capture_source_identity(root).as_dict()
    assert identity["file_count"] == 1
    assert identity["complete"] is False
    assert identity["sha256"] is None
    assert identity["error"] == "python_sources_unreadable"
    assert "private path" not in json.dumps(identity)


def test_directory_enumeration_errors_are_not_silently_ignored(tmp_path, monkeypatch):
    root = _tiny_package(tmp_path)

    def broken_walk(directory, onerror):
        onerror(PermissionError("unreadable subdirectory"))

    monkeypatch.setattr(I.os, "walk", broken_walk)
    assert I.capture_source_identity(root).as_dict()["complete"] is False


def test_skipped_directory_symlinks_are_not_a_complete_manifest(tmp_path, monkeypatch):
    root = _tiny_package(tmp_path)
    (root / "linked").mkdir()
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "linked" or original(path))
    assert I.capture_source_identity(root).as_dict()["complete"] is False


@pytest.mark.parametrize("mutation", ["change_file", "add_file"])
def test_source_changes_during_the_read_are_unverifiable(tmp_path, monkeypatch, mutation):
    root = _tiny_package(tmp_path)
    original = Path.open

    class MutatingReader:
        def __enter__(self):
            self.file = original(root / "__init__.py", "rb")
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self):
            contents = self.file.read()
            name = "__init__.py" if mutation == "change_file" else "new.py"
            with original(root / name, "wb") as target:
                target.write(b"VALUE = 'changed source'\n")
            return contents

    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs: MutatingReader())
    identity = I.capture_source_identity(root).as_dict()
    assert identity["complete"] is False
    assert identity["sha256"] is None
    assert identity["error"] == "python_sources_changed_during_read"


def test_an_already_hashed_file_cannot_change_while_later_files_are_read(tmp_path, monkeypatch):
    root = _tiny_package(tmp_path)
    (root / "later.py").write_bytes(b"LATER = 1\n")
    original = Path.open

    class LaterReader:
        def __enter__(self):
            self.file = original(root / "later.py", "rb")
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self):
            contents = self.file.read()
            with original(root / "__init__.py", "wb") as target:
                target.write(b"VALUE = 'changed after its hash was recorded'\n")
            return contents

    monkeypatch.setattr(
        Path, "open",
        lambda path, *args, **kwargs: LaterReader()
        if path.name == "later.py" else original(path, *args, **kwargs),
    )
    identity = I.capture_source_identity(root).as_dict()
    assert identity["complete"] is False
    assert identity["error"] == "python_sources_changed_during_read"


@pytest.mark.parametrize(
    "patch",
    [
        {"schema": "older-schema"}, {"scope": "only_one_file"},
        {"complete": False}, {"complete": 1}, {"error": "unreadable"},
        {"sha256": None}, {"sha256": ""}, {"sha256": "z" * 64},
        {"file_count": 0}, {"file_count": True}, {"file_count": "1"},
    ],
)
def test_malformed_or_partial_identity_cannot_pass_by_matching_itself(tmp_path, patch):
    complete = I.capture_source_identity(_tiny_package(tmp_path)).as_dict()
    invalid = {**complete, **patch}
    assert I.compare_source_identities(invalid, invalid) == "unverifiable"
    assert I.compare_source_identities(invalid, complete) == "unverifiable"
    assert I.compare_source_identities(complete, invalid) == "unverifiable"


@pytest.mark.parametrize("invalid", [None, {}, [], "a" * 64, 123])
def test_legacy_or_missing_identity_is_unknown(invalid):
    assert I.compare_source_identities(invalid, invalid) == "unverifiable"


def test_same_digest_with_different_file_count_is_not_accepted(tmp_path):
    identity = I.capture_source_identity(_tiny_package(tmp_path)).as_dict()
    different = {**identity, "file_count": identity["file_count"] + 1}
    assert I.compare_source_identities(identity, different) == "stale_process"


@pytest.mark.parametrize("restore_access", [False, True])
@pytest.mark.parametrize("unreadable_name", ["server.py", *IMPORTED_JAVASCRIPT])
def test_unreadable_startup_snapshot_cannot_be_refreshed_by_a_later_request(
    tmp_path, restore_access, unreadable_name,
):
    script = r'''
import json
import sys
from pathlib import Path
original_open = Path.open
def unreadable(path, *args, **kwargs):
    if path == Path(sys.argv[1]) / sys.argv[3]:
        raise PermissionError("test")
    return original_open(path, *args, **kwargs)
Path.open = unreadable
import browsertap_mcp
from browsertap_mcp import runtime_identity as I
assert Path(browsertap_mcp.__file__).parent == Path(sys.argv[1])
if sys.argv[2] == "restore":
    Path.open = original_open
loaded = I.loaded_source_identity()
current = I.current_source_identity()
print(json.dumps({"loaded": loaded, "current": current,
                  "verdict": I.compare_source_identities(loaded, current)}))
'''
    result = _run_package_script(
        _package_copy(tmp_path), script, "restore" if restore_access else "keep_unreadable",
        unreadable_name,
    )
    assert result["loaded"]["complete"] is False
    assert result["current"]["complete"] is restore_access
    assert result["verdict"] == "unverifiable"


@pytest.mark.parametrize("changed_name", ["server.py", *IMPORTED_JAVASCRIPT])
def test_package_reload_does_not_replace_the_original_snapshot(tmp_path, changed_name):
    script = r'''
import importlib
import json
import sys
from pathlib import Path
import browsertap_mcp
from browsertap_mcp import runtime_identity as I
original = I.loaded_source_identity()
source = Path(browsertap_mcp.__file__).parent / sys.argv[2]
source.write_bytes(source.read_bytes() + b"\n" + (b"#" if source.suffix == ".py" else b"//") + b" edited source\n")
importlib.reload(browsertap_mcp)
importlib.reload(I)
print(json.dumps({"original": original, "loaded": I.loaded_source_identity(),
                  "verdict": I.compare_source_identities(I.loaded_source_identity(),
                                                        I.current_source_identity())}))
'''
    result = _run_package_script(_package_copy(tmp_path), script, changed_name)
    assert result["loaded"] == result["original"]
    assert result["verdict"] == "stale_process"


@pytest.mark.parametrize("asset_name", IMPORTED_JAVASCRIPT)
def test_imported_asset_bytes_are_part_of_the_source_identity(tmp_path, asset_name):
    root = _tiny_package(tmp_path)
    before = I.capture_source_identity(root)
    asset = root / asset_name
    asset.write_bytes(asset.read_bytes() + b"// changed cached behavior\n")
    after = I.capture_source_identity(root)
    assert before.sha256 != after.sha256
    assert I.compare_source_identities(before.as_dict(), after.as_dict()) == "stale_process"


@pytest.mark.parametrize("asset_name", IMPORTED_JAVASCRIPT)
@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_required_imported_assets_cannot_be_omitted_from_a_complete_snapshot(
    tmp_path, monkeypatch, asset_name, failure,
):
    root = _tiny_package(tmp_path)
    asset = root / asset_name
    original = Path.open
    if failure == "missing":
        asset.unlink()
    else:
        def unreadable(path, *args, **kwargs):
            if path == asset:
                raise PermissionError("private asset path must not be published")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", unreadable)
    identity = I.capture_source_identity(root).as_dict()
    assert identity["complete"] is False
    assert identity["sha256"] is None
    assert identity["error"] == "python_sources_unreadable"
    assert "private asset path" not in json.dumps(identity)
    assert I.compare_source_identities(identity, identity) == "unverifiable"


@pytest.mark.parametrize("mutation", ["during_asset_read", "after_asset_read"])
def test_imported_asset_changes_during_snapshot_are_unverifiable(tmp_path, monkeypatch, mutation):
    root = _tiny_package(tmp_path)
    target = root / "chrome_extension/guarded_eval.js"
    trigger = target if mutation == "during_asset_read" else root / "page_scripts/page_outline.js"
    original = Path.open

    class MutatingReader:
        def __enter__(self):
            self.file = original(trigger, "rb")
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self):
            content = self.file.read()
            with original(target, "wb") as asset:
                asset.write(b"// changed while snapshotting imported JavaScript\n")
            return content

    monkeypatch.setattr(
        Path, "open", lambda path, *args, **kwargs:
        MutatingReader() if path == trigger else original(path, *args, **kwargs),
    )
    identity = I.capture_source_identity(root).as_dict()
    assert identity["complete"] is False
    assert identity["error"] == "python_sources_changed_during_read"


@pytest.mark.parametrize("legacy", [
    {"schema": "btap.python-source.v1"}, {"scope": "package_python_sources"},
    {"schema": "btap.python-source.v1", "scope": "package_python_sources"},
])
def test_python_only_identity_cannot_verify_the_imported_asset_scope(tmp_path, legacy):
    current = I.capture_source_identity(_tiny_package(tmp_path)).as_dict()
    old = {**current, **legacy}
    assert I.compare_source_identities(old, old) == "unverifiable"
    assert I.compare_source_identities(old, current) == "unverifiable"
    assert I.compare_source_identities(current, old) == "unverifiable"


def test_snapshot_retains_its_capture_schema_and_scope(tmp_path, monkeypatch):
    snapshot = I.capture_source_identity(_tiny_package(tmp_path))
    sealed = snapshot.as_dict()
    # A frozen snapshot can outlive the module generation that created it.
    monkeypatch.setattr(I, "SOURCE_IDENTITY_SCHEMA", "future-schema")
    monkeypatch.setattr(I, "_SOURCE_SCOPE", "future-scope")
    assert snapshot.as_dict() == sealed


@pytest.mark.parametrize("component", ["mcp", "bridge"])
def test_python_only_identity_cannot_certify_diagnostics_or_live_preflight(monkeypatch, component):
    from browsertap_mcp import __version__
    from browsertap_mcp import server as S
    from tests import live_preflight as P

    current = I.current_source_identity()
    legacy = {
        **current,
        "schema": "btap.python-source.v1",
        "scope": "package_python_sources",
    }

    class Driver:
        is_remote = True
        default_session_id = None

        def diagnose(self, timeout=None):
            return {
                "cause": "healthy", "ok": True,
                "bridge_version": __version__, "extension_version": __version__,
                "protocol_version": 3,
                "bridge_source_identity": legacy if component == "bridge" else current,
                "extension_capabilities": {
                    "content_command_channel_removed": True, "batch_result_guard": True,
                },
                "extension_build_stamp": S.compute_extension_stamp(S.chrome_extension_dir()),
            }

    monkeypatch.setattr(S, "get_driver", lambda: Driver())
    monkeypatch.setattr(S, "compact_tabs", lambda **kwargs: [])
    monkeypatch.setattr(S, "loaded_source_identity", lambda: legacy if component == "mcp" else current)
    status = S.get_setup_status()
    # Connection health cannot certify the source bytes used by either process.
    assert status["status"] == "healthy"
    assert status["action"] == "none"
    assert status[f"{component}_build_verdict"] == "unverifiable"
    assert status[f"{component}_build_enforced"] is False
    other = "bridge" if component == "mcp" else "mcp"
    assert status[f"{other}_build_verdict"] == "matches_tree"
    assert status[f"{other}_build_enforced"] is True
    assert status["extension_build_verdict"] == "matches_tree"
    reason = P.stale_component_reason(status)
    assert reason is not None
    assert ("this MCP process" if component == "mcp" else "the bridge daemon") in reason


@pytest.mark.parametrize("asset_name", IMPORTED_JAVASCRIPT)
def test_same_version_import_cached_asset_edit_requires_only_mcp_restart(tmp_path, asset_name):
    script = r'''
import json
import sys
from pathlib import Path
import browsertap_mcp as package
from browsertap_mcp import server as S, simphtml as H
from browsertap_mcp.extension_build import write_extension_stamp
from browsertap_mcp.runtime_identity import current_source_identity

root = Path(package.__file__).parent
assert root == Path(sys.argv[1]), root
name = sys.argv[2]
def cached_asset():
    return {
        "chrome_extension/result_serialization.js": S._RESULT_SERIALIZER_SOURCE,
        "chrome_extension/guarded_eval.js": S._GUARDED_EVAL_SOURCE,
        "page_scripts/page_outline.js": H.js_page_outline,
        "page_scripts/list_groups.js": H.js_list_groups,
    }[name]
loaded_asset = cached_asset()
asset = root / name
asset.write_bytes(asset.read_bytes() + b"\n// same-version asset edit after MCP import\n")
if asset.parent.name == "chrome_extension":
    write_extension_stamp(root / "chrome_extension")

class Driver:
    is_remote = True
    default_session_id = None
    def diagnose(self, timeout=None):
        return {
            "cause": "healthy", "ok": True,
            "bridge_version": package.__version__,
            "extension_version": package.__version__, "protocol_version": 3,
            "bridge_source_identity": current_source_identity(),
            "extension_capabilities": {
                "content_command_channel_removed": True, "batch_result_guard": True,
            },
            "extension_build_stamp": S.compute_extension_stamp(S.chrome_extension_dir()),
        }
S.get_driver = lambda: Driver()
S.compact_tabs = lambda **kwargs: []
status = S.get_setup_status()
print(json.dumps({
    "status": {key: status[key] for key in (
        "status", "action", "mcp_build_verdict", "bridge_build_verdict", "extension_build_verdict",
        "mcp_build_enforced", "bridge_build_enforced", "restart_mcp_session_required",
        "restart_bridge_required", "reload_extension_required",
    )},
    "still_uses_import_cache": cached_asset() == loaded_asset,
    "loaded_asset_differs_from_disk": cached_asset().rstrip("\n") != asset.read_text(encoding="utf-8").rstrip("\n"),
}))
'''
    package = _package_copy(tmp_path)
    result = _run_package_script(package, script, asset_name)
    status = result["status"]
    assert result["still_uses_import_cache"] is True
    assert result["loaded_asset_differs_from_disk"] is True
    assert status["status"] == "stale_package"
    assert status["action"] == "restart_mcp_session"
    assert status["mcp_build_verdict"] == "stale_process"
    assert status["bridge_build_verdict"] == status["extension_build_verdict"] == "matches_tree"
    assert status["mcp_build_enforced"] is status["bridge_build_enforced"] is True
    assert status["restart_mcp_session_required"] is True
    assert status["restart_bridge_required"] is status["reload_extension_required"] is False
    fresh = _subprocess_status(package, "none", fresh_bridge=True)
    assert fresh["status"] == "healthy"
    assert fresh["mcp_build_verdict"] == fresh["bridge_build_verdict"] == "matches_tree"
