"""Bounded, read-only storage usage inventory for an explicitly selected directory.

Logical byte totals count directory entries (including hardlink aliases). POSIX
allocated bytes are an estimate using st_blocks, deduplicated by file identity.
Windows physical allocation is deliberately reported as unknown, not guessed.
No file contents are read and no files are modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import os
from pathlib import Path
import stat

from .scanner import _CAN_SCAN_FD, _check_directory, _is_link, _open_beneath_root


@dataclass(frozen=True)
class UsageItem:
    path: Path
    logical_bytes: int
    allocated_bytes: int | None


@dataclass(frozen=True)
class DirectoryUsage:
    """Observed recursive logical bytes; physical allocation is not attributable.

    Entries whose descendants could not all be scanned are conservative partial
    observations. Individual directory allocations cannot be summed safely in
    the presence of hardlinks spanning sibling directories.
    """

    path: Path
    logical_bytes: int
    allocated_bytes: int | None
    complete: bool


@dataclass(frozen=True)
class UsageReport:
    root: Path
    entries_visited: int
    files: int
    directories: int
    skipped_links: int
    skipped_special: int
    logical_bytes: int
    allocated_bytes: int | None
    largest_files: tuple[UsageItem, ...]
    largest_children: tuple[UsageItem, ...]
    issues: tuple[str, ...]
    complete: bool
    directory_totals: tuple[DirectoryUsage, ...] = ()


def scan_usage(
    root: Path, *, max_entries: int = 100_000, top: int = 20, max_depth: int = 32,
    include_hierarchy: bool = False,
) -> UsageReport:
    """Inventory a selected root without following directory links.

    Every skipped/inaccessible subtree or exhausted budget makes the report
    incomplete. Even a complete result is a snapshot, not a deletion order.
    """
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("usage root must be an explicit absolute directory")
    if type(max_entries) is not int or not 1 <= max_entries <= 1_000_000:
        raise ValueError("max_entries must be between 1 and 1000000")
    if type(top) is not int or not 1 <= top <= 100:
        raise ValueError("top must be between 1 and 100")
    if type(max_depth) is not int or not 0 <= max_depth <= 64:
        raise ValueError("max_depth must be between 0 and 64")
    if type(include_hierarchy) is not bool:
        raise ValueError("include_hierarchy must be a boolean")
    original = root.lstat()
    if _is_link(original) or not stat.S_ISDIR(original.st_mode):
        raise ValueError("usage root must be a real directory, not a link or reparse point")
    if not _check_directory(root, root):
        raise ValueError("usage root cannot be verified")

    entries = files = directories = links = special = logical = allocated = 0
    allocated_known = os.name != "nt"
    identities: set[tuple[int, int]] = set()
    children: dict[Path, int] = {}
    directory_bytes: dict[Path, int] = {root: 0} if include_hierarchy else {}
    largest: list[UsageItem] = []
    issues: list[str] = []
    stack = [(root, 0, (original.st_dev, original.st_ino, original.st_mtime_ns))]
    stopped = False

    def issue(message: str) -> None:
        if len(issues) < 20:
            issues.append(message)
        elif len(issues) == 20:
            issues.append("additional scan errors omitted")

    dirflags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    ) if _CAN_SCAN_FD else 0
    root_fd: int | None = None
    if _CAN_SCAN_FD:
        root_fd = os.open(root, dirflags)
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (original.st_dev, original.st_ino):
            os.close(root_fd)
            raise ValueError("usage root identity changed before opening")
    try:
        while stack and not stopped:
            directory, depth, expected = stack.pop()
            directory_fd: int | None = None
            try:
                if root_fd is not None:
                    components = directory.relative_to(root).parts
                    directory_fd = (
                        root_fd if not components else
                        _open_beneath_root(root_fd, components, dirflags)
                    )
                    if directory_fd is None:
                        issue(f"directory replaced or linked: {directory}")
                        continue
                    handle = directory_fd
                else:
                    if not _check_directory(directory, root):
                        issue(f"directory unavailable: {directory}")
                        continue
                    handle = directory

                current = (
                    os.fstat(directory_fd) if directory_fd is not None
                    else directory.lstat()
                )
                if (_is_link(current) or not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev, current.st_ino, current.st_mtime_ns) != expected):
                    issue(f"directory identity or modification time changed: {directory}")
                    continue

                # This directory's scan-time identity is verified. A queued
                # child may already have an observed-zero row, but the report
                # stays incomplete if its contents could not be inspected.
                if include_hierarchy:
                    directory_bytes.setdefault(directory, 0)

                remaining = max_entries - entries
                with os.scandir(handle) as stream:
                    names = [entry.name for entry in islice(stream, remaining + 1)]
                if len(names) > remaining:
                    issue(f"entry budget reached at {directory}")
                    names = names[:remaining]
                    stopped = True
                for name in sorted(names, key=lambda value: (value.casefold(), value)):
                    entries += 1
                    path = directory / name
                    try:
                        details = (
                            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                            if directory_fd is not None else path.lstat()
                        )
                    except (OSError, ValueError) as exc:
                        issue(f"cannot stat {path}: {type(exc).__name__}")
                        continue
                    if _is_link(details):
                        links += 1
                        continue
                    if details.st_dev != original.st_dev:
                        issue(f"other filesystem held: {path}")
                        continue
                    if stat.S_ISDIR(details.st_mode):
                        directories += 1
                        # Preserve a discovered node for incomplete-scan
                        # visibility; zero is observed bytes, never proof of
                        # emptiness unless the complete flag is true.
                        if include_hierarchy:
                            directory_bytes.setdefault(path, 0)
                        if depth >= max_depth:
                            issue(f"depth budget reached at {path}")
                        else:
                            stack.append((path, depth + 1, (details.st_dev, details.st_ino, details.st_mtime_ns)))
                        continue
                    if not stat.S_ISREG(details.st_mode):
                        special += 1
                        continue
                    files += 1
                    size = details.st_size
                    logical += size
                    if include_hierarchy:
                        parent = path.parent
                        while True:
                            directory_bytes[parent] += size
                            if parent == root:
                                break
                            parent = parent.parent
                    first = path.relative_to(root).parts
                    bucket = root / first[0] if len(first) > 1 else root
                    children[bucket] = children.get(bucket, 0) + size
                    physical: int | None = None
                    blocks = getattr(details, "st_blocks", None)
                    identity = (details.st_dev, details.st_ino)
                    if allocated_known and isinstance(blocks, int) and blocks >= 0 and identity[1] != 0:
                        if identity not in identities:
                            physical = blocks * 512
                            allocated += physical
                            identities.add(identity)
                        # Shared hardlink allocation has no safe per-path attribution.
                    else:
                        allocated_known = False
                    largest.append(UsageItem(path, size, physical))
                    if len(largest) > top:
                        largest.sort(key=lambda item: (-item.logical_bytes, str(item.path)))
                        largest.pop()
                after = (
                    os.fstat(directory_fd) if directory_fd is not None
                    else directory.lstat()
                )
                if (_is_link(after) or not stat.S_ISDIR(after.st_mode)
                        or (after.st_dev, after.st_ino, after.st_mtime_ns) != expected):
                    issue(f"directory changed during inventory: {directory}")
            except (OSError, ValueError) as exc:
                issue(f"cannot scan {directory}: {type(exc).__name__}")
            finally:
                if directory_fd is not None and directory_fd != root_fd:
                    os.close(directory_fd)
    finally:
        if root_fd is not None:
            os.close(root_fd)

    try:
        now = root.lstat()
        if (_is_link(now) or not stat.S_ISDIR(now.st_mode)
                or now.st_dev != original.st_dev or now.st_ino != original.st_ino
                or now.st_mtime_ns != original.st_mtime_ns):
            issue("usage root identity changed during scan")
    except OSError:
        issue("usage root disappeared during scan")

    largest.sort(key=lambda item: (-item.logical_bytes, str(item.path)))
    largest_children = tuple(
        UsageItem(path, size, None)
        for path, size in sorted(children.items(), key=lambda item: (-item[1], str(item[0])))[:top]
    )
    complete = not issues and not stopped
    directory_totals = tuple(
        DirectoryUsage(path, size, None, complete)
        for path, size in sorted(
            directory_bytes.items(),
            key=lambda entry: (entry[0] != root, str(entry[0])),
        )
    )
    return UsageReport(
        root=root, entries_visited=entries, files=files, directories=directories,
        skipped_links=links, skipped_special=special, logical_bytes=logical,
        allocated_bytes=allocated if allocated_known else None,
        largest_files=tuple(largest), largest_children=largest_children,
        issues=tuple(issues), complete=complete, directory_totals=directory_totals,
    )
