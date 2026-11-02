"""Fail-closed Win32, handle-anchored rename without ordinary replacement.

This module is deliberately *not* wired into ``apply_moves`` yet.  The
filesystem/Windows-version matrix and the same-file hardlink collision case
need live qualification before Windows mutation is enabled.

``rename_noreplace`` matches the Linux commit primitive's arguments.  It
holds every ancestor directory open without FILE_SHARE_DELETE, opens the
source itself with DELETE access and without following its final reparse
point, checks metadata on that *handle*, and passes the held destination
directory as FILE_RENAME_INFO.RootDirectory.  It neither copies nor deletes.

Only simple absolute paths on fixed, local NTFS/ReFS drive letters are
accepted.  In particular UNC, SUBST/network, device, ADS, ambiguous Win32
name spellings and all reparse points are refused.  Unexpected Win32/API
behavior is an error, not permission to try an unanchored os.rename().
"""

from __future__ import annotations

from contextlib import ExitStack
import ctypes
from dataclasses import dataclass
import os
import re
from typing import Protocol
import unicodedata


class UnsafeWindowsMove(OSError):
    """A Windows move failed one of the mutation boundary's safety checks."""


_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_READ_ATTRIBUTES = 0x80
_FILE_TRAVERSE = 0x20
_DELETE = 0x10000
_FILE_SHARE_READ = 1
_FILE_SHARE_WRITE = 2
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_NT_FILE_RENAME_INFORMATION_CLASS = 10
_FILE_BASIC_INFO_CLASS = 0
_FILE_STANDARD_INFO_CLASS = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_INFO_CLASS = 18
_DRIVE_FIXED = 3
_ERROR_FILE_NOT_FOUND = 2
_FILETIME_UNIX_EPOCH = 116444736000000000


@dataclass(frozen=True)
class FileInfo:
    """Information queried from one still-open handle, not from its path."""

    volume: int
    file_id: int  # Full, unsigned 128-bit FILE_ID_INFO, little-endian.
    attributes: int
    directory: bool
    size: int
    mtime_ns: int

    @property
    def identity(self) -> tuple[int, int]:
        # CPython 3.10+ Windows st_dev/st_ino use the volume serial and file
        # index.  CPython 3.13+ can expose the full 128-bit file ID.
        return self.volume, self.file_id


class _Api(Protocol):
    def require_local_drive(self, drive: str) -> None: ...
    def open(
        self, path: str, *, source: bool = False, missing_ok: bool = False,
        directory: bool = False,
    ) -> object | None: ...
    def info(self, handle: object) -> FileInfo: ...
    def rename(self, source: object, destination_parent: object, leaf: str) -> None: ...
    def close(self, handle: object) -> None: ...


