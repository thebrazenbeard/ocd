"""Read-only, fail-closed filesystem discovery for Windows and Linux roots.

The scanner never opens file content or follows directory links. Its stability
check is a point-in-time mtime cutoff; consumers must revalidate files before
performing an operation because the filesystem can change after a scan.
"""

from __future__ import annotations

import errno
import math
import os
from pathlib import Path
import stat
import time

from .config import _unlinked_directory_chain
from .types import FileEntry, RootConfig


_CAN_SCAN_FD = (
    os.name != "nt"
    and os.scandir in os.supports_fd
    and os.open in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


def _is_reparse_point(details: os.stat_result) -> bool:
    """Include Windows junctions and cloud placeholders in link exclusions."""
    reparse_bit = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(details, "st_file_attributes", 0) & reparse_bit)


def _is_link(details: os.stat_result) -> bool:
    return stat.S_ISLNK(details.st_mode) or _is_reparse_point(details)


def _check_directory(path: Path, source: Path) -> bool:
    """Validate again before descending, including an intervening link swap.

    Returns false when a descendant disappeared or became a non-directory. A
    root with these properties is rejected by the caller before iteration.
    """
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False

    if _is_link(details) or not stat.S_ISDIR(details.st_mode):
        return False

    # A swapped ancestor could redirect a path even if its final component is
    # an ordinary directory; Path.resolve checks the complete component chain.
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        return False
    if os.name == "nt":
        # Windows' canonical resolver expands harmless 8.3 DOS short names,
        # so a literal path equality check rejects ordinary user temp roots.
        # Keep a lexical containment check and explicitly reject *every*
        # reparse-point ancestor before checking the filesystem identity.
        try:
            valid = (
                path.is_relative_to(source)
                and _unlinked_directory_chain(path)
                and os.path.samefile(path, resolved)
            )
        except OSError:
            valid = False
        if not valid:
            raise ValueError(f"Unsafe traversal outside registered source: {path}")
    elif resolved != path or not resolved.is_relative_to(source):
        raise ValueError(f"Unsafe traversal outside registered source: {path}")
    return True


def _file_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return _file_identity(a) == _file_identity(b)


def _open_beneath_root(root_fd: int, components: tuple[str, ...], flags: int) -> int | None:
    """Reopen a descendant through the root, without retaining ancestor FDs.

    Every component is opened relative to the preceding *directory descriptor*,
    with O_NOFOLLOW. A renamed or symlink-swapped ancestor therefore cannot
    redirect a queued path to an unrelated tree. At most two additional
    descriptors are live during this walk, regardless of its depth.
    """
    parent_fd = root_fd
    completed = False
    try:
        for component in components:
            try:
                child_fd = os.open(component, flags, dir_fd=parent_fd)
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    return None
                raise
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = child_fd
        completed = True
        return parent_fd
    finally:
        # Ownership of the final descriptor passes to the caller only on a
        # completed walk; failed opens and intervening errors must not leak it.
        if not completed and parent_fd != root_fd:
            os.close(parent_fd)


