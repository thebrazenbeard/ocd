"""Process-owned foreground state locks; not an installed OCD observer.

The lock file is permanent: unlinking it could let different processes lock
different inodes for the same logical state. This module assumes cooperating
processes under the same OS account; malicious same-UID path replacement and
network filesystems are outside its initial security contract.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import sys
from typing import Iterator


_LOCK_NAME = "observer.lock"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class UnsafeStateDirectory(OSError):
    """State directory or lock file did not meet the safety boundary."""


class StateLockBusy(OSError):
    """Another process holds the kernel lock."""


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE
    )


def _check_state_chain(path: Path) -> None:
    current = Path(path.anchor)
    # Existing ancestors must not be replaceable by another unprivileged UID.
    # /tmp's root-owned sticky bit is admitted: the kernel prevents other UIDs
    # from renaming a directory owned by this user beneath it.
    for name in path.parts[1:]:
        current = current / name
        try:
            item = current.lstat()
        except FileNotFoundError:
            if current == path:
                return
            raise UnsafeStateDirectory(f"missing state ancestor: {current}") from None
        if _is_link(item) or not stat.S_ISDIR(item.st_mode):
            raise UnsafeStateDirectory(f"unsafe linked or non-directory state path: {current}")
        if os.name == "posix":
            mode = stat.S_IMODE(item.st_mode)
            if item.st_uid not in (0, os.geteuid()):
                raise UnsafeStateDirectory(f"state ancestor has an untrusted owner: {current}")
            if mode & 0o022 and not (
                item.st_uid == 0 and mode & stat.S_ISVTX and mode & 0o002
            ):
                raise UnsafeStateDirectory(f"state ancestor is replaceable by other users: {current}")


def _private_state_dir(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise UnsafeStateDirectory("state directory must be a non-root absolute path without '..'")
    if os.name == "nt":
        # Restrict to the user's local profile and reject junction ancestors.
        # Inherited Windows DACLs still require separate native qualification.
        appdata = os.environ.get("LOCALAPPDATA")
        if not appdata or not path.is_relative_to(Path(appdata)):
            raise UnsafeStateDirectory("Windows state must be beneath LOCALAPPDATA")
    _check_state_chain(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise UnsafeStateDirectory(f"cannot create state directory: {path}") from exc
    _check_state_chain(path)
    item = path.lstat()
    if _is_link(item) or not stat.S_ISDIR(item.st_mode):
        raise UnsafeStateDirectory("state directory changed type")
    if os.name == "posix" and (
        item.st_uid != os.geteuid() or stat.S_IMODE(item.st_mode) & 0o077
    ):
        raise UnsafeStateDirectory("state directory is not owned and private (0700)")
    return path


@contextmanager
def _posix_lock(state: Path) -> Iterator[Path]:
    import fcntl

    path = state / _LOCK_NAME
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                     getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeStateDirectory("linked or non-file lock path refused") from exc
        raise
    locked = False
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            _is_link(opened) or _is_link(current)
            or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            raise UnsafeStateDirectory("state lock file is linked, shared, or not private")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateLockBusy(f"state lock already held: {path}") from exc
        locked = True
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise UnsafeStateDirectory("state lock replaced during acquisition")
        yield path
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def _windows_lock(state: Path) -> Iterator[Path]:
    """One-byte native LockFileEx on a non-deletably shared Win32 handle.

    Requires native NTFS/DACL and crash testing before this primitive can be
    used by a service. No Windows state ACL is inferred from ``os.chmod``.
    """
    import ctypes
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    class _FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME),
            ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
        ]

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    dll.GetDriveTypeW.restype = wintypes.UINT
    dll.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    dll.GetVolumeInformationW.restype = wintypes.BOOL
    dll.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    dll.CreateFileW.restype = wintypes.HANDLE
    dll.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInformation)]
    dll.GetFileInformationByHandle.restype = wintypes.BOOL
    dll.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    dll.LockFileEx.restype = wintypes.BOOL
    dll.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    dll.UnlockFileEx.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL

    if not state.drive or dll.GetDriveTypeW(state.anchor) != 3:
        raise UnsafeStateDirectory("Windows state requires a fixed local drive")
    filesystem = ctypes.create_unicode_buffer(32)
    if not dll.GetVolumeInformationW(
        state.anchor, None, 0, None, None, None, filesystem, len(filesystem)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if filesystem.value.upper() not in {"NTFS", "REFS"}:
        raise UnsafeStateDirectory("Windows state requires local NTFS or ReFS")
    path = state / _LOCK_NAME
    # Share READ/WRITE but *not DELETE*; a different process may open the same
    # sentinel to contest the kernel lock, but cannot replace it while held.
    # OPEN_REPARSE_POINT makes an existing symlink fail metadata checks rather
    # than following it to an unrelated file.
    handle = dll.CreateFileW(str(path), 0xC0000000, 0x00000003, None,
                             4, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    locked = False
    region = _Overlapped()
    try:
        details = _FileInformation()
        if not dll.GetFileInformationByHandle(handle, ctypes.byref(details)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            details.attributes & (0x10 | 0x400)
            or not details.volume
            or not (details.index_high or details.index_low)
            or details.links != 1
        ):
            raise UnsafeStateDirectory("Windows lock is linked, reparse, or identity unavailable")
        now = path.lstat()
        if _is_link(now) or not stat.S_ISREG(now.st_mode):
            raise UnsafeStateDirectory("Windows lock pathname is unsafe")
        # Lock one byte starting at offset zero, fail immediately on overlap.
        if not dll.LockFileEx(handle, 0x00000003, 0, 1, 0, ctypes.byref(region)):
            error = ctypes.get_last_error()
            if error in (32, 33):
                raise StateLockBusy(f"Windows state lock already held: {path}")
            raise ctypes.WinError(error)
        locked = True
        yield path
    finally:
        try:
            if locked and not dll.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(region)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            dll.CloseHandle(handle)


@contextmanager
def acquire_state_lock(directory: Path) -> Iterator[Path]:
    """Acquire a nonblocking OS lock; never unlink its lockfile on release.

    Windows native LockFileEx/DACL qualification is incomplete, so Windows
    use is disabled by default and only disposable fixtures may opt in with
    ``OCD_EXPERIMENTAL_WINDOWS_STATE_LOCK=1``. This is never called by an
    installed OCD service or the organizer's apply path.
    """
    if os.name not in ("nt", "posix") or (
        os.name == "posix" and not sys.platform.startswith("linux")
    ):
        raise UnsafeStateDirectory("state locks support Windows and Linux only")
    if os.name == "nt" and os.environ.get("OCD_EXPERIMENTAL_WINDOWS_STATE_LOCK") != "1":
        raise UnsafeStateDirectory("experimental Windows state lock disabled until native qualification")
    state = _private_state_dir(Path(directory))
    if sys.platform.startswith("linux"):
        with _posix_lock(state) as lock_path:
            yield lock_path
    elif os.name == "nt":
        with _windows_lock(state) as lock_path:
            yield lock_path
    else:
        raise UnsafeStateDirectory("state locks unsupported on this OS")
