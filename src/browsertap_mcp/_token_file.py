"""Private token-file I/O. Windows security and content use the same handle."""
from __future__ import annotations

import ctypes
import errno
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

_WINDOWS = os.name == "nt"
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_DELETE = 0x00010000
_FILE_ALL_ACCESS = 0x001F01FF
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                ("inherit", wintypes.BOOL)]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


def _unsafe_file() -> PermissionError:
    return PermissionError(errno.EACCES, "Bridge token file security could not be verified")


class _WindowsSecurity:
    """Small, lazy Win32 binding; no DLL loads on POSIX or package import."""

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        out_pointer = ctypes.POINTER(pointer)
        dword = wintypes.DWORD
        out_dword = ctypes.POINTER(dword)
        handle = wintypes.HANDLE
        boolean = wintypes.BOOL
        signatures: tuple[tuple[Any, str, Any, list[Any]], ...] = (
            (self.kernel, "GetCurrentProcess", handle, []),
            (self.kernel, "CloseHandle", boolean, [handle]),
            (self.kernel, "LocalFree", pointer, [pointer]),
            (self.kernel, "CreateFileW", handle,
             [wintypes.LPCWSTR, dword, dword, ctypes.POINTER(_SecurityAttributes),
              dword, dword, handle]),
            (self.kernel, "GetFileType", dword, [handle]),
            (self.kernel, "GetFileInformationByHandleEx", boolean,
             [handle, ctypes.c_int, pointer, dword]),
            (self.kernel, "SetFileInformationByHandle", boolean,
             [handle, ctypes.c_int, pointer, dword]),
            (self.kernel, "SetFilePointerEx", boolean,
             [handle, ctypes.c_longlong, pointer, dword]),
            (self.kernel, "SetEndOfFile", boolean, [handle]),
            (self.security, "OpenProcessToken", boolean, [handle, dword, out_pointer]),
            (self.security, "GetTokenInformation", boolean,
             [handle, ctypes.c_int, pointer, dword, out_dword]),
            (self.security, "ConvertSidToStringSidW", boolean, [pointer, out_pointer]),
            (self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", boolean,
             [wintypes.LPCWSTR, dword, out_pointer, out_dword]),
            (self.security, "GetSecurityDescriptorDacl", boolean,
             [pointer, ctypes.POINTER(boolean), out_pointer, ctypes.POINTER(boolean)]),
            (self.security, "GetSecurityDescriptorControl", boolean,
             [pointer, ctypes.POINTER(wintypes.WORD), out_dword]),
            (self.security, "GetSecurityInfo", dword,
             [handle, ctypes.c_int, dword, out_pointer, out_pointer, out_pointer,
              out_pointer, out_pointer]),
            (self.security, "SetSecurityInfo", dword,
             [handle, ctypes.c_int, dword, pointer, pointer, pointer, pointer]),
            (self.security, "IsValidAcl", boolean, [pointer]),
            (self.security, "IsValidSid", boolean, [pointer]),
            (self.security, "EqualSid", boolean, [pointer, pointer]),
            (self.security, "GetAce", boolean, [pointer, dword, out_pointer]),
        )
        for library, name, result_type, argument_types in signatures:
            function = getattr(library, name)
            function.restype, function.argtypes = result_type, argument_types
        self._user_buffer, self.user_sid, self.user_sid_text = self._current_user()

    @staticmethod
    def _check(ok: Any) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _current_user(self) -> tuple[Any, int, str]:
        token = wintypes.HANDLE()
        self._check(self.security.OpenProcessToken(
            self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token),
        ))
        try:
            size = wintypes.DWORD()
            if self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size)):
                raise _unsafe_file()
            if ctypes.get_last_error() != 122 or size.value < ctypes.sizeof(ctypes.c_void_p):
                raise _unsafe_file()
            buffer = ctypes.create_string_buffer(size.value)
            self._check(self.security.GetTokenInformation(
                token, 1, buffer, size, ctypes.byref(size),
            ))
            sid = ctypes.c_void_p.from_buffer(buffer).value
            if sid is None or not self.security.IsValidSid(sid):
                raise _unsafe_file()
            text = ctypes.c_void_p()
            self._check(self.security.ConvertSidToStringSidW(sid, ctypes.byref(text)))
            try:
                return buffer, sid, ctypes.wstring_at(text)
            finally:
                self.kernel.LocalFree(text)
        finally:
            self.kernel.CloseHandle(token)

    @contextmanager
    def descriptor(self) -> Iterator[ctypes.c_void_p]:
        descriptor = ctypes.c_void_p()
        # Protected, non-inheriting, owner-only. No broad group or inherited ACEs.
        sddl = f"O:{self.user_sid_text}D:P(A;;FA;;;{self.user_sid_text})"
        self._check(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None,
        ))
        try:
            yield descriptor
        finally:
            self.kernel.LocalFree(descriptor)

    @contextmanager
    def file_security(self, handle: int) -> Iterator[tuple[Any, Any, Any]]:
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        result = self.security.GetSecurityInfo(
            handle, 1, 0x1 | _DACL_SECURITY_INFORMATION, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if result:
            raise ctypes.WinError(result)
        try:
            yield owner, dacl, descriptor
        finally:
            self.kernel.LocalFree(descriptor)

    def check_owner(self, owner: Any) -> None:
        if (not owner or not self.security.IsValidSid(owner)
                or not self.security.EqualSid(owner, self.user_sid)):
            # Another owner retains implicit WRITE_DAC even after replacing the DACL.
            raise _unsafe_file()

    def check_regular_file(self, handle: int) -> None:
        info = _FileAttributeTagInfo()
        self._check(self.kernel.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info),
        ))
        if self.kernel.GetFileType(handle) != 1 or info.attributes & (0x10 | 0x400):
            raise _unsafe_file()  # Directory, device or final-component reparse point.

    def protect_existing(self, handle: int) -> None:
        with self.file_security(handle) as (owner, _, _):
            self.check_owner(owner)
        with self.descriptor() as descriptor:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
            self._check(self.security.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted),
            ))
            if not present or not dacl:
                raise _unsafe_file()
            result = self.security.SetSecurityInfo(
                handle, 1, _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, dacl, None,
            )
            if result:
                raise ctypes.WinError(result)

    def verify(self, handle: int) -> None:
        with self.file_security(handle) as (owner, dacl, descriptor):
            self.check_owner(owner)
            control, revision = wintypes.WORD(), wintypes.DWORD()
            self._check(self.security.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision),
            ))
            if (not control.value & _SE_DACL_PROTECTED or not dacl
                    or not self.security.IsValidAcl(dacl)
                    or ctypes.c_ushort.from_address(dacl.value + 4).value != 1):
                raise _unsafe_file()
            ace = ctypes.c_void_p()
            self._check(self.security.GetAce(dacl, 0, ctypes.byref(ace)))
            # ACCESS_ALLOWED_ACE: header (type, flags, size), mask, then SID.
            if (not ace.value or ctypes.c_ubyte.from_address(ace.value).value != 0
                    or ctypes.c_ubyte.from_address(ace.value + 1).value != 0
                    or ctypes.c_uint32.from_address(ace.value + 4).value != _FILE_ALL_ACCESS
                    or not self.security.IsValidSid(ace.value + 8)
                    or not self.security.EqualSid(ace.value + 8, self.user_sid)):
                raise _unsafe_file()

    def open(self, path: Path, create: bool) -> int:
        access = _READ_CONTROL | _WRITE_DAC | (0x40000000 | _DELETE if create else 0x80000000)
        with self.descriptor() as descriptor:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            for attempt in range(21):
                handle = self.kernel.CreateFileW(
                    str(path), access, 0 if create else 1,
                    ctypes.byref(attributes) if create else None, 1 if create else 3,
                    0x80 | 0x00200000, None,
                )
                if handle != ctypes.c_void_p(-1).value:
                    return handle
                error = ctypes.get_last_error()
                # A creating process holds an exclusive handle until its final byte.
                if create or error != 32 or attempt == 20:
                    raise ctypes.WinError(error)
                time.sleep(0.01)
        raise _unsafe_file()

    def discard_created(self, handle: int) -> None:
        # Delete the exact created object, never re-resolve a possibly replaced path.
        delete = wintypes.BOOL(True)
        if not self.kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete),
                                                     ctypes.sizeof(delete)):
            # A filesystem may refuse delete disposition. Leave no partial token.
            self._check(self.kernel.SetFilePointerEx(handle, 0, None, 0))
            self._check(self.kernel.SetEndOfFile(handle))