def _scan_with_directory_fds(
    source: Path, source_details: os.stat_result, destination: Path, cutoff_ns: int
) -> list[FileEntry]:
    """Anchor recursion to opened directory inodes, never a re-resolved path.

    Linux exposes scandir(fd), openat(dir_fd=...), and O_NOFOLLOW. Together
    they prevent a symlink replacement between inspection and descent from
    redirecting enumeration outside the registered directory tree.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(source, flags)
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno == errno.ELOOP:
            raise ValueError(f"Source root became unsafe: {source}") from exc
        raise
    if not _same_file(os.fstat(root_fd), source_details):
        os.close(root_fd)
        raise ValueError(f"Source root changed during admission: {source}")

    discovered: list[FileEntry] = []
    # Queue identities, not opened descriptors: a root with thousands of
    # siblings must not exhaust RLIMIT_NOFILE before visiting the first one.
    pending: list[tuple[Path, tuple[int, int]]] = [(source, _file_identity(source_details))]
    try:
        while pending:
            directory, expected_identity = pending.pop()
            directory_fd = (
                root_fd
                if directory == source
                else _open_beneath_root(root_fd, directory.relative_to(source).parts, flags)
            )
            if directory_fd is None:
                continue
            try:
                if _file_identity(os.fstat(directory_fd)) != expected_identity:
                    continue
                with os.scandir(directory_fd) as children:
                    for child in children:
                        try:
                            details = child.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if _is_link(details):
                            continue

                        path = directory / child.name
                        if destination == path or destination in path.parents:
                            continue
                        if stat.S_ISDIR(details.st_mode):
                            pending.append((path, _file_identity(details)))
                        elif stat.S_ISREG(details.st_mode) and details.st_mtime_ns <= cutoff_ns:
                            discovered.append(
                                FileEntry(path=path, size=details.st_size, mtime_ns=details.st_mtime_ns)
                            )
            finally:
                if directory_fd != root_fd:
                    os.close(directory_fd)
    finally:
        os.close(root_fd)
    return discovered


def _scan_by_path(source: Path, destination: Path, cutoff_ns: int) -> list[FileEntry]:
    """Windows fallback: validate paths before and immediately after opening.

    Windows os.scandir does not support directory descriptors. Rechecking
    identity and every ancestor prevents simple swaps, but malicious repeated
    replacements cannot be made atomic through this standard-library API.
    """
    discovered: list[FileEntry] = []
    pending = [source]
    while pending:
        directory = pending.pop()
        if not _check_directory(directory, source):
            continue
        before = directory.lstat()
        with os.scandir(directory) as children:
            if not _check_directory(directory, source) or not _same_file(before, directory.lstat()):
                raise ValueError(f"Directory changed during scan: {directory}")
            for child in children:
                try:
                    details = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if _is_link(details):
                    continue

                path = Path(child.path)
                if destination == path or destination in path.parents:
                    continue
                if stat.S_ISDIR(details.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(details.st_mode) and details.st_mtime_ns <= cutoff_ns:
                    discovered.append(FileEntry(path=path, size=details.st_size, mtime_ns=details.st_mtime_ns))

    return discovered


def scan_root(root: RootConfig) -> tuple[FileEntry, ...]:
    """Return stable regular files beneath a registered source root.

    Paths are absolute; results have a deterministic lexical path order.
    Symbolic links, Windows junctions and other reparse points are excluded,
    not traversed. An equal/nested destination is pruned from discovery.

    Invalid/linked source roots raise ``ValueError``. Permission and other I/O
    errors propagate so a partial scan is not mistaken for a complete one.
    Files that disappear while being listed are simply omitted.
    """
    try:
        settle_seconds = float(root.settle_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("settle_seconds must be a finite, nonnegative number") from exc
    if not math.isfinite(settle_seconds) or settle_seconds < 0:
        raise ValueError("settle_seconds must be a finite, nonnegative number")

    source = Path(root.source).absolute()
    try:
        source_details = source.lstat()
        resolved_source = source.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ValueError(f"Source root is missing or unsafe: {source}") from exc
    unsafe_root = _is_link(source_details) or not stat.S_ISDIR(source_details.st_mode)
    if os.name == "nt":
        try:
            unsafe_root = (
                unsafe_root
                or not _unlinked_directory_chain(source)
                or not os.path.samefile(source, resolved_source)
            )
        except OSError:
            unsafe_root = True
    else:
        unsafe_root = unsafe_root or source != resolved_source
    if unsafe_root:
        raise ValueError(f"Source root must be a real, unlinked directory: {source}")

    # Keep the lexical spelling on Windows: resolve() can expand 8.3 names,
    # breaking exclusion comparisons against entries under the source spelling.
    # The registered destination is independently checked for reparse points.
    destination = (
        Path(root.destination).absolute()
        if os.name == "nt"
        else Path(root.destination).resolve(strict=False)
    )
    if destination == source:
        return ()

    cutoff_ns = time.time_ns() - int(settle_seconds * 1_000_000_000)
    if _CAN_SCAN_FD:
        discovered = _scan_with_directory_fds(source, source_details, destination, cutoff_ns)
    else:
        discovered = _scan_by_path(source, destination, cutoff_ns)

    return tuple(sorted(discovered, key=lambda entry: str(entry.path)))
