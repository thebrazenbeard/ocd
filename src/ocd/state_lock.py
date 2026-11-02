"""Process-owned foreground state locks; not an installed OCD observer.

The lock file is permanent: unlinking it could let different processes lock
different inodes for the same logical state. This module assumes cooperating
processes under the same OS account; malicious same-UID path replacement is
outside its initial security contract. Unqualified or network mount types are
rejected on Linux; their ``flock`` behavior is not assumed to be local.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re
import stat
import sys
from typing import Iterator


_LOCK_NAME = "observer.lock"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_WINDOWS_WRITE_BITS = (
    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x00000010  # FILE_WRITE_EA
    | 0x00000040  # FILE_DELETE_CHILD (directory)
    | 0x00000100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x02000000  # MAXIMUM_ALLOWED (cannot prove bounded permissions)
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
# Unknown filesystems fail closed: writable OverlayFS may itself be backed by
# remote storage, and its underlying storage cannot be proved from mountinfo.
_LOCAL_LINUX_FILESYSTEMS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "bcachefs",
    "tmpfs", "ramfs", "jfs", "nilfs2",
})
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


class UnsafeStateDirectory(OSError):
    """State directory or lock file did not meet the safety boundary."""


class StateLockBusy(OSError):
    """Another process holds the kernel lock."""


def _windows_untrusted_writable_ace(ace_type: int, mask: int, trusted: bool) -> bool:
    """Fail-closed ACL policy; not a substitute for native Win32 DACL readback.

    Windows file/directory ACCESS_ALLOWED_ACE = 0, ACCESS_DENIED_ACE = 1.
    An unrecognized entry type (e.g. an object ACE with a different SID
    position) must never be interpreted as a harmless allow entry.
    """
    if ace_type == 1:
        return False
    if ace_type != 0:
        raise UnsafeStateDirectory("unsupported Windows DACL ACE type")
    return bool(mask & _WINDOWS_WRITE_BITS) and not trusted


def _windows_sid_length(ace_size: int, sid_prefix: bytes) -> int:
    """Bound a standard allow/deny ACE SID before handing it to Win32.

    ACCESS_ALLOWED_ACE and ACCESS_DENIED_ACE place their SID at byte 8.
    A SID has an 8-byte fixed prefix and at most 15 DWORD subauthorities.
    Reject truncated or malformed ACEs rather than letting EqualSid inspect
    bytes beyond the ACE boundary.
    """
    if len(sid_prefix) != 8 or ace_size < 16:
        raise UnsafeStateDirectory("invalid Windows ACL ACE/SID length")
    revision, count = sid_prefix[0], sid_prefix[1]
    sid_size = 8 + 4 * count
    if revision != 1 or count > 15 or 8 + sid_size > ace_size:
        raise UnsafeStateDirectory("invalid Windows ACL ACE/SID length")
    return sid_size


def _windows_check_private_acl(path: Path) -> None:
    """Read existing native NTFS/ReFS ownership/DACL; never rewrite an ACL.

    An untrusted principal may receive read/execute, but not any file-system
    write/delete/ownership rights. TokenUser, LOCAL SYSTEM and BUILTIN
    Administrators are trusted owners; Windows can assign an elevated token's
    Administrators group as TokenOwner. OWNER RIGHTS refers to the validated
    object owner, not another principal. This cannot eliminate same-user
    replacement or ancestor TOCTOU.
    """
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                            ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD,
                                              wintypes.DWORD,
                                              ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR,
                                               ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        # TokenUser = 1: the first member of TOKEN_USER is SID_AND_ATTRIBUTES.Sid.
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        token_data = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, token_data, needed,
                                           ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        user_sid = ctypes.cast(token_data, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not user_sid:
            raise UnsafeStateDirectory("Windows token owner SID is unavailable")

        system_sid = ctypes.c_void_p()
        admin_sid = ctypes.c_void_p()
        owner_rights_sid = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW("S-1-5-18", ctypes.byref(system_sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not advapi.ConvertStringSidToSidW("S-1-5-32-544", ctypes.byref(admin_sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not advapi.ConvertStringSidToSidW("S-1-3-4", ctypes.byref(owner_rights_sid)):
                    raise ctypes.WinError(ctypes.get_last_error())
                owner = ctypes.c_void_p()
                dacl = ctypes.c_void_p()
                descriptor = ctypes.c_void_p()
                # SE_FILE_OBJECT = 1; OWNER_SECURITY_INFORMATION | DACL = 5.
                status = advapi.GetNamedSecurityInfoW(
                    str(path), 1, 5, ctypes.byref(owner), None,
                    ctypes.byref(dacl), None, ctypes.byref(descriptor),
                )
                if status:
                    raise OSError(status, f"cannot inspect Windows state DACL: {path}")
                try:
                    owner_trusted = bool(owner.value) and any(
                        advapi.EqualSid(owner, sid)
                        for sid in (user_sid, system_sid, admin_sid)
                    )
                    if not owner_trusted or not dacl.value:
                        raise UnsafeStateDirectory(
                            f"Windows state owner or DACL is unsafe: {path}")

                    class _AclSize(ctypes.Structure):
                        _fields_ = [("ace_count", wintypes.DWORD),
                                    ("bytes_in_use", wintypes.DWORD),
                                    ("bytes_free", wintypes.DWORD)]

                    size = _AclSize()
                    # AclSizeInformation = 2.
                    if not advapi.GetAclInformation(dacl, ctypes.byref(size),
                                                   ctypes.sizeof(size), 2):
                        raise ctypes.WinError(ctypes.get_last_error())
                    for i in range(size.ace_count):
                        ace = ctypes.c_void_p()
                        if not advapi.GetAce(dacl, i, ctypes.byref(ace)):
                            raise ctypes.WinError(ctypes.get_last_error())
                        header = ctypes.string_at(ace.value, 4)
                        ace_type = header[0]
                        ace_size = int.from_bytes(header[2:4], "little")
                        if ace_type not in (0, 1) or ace_size < 16:
                            raise UnsafeStateDirectory(
                                f"unsupported or truncated Windows DACL ACE on {path}")
                        mask = int.from_bytes(ctypes.string_at(ace.value + 4, 4), "little")
                        _windows_sid_length(ace_size, ctypes.string_at(ace.value + 8, 8))
                        trustee = ctypes.c_void_p(ace.value + 8)
                        trusted = any(advapi.EqualSid(trustee, sid) for sid in
                                      (user_sid, system_sid, admin_sid, owner_rights_sid))
                        if _windows_untrusted_writable_ace(ace_type, mask, trusted):
                            raise UnsafeStateDirectory(
                                f"Windows state has an untrusted writable ACE: {path}")
                finally:
                    if descriptor.value:
                        kernel.LocalFree(descriptor)
            finally:
                if owner_rights_sid.value:
                    kernel.LocalFree(owner_rights_sid)
                if admin_sid.value:
                    kernel.LocalFree(admin_sid)
        finally:
            if system_sid.value:
                kernel.LocalFree(system_sid)
    finally:
        kernel.CloseHandle(token)


def _windows_check_state_acl_chain(state: Path) -> None:
    """Inspect LOCALAPPDATA, every child ancestor, and the state directory."""
    appdata = Path(os.environ["LOCALAPPDATA"])
    current = appdata
    _windows_check_private_acl(current)
    for component in state.relative_to(appdata).parts:
        current = current / component
        _windows_check_private_acl(current)


def _linux_local_filesystem(path: Path) -> None:
    """Refuse unqualified Linux mounts before creating/opening the sentinel.

    ``/proc/self/mountinfo`` names the actual mount namespace (including bind
    mounts) and its filesystem type. A missing, malformed, or ambiguous view
    is a refusal, not permission to fall back to unverified ``flock`` behavior.
    Linux mount changes by privileged actors during acquisition remain outside
    the single-account, local-filesystem contract.
    """
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise UnsafeStateDirectory("cannot verify Linux state filesystem") from exc

    matches: list[tuple[int, str]] = []
    for line in lines:
        header, separator, trailer = line.partition(" - ")
        mount_fields = header.split()
        fs_fields = trailer.split()
        if not separator or len(mount_fields) < 6 or len(fs_fields) < 3:
            raise UnsafeStateDirectory("malformed Linux mountinfo")
        encoded_mount = mount_fields[4]
        mount_text = _MOUNTINFO_ESCAPE.sub(
            lambda match: chr(int(match.group(1), 8)), encoded_mount
        )
        if "\\" in mount_text or not mount_text.startswith("/"):
            raise UnsafeStateDirectory("ambiguous Linux mountinfo path")
        mount = Path(mount_text)
        if mount == path or mount in path.parents:
            matches.append((len(mount.parts), fs_fields[0].lower()))

    if not matches:
        raise UnsafeStateDirectory("Linux state mount not identified")
    best_length = max(length for length, _ in matches)
    current = [fstype for length, fstype in matches if length == best_length]
    if len(current) != 1 or current[0] not in _LOCAL_LINUX_FILESYSTEMS:
        raise UnsafeStateDirectory(
            "Linux state must reside on an independently identifiable local filesystem"
        )


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
    if os.name == "nt":
        _windows_check_state_acl_chain(path)
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
def _windows_pin_state_chain(state: Path) -> Iterator[None]:
    """Pin every profile-relative directory while holding a Windows lock.

    A no-delete-sharing handle to the final sentinel alone cannot prevent a
    validated parent from being swapped with a junction *before* it is opened.
    Open each ancestor from LOCALAPPDATA outward, without following final
    reparse points, verify the actual directory handles and reread their ACLs,
    and keep all handles alive until the file lock has been released.
    """
    import ctypes
    from ctypes import wintypes

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
    dll.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    dll.CreateFileW.restype = wintypes.HANDLE
    dll.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_FileInformation),
    ]
    dll.GetFileInformationByHandle.restype = wintypes.BOOL
    dll.GetVolumeInformationByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    dll.GetVolumeInformationByHandleW.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL

    profile = Path(os.environ["LOCALAPPDATA"])
    try:
        components = state.relative_to(profile).parts
    except ValueError as exc:
        raise UnsafeStateDirectory("Windows state escaped LOCALAPPDATA") from exc
    current = profile
    directory_handles: list[int] = []
    try:
        for component in (None, *components):
            if component is not None:
                current = current / component
            # FILE_READ_ATTRIBUTES; FILE_SHARE_READ|FILE_SHARE_WRITE, explicitly
            # no FILE_SHARE_DELETE; OPEN_EXISTING; BACKUP_SEMANTICS|OPEN_REPARSE.
            handle = dll.CreateFileW(str(current), 0x80, 0x3, None, 3,
                                     0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            directory_handles.append(handle)
            details = _FileInformation()
            if not dll.GetFileInformationByHandle(handle, ctypes.byref(details)):
                raise ctypes.WinError(ctypes.get_last_error())
            if (
                not details.attributes & 0x10  # FILE_ATTRIBUTE_DIRECTORY
                or details.attributes & 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
                or not details.volume
                or not (details.index_high or details.index_low)
            ):
                raise UnsafeStateDirectory(
                    f"Windows state directory handle is reparse or unverified: {current}")
            # ACL readback happens while this object and all its ancestors are
            # held open against rename/delete; the path cannot be substituted.
            _windows_check_private_acl(current)

        filesystem = ctypes.create_unicode_buffer(32)
        flags = wintypes.DWORD()
        if not dll.GetVolumeInformationByHandleW(
            directory_handles[-1], None, 0, None, None,
            ctypes.byref(flags), filesystem, len(filesystem),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if filesystem.value.upper() not in {"NTFS", "REFS"} or not flags.value & 0x8:
            raise UnsafeStateDirectory(
                "Windows state requires NTFS/ReFS with persistent ACLs")
        yield
    finally:
        for handle in reversed(directory_handles):
            dll.CloseHandle(handle)


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
        # A fresh OPEN_ALWAYS lock file can inherit an unsafe DACL even when
        # state parent checks passed; read back the file's own security data.
        _windows_check_private_acl(path)
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
    if sys.platform.startswith("linux"):
        _linux_local_filesystem(Path(directory))
    state = _private_state_dir(Path(directory))
    if sys.platform.startswith("linux"):
        with _posix_lock(state) as lock_path:
            yield lock_path
    elif os.name == "nt":
        with _windows_pin_state_chain(state):
            with _windows_lock(state) as lock_path:
                yield lock_path
    else:
        raise UnsafeStateDirectory("state locks unsupported on this OS")
