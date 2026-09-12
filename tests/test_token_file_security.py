"""Token security at the filesystem boundary, using only throwaway state."""
from __future__ import annotations

import os
import sys
import traceback
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from browsertap_mcp import _token_file as F
from browsertap_mcp import browser_bridge as B


def test_posix_creation_keeps_exclusive_mode_0600(monkeypatch, tmp_path):
    monkeypatch.setattr(F, "_WINDOWS", False)
    original_open = os.open
    calls = []

    def open_file(path, flags, mode):
        calls.append((flags, mode))
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", open_file)
    path = tmp_path / "posix-token"
    assert B._persist_token(path, "synthetic-posix") == "synthetic-posix"
    assert calls == [(os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)]
    assert F.read_text(path) == "synthetic-posix\n"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_storage_error_has_no_secret_exception_chain(monkeypatch, tmp_path):
    secret = "synthetic-private-io-error"

    @contextmanager
    def fail(path):
        raise OSError(secret)
        yield  # pragma: no cover - make this the same context-manager interface

    monkeypatch.setattr(F, "create", fail)
    with pytest.raises(RuntimeError, match="cannot persist") as failure:
        B._persist_token(tmp_path / "token", "synthetic-input")
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__
    assert secret not in "".join(traceback.format_exception(failure.value))


def test_unencodable_bootstrap_token_does_not_escape_in_an_encoding_exception(tmp_path):
    secret = "synthetic-bootstrap-secret-\ud800"
    path = tmp_path / "token"
    with pytest.raises(RuntimeError, match="invalid_utf8") as failure:
        B._persist_token(path, secret)
    assert "synthetic-bootstrap-secret" not in repr(failure.value)
    assert "synthetic-bootstrap-secret" not in "".join(traceback.format_exception(failure.value))
    if os.name == "nt":
        assert not path.exists()


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("failure_at", [None, "open", "regular", "protect", "verify", "transfer", "body"])
def test_windows_handle_ownership_and_failure_order_without_native_apis(
    monkeypatch, tmp_path, create, failure_at,
):
    events = []

    def step(name, result=None):
        events.append(name)
        if failure_at == name:
            raise PermissionError("synthetic native failure")
        return result

    api = SimpleNamespace(
        open=lambda path, create: step("open", 17),
        check_regular_file=lambda handle: step("regular"),
        protect_existing=lambda handle: step("protect"),
        verify=lambda handle: step("verify"),
        discard_created=lambda handle: step("discard"),
        kernel=SimpleNamespace(CloseHandle=lambda handle: step("close_handle")),
    )
    monkeypatch.setattr(F, "_WindowsSecurity", lambda: api)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        open_osfhandle=lambda handle, flags: step("transfer", 42),
    ))
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: step("close_fd"))
    sequence = ["open", "regular", *([] if create else ["protect"]), "verify", "transfer", "body"]
    should_fail = failure_at in sequence
    try:
        with F._windows_file(tmp_path / "token", create=create) as fd:
            assert fd == 42
            step("body")
    except PermissionError:
        assert should_fail
    else:
        assert not should_fail
    expected = sequence if not should_fail else sequence[:sequence.index(failure_at) + 1]
    if failure_at != "open":
        if create and should_fail:
            expected.append("discard")
        expected.append("close_fd" if "transfer" in events and failure_at != "transfer" else "close_handle")
    assert events == expected


