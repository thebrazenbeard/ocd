"""Opt-in, durable, bounded, **observe-only** Windows/Linux polling.

This is a foreground developer-preview engine, not an installed service.
Nothing here invokes the mover, downloads artifacts, or deletes user files.
Only complete scans may advance a persisted fingerprint. SQLite transactions
commit each change event and its checkpoint together.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from threading import Event
import time
from typing import Callable, Iterable, Iterator

from .config import verified_root
from .planner import build_plan
from .scanner import ScanBudget, scan_root
from .types import RootConfig


class ObserverError(RuntimeError):
    """Unsafe, missing, locked, or inconsistent durable observer state."""


_APPLICATION_ID = 0x4F43444F  # Explicit SQLite ownership marker: "OCDO".


@dataclass(frozen=True)
class DurableEvent:
    id: int
    root: str
    kind: str
    fingerprint: str | None
    detail: str


def default_state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "OCD" / "state"
    base = os.environ.get("XDG_STATE_HOME")
    directory = Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "state"
    return directory / "ocd"


def _check_state_path(state_dir: Path, *, create: bool) -> Path:
    """Reject symlink/reparse ancestors; never silently redirect SQLite state."""
    state_dir = Path(state_dir).expanduser()
    if not state_dir.is_absolute() or any(part in (".", "..") for part in state_dir.parts):
        raise ObserverError("state directory must be a normalized absolute path")
    components = (*reversed(state_dir.parents), state_dir)
    for component in components:
        try:
            details = component.lstat()
        except FileNotFoundError:
            if not create:
                raise ObserverError(f"observer state directory unavailable: {state_dir}") from None
            try:
                component.mkdir(mode=0o700)
            except FileExistsError:
                details = component.lstat()
            else:
                details = component.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode)
                or getattr(details, "st_file_attributes", 0) & reparse):
            raise ObserverError(f"linked or non-directory state ancestor refused: {component}")
    if os.name != "nt":
        state_info = state_dir.lstat()
        if state_info.st_uid != os.geteuid() or state_info.st_mode & 0o022:
            raise ObserverError("observer state must be user-owned and not group/other writable")
    return state_dir


def _validate_state_location(state_dir: Path, roots: tuple[RootConfig, ...]) -> None:
    # Resolve existing ancestors as well as comparing literal spellings: a
    # Windows 8.3 alias can name the same root through a different string.
    canonical_state = state_dir.resolve(strict=False)
    for root in roots:
        # The entire observer state (including lock/WAL) must lie outside
        # every monitored source and destination, even when no files exist.
        for boundary in (root.source, root.destination):
            parent = Path(boundary)
            canonical_parent = parent.resolve(strict=False)
            if (state_dir == parent or state_dir.is_relative_to(parent)
                    or canonical_state == canonical_parent
                    or canonical_state.is_relative_to(canonical_parent)):
                raise ObserverError(f"state directory overlaps a registered root: {boundary}")


def _check_database_files(db: Path) -> None:
    """Reject linked/nonregular state files before SQLite touches them."""
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for file in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        try:
            details = file.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode)
                or getattr(details, "st_file_attributes", 0) & reparse
                or getattr(details, "st_nlink", 1) > 1):
            raise ObserverError(f"linked/shared/unsafe observer state file refused: {file}")


def _connect(db: Path) -> sqlite3.Connection:
    _check_database_files(db)
    existed = db.exists()
    if existed:
        # A foreign SQLite database is NOT an OCD state migration target.
        # Inspect it without permitting journal, schema, or header writes.
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1",
                                         uri=True, timeout=0.5)) as inspection:
                if inspection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,):
                    raise ObserverError("existing database is not OCD observer state")
                if inspection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ObserverError("observer database integrity check failed")
                tables = {row[0] for row in inspection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"meta", "roots", "events", "control"}.issubset(tables):
                    raise ObserverError("incomplete observer state schema")
                if inspection.execute("SELECT version FROM meta").fetchall() != [(1,)]:
                    raise ObserverError("unsupported observer state schema")
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ObserverError(f"cannot inspect existing observer database: {exc}") from exc
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(db), timeout=0.5, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=500")
        connection.execute("PRAGMA foreign_keys=ON")
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise ObserverError("observer database integrity check failed; preserve state and recover manually")
        connection.execute("PRAGMA synchronous=FULL")
        if not existed:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in (
                    "CREATE TABLE meta(version INTEGER NOT NULL)",
                    "CREATE TABLE roots(source TEXT PRIMARY KEY, fingerprint TEXT, last_error TEXT)",
                    "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "source TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('change','error')), "
                    "fingerprint TEXT, detail TEXT NOT NULL, created_ns INTEGER NOT NULL)",
                    "CREATE TABLE control(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "INSERT INTO meta(version) VALUES(1)",
                ):
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        elif connection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,):
            raise ObserverError("observer state changed since read-only validation")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
    except (sqlite3.DatabaseError, OSError, ObserverError) as exc:
        if connection is not None:
            connection.close()
        raise ObserverError(f"cannot open observer state; preserve it for recovery: {exc}") from exc


@contextmanager
def _writer(state_dir: Path, roots: tuple[RootConfig, ...]) -> Iterator[sqlite3.Connection]:
    _validate_state_location(state_dir, roots)
    state_dir = _check_state_path(state_dir, create=True)
    lock = state_dir / "observer.lock"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(lock, flags, 0o600)
    except FileExistsError as exc:
        raise ObserverError("observer state already owned or stale lock; do not delete lock automatically") from exc
    connection: sqlite3.Connection | None = None
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        connection = _connect(state_dir / "observer.sqlite3")
        yield connection
    finally:
        if connection is not None:
            connection.close()
        # A removed/replaced lock no longer belongs to this process. In
        # particular, never erase a subsequent writer's lock during exit.
        # This identity check is best effort; hostile same-user lock-path
        # races still require OS-level qualification.
        try:
            owned = os.fstat(fd)
            current = lock.lstat()
            release_own_lock = (owned.st_dev, owned.st_ino) == (current.st_dev, current.st_ino)
        except OSError:
            release_own_lock = False
        os.close(fd)
        # Clean exits release only the same inode they created. A killed
        # process retains its lock for explicit operator investigation.
        if release_own_lock:
            lock.unlink(missing_ok=True)


def _fingerprint(root: RootConfig, files, plan) -> str:
    payload = {
        "source": str(root.source), "destination": str(root.destination),
        "mode": root.mode, "tags": root.tags, "settle_seconds": root.settle_seconds,
        "files": [(str(entry.path), entry.size, entry.mtime_ns) for entry in files],
        "moves": [(str(item.source), str(item.destination), item.tag) for item in plan.moves],
        "holds": [(str(item.path), item.reason) for item in plan.holds],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _store_event(connection: sqlite3.Connection, source: str, kind: str,
                 fingerprint: str | None, detail: str, *, max_history: int) -> DurableEvent:
    connection.execute("BEGIN IMMEDIATE")
    try:
        old = connection.execute("SELECT fingerprint,last_error FROM roots WHERE source=?", (source,)).fetchone()
        if kind == "change" and old is not None and old == (fingerprint, None):
            connection.execute("COMMIT")
            return DurableEvent(0, source, "unchanged", fingerprint, detail)
        if kind == "error" and old is not None and old[1] == detail:
            connection.execute("COMMIT")
            return DurableEvent(0, source, "unchanged", old[0], detail)
        if kind == "error":
            # The last complete fingerprint is deliberately retained.
            connection.execute("INSERT INTO roots(source,fingerprint,last_error) VALUES(?,?,?) "
                               "ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error",
                               (source, old[0] if old else None, detail))
        else:
            connection.execute("INSERT INTO roots(source,fingerprint,last_error) VALUES(?,?,NULL) "
                               "ON CONFLICT(source) DO UPDATE SET fingerprint=excluded.fingerprint,last_error=NULL",
                               (source, fingerprint))
        cursor = connection.execute(
            "INSERT INTO events(source,kind,fingerprint,detail,created_ns) VALUES(?,?,?,?,?)",
            (source, kind, fingerprint, detail, time.time_ns()),
        )
        connection.execute("DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id),0)-? FROM events)",
                           (max_history,))
        connection.execute("COMMIT")
        return DurableEvent(int(cursor.lastrowid), source, kind, fingerprint, detail)
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _paused(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT value FROM control WHERE key='paused'").fetchone()
    return row == ("1",)


def run_observer(
    roots: Iterable[RootConfig], on_event: Callable[[DurableEvent], None],
    *, state_dir: Path, stop_event: Event, interval_seconds: float = 30.0,
    max_polls: int | None = None, budget: ScanBudget | None = None,
    max_history: int = 1_000,
) -> None:
    """Run bounded foreground polls; every emitted event is committed first."""
    selected = tuple(roots)
    if not selected or any(not isinstance(root, RootConfig) for root in selected):
        raise ObserverError("observer requires registered RootConfig roots")
    if not isinstance(stop_event, Event) or not callable(on_event):
        raise ObserverError("observer requires a stop event and event callback")
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ObserverError("interval must be finite and positive")
    if max_polls is not None and (type(max_polls) is not int or max_polls < 1):
        raise ObserverError("max_polls must be a positive integer")
    if type(max_history) is not int or max_history < 1 or max_history > 100_000:
        raise ObserverError("max_history must be 1..100000")
    selected_budget = budget if budget is not None else ScanBudget()
    if not isinstance(selected_budget, ScanBudget):
        raise ObserverError("budget must be ScanBudget")

    with _writer(Path(state_dir), selected) as db:
        polls = 0
        while not stop_event.is_set():
            if not _paused(db):
                for root in selected:
                    if stop_event.is_set():
                        break
                    source = str(root.source)
                    try:
                        verified_root(root)
                        files = scan_root(root, budget=selected_budget)
                        plan = build_plan(root, files)
                        digest = _fingerprint(root, files, plan)
                        detail = json.dumps({"files": len(files), "moves": len(plan.moves),
                                             "holds": len(plan.holds)}, sort_keys=True)
                        event = _store_event(db, source, "change", digest, detail,
                                             max_history=max_history)
                    except (OSError, RuntimeError, ValueError, TypeError) as exc:
                        event = _store_event(db, source, "error", None,
                                             f"{type(exc).__name__}: {exc}", max_history=max_history)
                    if event.id:
                        on_event(event)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            stop_event.wait(interval_seconds)


def set_paused(state_dir: Path, paused: bool) -> None:
    """Pause or resume a previously initialized observer without scanning."""
    if type(paused) is not bool:
        raise ObserverError("paused must be boolean")
    state_dir = _check_state_path(Path(state_dir), create=False)
    db_file = state_dir / "observer.sqlite3"
    _check_database_files(db_file)
    if not db_file.is_file():
        raise ObserverError("observer database absent or linked; run observer first")
    db = _connect(db_file)
    try:
        db.execute("INSERT INTO control(key,value) VALUES('paused',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if paused else "0",))
    finally:
        db.close()


def observer_status(state_dir: Path) -> tuple[bool, int]:
    """Return pause flag and durable journal count without scanning roots."""
    state_dir = _check_state_path(Path(state_dir), create=False)
    db_file = state_dir / "observer.sqlite3"
    _check_database_files(db_file)
    if not db_file.is_file():
        raise ObserverError("observer database absent or linked; run observer first")
    try:
        # Unlike run/pause, status never creates SQLite schema or WAL files.
        db = sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    except sqlite3.DatabaseError as exc:
        raise ObserverError(f"cannot read observer state: {exc}") from exc
    try:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ObserverError("observer database integrity check failed")
        return _paused(db), int(db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise ObserverError(f"cannot read observer state: {exc}") from exc
    finally:
        db.close()
