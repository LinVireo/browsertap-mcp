"""A recoverable subtree must exist before any bookmark deletion is dispatched."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from browsertap_mcp import bookmark_backup as B
from browsertap_mcp import server as S

SUBTREE = {
    "id": "folder-7", "parentId": "root", "index": 2, "title": "Synthetic folder",
    "children": [{"id": "bookmark-8", "title": "Example", "url": "https://example.test/"}],
}


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("enveloped", [True, False])
def test_removal_pins_the_client_and_saves_the_subtree_first(monkeypatch, state, enveloped):
    calls = []

    class Driver:
        def select_client_id(self, **kwargs):
            return "synthetic-browser"

        def ext_cmd(self, command, **kwargs):
            calls.append((command, kwargs))
            assert kwargs["client_id"] == "synthetic-browser"
            if command["method"] == "tree":
                data = [{"id": "root", "children": [SUBTREE]}]
                return {"data": {"ok": True, "data": data} if enveloped else data}
            backups = list((state / "bookmark-backups").glob("*.json"))
            assert len(backups) == 1, "the backup must already exist at deletion dispatch"
            assert json.loads(backups[0].read_text(encoding="utf-8"))["subtree"] == SUBTREE
            return {"data": {"ok": True}}

    monkeypatch.setattr(S, "require_driver", Driver)
    result = S.remove_bookmark("folder-7", recursive=True)
    assert result["status"] == "ok", result
    content = Path(result["backup_path"]).read_bytes()
    assert result["backup_sha256"] == hashlib.sha256(content).hexdigest()
    assert json.loads(content)["client_id"] == "synthetic-browser"
    assert [command["method"] for command, _ in calls] == ["tree", "removeTree"]


@pytest.mark.parametrize("reply", [{}, {"data": None}, {"data": []}, {"data": {"ok": False, "error": "unavailable"}}])
def test_missing_backup_data_refuses_deletion(monkeypatch, state, reply):
    calls = []

    class Driver:
        def ext_cmd(self, command, **kwargs):
            calls.append(command)
            return reply

    monkeypatch.setattr(S, "require_driver", Driver)
    result = S.remove_bookmark("folder-7", session_id="synthetic-browser:7")
    assert result["status"] == "error"
    assert result["dispatched"] is False
    assert result["retry_safe"] is True
    assert [command["method"] for command in calls] == ["tree"]
    assert not list(state.rglob("*.json"))


def test_backup_write_failure_prevents_delete_and_leaves_no_partial_file(monkeypatch, state):
    calls = []

    class Driver:
        def ext_cmd(self, command, **kwargs):
            calls.append(command)
            return {"data": {"ok": True, "data": [SUBTREE]}}

    def denied(*args):
        raise PermissionError("synthetic replace denied")

    monkeypatch.setattr(S, "require_driver", Driver)
    monkeypatch.setattr(B.os, "replace", denied)
    result = S.remove_bookmark("folder-7", recursive=True, session_id="synthetic-browser:7")
    assert result["code"] == "bookmark_backup_failed"
    assert result["dispatched"] is False
    assert [command["method"] for command in calls] == ["tree"]
    assert not list((state / "bookmark-backups").iterdir())


def test_uncertain_deletion_keeps_the_backup_and_original_operation(monkeypatch, state):
    calls = []

    class Driver:
        def ext_cmd(self, command, **kwargs):
            calls.append(command)
            if command["method"] == "tree":
                return {"data": [SUBTREE]}
            raise S.BridgeNoResponseError(
                "reply lost", delivery_state="sent_unconfirmed", retry_safe=False,
                operation_id="original-delete", reservation_held=True,
            )

    monkeypatch.setattr(S, "require_driver", Driver)
    result = S.remove_bookmark("folder-7", recursive=True, session_id="synthetic-browser:7")
    assert result["status"] == "error"
    assert result["retry_safe"] is False
    assert result["operation_id"] == "original-delete"
    assert Path(result["backup_path"]).is_file()
    assert [command["method"] for command in calls] == ["tree", "removeTree"]


def test_backup_retention_limits_count_bytes_and_age_without_touching_other_files(monkeypatch, state):
    monkeypatch.setattr(B, "MAX_BACKUPS", 2)
    first = Path(B.save_bookmark_backup(SUBTREE, client_id="test", recursive=True)["backup_path"])
    directory = first.parent
    unrelated = directory / "user-notes.json"
    unrelated.write_text("preserve", encoding="utf-8")
    os.utime(first, (1, 1))
    second = Path(B.save_bookmark_backup(SUBTREE, client_id="test", recursive=True)["backup_path"])
    assert not first.exists()
    third = Path(B.save_bookmark_backup(SUBTREE, client_id="test", recursive=True)["backup_path"])
    size = third.stat().st_size
    monkeypatch.setattr(B, "MAX_STORE_BYTES", size + 20)
    latest = Path(B.save_bookmark_backup(SUBTREE, client_id="test", recursive=True)["backup_path"])
    assert not second.exists() and not third.exists()
    assert list(directory.glob("btap-bookmarks-*.json")) == [latest]
    assert unrelated.read_text(encoding="utf-8") == "preserve"


def test_oversized_subtree_does_not_create_a_backup(monkeypatch, state):
    monkeypatch.setattr(B, "MAX_BACKUP_BYTES", 16)
    with pytest.raises(ValueError, match="size limit"):
        B.save_bookmark_backup(SUBTREE, client_id="test", recursive=True)
    assert not list(state.rglob("*.json"))


@pytest.mark.parametrize("tree", [None, ["bad"], [{"id": "x", "children": None}], [SUBTREE, SUBTREE]])
def test_malformed_or_ambiguous_tree_cannot_be_backed_up(tree):
    with pytest.raises(ValueError):
        B.bookmark_subtree(tree, "folder-7")


DIRECTORY_LINKS = ["symlink"]
if os.name == "nt":
    DIRECTORY_LINKS.append("junction")


def _make_directory_link(link, target, kind):
    if kind == "symlink":
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows directory symlink privilege is unavailable")
            raise
        assert link.is_symlink()
    else:
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=True, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _populate_backup_directory(directory):
    directory.mkdir()
    expired = directory / ("btap-bookmarks-" + "a" * 32 + ".json")
    expired.write_text("synthetic expired backup", encoding="utf-8")
    os.utime(expired, (1, 1))
    (directory / (".btap-bookmarks-" + "b" * 32 + ".tmp")).write_text(
        "synthetic partial backup", encoding="utf-8",
    )
    (directory / "user-notes.txt").write_text("synthetic preserve", encoding="utf-8")
    (directory / "nested").mkdir()
    (directory / "nested/keep.txt").write_text("synthetic nested preserve", encoding="utf-8")


def _directory_snapshot(directory):
    return {
        str(path.relative_to(directory)): (
            ("file", path.read_bytes(), path.stat().st_mtime_ns) if path.is_file()
            else ("directory",)
        )
        for path in directory.rglob("*")
    }


def _remove_with_synthetic_driver(monkeypatch, *, recursive=True, subtree=None):
    subtree = SUBTREE if subtree is None else subtree
    calls = []

    class Driver:
        def ext_cmd(self, command, **kwargs):
            calls.append(command["method"])
            return {"data": [subtree]} if command["method"] == "tree" else {"data": {"ok": True}}

    monkeypatch.setattr(S, "require_driver", Driver)
    result = S.remove_bookmark(subtree["id"], recursive=recursive, session_id="synthetic-browser:7")
    return result, calls


@pytest.mark.parametrize("kind", DIRECTORY_LINKS)
@pytest.mark.parametrize("recursive", [False, True])
def test_linked_backup_directory_refuses_deletion_and_preserves_target(
    monkeypatch, state, kind, recursive,
):
    configured = state / "configured"
    configured.mkdir()
    target = state / "outside-state"
    _populate_backup_directory(target)
    before = _directory_snapshot(target)
    _make_directory_link(configured / "bookmark-backups", target, kind)
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(configured))

    result, calls = _remove_with_synthetic_driver(monkeypatch, recursive=recursive)

    assert result.get("code") == "bookmark_backup_failed", result
    assert result["dispatched"] is False and result["retry_safe"] is True
    assert "backup_path" not in result
    assert calls == ["tree"]
    assert _directory_snapshot(target) == before


@pytest.mark.parametrize("fault", ["unreadable", "missing", "not_directory", "other_reparse"])
def test_backup_directory_metadata_failure_prevents_all_storage_changes(monkeypatch, state, fault):
    directory = state / "bookmark-backups"
    _populate_backup_directory(directory)
    before = _directory_snapshot(directory)
    real_lstat = Path.lstat

    def metadata(path, *args, **kwargs):
        if path == directory:
            if fault == "unreadable":
                raise PermissionError("synthetic backup directory metadata denied")
            if fault == "missing":
                raise FileNotFoundError("synthetic backup directory disappeared")
            return SimpleNamespace(
                st_mode=stat.S_IFREG if fault == "not_directory" else stat.S_IFDIR,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if fault == "other_reparse" else 0,
            )
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", metadata)
    result, calls = _remove_with_synthetic_driver(monkeypatch)

    assert result.get("code") == "bookmark_backup_failed", result
    assert result["dispatched"] is False and result["retry_safe"] is True
    assert "backup_path" not in result
    assert calls == ["tree"]
    assert _directory_snapshot(directory) == before


@pytest.mark.parametrize("kind", DIRECTORY_LINKS)
def test_explicit_state_root_link_keeps_its_configured_resolution(monkeypatch, state, kind):
    target = state / "configured-state-target"
    target.mkdir()
    sentinel = target / "user-notes.txt"
    sentinel.write_text("synthetic preserve", encoding="utf-8")
    configured = state / "configured-state"
    _make_directory_link(configured, target, kind)
    monkeypatch.setenv("BROWSERTAP_STATE_DIR", str(configured))

    result = B.save_bookmark_backup(SUBTREE, client_id="synthetic-browser", recursive=True)

    assert Path(result["backup_path"]).parent == target.resolve() / "bookmark-backups"
    assert json.loads(Path(result["backup_path"]).read_text(encoding="utf-8"))["subtree"] == SUBTREE
    assert sentinel.read_text(encoding="utf-8") == "synthetic preserve"


@pytest.mark.parametrize(("field", "value"), [
    pytest.param("title", "\ud800", id="high-title"),
    pytest.param("title", "\udfff", id="low-title"),
    pytest.param("url", "\udbff", id="high-url"),
    pytest.param("url", "\udc00", id="low-url"),
    pytest.param("key", "\ud800", id="high-key"),
    pytest.param("key", "\udfff", id="low-key"),
    pytest.param("title", "\ud83d\ude00", id="paired-title"),
    pytest.param("key", "\ud83d\ude00", id="paired-key"),
])
def test_json_bookmark_unicode_is_preserved_before_deletion(monkeypatch, state, field, value):
    subtree = dict(SUBTREE)
    value = "中文" + value + "😀" + r"\ud800" + '"\n'
    if field == "key":
        subtree[value] = {"nested": "中文😀", "literal": r"\ud800"}
    else:
        subtree[field] = value
    # Match the browser's JSON boundary, including paired-surrogate decoding.
    subtree = json.loads(json.dumps(subtree))

    result, calls = _remove_with_synthetic_driver(monkeypatch, subtree=subtree)

    assert result["status"] == "ok", result
    assert calls == ["tree", "removeTree"]
    content = Path(result["backup_path"]).read_bytes()
    assert json.loads(content.decode("utf-8"))["subtree"] == subtree
    assert "中文".encode("utf-8") in content and "😀".encode("utf-8") in content
    assert result["backup_bytes"] == len(content)
    assert result["backup_sha256"] == hashlib.sha256(content).hexdigest()


def test_scalar_unicode_backup_keeps_the_existing_utf8_bytes(monkeypatch, state):
    now = 2_000_000_000.25
    monkeypatch.setattr(B.time, "time", lambda: now)
    subtree = dict(SUBTREE, title="中文😀e\u0301\U00010000\U0010ffff")

    result = B.save_bookmark_backup(subtree, client_id="synthetic-browser", recursive=True)

    expected = (json.dumps({
        "schema": "btap.bookmark-backup.v1", "captured_at": now,
        "client_id": "synthetic-browser", "bookmark_id": subtree["id"],
        "recursive": True, "subtree": subtree,
    }, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    assert Path(result["backup_path"]).read_bytes() == expected
    assert result["backup_bytes"] == len(expected)
    assert result["backup_sha256"] == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("limit", ["MAX_BACKUP_BYTES", "MAX_STORE_BYTES"])
@pytest.mark.parametrize("title", ["中文😀", "中文\ud800😀"], ids=["scalar", "unpaired"])
def test_unicode_backup_budget_uses_published_bytes_and_refuses_delete_below_limit(
    monkeypatch, state, limit, title,
):
    monkeypatch.setattr(B.time, "time", lambda: 2_000_000_000.25)
    subtree = dict(SUBTREE, title=title)
    first = B.save_bookmark_backup(subtree, client_id="synthetic-browser", recursive=True)
    path = Path(first["backup_path"])
    size = len(path.read_bytes())
    before = _directory_snapshot(path.parent)
    monkeypatch.setattr(B, limit, size - 1)

    refused, calls = _remove_with_synthetic_driver(monkeypatch, subtree=subtree)

    assert refused["code"] == "bookmark_backup_failed"
    assert "size limit" in refused["error"]
    assert refused["dispatched"] is False and refused["retry_safe"] is True
    assert calls == ["tree"]
    assert _directory_snapshot(path.parent) == before

    monkeypatch.setattr(B, limit, size)
    accepted, calls = _remove_with_synthetic_driver(monkeypatch, subtree=subtree)

    assert accepted["status"] == "ok", accepted
    assert calls == ["tree", "removeTree"]
    assert accepted["backup_bytes"] == size == len(Path(accepted["backup_path"]).read_bytes())


def test_surrogate_backup_publication_failure_still_prevents_deletion(monkeypatch, state):
    reached_publish = []

    def denied(source, destination):
        reached_publish.append((source, destination))
        raise PermissionError("synthetic publication denied")

    monkeypatch.setattr(B.os, "replace", denied)
    subtree = dict(SUBTREE, title="中文\udfff😀")

    result, calls = _remove_with_synthetic_driver(monkeypatch, subtree=subtree)

    assert len(reached_publish) == 1
    assert result["code"] == "bookmark_backup_failed"
    assert result["dispatched"] is False and result["retry_safe"] is True
    assert calls == ["tree"]
    assert not list((state / "bookmark-backups").iterdir())