@contextmanager
def _windows_file(path: Path, *, create: bool) -> Iterator[int]:
    import msvcrt

    api = _WindowsSecurity()
    handle = api.open(path, create)
    fd: int | None = None
    completed = False
    try:
        api.check_regular_file(handle)
        if not create:
            api.protect_existing(handle)
        api.verify(handle)
        # open_osfhandle transfers handle ownership to the CRT only on success.
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_WRONLY if create else os.O_RDONLY))
        yield fd
        completed = True
    finally:
        try:
            if create and not completed:
                api.discard_created(handle)
        finally:
            if fd is None:
                api.kernel.CloseHandle(handle)
            else:
                os.close(fd)


def read_text(path: Path) -> str:
    if not _WINDOWS:
        return path.read_text(encoding="utf-8")
    with _windows_file(path, create=False) as fd:
        chunks = []
        while chunk := os.read(fd, 8192):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")


@contextmanager
def create(path: Path) -> Iterator[int]:
    """Publish a complete private token without replacing a competing creator."""
    if _WINDOWS:
        with _windows_file(path, create=True) as fd:
            yield fd
    else:
        # A reader accepts old tokens without a trailing newline, so it cannot
        # distinguish a short write from a complete legacy token. Keep the final
        # name absent until all bytes are written. mkstemp creates this same-
        # directory candidate exclusively with mode 0600; link publishes it
        # atomically and refuses to replace an existing or racing token.
        fd, temporary_name = tempfile.mkstemp(prefix=".btap-token-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            try:
                yield fd
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass  # Creation's 0600 mode is already private on POSIX.
            os.link(temporary, path)
        finally:
            # Only this unpublished name belongs to us; never unlink path,
            # including when publication lost a race or the winner is unreadable.
            temporary.unlink(missing_ok=True)
