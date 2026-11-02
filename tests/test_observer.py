"""Disposable foreground observer tests; no host service or user files."""

from __future__ import annotations

from pathlib import Path
from contextlib import closing, redirect_stdout
import io
import os
import sqlite3
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest import mock

from ocd.observer import (
    ObserverError, _writer, observer_status, run_observer, set_paused,
)
from ocd.scanner import ScanBudget, ScanBudgetExceeded, scan_root
from ocd.types import RootConfig
from ocd.config import Config, save_config
from ocd.cli import main


class BoundedScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocd-bounded-scan-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.src = self.base / "source"
        self.dst = self.base / "dest"
        self.src.mkdir()
        self.dst.mkdir()
        self.root = RootConfig(self.src, self.dst, settle_seconds=0)

    def test_entry_budget_fails_without_returning_partial_inventory(self):
        for n in range(3):
            (self.src / f"file{n}.txt").write_text("x")
        with self.assertRaisesRegex(ScanBudgetExceeded, "entry budget"):
            scan_root(self.root, budget=ScanBudget(max_entries=2))

    def test_directory_and_depth_budget_fail_closed(self):
        (self.src / "nested").mkdir()
        with self.assertRaisesRegex(ScanBudgetExceeded, "depth budget"):
            scan_root(self.root, budget=ScanBudget(max_depth=0))
        with self.assertRaisesRegex(ScanBudgetExceeded, "directory budget"):
            scan_root(self.root, budget=ScanBudget(max_directories=1))

    def test_queued_directory_missing_fails_closed_in_windows_path_fallback(self):
        nested = self.src / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("data")
        from ocd import scanner
        original = scanner._check_directory

        def missing_descendant(path, source):
            return path != nested and original(path, source)

        with mock.patch("ocd.scanner._CAN_SCAN_FD", False), \
             mock.patch("ocd.scanner._check_directory", side_effect=missing_descendant):
            with self.assertRaisesRegex(ScanBudgetExceeded, "queued directory"):
                scan_root(self.root, budget=ScanBudget())

    @unittest.skipUnless(os.name != "nt", "POSIX openat traversal")
    def test_queued_directory_missing_fails_closed_in_fd_traversal(self):
        nested = self.src / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("data")
        from ocd import scanner
        if not scanner._CAN_SCAN_FD:
            self.skipTest("directory-FD traversal unavailable")
        with mock.patch("ocd.scanner._open_beneath_root", return_value=None):
            with self.assertRaisesRegex(ScanBudgetExceeded, "queued directory"):
                scan_root(self.root, budget=ScanBudget())

    def test_budget_validation(self):
        for kwargs in ({"max_entries": 0}, {"max_directories": True},
                       {"max_depth": -1}, {"max_seconds": float("nan")},
                       {"max_seconds": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ScanBudget(**kwargs)

    def test_elapsed_time_budget_fails_closed(self):
        (self.src / "file.txt").write_text("one")
        with mock.patch("ocd.scanner.time.monotonic", side_effect=[0.0, 100.0]):
            with self.assertRaisesRegex(ScanBudgetExceeded, "elapsed-time"):
                scan_root(self.root, budget=ScanBudget(max_seconds=1))


class DurableObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocd-observer-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.src = self.base / "source"
        self.dst = self.base / "dest"
        self.src.mkdir()
        self.dst.mkdir()
        self.state = self.base / "state"
        self.root = RootConfig(self.src, self.dst, mode="apply", tags={".txt": "Notes"}, settle_seconds=0)
        self.events = []

    def run_once(self, *, budget=None, state=None, max_history=1_000):
        run_observer((self.root,), self.events.append, state_dir=state or self.state,
                     stop_event=Event(), interval_seconds=.001, max_polls=1,
                     budget=budget, max_history=max_history)

    def rows(self):
        with closing(sqlite3.connect(self.state / "observer.sqlite3")) as db:
            return db.execute("SELECT kind, fingerprint FROM events ORDER BY id").fetchall()

    def test_restart_reconciles_added_and_removed_last_file_once(self):
        self.run_once()
        self.assertEqual(len(self.events), 1)  # Initial complete empty observation.
        self.run_once()
        self.assertEqual(len(self.events), 1)  # Restart, no duplicate.
        file = self.src / "note.txt"
        file.write_text("new")
        self.run_once()
        self.assertEqual(len(self.events), 2)
        self.run_once()
        self.assertEqual(len(self.events), 2)
        file.unlink()
        self.run_once()
        self.assertEqual(len(self.events), 3)
        self.assertEqual([kind for kind, _ in self.rows()], ["change"] * 3)
        self.assertFalse((self.dst / "Notes").exists())

    def test_pause_prevents_scanning_then_resume_reconciles(self):
        self.run_once()
        set_paused(self.state, True)
        self.assertEqual(observer_status(self.state), (True, 1))
        (self.src / "missed.txt").write_text("missed")
        with mock.patch("ocd.observer.scan_root", side_effect=AssertionError("paused must not scan")):
            self.run_once()
        self.assertEqual(len(self.events), 1)
        set_paused(self.state, False)
        self.run_once()
        self.assertEqual(len(self.events), 2)
        self.assertEqual(observer_status(self.state), (False, 2))

    def test_budget_error_does_not_advance_checkpoint(self):
        self.run_once()
        initial = self.rows()[0][1]
        (self.src / "one.txt").write_text("one")
        (self.src / "two.txt").write_text("two")
        self.run_once(budget=ScanBudget(max_entries=1))
        self.assertEqual([kind for kind, _ in self.rows()], ["change", "error"])
        with closing(sqlite3.connect(self.state / "observer.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT fingerprint FROM roots").fetchone()[0], initial)
        self.run_once()  # Recovery commits a fresh complete checkpoint.
        self.assertEqual([kind for kind, _ in self.rows()], ["change", "error", "change"])

    def test_second_runner_cannot_share_writer_lock(self):
        with _writer(self.state, (self.root,)):
            with self.assertRaisesRegex(ObserverError, "already owned"):
                self.run_once()

    def test_writer_exit_preserves_replaced_lock(self):
        lock = self.state / "observer.lock"
        real_lstat = Path.lstat

        def simulated_replacement(path, *args, **kwargs):
            if path == lock:
                # A concurrent actor replaced our lock between admission and
                # shutdown. Simulate independent of OS open-file unlink rules.
                return SimpleNamespace(st_dev=-1, st_ino=-1)
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", simulated_replacement):
            with _writer(self.state, (self.root,)):
                self.assertTrue(lock.exists())
        self.assertTrue(lock.exists(), "must not delete another writer's lock")
        with self.assertRaisesRegex(ObserverError, "already owned"):
            self.run_once()

    def test_state_inside_monitored_root_refused_without_write(self):
        unsafe = self.src / "state"
        with self.assertRaisesRegex(ObserverError, "overlaps"):
            self.run_once(state=unsafe)
        self.assertFalse(unsafe.exists())

    def test_corrupted_state_is_preserved_and_fails_closed(self):
        self.run_once()
        db_path = self.state / "observer.sqlite3"
        db_path.write_bytes(b"corrupted sqlite, not deleted")
        with self.assertRaises(ObserverError):
            self.run_once()
        self.assertEqual(db_path.read_bytes(), b"corrupted sqlite, not deleted")

    def test_foreign_database_rejected_without_schema_or_journal_writes(self):
        self.state.mkdir()
        db_path = self.state / "observer.sqlite3"
        with closing(sqlite3.connect(db_path)) as db:
            db.execute("CREATE TABLE foreign_records(value TEXT)")
            db.execute("INSERT INTO foreign_records VALUES('keep')")
            db.commit()
        original = db_path.read_bytes()
        with self.assertRaisesRegex(ObserverError, "not OCD observer"):
            self.run_once()
        with self.assertRaisesRegex(ObserverError, "not OCD observer"):
            set_paused(self.state, True)
        self.assertEqual(db_path.read_bytes(), original)
        self.assertFalse(Path(str(db_path) + "-wal").exists())
        self.assertFalse(Path(str(db_path) + "-shm").exists())

    @unittest.skipIf(os.name == "nt", "POSIX owner/mode protection")
    def test_world_writable_state_directory_refused(self):
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o777)
        with self.assertRaisesRegex(ObserverError, "user-owned"):
            self.run_once()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_transaction_failure_rolls_back_event_and_checkpoint(self):
        self.run_once()
        initial = self.rows()[0][1]
        (self.src / "new.txt").write_text("new")
        with mock.patch("ocd.observer.time.time_ns", side_effect=RuntimeError("crash before event")):
            with self.assertRaisesRegex(RuntimeError, "crash before event"):
                self.run_once()
        self.assertEqual(len(self.rows()), 1)
        with closing(sqlite3.connect(self.state / "observer.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT fingerprint FROM roots").fetchone()[0], initial)
        self.run_once()
        self.assertEqual(len(self.rows()), 2)

    def test_linked_database_refused_without_touching_target(self):
        self.state.mkdir()
        target = self.base / "unrelated.txt"
        target.write_text("must stay intact")
        try:
            (self.state / "observer.sqlite3").symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {exc}")
        with self.assertRaises(ObserverError):
            self.run_once()
        self.assertEqual(target.read_text(), "must stay intact")

    def test_linked_state_directory_refused(self):
        other = self.base / "other"
        other.mkdir()
        linked = self.base / "state-link"
        try:
            linked.symlink_to(other, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {exc}")
        with self.assertRaises(ObserverError):
            self.run_once(state=linked)
        self.assertEqual(list(other.iterdir()), [])

    def test_durable_history_is_bounded(self):
        self.run_once(max_history=2)
        for n in range(3):
            (self.src / f"file{n}.txt").write_text(str(n))
            self.run_once(max_history=2)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.events), 4)

    def test_no_organizer_mutation_even_when_registered_apply_mode(self):
        (self.src / "file.txt").write_text("source content")
        with mock.patch("ocd.apply.apply_moves", side_effect=AssertionError("MUST NOT APPLY")):
            self.run_once()
        self.assertTrue((self.src / "file.txt").exists())
        self.assertFalse((self.dst / "Notes").exists())

    def test_explicit_cli_run_and_status_only_registered_root(self):
        config = self.base / "ocd.json"
        save_config(Config(roots=(self.root,)), config)
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(["--config", str(config), "observe", "run",
                       "--state-dir", str(self.state), "--max-polls", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("OBSERVE CHANGE", output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config", str(config), "observe", "status",
                                   "--state-dir", str(self.state)]), 0)
        self.assertIn("durable_events=1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
