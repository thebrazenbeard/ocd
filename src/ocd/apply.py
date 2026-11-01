"""Conservative, journalled application of organizer move plans.

This module deliberately has no copy/delete fallback.  An interrupted move is
left for a human to reconcile rather than guessed at on the next invocation.
"""

from __future__ import annotations

import ctypes
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Iterable, Iterator, TextIO
from uuid import uuid4

from .types import Hold, MovePlan, RootConfig


@dataclass(frozen=True)
class ApplyResult:
    """Net outcome; ``moved`` excludes moves successfully rolled back."""

    moved: tuple[MovePlan, ...] = ()
    observed: tuple[MovePlan, ...] = ()
    rolled_back: tuple[MovePlan, ...] = ()
    holds: tuple[Hold, ...] = ()
    recovery_required: bool = False


class _UnsafeMove(Exception):
    """A plan or filesystem state failed a safety check."""


# Windows cannot take a CRT byte-range lock on a directory handle.  Keep its
# sentinel for the lifetime of the application; removing it while another
# process holds the old inode would let two writers acquire different locks.
_WINDOWS_LOCK_NAME = ".ocd-apply.lock"


def _absolute_clean(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise _UnsafeMove(f"path must be absolute without '..': {path}")
    return path


def _is_reparse(st: os.stat_result) -> bool:
    # Windows junctions and other reparse points must not bypass the symlink
    # check.  This attribute is not present on POSIX stat results.
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _inspect_components(path: Path, *, missing_ok: bool = False) -> os.stat_result:
    """Reject symlinks in *any* existing component, not just the final path.

    Returns the last existing component's lstat; callers may use its st_dev
    for a same-filesystem check before creating missing destination folders.
    """
    path = _absolute_clean(path)
    current = Path(path.anchor)
    last = os.lstat(current)
    for part in path.parts[1:]:
        current = current / part
        try:
            entry = os.lstat(current)
        except FileNotFoundError:
            if missing_ok:
                return last
            raise _UnsafeMove(f"missing path: {current}") from None
        if _is_reparse(entry):
            raise _UnsafeMove(f"symlink/reparse traversal refused: {current}")
        if current != path and not stat.S_ISDIR(entry.st_mode):
            raise _UnsafeMove(f"non-directory path component: {current}")
        last = entry
    return last


def _root_devices(root: RootConfig) -> tuple[int, int]:
    source = _absolute_clean(root.source)
    destination = _absolute_clean(root.destination)
    src = _inspect_components(source)
    dst = _inspect_components(destination)
    if not stat.S_ISDIR(src.st_mode) or not stat.S_ISDIR(dst.st_mode):
        raise _UnsafeMove("configured source and destination must be existing directories")
    if src.st_dev != dst.st_dev:
        raise _UnsafeMove("cross-filesystem moves are forbidden")
    return src.st_dev, dst.st_dev


def _verify_move(
    root: RootConfig, plan: MovePlan, *, identity: tuple[int, int] | None = None
) -> tuple[int, int]:
    source = _absolute_clean(plan.source)
    destination = _absolute_clean(plan.destination)
    try:
        source.relative_to(_absolute_clean(root.source))
        destination.relative_to(_absolute_clean(root.destination))
    except ValueError:
        raise _UnsafeMove("move escapes its configured source/destination root") from None
    if source == destination:
        raise _UnsafeMove("source and destination are identical")
    root_dev, _ = _root_devices(root)
    src_st = _inspect_components(source)
    if not stat.S_ISREG(src_st.st_mode):
        raise _UnsafeMove(f"source is not a regular file: {source}")
    if src_st.st_dev != root_dev:
        raise _UnsafeMove("source crosses a filesystem mount")
    if src_st.st_size != plan.size or src_st.st_mtime_ns != plan.source_mtime_ns:
        raise _UnsafeMove("source size/mtime changed since planning")
    actual_identity = (src_st.st_dev, src_st.st_ino)
    if identity is not None and actual_identity != identity:
        raise _UnsafeMove("source file identity changed since preflight")

    # A dangling symlink is also an occupied destination.  lstat, not exists,
    # is important here; an occupied destination must NEVER be overwritten.
    _inspect_components(destination.parent, missing_ok=True)
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        raise _UnsafeMove(f"destination already exists: {destination}")
    parent_st = _inspect_components(destination.parent, missing_ok=True)
    if parent_st.st_dev != src_st.st_dev:
        raise _UnsafeMove("destination crosses a filesystem mount")
    return actual_identity


def _ensure_destination_parents(root: RootConfig, destination: Path) -> None:
    """Create directories one at a time, rejecting symlinks at each step."""
    base = _absolute_clean(root.destination)
    relative = _absolute_clean(destination).parent.relative_to(base)
    if not sys.platform.startswith("linux"):
        raise _UnsafeMove("safe destination parent creation is unavailable on this platform")
    fd = _open_directory_nofollow(base)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        for part in relative.parts:
            try:
                os.mkdir(part, dir_fd=fd)
                # A new directory entry must be durable before a file is
                # moved below it and journalled as complete.
                os.fsync(fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    finally:
        os.close(fd)


def _open_directory_nofollow(path: Path) -> int:
    """Open a Linux directory one component at a time without following links."""
    path = _absolute_clean(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _exclusive_directory_lock(directory: Path) -> Iterator[None]:
    """Nonblocking process-wide exclusion for a directory's apply activity.

    No PID file or timeout is involved: the OS releases these advisory locks
    when the owning descriptor closes, including after an unclean process exit.
    On Linux, locking the anchored directory inode also avoids a replaceable
    sidecar file in the source tree.  Windows needs a persistent sidecar for
    msvcrt's mandatory one-byte file-region lock.
    """
    directory = _absolute_clean(directory)
    if sys.platform.startswith("linux"):
        import fcntl

        fd = _open_directory_nofollow(directory)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)  # Releases flock even if the body raises.
        return

    if os.name == "nt":
        import msvcrt

        details = _inspect_components(directory)
        if not stat.S_ISDIR(details.st_mode):
            raise _UnsafeMove(f"apply lock parent is not a directory: {directory}")
        path = directory / _WINDOWS_LOCK_NAME
        flags = os.O_RDWR | os.O_CREAT | os.O_BINARY | os.O_NOINHERIT
        fd = os.open(path, flags, 0o600)
        locked = False
        try:
            opened = os.fstat(fd)
            current = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_reparse(opened)
                or _is_reparse(current)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise _UnsafeMove(f"unsafe apply lock file: {path}")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            locked = True
            # Catch a sidecar replacement between the initial lstat and
            # acquiring the byte-range lock. Never unlink a locked sidecar.
            current = os.lstat(path)
            if _is_reparse(current) or (
                opened.st_dev, opened.st_ino
            ) != (current.st_dev, current.st_ino):
                raise _UnsafeMove(f"apply lock file changed during acquisition: {path}")
            yield
        finally:
            try:
                if locked:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)
        return

    raise _UnsafeMove("interprocess apply locking unavailable on this platform")


@contextmanager
def _exclusive_apply_locks(root: RootConfig, journal_path: Path) -> Iterator[None]:
    """Lock both roots and the journal directory before the first preflight.

    Root locks also exclude writers using different journal files; the journal
    directory lock excludes journal writers using different root configs.
    Sorting and deduplicating the paths keeps acquisition deterministic.
    """
    journal = _absolute_clean(Path(journal_path))
    directories = sorted(
        {_absolute_clean(root.source), _absolute_clean(root.destination), journal.parent},
        key=os.fspath,
    )
    with ExitStack() as stack:
        for directory in directories:
            stack.enter_context(_exclusive_directory_lock(directory))
        yield


def _rename_noreplace(
    source: Path, destination: Path, *,
    identity: tuple[int, int], size: int, mtime_ns: int,
) -> None:
    """Atomic rename-without-replacement on supported Linux kernels.

    A pre-rename existence check alone is insufficient on POSIX: os.rename()
    would replace a destination created by another process in the meantime.
    Linux renameat2(RENAME_NOREPLACE) provides the needed atomic condition.
    """
    if os.name == "nt":
        raise _UnsafeMove(
            "Windows apply disabled until anchored Win32 directory-handle rename is qualified"
        )
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        source_fd = _open_directory_nofollow(source.parent)
        try:
            destination_fd = _open_directory_nofollow(destination.parent)
            try:
                if os.fstat(source_fd).st_dev != os.fstat(destination_fd).st_dev:
                    raise _UnsafeMove("cross-filesystem rename refused at commit")
                current = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or _is_reparse(current):
                    raise _UnsafeMove("source is no longer a regular file at commit")
                if (current.st_dev, current.st_ino) != identity:
                    raise _UnsafeMove("source identity changed immediately before rename")
                if (current.st_size, current.st_mtime_ns) != (size, mtime_ns):
                    raise _UnsafeMove("source metadata changed immediately before rename")
                result = function(
                    source_fd, os.fsencode(source.name),
                    destination_fd, os.fsencode(destination.name), 1,
                )
                if result != 0:
                    number = ctypes.get_errno()
                    raise OSError(number, os.strerror(number), str(source), str(destination))
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
        return
    raise OSError(errno.ENOTSUP, "atomic no-replace rename unsupported on this platform")


def _fsync_rename_parents(source: Path, destination: Path) -> None:
    """Make a committed Linux rename durable before recording completion."""
    if not sys.platform.startswith("linux"):
        raise _UnsafeMove("directory durability unsupported on this platform")
    source_fd = _open_directory_nofollow(source.parent)
    try:
        destination_fd = _open_directory_nofollow(destination.parent)
        try:
            os.fsync(source_fd)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _journal_event(handle: TextIO, **fields: object) -> None:
    handle.write(json.dumps({"at_ns": time.time_ns(), **fields}, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _open_journal(path: Path) -> TextIO:
    path = _absolute_clean(path)
    parent = _inspect_components(path.parent)
    if not stat.S_ISDIR(parent.st_mode):
        raise _UnsafeMove("journal parent is not a directory")
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if sys.platform.startswith("linux"):
        parent_fd = _open_directory_nofollow(path.parent)
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
            # Sync the directory entry before any file mutation: a durable
            # intent is useless if the journal filename disappears on crash.
            os.fsync(parent_fd)
        except BaseException:
            if "fd" in locals():
                os.close(fd)
            raise
        finally:
            os.close(parent_fd)
    else:
        fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _UnsafeMove("journal must be a regular file")
        return os.fdopen(fd, "r+", encoding="utf-8", newline="\n")
    except BaseException:
        os.close(fd)
        raise


def _pending_journal(handle: TextIO) -> bool:
    """Unclosed batches or malformed journal records require manual review."""
    handle.seek(0)
    pending: dict[str, dict[str, str]] = {}
    for line in handle:
        try:
            if not line.endswith("\n"):
                return True
            record = json.loads(line)
            if not isinstance(record, dict):
                return True
            kind, tx = record["event"], record["transaction"]
            if not isinstance(tx, str):
                return True
            if kind == "begin":
                if tx in pending:
                    return True
                pending[tx] = {}
            elif kind == "end":
                if tx not in pending:
                    return True
                status = record.get("status")
                expected = {"complete"} if status == "complete" else {"aborted", "reversed"}
                if status not in {"complete", "rolled_back"}:
                    return True
                if any(state not in expected for state in pending[tx].values()):
                    return True
                del pending[tx]
            elif kind in {"intent", "complete", "abort_intent", "rollback_intent", "rollback_complete"}:
                if tx not in pending:
                    return True
                source = record.get("source")
                if not isinstance(source, str):
                    return True
                states = pending[tx]
                before, after = {
                    "intent": (None, "intent"),
                    "complete": ("intent", "complete"),
                    "abort_intent": ("intent", "aborted"),
                    "rollback_intent": ("complete", "reversing"),
                    "rollback_complete": ("reversing", "reversed"),
                }[kind]
                if states.get(source) != before:
                    return True
                states[source] = after
            else:
                return True
        except (ValueError, KeyError, TypeError):
            return True
    handle.seek(0, os.SEEK_END)
    return bool(pending)


def _plan_fields(plan: MovePlan) -> dict[str, object]:
    return {
        "source": str(plan.source),
        "destination": str(plan.destination),
        "tag": plan.tag,
        "size": plan.size,
        "source_mtime_ns": plan.source_mtime_ns,
    }


def _hold_all(plans: tuple[MovePlan, ...], reason: str, *, recovery: bool = False) -> ApplyResult:
    return ApplyResult(
        holds=tuple(Hold(plan.source, reason) for plan in plans),
        recovery_required=recovery,
    )


def apply_moves(root: RootConfig, moves: Iterable[MovePlan], journal_path: Path) -> ApplyResult:
    """Apply an explicitly enabled batch without overwriting or deleting files.

    Observe mode does not even create a journal or destination directory.  Any
    failed preflight blocks the entire batch.  A runtime failure attempts
    reverse-order rollback using the same atomic no-replace primitive.  An
    ambiguous or incomplete journal blocks future application until reviewed.
    """
    plans = tuple(moves)
    if root.mode == "observe":
        return ApplyResult(observed=plans)
    if root.mode != "apply":
        return _hold_all(plans, f"unsupported mutation mode: {root.mode!r}")
    if not plans:
        return ApplyResult()
    if os.name == "nt":
        return _hold_all(
            plans,
            "Windows apply disabled: native no-replace hardlink races, secure journal "
            "publication, directory creation, durability and rollback require "
            "Windows-host qualification; no moves attempted",
        )

    try:
        # Keep every preflight check, journal read/write, forward rename, and
        # reverse-order rollback inside the *same* interprocess lock scope.
        with _exclusive_apply_locks(root, journal_path):
            return _apply_moves_locked(root, plans, journal_path)
    except (OSError, ValueError, _UnsafeMove) as exc:
        # Lock contention or unsafe lock targets are a normal hold, not a
        # recovery incident: nothing has been mutated by this transaction.
        return _hold_all(plans, f"apply lock unavailable; no moves attempted: {exc}")


def _apply_moves_locked(
    root: RootConfig, plans: tuple[MovePlan, ...], journal_path: Path
) -> ApplyResult:
    """Execute an apply batch while all root/journal locks remain held."""

    identities: dict[Path, tuple[int, int]] = {}
    sources: set[Path] = set()
    destinations: set[Path] = set()
    try:
        journal = _absolute_clean(Path(journal_path))
        if os.name == "nt":
            reserved = {directory / _WINDOWS_LOCK_NAME for directory in (
                _absolute_clean(root.source), _absolute_clean(root.destination), journal.parent,
            )}
        else:
            reserved = set()
        reserved.add(journal)
        for plan in plans:
            if plan.source in reserved or plan.destination in reserved:
                raise _UnsafeMove("batch overlaps the journal or an apply lock file")
            if plan.source in sources or plan.destination in destinations:
                raise _UnsafeMove("duplicate source or destination in batch")
            sources.add(plan.source)
            destinations.add(plan.destination)
            identities[plan.source] = _verify_move(root, plan)
        if sources & destinations:
            raise _UnsafeMove("a batch destination overlaps another batch source")
    except (OSError, ValueError, _UnsafeMove) as exc:
        return _hold_all(plans, f"preflight hold: {exc}")

    moved: list[MovePlan] = []
    rolled_back: list[MovePlan] = []
    holds: list[Hold] = []
    transaction = uuid4().hex
    try:
        with _open_journal(journal_path) as journal:
            if _pending_journal(journal):
                return _hold_all(plans, "journal needs manual recovery; no moves attempted", recovery=True)
            _journal_event(journal, event="begin", transaction=transaction)
            uncertain = False
            for index, plan in enumerate(plans):
                intent_written = False
                try:
                    # Intent is fsynced before creating even a target
                    # directory. Directory creation is a mutation too.
                    _journal_event(journal, event="intent", transaction=transaction, **_plan_fields(plan))
                    intent_written = True
                    _ensure_destination_parents(root, plan.destination)
                    _verify_move(root, plan, identity=identities[plan.source])
                    _rename_noreplace(
                        plan.source, plan.destination,
                        identity=identities[plan.source], size=plan.size,
                        mtime_ns=plan.source_mtime_ns,
                    )
                    moved.append(plan)
                    # A journal write failure here means the rename succeeded
                    # but its durable completion record may be absent.
                    try:
                        _fsync_rename_parents(plan.source, plan.destination)
                        _journal_event(journal, event="complete", transaction=transaction, **_plan_fields(plan))
                    except (OSError, ValueError):
                        uncertain = True
                        raise
                except (OSError, ValueError, _UnsafeMove) as exc:
                    holds.append(Hold(plan.source, f"apply failed: {exc}"))
                    holds.extend(Hold(p.source, "batch stopped after a failed move") for p in plans[index + 1 :])
                    if intent_written and plan not in moved and not uncertain:
                        try:
                            _journal_event(
                                journal, event="abort_intent", transaction=transaction,
                                **_plan_fields(plan),
                            )
                        except (OSError, ValueError):
                            uncertain = True
                    if uncertain:
                        return ApplyResult(tuple(moved), (), (), tuple(holds), True)
                    for previous in reversed(moved[:]):
                        try:
                            # Rollback must never overwrite a source newly
                            # created by another process. Validate the moved
                            # file identity, size and mtime before reversal.
                            back = MovePlan(
                                previous.destination,
                                previous.source,
                                previous.tag,
                                previous.size,
                                previous.source_mtime_ns,
                            )
                            # The reverse path does not follow RootConfig's
                            # forward containment direction.
                            destination_st = _inspect_components(previous.destination)
                            if not stat.S_ISREG(destination_st.st_mode):
                                raise _UnsafeMove("rollback target is no longer a regular file")
                            if (destination_st.st_dev, destination_st.st_ino) != identities[previous.source]:
                                raise _UnsafeMove("rollback target identity changed")
                            if (destination_st.st_size, destination_st.st_mtime_ns) != (
                                back.size, back.source_mtime_ns
                            ):
                                raise _UnsafeMove("rollback target metadata changed")
                            _inspect_components(previous.source.parent)
                            try:
                                os.lstat(previous.source)
                            except FileNotFoundError:
                                pass
                            else:
                                raise _UnsafeMove("rollback source path is occupied")
                            _journal_event(
                                journal, event="rollback_intent", transaction=transaction,
                                **_plan_fields(previous),
                            )
                            _rename_noreplace(
                                previous.destination, previous.source,
                                identity=identities[previous.source], size=previous.size,
                                mtime_ns=previous.source_mtime_ns,
                            )
                            moved.remove(previous)
                            rolled_back.append(previous)
                            _fsync_rename_parents(previous.destination, previous.source)
                            _journal_event(
                                journal, event="rollback_complete", transaction=transaction,
                                **_plan_fields(previous),
                            )
                        except (OSError, ValueError, _UnsafeMove) as rollback_exc:
                            holds.append(Hold(previous.destination, f"rollback needs manual recovery: {rollback_exc}"))
                            uncertain = True
                            break
                    if not uncertain:
                        try:
                            _journal_event(journal, event="end", transaction=transaction, status="rolled_back")
                        except OSError as journal_exc:
                            holds.append(Hold(Path(journal_path), f"journal end failed: {journal_exc}"))
                            uncertain = True
                    return ApplyResult(tuple(moved), (), tuple(rolled_back), tuple(holds), uncertain)
            _journal_event(journal, event="end", transaction=transaction, status="complete")
    except (OSError, ValueError, _UnsafeMove) as exc:
        # Especially when a journal append fails, don't guess whether a
        # committed rename has its completion record. Preserve all data.
        holds.extend(Hold(p.source, f"journal/apply needs manual recovery: {exc}") for p in plans)
        return ApplyResult(tuple(moved), (), tuple(rolled_back), tuple(holds), True)
    return ApplyResult(moved=tuple(moved))
