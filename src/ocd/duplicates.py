"""Read-only, bounded discovery of exact-content duplicate *candidates*.

The only search locations are explicitly supplied :class:`RootConfig` source
directories. No file is modified, moved, linked, or removed. Regular files of
every name/type are eligible, regardless of organizer tags or settle interval.

Candidates must have equal lengths and streaming SHA-256 digests. A digest is
not a promise that files will remain equal after the scan, nor permission to
delete them. ``potential_reclaim_bytes`` counts *logical* bytes in removable
single-link candidates; it is not a measurement or guarantee of physical disk
space (sparse files, compression, reflinks, and filesystem dedup can share it).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterable, Iterator

from .scanner import (
    _CAN_SCAN_FD, _check_directory, _is_link, _open_beneath_root,
    _same_file, _unlinked_directory_chain,
)
from .types import RootConfig


# CPython documents that DirEntry.stat() on Windows returns zero for st_dev,
# st_ino, and st_nlink, even for ordinary NTFS files. A separate Path.lstat()
# is necessary before file identity, stability, and hardlink checks can work.
_WINDOWS_NEEDS_PATH_LSTAT = os.name == "nt"
# NTFS can finalize an inode's change/creation timestamp a few milliseconds
# after a just-written file is closed. An enumerated candidate may therefore
# have an older st_ctime_ns than the *same* file opened for hashing. Validate
# its identity, size and mtime against enumeration, then use the opened handle
# as the strict ctime baseline for every check while/after reading.
_WINDOWS_CTIME_CAN_SETTLE = os.name == "nt"


@dataclass(frozen=True)
class DuplicateFile:
    path: Path
    size: int
    mtime_ns: int
    device: int
    inode: int
    link_count: int


@dataclass(frozen=True)
class DuplicateGroup:
    sha256: str
    size: int
    files: tuple[DuplicateFile, ...]
    logical_duplicate_bytes: int
    potential_reclaim_bytes: int


@dataclass(frozen=True)
class DuplicateReport:
    groups: tuple[DuplicateGroup, ...]
    files_scanned: int
    files_hashed: int
    skipped_hardlinks: int
    skipped_unstable: tuple[Path, ...]
    logical_duplicate_bytes: int
    potential_reclaim_bytes: int


@dataclass(frozen=True)
class _Candidate:
    file: DuplicateFile
    source: Path
    root_details: os.stat_result
    details: os.stat_result


def _check_limit(value: int, name: str, *, ceiling: int | None = None) -> None:
    if type(value) is not int or value <= 0 or (ceiling is not None and value > ceiling):
        requirement = f"between 1 and {ceiling}" if ceiling is not None else "positive"
        raise ValueError(f"{name} must be a {requirement} integer")


def _prune_destination_for_source(source: Path, destination: Path) -> Path:
    """Keep a Windows destination comparable to the source's 8.3 spelling.

    ``Path.resolve`` expands DOS short-name aliases. Without projecting a
    canonical destination's relative suffix onto the *original* source path,
    a destination within that source can be accidentally included in scans.
    This function affects pruning only; it never grants traversal access.
    """
    if os.name != "nt":
        return destination.resolve(strict=False)
    target = destination.absolute()
    try:
        canonical_source = source.resolve(strict=True)
        canonical_target = target.resolve(strict=False)
        relative = canonical_target.relative_to(canonical_source)
    except (OSError, RuntimeError, ValueError):
        return target
    return source.joinpath(*relative.parts)


def _admit_source(root: RootConfig) -> tuple[Path, os.stat_result, Path]:
    if not isinstance(root, RootConfig):
        raise TypeError("roots must contain registered RootConfig objects")
    source = Path(root.source).absolute()
    try:
        details = source.lstat()
        resolved = source.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ValueError(f"Source root is missing or unsafe: {source}") from exc
    unsafe = _is_link(details) or not stat.S_ISDIR(details.st_mode)
    if os.name == "nt":
        try:
            unsafe = unsafe or not _unlinked_directory_chain(source) or not os.path.samefile(source, resolved)
        except OSError:
            unsafe = True
    else:
        unsafe = unsafe or source != resolved
    if unsafe:
        raise ValueError(f"Source root must be a real, unlinked directory: {source}")
    destination = _prune_destination_for_source(source, Path(root.destination))
    return source, details, destination


def _from_stat(path: Path, source: Path, root_details: os.stat_result, details: os.stat_result) -> _Candidate:
    return _Candidate(
        file=DuplicateFile(
            path=path,
            size=details.st_size,
            mtime_ns=details.st_mtime_ns,
            device=details.st_dev,
            inode=details.st_ino,
            link_count=details.st_nlink,
        ),
        source=source,
        root_details=root_details,
        details=details,
    )


def _enumerate_root(
    source: Path,
    root_details: os.stat_result,
    destination: Path,
    budget: list[int],
) -> Iterator[_Candidate]:
    """Use scanner's no-follow traversal primitives, with an entry budget.

    ``budget`` is shared across source roots and includes directories, links,
    and nonregular entries, preventing unbounded queues of empty directories.
    """
    if destination == source:
        return
    pending: list[tuple[Path, os.stat_result]] = [(source, root_details)]
    if _CAN_SCAN_FD:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            root_fd = os.open(source, flags)
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno == errno.ELOOP:
                raise ValueError(f"Source root became unsafe: {source}") from exc
            raise
        try:
            if not _same_file(os.fstat(root_fd), root_details):
                raise ValueError(f"Source root changed during scan: {source}")
            while pending:
                directory, expected = pending.pop()
                directory_fd = (
                    root_fd
                    if directory == source
                    else _open_beneath_root(root_fd, directory.relative_to(source).parts, flags)
                )
                if directory_fd is None:
                    raise ValueError(f"Directory changed during scan: {directory}")
                try:
                    if not _same_file(os.fstat(directory_fd), expected):
                        raise ValueError(f"Directory changed during scan: {directory}")
                    with os.scandir(directory_fd) as children:
                        for child in children:
                            budget[0] -= 1
                            if budget[0] < 0:
                                raise ValueError("max_entries exceeded during duplicate scan")
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
                                pending.append((path, details))
                            elif stat.S_ISREG(details.st_mode):
                                yield _from_stat(path, source, root_details, details)
                finally:
                    if directory_fd != root_fd:
                        os.close(directory_fd)
        finally:
            os.close(root_fd)
    else:
        # Windows lacks scandir(fd)/openat: retain the scanner's path and
        # ancestor validation. This is best-effort against concurrent swaps.
        while pending:
            directory, expected = pending.pop()
            if not _check_directory(directory, source):
                raise ValueError(f"Directory changed during scan: {directory}")
            if not _same_file(directory.lstat(), expected):
                raise ValueError(f"Directory changed during scan: {directory}")
            with os.scandir(directory) as children:
                for child in children:
                    budget[0] -= 1
                    if budget[0] < 0:
                        raise ValueError("max_entries exceeded during duplicate scan")
                    path = directory / child.name
                    try:
                        details = (
                            path.lstat()
                            if _WINDOWS_NEEDS_PATH_LSTAT
                            else child.stat(follow_symlinks=False)
                        )
                    except FileNotFoundError:
                        continue
                    if _is_link(details):
                        continue
                    if destination == path or destination in path.parents:
                        continue
                    if stat.S_ISDIR(details.st_mode):
                        pending.append((path, details))
                    elif stat.S_ISREG(details.st_mode):
                        yield _from_stat(path, source, root_details, details)
            if not _check_directory(directory, source) or not _same_file(directory.lstat(), expected):
                raise ValueError(f"Directory changed during scan: {directory}")


def _matching(
    details: os.stat_result, original: os.stat_result, *, check_ctime: bool = True,
) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and not _is_link(details)
        and details.st_dev == original.st_dev
        and details.st_ino == original.st_ino
        and details.st_size == original.st_size
        and details.st_mtime_ns == original.st_mtime_ns
        and (not check_ctime or details.st_ctime_ns == original.st_ctime_ns)
        and details.st_nlink == original.st_nlink
    )


def _hash_candidate(candidate: _Candidate, chunk_size: int) -> str | None:
    """Hash a revalidated regular file; return None if it changed or vanished."""
    path = candidate.file.path
    source = candidate.source
    fd: int | None = None
    parent_fd: int | None = None
    root_fd: int | None = None
    try:
        if _CAN_SCAN_FD:
            dirflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            root_fd = os.open(source, dirflags)
            if not _same_file(os.fstat(root_fd), candidate.root_details):
                return None
            components = path.relative_to(source).parts
            parent_fd = (
                root_fd if len(components) == 1 else _open_beneath_root(root_fd, components[:-1], dirflags)
            )
            if parent_fd is None:
                return None
            fileflags = (
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            )
            fd = os.open(components[-1], fileflags, dir_fd=parent_fd)
        else:
            if not _check_directory(source, source):
                return None
            if not _same_file(source.lstat(), candidate.root_details):
                return None
            # Validate the complete ancestor chain and the leaf before open.
            if path.parent != source and not _check_directory(path.parent, source):
                return None
            if not _matching(
                path.lstat(), candidate.details, check_ctime=not _WINDOWS_CTIME_CAN_SETTLE,
            ):
                return None
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))

        before = os.fstat(fd)
        if not _matching(
            before, candidate.details, check_ctime=not _WINDOWS_CTIME_CAN_SETTLE,
        ):
            return None
        digest = hashlib.sha256()
        total = 0
        while total <= candidate.file.size:
            block = os.read(fd, min(chunk_size, candidate.file.size + 1 - total))
            if not block:
                break
            total += len(block)
            digest.update(block)
        if total != candidate.file.size or not _matching(os.fstat(fd), before):
            return None
        if not _CAN_SCAN_FD:
            if not _matching(path.lstat(), before):
                return None
            if path.parent != source and not _check_directory(path.parent, source):
                return None
            if not _check_directory(source, source) or not _same_file(source.lstat(), candidate.root_details):
                return None
        return digest.hexdigest()
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ESTALE, errno.EISDIR):
            return None
        raise
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None and parent_fd != root_fd:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)


def _reclaimable_count(files: tuple[DuplicateFile, ...]) -> int:
    """Count distinct single-link files avoidable while retaining one copy."""
    singles = sum(item.link_count == 1 for item in files)
    # A multiply-linked inode is effectively retained unless all its links
    # (possibly outside these registered roots) are independently accounted for.
    return max(0, singles - (0 if singles < len(files) else 1))


def scan_duplicates(
    roots: Iterable[RootConfig],
    *,
    max_files: int = 100_000,
    max_entries: int = 200_000,
    max_roots: int = 64,
    chunk_size: int = 1024 * 1024,
) -> DuplicateReport:
    """Find matching-size/SHA-256 candidate groups under registered roots.

    The enumeration/file/root budgets are hard limits: exceeding one raises
    ``ValueError`` rather than returning a misleading partial report. Content
    is streamed in chunks of at most 8 MiB. Disappeared/replaced/modified
    files are reported in ``skipped_unstable`` and never put in a group.

    Each physical inode is counted only once even if exposed by several hard
    links or overlapping roots. A missing/unsafe root is an error. Stable
    permissions and other unexpected I/O errors propagate to the caller.
    """
    _check_limit(max_files, "max_files")
    _check_limit(max_entries, "max_entries")
    _check_limit(max_roots, "max_roots")
    _check_limit(chunk_size, "chunk_size", ceiling=8 * 1024 * 1024)

    candidates: list[_Candidate] = []
    seen_paths: set[Path] = set()
    budget = [max_entries]
    count_roots = 0
    for root in roots:
        count_roots += 1
        if count_roots > max_roots:
            raise ValueError("max_roots exceeded during duplicate scan")
        source, source_details, destination = _admit_source(root)
        for candidate in _enumerate_root(source, source_details, destination, budget):
            if candidate.file.path in seen_paths:
                continue
            if len(candidates) >= max_files:
                raise ValueError("max_files exceeded during duplicate scan")
            seen_paths.add(candidate.file.path)
            candidates.append(candidate)

    # The selection among multiple names for one inode must not depend on
    # filesystem enumeration order or the order of registered roots.
    candidates.sort(key=lambda candidate: str(candidate.file.path))
    unique: list[_Candidate] = []
    seen_inodes: set[tuple[int, int]] = set()
    skipped_hardlinks = 0
    for candidate in candidates:
        details = candidate.details
        # Some filesystems return st_ino=0; never conflate distinct files on
        # an unknown identity. A repeated path was already removed above.
        identity = (details.st_dev, details.st_ino)
        if details.st_ino and identity in seen_inodes:
            skipped_hardlinks += 1
            continue
        if details.st_ino:
            seen_inodes.add(identity)
        unique.append(candidate)

    by_size: dict[int, list[_Candidate]] = defaultdict(list)
    for candidate in unique:
        by_size[candidate.file.size].append(candidate)

    by_digest: dict[tuple[int, str], list[DuplicateFile]] = defaultdict(list)
    files_hashed = 0
    unstable: list[Path] = []
    for size in sorted(by_size):
        if len(by_size[size]) < 2:
            continue
        for candidate in by_size[size]:
            digest = _hash_candidate(candidate, chunk_size)
            if digest is None:
                unstable.append(candidate.file.path)
            else:
                files_hashed += 1
                by_digest[(size, digest)].append(candidate.file)

    groups: list[DuplicateGroup] = []
    for (size, digest), files in by_digest.items():
        if len(files) < 2:
            continue
        ordered = tuple(sorted(files, key=lambda item: str(item.path)))
        groups.append(
            DuplicateGroup(
                sha256=digest,
                size=size,
                files=ordered,
                logical_duplicate_bytes=size * (len(ordered) - 1),
                potential_reclaim_bytes=size * _reclaimable_count(ordered),
            )
        )
    groups.sort(key=lambda group: (str(group.files[0].path), group.size, group.sha256))
    return DuplicateReport(
        groups=tuple(groups),
        files_scanned=len(unique),
        files_hashed=files_hashed,
        skipped_hardlinks=skipped_hardlinks,
        skipped_unstable=tuple(sorted(unstable, key=str)),
        logical_duplicate_bytes=sum(group.logical_duplicate_bytes for group in groups),
        potential_reclaim_bytes=sum(group.potential_reclaim_bytes for group in groups),
    )
