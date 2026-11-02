"""Read-only, bounded Windows/Linux disk-usage reporting contracts."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ocd.cli import main
from ocd.usage import scan_usage


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "root"
        self.root.mkdir()

    def test_empty_and_nested_sizes_without_mutation(self):
        empty = scan_usage(self.root)
        self.assertTrue(empty.complete)
        self.assertEqual((empty.logical_bytes, empty.files, empty.entries_visited), (0, 0, 0))
        self.assertEqual(empty.largest_files, ())
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "a.bin").write_bytes(b"a" * 10)
        (nested / "b.bin").write_bytes(b"b" * 10)
        (self.root / "c.bin").write_bytes(b"c" * 2)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*.bin")}
        report = scan_usage(self.root, top=2)
        self.assertTrue(report.complete, report.issues)
        self.assertEqual(report.logical_bytes, 22)
        self.assertEqual(report.files, 3)
        self.assertEqual(report.directories, 1)
        self.assertEqual([item.path.name for item in report.largest_files], ["a.bin", "b.bin"])
        self.assertEqual(
            [(item.path, item.logical_bytes) for item in report.largest_children],
            [(nested, 20), (self.root, 2)],
        )
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*.bin")})
        self.assertEqual(list(self.root.rglob("journal.jsonl")), [])

    def test_budget_and_depth_hold_instead_of_claiming_complete(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "one").write_bytes(b"abc")
        (nested / "two").write_bytes(b"xyz")
        limited = scan_usage(self.root, max_entries=1)
        self.assertFalse(limited.complete)
        self.assertTrue(any("budget" in item for item in limited.issues))
        self.assertLessEqual(limited.entries_visited, 1)
        shallow = scan_usage(self.root, max_depth=0)
        self.assertFalse(shallow.complete)
        self.assertTrue(any("depth budget" in item for item in shallow.issues))
        full = scan_usage(self.root, max_entries=3, max_depth=1)
        self.assertTrue(full.complete, full.issues)
        self.assertEqual(full.logical_bytes, 6)

    def test_symlinks_and_external_content_never_counted(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"private" * 20)
        (self.root / "safe").write_bytes(b"safe")
        try:
            (self.root / "linked-file").symlink_to(outside / "secret")
            (self.root / "linked-dir").symlink_to(outside, target_is_directory=True)
            (self.root / "cycle").symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        result = scan_usage(self.root)
        self.assertTrue(result.complete, result.issues)
        self.assertEqual(result.logical_bytes, 4)
        self.assertEqual(result.skipped_links, 3)
        self.assertEqual(result.files, 1)
        with self.assertRaises(ValueError):
            scan_usage(self.root / "linked-dir")

    def test_rejects_untrusted_roots_and_invalid_limits(self):
        regular = self.root / "regular.txt"
        regular.write_text("data")
        for path in (Path("relative"), regular, self.root / "missing"):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                scan_usage(path)
        for kw in ({"max_entries": 0}, {"max_entries": True}, {"top": 0},
                   {"top": 101}, {"top": 1.5}, {"max_depth": -1},
                   {"max_depth": 65}, {"max_depth": True}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                scan_usage(self.root, **kw)

    def test_child_permission_failure_marks_partial_report(self):
        blocked = self.root / "blocked"
        blocked.mkdir()
        (blocked / "not-readable").write_bytes(b"123")
        (self.root / "good").write_bytes(b"ok")
        real_scandir = os.scandir

        def deny(path):
            if Path(path) == blocked:
                raise PermissionError("mock access denied")
            return real_scandir(path)

        with patch("ocd.usage.os.scandir", side_effect=deny):
            report = scan_usage(self.root)
        self.assertFalse(report.complete)
        self.assertTrue(any("cannot scan" in issue for issue in report.issues))
        self.assertEqual(report.logical_bytes, 2)

    def test_hardlinks_are_not_two_physical_allocations(self):
        original = self.root / "one"
        alias = self.root / "two"
        original.write_bytes(b"content" * 100)
        try:
            os.link(original, alias)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlink unsupported: {exc}")
        report = scan_usage(self.root)
        self.assertTrue(report.complete)
        self.assertEqual(report.files, 2)
        self.assertEqual(report.logical_bytes, 1400)
        if report.allocated_bytes is not None:
            blocks = original.stat().st_blocks * 512
            self.assertEqual(report.allocated_bytes, blocks)
        if os.name == "nt":
            self.assertIsNone(report.allocated_bytes)

    def test_cli_standalone_json_and_partial_exit(self):
        (self.root / "file.bin").write_bytes(b"hello")
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--config", str(Path(self.temporary.name) / "absent.json"),
                           "usage", "--path", str(self.root), "--json"])
        self.assertEqual(status, 0, err.getvalue())
        data = json.loads(out.getvalue())
        self.assertTrue(data["complete"])
        self.assertEqual(data["logical_bytes"], 5)
        self.assertEqual(data["largest_files"][0]["path"], str(self.root / "file.bin"))
        self.assertFalse((Path(self.temporary.name) / "absent.json").exists())
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["usage", "--path", str(self.root), "--max-entries", "0"])
        self.assertEqual(status, 2)
        out, err = StringIO(), StringIO()
        (self.root / "other.bin").write_bytes(b"other")
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["usage", "--path", str(self.root), "--max-entries", "1"])
        self.assertEqual(status, 1)
        self.assertIn("INCOMPLETE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