def _path_parts(value: os.PathLike[str] | str) -> tuple[str, tuple[str, ...]]:
    """Parse literal Win32 names, rejecting names Win32 may reinterpret."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not re.match(r"^[A-Za-z]:\\", raw):
        raise UnsafeWindowsMove("only absolute local drive-letter paths are supported")
    if "/" in raw or "\x00" in raw or "\\\\" in raw[3:]:
        raise UnsafeWindowsMove("path has an ambiguous separator or NUL")
    components = tuple(raw[3:].split("\\"))
    if not components or any(not c or c in (".", "..") for c in components):
        raise UnsafeWindowsMove("empty or dot path component refused")
    forbidden = '<>:"|?*'
    for part in components:
        # Control, formatting, and surrogate codepoints include bidi
        # overrides and zero-width controls which can spoof the visible leaf
        # name.  Reject them before Windows or an operator interprets it.
        if (part.endswith((" ", ".")) or any(
            c in forbidden or unicodedata.category(c) in {"Cc", "Cf", "Cs"}
            for c in part
        )):
            raise UnsafeWindowsMove(f"ambiguous or invalid Windows path component: {part!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(
            r"(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]", stem
        ):
            raise UnsafeWindowsMove(f"reserved Windows device name: {part!r}")
    return raw[:2].upper(), components


def _directory_chain(drive: str, parts: tuple[str, ...]) -> list[str]:
    # A plain drive root is opened first, then each existing component.  A
    # held non-SHARE_DELETE directory cannot be replaced by a junction while
    # descendants are being opened by their full Win32 paths.
    root = drive + "\\"
    result = [root]
    for index in range(1, len(parts) + 1):
        result.append(root + "\\".join(parts[:index]))
    return result


def _checked_info(api: _Api, handle: object, *, directory: bool) -> FileInfo:
    info = api.info(handle)
    if not (info.volume and info.file_id):
        raise UnsafeWindowsMove("filesystem did not provide a usable file identity")
    if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsafeWindowsMove("symlink/junction/reparse point refused")
    if bool(info.attributes & _FILE_ATTRIBUTE_DIRECTORY) != bool(info.directory):
        raise UnsafeWindowsMove("inconsistent directory information")
    if info.directory != directory:
        raise UnsafeWindowsMove("path component is not the required file/directory type")
    return info


def _hold_directories(
    api: _Api, paths: list[str], stack: ExitStack,
) -> tuple[object, FileInfo]:
    volume: int | None = None
    last_handle: object | None = None
    last_info: FileInfo | None = None
    for path in paths:
        # FILE_RENAME_INFO.RootDirectory resolves a relative target name via
        # this open directory handle. Request FILE_TRAVERSE explicitly for
        # that operation, not just FILE_READ_ATTRIBUTES.
        handle = api.open(path, directory=True)
        if handle is None:
            raise UnsafeWindowsMove(f"required directory disappeared: {path}")
        stack.callback(api.close, handle)
        info = _checked_info(api, handle, directory=True)
        if volume is not None and info.volume != volume:
            raise UnsafeWindowsMove("directory traversal crosses a volume")
        volume = info.volume
        last_handle, last_info = handle, info
    assert last_handle is not None and last_info is not None
    return last_handle, last_info


def rename_noreplace(
    source: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    *,
    identity: tuple[int, int],
    size: int,
    mtime_ns: int,
    _api: _Api | None = None,
) -> None:
    """Rename one validated file under locked directory handles.

    Success means native FileRenameInformation with ReplaceIfExists=FALSE succeeded. An
    existing *different* file is not replaced according to Microsoft's API
    contract.  An existing hardlink to the *same* file is a special case in
    the underlying MS-FSA algorithm; do not enable apply before qualifying
    that case and concurrent hardlink creation on the supported filesystem.

    ``_api`` is dependency injection for non-mutating Linux mock tests only.
    """
    src_drive, src = _path_parts(source)
    dst_drive, dst = _path_parts(destination)
    if src_drive != dst_drive:
        raise UnsafeWindowsMove("cross-drive moves refused")
    if [c.casefold() for c in src] == [c.casefold() for c in dst]:
        raise UnsafeWindowsMove("source and destination are identical or case aliases")
    if not isinstance(identity, tuple) or len(identity) != 2 or not all(
        isinstance(value, int) and value > 0 for value in identity
    ):
        raise UnsafeWindowsMove("expected source file identity unavailable")
    if not isinstance(size, int) or size < 0 or not isinstance(mtime_ns, int):
        raise UnsafeWindowsMove("invalid expected source metadata")
    if _api is None:
        if os.name != "nt":
            raise UnsafeWindowsMove("Win32 rename unavailable on this platform")
        _api = _Win32Api()
    api = _api
    api.require_local_drive(src_drive)

    # Keep ALL ancestor handles alive until the rename returns.  A file path
    # check followed by an os.rename() would reintroduce a junction-swap race.
    with ExitStack() as stack:
        source_parent, src_parent_info = _hold_directories(
            api, _directory_chain(src_drive, src[:-1]), stack
        )
        destination_parent, dst_parent_info = _hold_directories(
            api, _directory_chain(dst_drive, dst[:-1]), stack
        )
        if src_parent_info.volume != dst_parent_info.volume:
            raise UnsafeWindowsMove("cross-volume move refused")

        source_handle = api.open(str(source), source=True)
        if source_handle is None:
            raise UnsafeWindowsMove("source disappeared")
        stack.callback(api.close, source_handle)
        current = _checked_info(api, source_handle, directory=False)
        if current.volume != src_parent_info.volume:
            raise UnsafeWindowsMove("source file crosses a volume")
        if current.identity != identity:
            raise UnsafeWindowsMove("source identity changed since preflight")
        if current.size != size or current.mtime_ns != mtime_ns:
            raise UnsafeWindowsMove("source size/mtime changed since preflight")

        # Also reject an existing hardlink to this same source, which can be
        # treated specially by FileRenameInfo even with ReplaceIfExists=FALSE.
        occupied = api.open(str(destination), missing_ok=True)
        if occupied is not None:
            stack.callback(api.close, occupied)
            raise UnsafeWindowsMove("destination already exists")
        api.rename(source_handle, destination_parent, dst[-1])


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime"
    )] + [("FileAttributes", ctypes.c_uint32)]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_int64), ("EndOfFile", ctypes.c_int64),
        ("NumberOfLinks", ctypes.c_uint32), ("DeletePending", ctypes.c_uint8),
        ("Directory", ctypes.c_uint8),
    ]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_uint32), ("ReparseTag", ctypes.c_uint32)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_uint8 * 16)]


class _FILE_RENAME_INFO(ctypes.Structure):
    # Native FILE_RENAME_INFORMATION stores BOOLEAN + padding; zeroed DWORD
    # followed by pointer alignment is ABI-compatible. WCHAR is UTF-16,
    # including for portable ABI tests on Linux.
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    # Native status/pointer union and Information are pointer-sized.
    _fields_ = [
        ("Status", ctypes.c_size_t),
        ("Information", ctypes.c_size_t),
    ]


def _rename_buffer(directory_handle: object, leaf: str) -> tuple[ctypes.Array, int]:
    name = leaf.encode("utf-16le", "surrogatepass")
    offset = _FILE_RENAME_INFO.FileName.offset
    # Include the harmless terminating UTF-16 NUL, although the API does not
    # require it or include it in FileNameLength.
    # Native FILE_RENAME_INFORMATION requires sizeof(struct) plus the UTF-16
    # bytes; the trailing WCHAR[1] already contributes to sizeof.
    count = ctypes.sizeof(_FILE_RENAME_INFO) + len(name)
    buffer = ctypes.create_string_buffer(count)
    header = ctypes.cast(buffer, ctypes.POINTER(_FILE_RENAME_INFO)).contents
    header.ReplaceIfExists = 0
    header.RootDirectory = int(directory_handle)
    header.FileNameLength = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + offset, name, len(name))
    return buffer, count


class _Win32Api:
    """Small ctypes adapter kept separate from the auditable safety policy."""

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise UnsafeWindowsMove("kernel32 Win32 APIs unavailable")
        try:
            dll = ctypes.WinDLL("kernel32", use_last_error=True)
            self._create = dll.CreateFileW
            self._create.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ]
            self._create.restype = ctypes.c_void_p
            self._get = dll.GetFileInformationByHandleEx
            self._get.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ]
            self._get.restype = ctypes.c_int
            self._set = dll.SetFileInformationByHandle
            self._set.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ]
            self._set.restype = ctypes.c_int
            self._close = dll.CloseHandle
            self._close.argtypes = [ctypes.c_void_p]
            self._close.restype = ctypes.c_int
            self._drive_type = dll.GetDriveTypeW
            self._drive_type.argtypes = [ctypes.c_wchar_p]
            self._drive_type.restype = ctypes.c_uint32
            self._query_device = dll.QueryDosDeviceW
            self._query_device.argtypes = [
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
            ]
            self._query_device.restype = ctypes.c_uint32
            self._volume = dll.GetVolumeInformationW
            self._volume.argtypes = [
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p, ctypes.c_uint32,
            ]
            self._volume.restype = ctypes.c_int
            # Native FileRenameInformation retains the held RootDirectory.
            # Win32 FileRenameInfo rejected this relative form (WinError 87).
            native = ctypes.WinDLL("ntdll", use_last_error=True)
            self._nt_set = native.NtSetInformationFile
            self._nt_set.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_IO_STATUS_BLOCK),
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
            ]
            self._nt_set.restype = ctypes.c_int32
        except (AttributeError, OSError) as exc:
            raise UnsafeWindowsMove(f"required Win32 APIs unavailable: {exc}") from exc

    @staticmethod
    def _win_error() -> OSError:
        return ctypes.WinError(ctypes.get_last_error())

    def require_local_drive(self, drive: str) -> None:
        root = drive + "\\"
        if self._drive_type(root) != _DRIVE_FIXED:
            raise UnsafeWindowsMove("network/removable/non-fixed drive refused")
        mapping = ctypes.create_unicode_buffer(4096)
        if not self._query_device(drive, mapping, len(mapping)):
            raise self._win_error()
        # A SUBST drive is usually a DOS-device mapping to \??\X:\subdir,
        # and GetDriveTypeW can report DRIVE_FIXED for that target.  Refuse
        # mappings to paths rather than physical hard-disk volume devices.
        if not mapping.value.startswith("\\Device\\HarddiskVolume"):
            raise UnsafeWindowsMove("drive is mapped to an unsupported device/path")
        fs = ctypes.create_unicode_buffer(32)
        if not self._volume(root, None, 0, None, None, None, fs, len(fs)):
            raise self._win_error()
        if fs.value.upper() not in {"NTFS", "REFS"}:
            raise UnsafeWindowsMove("unsupported Windows filesystem")

    def open(
        self, path: str, *, source: bool = False, missing_ok: bool = False,
        directory: bool = False,
    ) -> int | None:
        # Use extended syntax to prevent Win32 from silently normalizing a
        # spelling after _path_parts() checked it.  Reparse processing is
        # suppressed for the final component of every individually opened path.
        flags = _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT
        handle = self._create(
            "\\\\?\\" + path,
            _FILE_READ_ATTRIBUTES | (_FILE_TRAVERSE if directory else 0) | (_DELETE if source else 0),
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING, flags, None,
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            err = ctypes.get_last_error()
            if missing_ok and err == _ERROR_FILE_NOT_FOUND:
                return None
            raise ctypes.WinError(err)
        return handle

    def _query(self, handle: object, kind: int, result: ctypes.Structure) -> None:
        if not self._get(handle, kind, ctypes.byref(result), ctypes.sizeof(result)):
            raise self._win_error()

    def info(self, handle: object) -> FileInfo:
        tag, standard, basic, fileid = (
            _FILE_ATTRIBUTE_TAG_INFO(), _FILE_STANDARD_INFO(),
            _FILE_BASIC_INFO(), _FILE_ID_INFO(),
        )
        self._query(handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, tag)
        self._query(handle, _FILE_STANDARD_INFO_CLASS, standard)
        self._query(handle, _FILE_BASIC_INFO_CLASS, basic)
        self._query(handle, _FILE_ID_INFO_CLASS, fileid)
        if tag.FileAttributes != basic.FileAttributes:
            raise UnsafeWindowsMove("inconsistent file attribute queries")
        return FileInfo(
            volume=int(fileid.VolumeSerialNumber),
            file_id=int.from_bytes(bytes(fileid.FileId), "little"),
            attributes=int(tag.FileAttributes), directory=bool(standard.Directory),
            size=int(standard.EndOfFile),
            mtime_ns=(int(basic.LastWriteTime) - _FILETIME_UNIX_EPOCH) * 100,
        )

    def rename(self, source: object, destination_parent: object, leaf: str) -> None:
        buffer, length = _rename_buffer(destination_parent, leaf)
        status_block = _IO_STATUS_BLOCK()
        status = self._nt_set(
            source, ctypes.byref(status_block), buffer, length,
            _NT_FILE_RENAME_INFORMATION_CLASS,
        )
        if status != 0 or status_block.Status != 0:
            # Never retry with an unanchored absolute-path fallback.
            raise UnsafeWindowsMove(
                "NtSetInformationFile anchored no-replace rename failed: "
                f"NTSTATUS 0x{status & 0xFFFFFFFF:08X}, "
                f"IO_STATUS 0x{status_block.Status & 0xFFFFFFFF:08X}"
            )

    def close(self, handle: object) -> None:
        if not self._close(handle):
            raise self._win_error()
