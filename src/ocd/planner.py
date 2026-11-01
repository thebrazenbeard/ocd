"""Conservative, read-only plans for explicitly tagged files and folders.

``build_plan`` never creates a directory or moves a file.  A plan is only a
snapshot: the eventual applier must independently check source identity and
atomically refuse to replace a destination, since the disk can change after
planning.
"""

from __future__ import annotations

import math
import os
import re
import stat
import time
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .types import FileEntry, Hold, MovePlan, PlanResult, RootConfig


_EXTENSION = re.compile(r"\.?[a-zA-Z0-9]+\Z")
_WINDOWS_DEVICE = re.compile(
    r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])\Z",
    re.IGNORECASE,
)
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]')


def _safe_segment(value: str) -> str | None:
    """Produce a single component valid on both Windows and Linux."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value)
    # Surrogates and invisible format controls should not reach filenames.
    normalized = "".join(
        "_" if unicodedata.category(char) in {"Cs", "Cf"} else char
        for char in normalized
    )
    # Leading dots are legitimate (e.g. ``.camera.jpg``), but Windows strips
    # trailing spaces/dots when looking up a component.
    normalized = _ILLEGAL.sub("_", normalized).strip().rstrip(" .")
    if not normalized or normalized in {".", ".."}:
        return None
    if _WINDOWS_DEVICE.fullmatch(normalized.split(".", 1)[0].rstrip(" .")):
        normalized = "_" + normalized
    # Most common filesystems limit a component to 255 bytes/code units.
    if len(normalized.encode("utf-8")) > 255 or len(normalized.encode("utf-16-le")) // 2 > 255:
        return None
    return normalized


def _normalized_folder_key(key: str) -> str | None:
    """Validate a portable folder tag: ``folder:relative/subdirectory``.

    The slash-separated form is platform-independent. We deliberately reject
    paths that would be silently altered by filesystem normalization.
    """
    if not isinstance(key, str) or not key.startswith("folder:"):
        return None
    relative = key[len("folder:"):]
    if not relative or len(relative) > 1024 or "\\" in relative:
        return None
    parts = relative.split("/")
    if len(parts) > 64:
        return None
    normalized: list[str] = []
    for part in parts:
        if not part or part in {".", ".."}:
            return None
        segment = unicodedata.normalize("NFC", part)
        if _safe_segment(part) != segment:
            return None
        # A folder literally named "CON" is ambiguous on Windows, even on
        # POSIX where it can exist, so don't silently rewrite it.
        normalized.append(segment)
    return "folder:" + "/".join(normalized)


def _normalized_tags(tags: dict[str, str]) -> tuple[dict[str, str], dict[tuple[str, ...], str]] | None:
    if not isinstance(tags, dict):
        return None
    normalized: dict[str, str] = {}
    folders: dict[tuple[str, ...], str] = {}
    category_spellings: dict[str, str] = {}
    for extension, tag in tags.items():
        if not isinstance(extension, str):
            return None
        folder_key = _normalized_folder_key(extension) if extension.startswith("folder:") else None
        if folder_key is None and not _EXTENSION.fullmatch(extension):
            return None
        category = _safe_segment(tag)
        if category is None:
            return None
        if folder_key is not None:
            key_parts = tuple(part.casefold() for part in folder_key[len("folder:"):].split("/"))
            if key_parts in folders:
                return None
            folders[key_parts] = category
        else:
            key = "." + extension.lstrip(".").lower()
            # Different spellings of the same extension must not silently win.
            if key in normalized and normalized[key] != category:
                return None
            normalized[key] = category
        # Case-sensitive and case-insensitive machines must agree on the
        # directory names created by a given configuration.
        category_key = category.casefold()
        if category_key in category_spellings and category_spellings[category_key] != category:
            return None
        category_spellings[category_key] = category
    return normalized, folders


def _folder_category(root: RootConfig, path: Path,
                     folders: dict[tuple[str, ...], str]) -> str | None:
    """Most specific explicit ancestor folder wins; extensions are fallback."""
    try:
        ancestors = tuple(unicodedata.normalize("NFC", component).casefold()
                          for component in path.relative_to(root.source).parts[:-1])
    except ValueError:
        return None
    for depth in range(len(ancestors), 0, -1):
        category = folders.get(ancestors[:depth])
        if category is not None:
            return category
    return None


def _is_link_or_junction(path: Path) -> bool:
    # Also catch Windows reparse points other than ordinary junctions.
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _has_link_ancestor(path: Path) -> bool:
    current = Path(path.anchor)
    if _is_link_or_junction(current):
        return True
    for part in path.parts[1:]:
        current /= part
        if _is_link_or_junction(current):
            return True
    return False


def _root_error(root: RootConfig) -> tuple[str | None, Path | None, Path | None]:
    source, destination = root.source, root.destination
    if not isinstance(source, Path) or not isinstance(destination, Path):
        return "unsupported path flavor", None, None
    if not source.is_absolute() or not destination.is_absolute():
        return "roots must be absolute paths", None, None
    if ".." in source.parts or ".." in destination.parts:
        return "root path contains traversal", None, None
    if not isinstance(root.mode, str) or root.mode not in {"observe", "apply"}:
        return "unsupported mode", None, None
    try:
        interval = float(root.settle_seconds)
    except (TypeError, ValueError, OverflowError):
        interval = math.inf
    if isinstance(root.settle_seconds, bool) or not 0 <= interval <= 1_000_000_000:
        return "invalid settle interval", None, None
    try:
        real_source, real_destination = source.resolve(), destination.resolve()
        if (
            real_source == real_destination
            or real_source.is_relative_to(real_destination)
            or real_destination.is_relative_to(real_source)
        ):
            return "source and destination overlap", None, None
        if _has_link_ancestor(source) or _has_link_ancestor(destination):
            return "root contains symlink or reparse ancestor", None, None
        if source.exists() and not source.is_dir():
            return "source root is not a directory", None, None
        if destination.exists() and not destination.is_dir():
            return "destination root is not a directory", None, None
    except (OSError, RuntimeError, ValueError):
        return "unresolvable root path", None, None
    return None, real_source, real_destination


def _source_error(entry: FileEntry, root: RootConfig, real_source: Path) -> str | None:
    path = entry.path
    if not isinstance(path, Path) or not path.is_absolute():
        return "unsupported or relative source path"
    if ".." in path.parts:
        return "source path contains traversal"
    if (
        isinstance(entry.size, bool)
        or not isinstance(entry.size, int)
        or entry.size < 0
        or isinstance(entry.mtime_ns, bool)
        or not isinstance(entry.mtime_ns, int)
        or entry.mtime_ns < 0
    ):
        return "invalid file metadata"
    try:
        relative = path.relative_to(root.source)
        if not relative.parts:
            return "source path is the root"
        current = root.source
        for component in relative.parts:
            current = current / component
            if _is_link_or_junction(current):
                return "source symlink or junction"
        resolved = path.resolve()
        if not resolved.is_relative_to(real_source):
            return "source escapes root"
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            return "source is not a regular file"
        if status.st_size != entry.size or status.st_mtime_ns != entry.mtime_ns:
            return "source changed since scan"
    except (ValueError, OSError, RuntimeError):
        return "source missing, inaccessible, or outside root"
    return None


def _destination_error(destination_root: Path, category: str, filename: str) -> str | None:
    """Detect existing names, including portable case-insensitive collisions.

    A missing directory is fine: only a future authorized applier may create it.
    """
    directory = destination_root
    for component, leaf in ((category, False), (filename, True)):
        try:
            with os.scandir(directory) as entries:
                matches = [item.name for item in entries if item.name.casefold() == component.casefold()]
        except FileNotFoundError:
            return None
        except OSError:
            return "destination inaccessible or not a directory"
        if matches:
            if leaf:
                return "destination already exists"
            if len(matches) != 1 or matches[0] != component:
                return "destination category case collision"
            child = directory / component
            try:
                if _is_link_or_junction(child) or not child.is_dir():
                    return "destination category is not a safe directory"
            except (OSError, RuntimeError):
                return "destination category inaccessible"
        directory = directory / component
    return None


def build_plan(root: RootConfig, files: Iterable[FileEntry]) -> PlanResult:
    """Return deterministic candidate moves and explicit reasons for holds.

    Only explicitly tagged ancestor folders or extensions can move.
    Every candidate is checked against its root, source snapshot, other
    candidates, and existing destination names.  No filesystem writes occur.
    """
    entries = sorted(
        files,
        key=lambda item: (str(item.path).casefold(), str(item.path), repr(item.size), repr(item.mtime_ns)),
    )
    root_problem, real_source, real_destination = _root_error(root)
    normalized_tags = _normalized_tags(root.tags)
    if root_problem is None and normalized_tags is None:
        root_problem = "invalid or ambiguous tag mapping"
    if root_problem is not None:
        return PlanResult((), tuple(Hold(item.path, root_problem) for item in entries))
    assert real_source is not None and real_destination is not None and normalized_tags is not None
    extension_tags, folder_tags = normalized_tags

    now_ns = time.time_ns()
    settle_ns = int(root.settle_seconds * 1_000_000_000)
    candidates: list[MovePlan] = []
    holds: list[Hold] = []
    for entry in entries:
        problem = _source_error(entry, root, real_source)
        if problem is None and now_ns - entry.mtime_ns < settle_ns:
            problem = "file has not settled"
        extension = entry.path.suffix.lower() if isinstance(entry.path, Path) else ""
        category = (_folder_category(root, entry.path, folder_tags)
                    if isinstance(entry.path, Path) else None)
        if category is None:
            category = extension_tags.get(extension)
        if problem is None and category is None:
            problem = "file has no explicit tag (folder or extension)"
        filename = _safe_segment(entry.path.name) if isinstance(entry.path, Path) else None
        if problem is None and (filename is None or Path(filename).suffix.lower() != extension):
            problem = "unsafe or unsupported filename"
        if problem is not None:
            holds.append(Hold(entry.path, problem))
            continue
        assert category is not None and filename is not None
        target = root.destination / category / filename
        try:
            if not target.resolve().is_relative_to(real_destination):
                problem = "destination escapes root"
            elif os.name == "nt" and len(str(target)) > 259:
                problem = "destination exceeds portable Windows path limit"
            else:
                problem = _destination_error(root.destination, category, filename)
        except (OSError, RuntimeError, ValueError):
            problem = "destination path is unsafe"
        if problem is None:
            candidates.append(MovePlan(entry.path, target, category, entry.size, entry.mtime_ns))
        else:
            holds.append(Hold(entry.path, problem))

    # Hold *all* participants in a many-to-one collision, not whichever entry
    # happened to come after the first. Casefold also protects future Windows
    # use of a plan generated on a case-sensitive filesystem.
    by_target: dict[str, list[MovePlan]] = defaultdict(list)
    for candidate in candidates:
        by_target[unicodedata.normalize("NFC", str(candidate.destination)).casefold()].append(candidate)
    moves = []
    for group in by_target.values():
        if len(group) > 1:
            holds.extend(Hold(item.source, "multiple sources target the same destination") for item in group)
        else:
            moves.extend(group)
    moves.sort(key=lambda item: (str(item.source).casefold(), str(item.source)))
    holds.sort(key=lambda item: (str(item.path).casefold(), str(item.path), item.reason))
    return PlanResult(tuple(moves), tuple(holds))
