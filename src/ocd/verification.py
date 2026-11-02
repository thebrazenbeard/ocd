"""Read-only, independently hashed verification of one manifested local artifact.

This module never creates a cache, stages data, or authorizes eviction.  The
path is anchored to already-existing directories.  POSIX uses no-follow file
descriptors; Windows pins local NTFS/ReFS ancestors with non-delete-sharing
handles, then reads a non-reparse file handle.  The result is a point-in-time
digest, not a backup or proof that an artifact will remain available.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import ctypes
import hashlib
import hmac
import os
from pathlib import Path
import stat

from .artifacts import (
    ArtifactEntry, ArtifactError, _assert_parent_still_attached,
    _open_root, _parse_entry, _secure_directory_flags,
)


_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ArtifactVerification:
    """Verified bytes read from one existing, explicitly selected file."""

    path: Path
    size: int
    sha256: str


def _hash_descriptor(fd: int, entry: ArtifactEntry) -> str:
    digest = hashlib.sha256()
    total = 0
    while True:
        data = os.read(fd, _CHUNK_SIZE)
        if not data:
            break
        total += len(data)
        if total > entry.size:
            raise ArtifactError("local artifact exceeds manifest size")
        digest.update(data)
    if total != entry.size:
        raise ArtifactError("local artifact size does not match manifest")
    actual = digest.hexdigest()
    if not hmac.compare_digest(actual, entry.sha256):
        raise ArtifactError("local artifact SHA-256 does not match manifest")
    return actual


def _unchanged(before, after) -> bool:
    return (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    ) == (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    )


def _verify_posix(entry: ArtifactEntry, root: Path) -> ArtifactVerification:
    root_fd = _open_root(root)
    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        root_info = os.fstat(root_fd)
        parent_fd = os.dup(root_fd)
        for part in entry.destination_relpath.split("/")[:-1]:
            next_fd = os.open(part, _secure_directory_flags(), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        if os.fstat(parent_fd).st_dev != root_info.st_dev:
            raise ArtifactError("artifact parent crosses another filesystem")
        name = entry.destination_relpath.split("/")[-1]
        flags = (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                 | getattr(os, "O_CLOEXEC", 0))
        file_fd = os.open(name, flags, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_dev != root_info.st_dev:
            raise ArtifactError("artifact must be a regular file on the selected filesystem")
        if before.st_size != entry.size:
            raise ArtifactError("local artifact size does not match manifest")
        _assert_parent_still_attached(root, root_fd, parent_fd, entry.destination_relpath)
        actual = _hash_descriptor(file_fd, entry)
        after = os.fstat(file_fd)
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _unchanged(before, after) or not _unchanged(before, visible):
            raise ArtifactError("local artifact changed identity or metadata during verification")
        _assert_parent_still_attached(root, root_fd, parent_fd, entry.destination_relpath)
        return ArtifactVerification(root / entry.destination_relpath, before.st_size, actual)
    except ArtifactError:
        raise
    except OSError:
        raise ArtifactError("local artifact is missing, inaccessible, or has an unsafe path") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def _verify_windows(entry: ArtifactEntry, root: Path) -> ArtifactVerification:
    """Use native held directory and file handles, with no mutation opt-in."""
    import msvcrt

    from .win32 import (
        _Win32Api, _checked_info, _directory_chain, _hold_directories,
        _path_parts, UnsafeWindowsMove,
    )

    if not root.is_absolute() or ".." in root.parts:
        raise ArtifactError("destination_root must be absolute and contain no '..'")
    destination = root.joinpath(*entry.destination_relpath.split("/"))
    try:
        drive, root_parts = _path_parts(str(root))
        dst_drive, parts = _path_parts(str(destination))
        if drive != dst_drive or len(parts) <= len(root_parts) or tuple(
            component.casefold() for component in parts[:len(root_parts)]
        ) != tuple(component.casefold() for component in root_parts):
            raise ArtifactError("artifact escapes selected Windows root")
        api = _Win32Api()
        api.require_local_drive(drive)
        with ExitStack() as stack:
            parent, parent_info = _hold_directories(
                api, _directory_chain(drive, parts[:-1]), stack
            )
            del parent  # Ancestor handles stay pinned by ExitStack.
            # GENERIC_READ | FILE_READ_ATTRIBUTES; share READ only. Refuse an
            # already-open writer and block new writers/deletion while the
            # file is being hashed. A reader-only verification must not race
            # with a writer that restores size/mtime after changing bytes.
            handle = api._create(
                "\\\\?\\" + str(destination), 0x80000000 | 0x80,
                0x1, None, 3, 0x00200000, None,
            )
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            owns_handle = True
            descriptor: int | None = None
            try:
                before = _checked_info(api, handle, directory=False)
                if before.volume != parent_info.volume or before.size != entry.size:
                    raise ArtifactError("local artifact is on another volume or has the wrong size")
                descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDONLY)
                owns_handle = False
                # Read the original pinned handle, not a path reopened after
                # symlink or junction checking.
                actual = _hash_descriptor(descriptor, entry)
                after = _checked_info(api, handle, directory=False)
                if before.identity != after.identity or (
                    before.size, before.mtime_ns
                ) != (after.size, after.mtime_ns):
                    raise ArtifactError("local artifact changed identity or metadata during verification")
                return ArtifactVerification(destination, before.size, actual)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                elif owns_handle:
                    api.close(handle)
    except ArtifactError:
        raise
    except (UnsafeWindowsMove, OSError):
        raise ArtifactError("local artifact is missing, inaccessible, or has an unsafe Windows path") from None


def verify_local_artifact(
    entry: ArtifactEntry, destination_root: str | os.PathLike[str],
) -> ArtifactVerification:
    """Independently verify one existing artifact without changing any files.

    A passing digest is *not* an eviction permit or a guarantee of remote
    recoverability; the caller must enforce any independent retention policy.
    """
    if not isinstance(entry, ArtifactEntry):
        raise ArtifactError("verify_local_artifact requires an ArtifactEntry")
    entry = _parse_entry(vars(entry))
    root = Path(destination_root)
    if not root.is_absolute() or ".." in root.parts:
        raise ArtifactError("destination_root must be absolute and contain no '..'")
    if os.name == "nt":
        return _verify_windows(entry, root)
    if os.name == "posix":
        return _verify_posix(entry, root)
    raise ArtifactError("secure read-only artifact verification unavailable on this platform")


__all__ = ["ArtifactVerification", "verify_local_artifact"]