# Windows-only tests are not collected on POSIX; canonical evidence forbids skips.
if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    def _security_snapshot(handle):
        """Independently inspect the kernel descriptor, not production helpers."""
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.c_void_p
        api.GetSecurityInfo.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
            ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
            ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ]
        api.GetSecurityInfo.restype = wintypes.DWORD
        api.GetSecurityDescriptorControl.argtypes = [
            pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
        ]
        api.GetSecurityDescriptorControl.restype = wintypes.BOOL
        api.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
        api.GetAce.restype = wintypes.BOOL
        api.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(pointer)]
        api.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [pointer]
        kernel.LocalFree.restype = pointer

        def sid_text(sid):
            text = pointer()
            assert api.ConvertSidToStringSidW(sid, ctypes.byref(text))
            try:
                return ctypes.wstring_at(text)
            finally:
                kernel.LocalFree(text)

        owner, dacl, descriptor = pointer(), pointer(), pointer()
        assert api.GetSecurityInfo(
            handle, 1, 0x5, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor),
        ) == 0
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            assert api.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision),
            )
            entries = []
            if dacl:
                count = ctypes.c_ushort.from_address(dacl.value + 4).value
                for index in range(count):
                    ace = pointer()
                    assert api.GetAce(dacl, index, ctypes.byref(ace))
                    entries.append({
                        "type": ctypes.c_ubyte.from_address(ace.value).value,
                        "flags": ctypes.c_ubyte.from_address(ace.value + 1).value,
                        "mask": ctypes.c_uint32.from_address(ace.value + 4).value,
                        "sid": sid_text(ace.value + 8),
                    })
            return {"protected": bool(control.value & 0x1000),
                    "owner": sid_text(owner), "entries": entries}
        finally:
            kernel.LocalFree(descriptor)

    def _assert_private(handle):
        security = _security_snapshot(handle)
        assert security["protected"], security
        assert security["entries"] == [
            {"type": 0, "flags": 0, "mask": 0x1F01FF, "sid": security["owner"]},
        ], security

    def test_created_windows_token_has_a_protected_owner_only_dacl(tmp_path):
        path = tmp_path / "bridge-token"
        assert B._persist_token(path, "synthetic-created") == "synthetic-created"
        with path.open("rb") as stream:
            _assert_private(msvcrt.get_osfhandle(stream.fileno()))

    @pytest.mark.parametrize("name", ["ordinary", "中文-\U0001f600", "high-\ud800", "low-\udfff"])
    def test_windows_private_io_preserves_native_unicode_filenames(tmp_path, name):
        path = tmp_path / name
        assert B._persist_token(path, "synthetic-unicode") == "synthetic-unicode"
        assert B._read_token_file(path) == "synthetic-unicode"
        with path.open("rb") as stream:
            _assert_private(msvcrt.get_osfhandle(stream.fileno()))

    def test_windows_token_is_private_before_the_first_secret_byte(monkeypatch, tmp_path):
        path = tmp_path / "bridge-token"
        writes = []
        original_write = os.write

        def check_then_write(fd, payload):
            _assert_private(msvcrt.get_osfhandle(fd))
            writes.append(len(payload))
            return original_write(fd, payload)

        monkeypatch.setattr(os, "write", check_then_write)
        assert B._persist_token(path, "synthetic-before-write") == "synthetic-before-write"
        assert writes

    def test_existing_windows_token_is_hardened_without_replacement(monkeypatch, tmp_path):
        path = tmp_path / "bridge-token"
        path.write_bytes(b"synthetic-existing\n")
        before = path.stat().st_ino
        monkeypatch.setenv(B.TOKEN_FILE_ENV, str(path))
        monkeypatch.delenv(B.TOKEN_AUTH_ENV, raising=False)
        monkeypatch.setenv(B.TOKEN_ENV, "synthetic-should-not-win")
        assert B.bridge_token() == "synthetic-existing"
        assert path.stat().st_ino == before
        assert path.read_bytes() == b"synthetic-existing\n"
        with path.open("rb") as stream:
            _assert_private(msvcrt.get_osfhandle(stream.fileno()))

    @pytest.mark.parametrize("stage", ["protect_existing", "verify"])
    def test_unverifiable_existing_file_is_never_read_or_overwritten(monkeypatch, tmp_path, stage):
        path = tmp_path / "bridge-token"
        original = b"synthetic-existing-secret\n"
        path.write_bytes(original)
        identity = path.stat().st_ino
        monkeypatch.setenv(B.TOKEN_FILE_ENV, str(path))
        monkeypatch.delenv(B.TOKEN_AUTH_ENV, raising=False)

        def fail(*args):
            raise PermissionError("synthetic-existing-secret")

        monkeypatch.setattr(F._WindowsSecurity, stage, fail)
        with monkeypatch.context() as patch:
            patch.setattr(os, "read", lambda *args: pytest.fail("read before security verification"))
            patch.setattr(os, "write", lambda *args: pytest.fail("overwrote an existing token"))
            state = B._token_file_state(path)
            assert state.status == "unreadable"
            assert state.value == ""
            with pytest.raises(RuntimeError, match="unreadable") as failure:
                B.bridge_token()
            assert "synthetic-existing-secret" not in "".join(traceback.format_exception(failure.value))
        assert path.stat().st_ino == identity
        assert path.read_bytes() == original

    def test_successful_acl_set_is_read_back_before_consuming_existing_token(monkeypatch, tmp_path):
        path = tmp_path / "bridge-token"
        path.write_bytes(b"synthetic-existing\n")
        api = F._WindowsSecurity()
        # A faulty filesystem/API that reports success but leaves the inherited ACL.
        monkeypatch.setattr(api.security, "SetSecurityInfo", lambda *args: 0)
        monkeypatch.setattr(F, "_WindowsSecurity", lambda: api)
        with monkeypatch.context() as patch:
            patch.setattr(os, "read", lambda *args: pytest.fail("unverified token read"))
            state = B._token_file_state(path)
        assert state.status == "unreadable"
        assert state.value == ""
        assert path.read_bytes() == b"synthetic-existing\n"

    def test_another_owner_is_refused_before_mutating_security(monkeypatch, tmp_path):
        path = tmp_path / "bridge-token"
        path.write_bytes(b"synthetic-existing\n")
        api = F._WindowsSecurity()
        with path.open("rb") as stream:
            before = _security_snapshot(msvcrt.get_osfhandle(stream.fileno()))

        def deny_owner(owner):
            raise PermissionError("synthetic owner mismatch")

        monkeypatch.setattr(api, "check_owner", deny_owner)
        monkeypatch.setattr(api.security, "SetSecurityInfo", lambda *args: pytest.fail("changed foreign ACL"))
        monkeypatch.setattr(F, "_WindowsSecurity", lambda: api)
        assert B._token_file_state(path).status == "unreadable"
        assert path.read_bytes() == b"synthetic-existing\n"
        with path.open("rb") as stream:
            assert _security_snapshot(msvcrt.get_osfhandle(stream.fileno())) == before

    def test_failed_initial_security_verification_removes_only_the_new_file(monkeypatch, tmp_path):
        path = tmp_path / "bridge-token"
        secret = "synthetic-new-secret"

        def fail(*args):
            raise PermissionError("synthetic-new-secret")

        monkeypatch.setattr(F._WindowsSecurity, "verify", fail)
        monkeypatch.setattr(os, "write", lambda *args: pytest.fail("wrote before ACL verification"))
        with pytest.raises(RuntimeError, match="cannot persist") as failure:
            B._persist_token(path, secret)
        assert not path.exists()
        assert "synthetic-new-secret" not in "".join(traceback.format_exception(failure.value))

    @pytest.mark.parametrize("delete_fails", [False, True])
    def test_failed_partial_write_is_deleted_or_emptied_under_its_private_acl(
        monkeypatch, tmp_path, delete_fails,
    ):
        path = tmp_path / "bridge-token"
        secret = "synthetic-partial-secret"
        api = F._WindowsSecurity()
        if delete_fails:
            monkeypatch.setattr(api.kernel, "SetFileInformationByHandle", lambda *args: False)
        monkeypatch.setattr(F, "_WindowsSecurity", lambda: api)
        original_write = os.write
        writes = []

        def partial_then_fail(fd, payload):
            _assert_private(msvcrt.get_osfhandle(fd))
            if writes:
                raise OSError("synthetic-partial-secret")
            writes.append(True)
            return original_write(fd, payload[:3])

        monkeypatch.setattr(os, "write", partial_then_fail)
        with pytest.raises(RuntimeError, match="cannot persist") as failure:
            B._persist_token(path, secret)
        assert "synthetic-partial-secret" not in "".join(traceback.format_exception(failure.value))
        if delete_fails:
            assert path.read_bytes() == b""
            with path.open("rb") as stream:
                _assert_private(msvcrt.get_osfhandle(stream.fileno()))
        else:
            assert not path.exists()

    @pytest.mark.parametrize("entrypoint", ["read_or_create", "create"])
    def test_concurrent_starters_cannot_observe_a_partial_windows_token(monkeypatch, tmp_path, entrypoint):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        path = tmp_path / "bridge-token"
        monkeypatch.setenv(B.TOKEN_FILE_ENV, str(path))
        monkeypatch.delenv(B.TOKEN_AUTH_ENV, raising=False)
        partial, release, read_started = threading.Event(), threading.Event(), threading.Event()
        original_write, original_open = os.write, F._WindowsSecurity.open

        def delayed_write(fd, payload):
            if not partial.is_set():
                count = original_write(fd, payload[:1])
                partial.set()
                assert release.wait(2)
                return count
            return original_write(fd, payload)

        def mark_read(api, path, create):
            if not create:
                read_started.set()
            return original_open(api, path, create)

        monkeypatch.setattr(os, "write", delayed_write)
        monkeypatch.setattr(F._WindowsSecurity, "open", mark_read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(B._persist_token, path, "synthetic-complete-winner")
            try:
                assert partial.wait(1)
                reader = (pool.submit(B.bridge_token) if entrypoint == "read_or_create"
                          else pool.submit(B._persist_token, path, "synthetic-loser"))
                assert read_started.wait(1)
                assert not release.wait(0.03)
                assert not reader.done()
            finally:
                release.set()
            assert writer.result(timeout=2) == "synthetic-complete-winner"
            assert reader.result(timeout=2) == "synthetic-complete-winner"
        assert path.read_bytes() == b"synthetic-complete-winner\n"

    def test_failed_race_read_does_not_remove_or_replace_the_competitor(monkeypatch, tmp_path):
        path = tmp_path / "bridge-token"
        path.write_bytes(b"synthetic-competitor\n")
        identity = path.stat().st_ino

        def fail(*args):
            raise PermissionError("synthetic-competitor")

        monkeypatch.setattr(F._WindowsSecurity, "verify", fail)
        with pytest.raises(RuntimeError, match="unreadable") as failure:
            B._persist_token(path, "synthetic-loser")
        assert "synthetic-competitor" not in "".join(traceback.format_exception(failure.value))
        assert path.read_bytes() == b"synthetic-competitor\n"
        assert path.stat().st_ino == identity

    def test_windows_access_check_allows_owner_but_denies_a_restricted_world_token(tmp_path):
        """Use kernel access checking as well as inspecting the descriptor's ACEs."""
        path = tmp_path / "bridge-token"
        B._persist_token(path, "synthetic-effective-access")
        api = F._WindowsSecurity()
        pointer = ctypes.c_void_p
        out_pointer = ctypes.POINTER(pointer)
        dword = wintypes.DWORD
        out_dword = ctypes.POINTER(dword)

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [("sid", pointer), ("attributes", dword)]

        class GenericMapping(ctypes.Structure):
            _fields_ = [("read", dword), ("write", dword), ("execute", dword), ("all", dword)]

        signatures = (
            ("DuplicateTokenEx", [pointer, dword, pointer, ctypes.c_int, ctypes.c_int, out_pointer]),
            ("ConvertStringSidToSidW", [wintypes.LPCWSTR, out_pointer]),
            ("CreateRestrictedToken", [pointer, dword, dword, pointer, dword, pointer,
                                       dword, ctypes.POINTER(SidAndAttributes), out_pointer]),
            ("AccessCheck", [pointer, pointer, dword, ctypes.POINTER(GenericMapping),
                             pointer, out_dword, out_dword, ctypes.POINTER(wintypes.BOOL)]),
        )
        for name, argument_types in signatures:
            function = getattr(api.security, name)
            function.argtypes, function.restype = argument_types, wintypes.BOOL
        primary, owner, restricted, world, descriptor = (pointer() for _ in range(5))
        try:
            assert api.security.OpenProcessToken(api.kernel.GetCurrentProcess(), 0xA, ctypes.byref(primary))
            assert api.security.DuplicateTokenEx(primary, 0xE, None, 2, 2, ctypes.byref(owner))
            assert api.security.ConvertStringSidToSidW("S-1-1-0", ctypes.byref(world))
            sid = SidAndAttributes(world, 0)
            assert api.security.CreateRestrictedToken(
                owner, 1, 0, None, 0, None, 1, ctypes.byref(sid), ctypes.byref(restricted),
            )
            with path.open("rb") as stream:
                handle = msvcrt.get_osfhandle(stream.fileno())
                _assert_private(handle)
                assert api.security.GetSecurityInfo(
                    handle, 1, 0x7, None, None, None, None, ctypes.byref(descriptor),
                ) == 0
            mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
            for token, expected in ((owner, True), (restricted, False)):
                privileges = ctypes.create_string_buffer(1024)
                size, granted, allowed = dword(1024), dword(), wintypes.BOOL()
                assert api.security.AccessCheck(
                    descriptor, token, mapping.read, ctypes.byref(mapping), privileges,
                    ctypes.byref(size), ctypes.byref(granted), ctypes.byref(allowed),
                )
                assert bool(allowed.value) is expected
        finally:
            for handle in (primary, owner, restricted):
                if handle:
                    api.kernel.CloseHandle(handle)
            for allocation in (world, descriptor):
                if allocation:
                    api.kernel.LocalFree(allocation)
