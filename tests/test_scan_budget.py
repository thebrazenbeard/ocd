"""Fail-closed bounded scanner tests; no observer or persistent state dependency."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import scanner
from ocd.scanner import ScanBudget, ScanBudgetExceeded, scan_root
from ocd.types import RootConfig


class BoundedScannerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ocd-scan-budget-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.src = self.base / "source"
        self.dst = self.base / "dest"
        self.src.mkdir()
        self.dst.mkdir()
        self.root = RootConfig(self.src, self.dst, settle_seconds=0)

    def test_unbudgeted_scan_preserves_existing_file_inventory(self):
        (self.src / "b.txt").write_text("b")
        (self.src / "a.txt").write_text("a")
        entries = scan_root(self.root)
        self.assertEqual([entry.path.name for entry in entries], ["a.txt", "b.txt"])
        self.assertEqual(entries, scan_root(self.root, budget=ScanBudget()))

    def test_entry_budget_never_returns_partial_inventory(self):
        for number in range(3):
            (self.src / f"file{number}.txt").write_text("x")
        with self.assertRaisesRegex(ScanBudgetExceeded, "entry budget"):
            scan_root(self.root, budget=ScanBudget(max_entries=2))

    def test_directory_budget_never_returns_partial_inventory(self):
        (self.src / "nested").mkdir()
        with self.assertRaisesRegex(ScanBudgetExceeded, "directory budget"):
            scan_root(self.root, budget=ScanBudget(max_directories=1))

    def test_depth_budget_never_returns_partial_inventory(self):
        (self.src / "nested").mkdir()
        with self.assertRaisesRegex(ScanBudgetExceeded, "depth budget"):
            scan_root(self.root, budget=ScanBudget(max_depth=0))

    def test_time_budget_never_returns_partial_inventory(self):
        (self.src / "file.txt").write_text("x")
        with mock.patch("ocd.scanner.time.monotonic", side_effect=[0.0, 100.0]):
            with self.assertRaisesRegex(ScanBudgetExceeded, "elapsed-time"):
                scan_root(self.root, budget=ScanBudget(max_seconds=1))

    def test_invalid_budget_dimensions_are_rejected(self):
        invalid = (
            {"max_entries": 0}, {"max_entries": 1.5},
            {"max_directories": True}, {"max_directories": 0},
            {"max_depth": -1}, {"max_depth": False},
            {"max_seconds": 0}, {"max_seconds": float("nan")},
            {"max_seconds": float("inf")}, {"max_seconds": True},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ScanBudget(**values)
        with self.assertRaises(TypeError):
            scan_root(self.root, budget=123)

    def test_queued_descendant_missing_in_windows_path_fallback(self):
        nested = self.src / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("x")
        original = scanner._check_directory

        def reject_descendant(path, source):
            return path != nested and original(path, source)

        with mock.patch("ocd.scanner._CAN_SCAN_FD", False), \
             mock.patch("ocd.scanner._check_directory", side_effect=reject_descendant):
            with self.assertRaisesRegex(ScanBudgetExceeded, "queued directory"):
                scan_root(self.root, budget=ScanBudget())
            # The unbudgeted one-shot scanner still skips a vanished descendant.
            self.assertEqual(scan_root(self.root), ())

    def test_entry_disappearance_in_path_fallback_fails_closed(self):
        vanished = mock.Mock()
        vanished.stat.side_effect = FileNotFoundError("gone")
        iterator = mock.MagicMock()
        iterator.__enter__.side_effect = lambda: iter((vanished,))
        with mock.patch("ocd.scanner._CAN_SCAN_FD", False), \
             mock.patch("ocd.scanner.os.scandir", return_value=iterator):
            with self.assertRaisesRegex(ScanBudgetExceeded, "entry disappeared"):
                scan_root(self.root, budget=ScanBudget())
            self.assertEqual(scan_root(self.root), ())

    @unittest.skipUnless(os.name != "nt", "POSIX directory-FD traversal")
    def test_queued_descendant_disappears_in_fd_traversal(self):
        if not scanner._CAN_SCAN_FD:
            self.skipTest("openat traversal unavailable")
        nested = self.src / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("x")
        original = scanner._open_beneath_root

        def reject_nested(root_fd, components, flags):
            if components == ("nested",):
                return None
            return original(root_fd, components, flags)

        with mock.patch("ocd.scanner._open_beneath_root", side_effect=reject_nested):
            with self.assertRaisesRegex(ScanBudgetExceeded, "queued directory"):
                scan_root(self.root, budget=ScanBudget())
            self.assertEqual(scan_root(self.root), ())

    @unittest.skipUnless(os.name != "nt", "POSIX directory-FD traversal")
    def test_queued_descendant_identity_swap_in_fd_traversal(self):
        if not scanner._CAN_SCAN_FD:
            self.skipTest("openat traversal unavailable")
        nested = self.src / "nested"
        decoy = self.src / "decoy"
        nested.mkdir()
        decoy.mkdir()
        (nested / "file.txt").write_text("x")
        original = scanner._open_beneath_root

        def swap_nested(root_fd, components, flags):
            if components == ("nested",):
                return os.open(decoy, flags)
            return original(root_fd, components, flags)

        with mock.patch("ocd.scanner._open_beneath_root", side_effect=swap_nested):
            with self.assertRaisesRegex(ScanBudgetExceeded, "changed identity"):
                scan_root(self.root, budget=ScanBudget())
            self.assertEqual(scan_root(self.root), ())

    @unittest.skipUnless(os.name != "nt", "POSIX directory-FD traversal")
    def test_entry_disappearance_in_fd_traversal_fails_closed(self):
        if not scanner._CAN_SCAN_FD:
            self.skipTest("openat traversal unavailable")
        # Force a disappearance at child.stat without changing actual paths.
        vanished = mock.Mock()
        vanished.stat.side_effect = FileNotFoundError("gone")
        iterator = mock.MagicMock()
        iterator.__enter__.side_effect = lambda: iter((vanished,))
        with mock.patch("ocd.scanner.os.scandir", return_value=iterator):
            with self.assertRaisesRegex(ScanBudgetExceeded, "entry disappeared"):
                scan_root(self.root, budget=ScanBudget())
            self.assertEqual(scan_root(self.root), ())


if __name__ == "__main__":
    unittest.main()
