"""Recursive directory usage fixtures for bounded, read-only inventory."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from ocd.cli import main
from ocd.usage import DirectoryUsage, scan_usage


class UsageHierarchyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.sandbox = Path(temporary.name)
        self.root = self.sandbox / "root"
        self.root.mkdir()

    def assert_directory_totals(self, report, expected):
        by_path = {item.path: item for item in report.directory_totals}
        self.assertEqual(len(by_path), len(report.directory_totals), "duplicate directory row")
        self.assertEqual(set(by_path), set(expected), "missing or unexpected directory rows")
        self.assertEqual(
            {path: item.logical_bytes for path, item in by_path.items()}, expected,
        )
        for item in report.directory_totals:
            self.assertIsInstance(item, DirectoryUsage)
            self.assertIsNone(item.allocated_bytes, "hardlinks prevent safe directory attribution")
            self.assertEqual(item.complete, report.complete)
        self.assertEqual(by_path[self.root].logical_bytes, report.logical_bytes)

    def test_recursive_totals_include_root_nested_and_empty_directories(self):
        outer = self.root / "a"
        nested = outer / "nested"
        nested.mkdir(parents=True)
        sibling = self.root / "b"
        sibling.mkdir()
        empty = self.root / "empty"
        empty.mkdir()
        (nested / "deep.bin").write_bytes(b"x" * 3)
        (outer / "flat.bin").write_bytes(b"y" * 5)
        (sibling / "other.bin").write_bytes(b"z" * 7)
        (self.root / "at-root.bin").write_bytes(b"q" * 2)

        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*.bin")
        }
        report = scan_usage(self.root, top=1, include_hierarchy=True)

        self.assertTrue(report.complete, report.issues)
        self.assertEqual((report.files, report.directories, report.logical_bytes), (4, 4, 17))
        self.assert_directory_totals(report, {
            self.root: 17, outer: 8, nested: 3, sibling: 7, empty: 0,
        })
        self.assertEqual(report.directory_totals[0].path, self.root)
        self.assertEqual(
            before,
            {path: (path.read_bytes(), path.stat().st_mtime_ns)
             for path in self.root.rglob("*.bin")},
            "usage collection must not modify file contents or timestamps",
        )

    def test_hardlink_aliases_count_logically_in_each_branch(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        original = first / "original.bin"
        original.write_bytes(b"content" * 11)
        try:
            os.link(original, second / "alias.bin")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlink unavailable: {exc}")

        report = scan_usage(self.root, include_hierarchy=True)

        self.assertTrue(report.complete, report.issues)
        self.assertEqual(report.files, 2)
        self.assert_directory_totals(report, {
            self.root: 154, first: 77, second: 77,
        })
        if report.allocated_bytes is not None:
            self.assertEqual(report.allocated_bytes, original.stat().st_blocks * 512)
        if os.name == "nt":
            self.assertIsNone(report.allocated_bytes)

    def test_symlink_targets_are_not_included_as_directory_rows_or_bytes(self):
        outside = self.sandbox / "outside"
        outside.mkdir()
        (outside / "external.bin").write_bytes(b"sensitive" * 20)
        safe = self.root / "safe"
        safe.mkdir()
        (safe / "safe.bin").write_bytes(b"safe")
        links = (
            (self.root / "link-file", outside / "external.bin", False),
            (self.root / "link-dir", outside, True),
            (self.root / "cycle", self.root, True),
            (self.root / "broken", self.sandbox / "missing", False),
        )
        try:
            for link, target, is_dir in links:
                link.symlink_to(target, target_is_directory=is_dir)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        report = scan_usage(self.root, include_hierarchy=True)

        self.assertTrue(report.complete, report.issues)
        self.assertEqual(report.skipped_links, 4)
        self.assertEqual(report.files, 1)
        self.assert_directory_totals(report, {self.root: 4, safe: 4})

    def test_entry_budget_marks_every_observed_directory_incomplete(self):
        chosen = self.root / "a"
        chosen.mkdir()
        (chosen / "not-scanned.bin").write_bytes(b"data")
        (self.root / "z.bin").write_bytes(b"more")

        report = scan_usage(self.root, max_entries=1, include_hierarchy=True)

        self.assertFalse(report.complete)
        self.assertLessEqual(report.entries_visited, 1)
        self.assertTrue(any("budget" in issue for issue in report.issues))
        # Entry names are truncated before sorting, so filesystems may yield
        # either the directory or the root file as the first observed entry.
        observed_paths = {item.path for item in report.directory_totals}
        self.assertIn(self.root, observed_paths)
        self.assertTrue(observed_paths <= {self.root, chosen})
        self.assertIn(report.logical_bytes, (0, 4))
        expected = {self.root: report.logical_bytes}
        if chosen in observed_paths:
            self.assertEqual(report.logical_bytes, 0)
            expected[chosen] = 0
        self.assert_directory_totals(report, expected)

    def test_depth_budget_marks_partial_subtree_and_root_incomplete(self):
        nested = self.root / "nested"
        deeper = nested / "deeper"
        deeper.mkdir(parents=True)
        (deeper / "not-scanned.bin").write_bytes(b"hidden")
        (self.root / "visible.bin").write_bytes(b"yes")

        report = scan_usage(self.root, max_depth=0, include_hierarchy=True)

        self.assertFalse(report.complete)
        self.assertTrue(any("depth budget" in issue for issue in report.issues))
        self.assert_directory_totals(report, {self.root: 3, nested: 0})

    def test_hierarchy_json_exposes_observed_totals_without_deletion_claims(self):
        child = self.root / "child"
        child.mkdir()
        (child / "payload.bin").write_bytes(b"payload")
        standard_out, standard_err = StringIO(), StringIO()
        missing_config = self.sandbox / "missing-config.json"

        with redirect_stdout(standard_out), redirect_stderr(standard_err):
            status = main([
                "--config", str(missing_config),
                "usage", "--path", str(self.root), "--hierarchy-json",
            ])

        self.assertEqual(status, 0, standard_err.getvalue())
        payload = json.loads(standard_out.getvalue())
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["logical_bytes"], 7)
        self.assertIs(payload["deletion_eligible"], False)
        self.assertIsNone(payload["reclaimable_bytes"])
        self.assertEqual(
            {row["path"]: row["logical_bytes"] for row in payload["directory_totals"]},
            {str(self.root): 7, str(child): 7},
        )
        for row in payload["directory_totals"]:
            self.assertTrue(row["complete"])
            self.assertIsNone(row["allocated_bytes"])
        self.assertFalse(missing_config.exists())

    def test_hierarchy_requires_explicit_opt_in(self):
        child = self.root / "child"
        child.mkdir()
        (child / "data.bin").write_bytes(b"abcd")

        ordinary = scan_usage(self.root)
        hierarchical = scan_usage(self.root, include_hierarchy=True)

        self.assertTrue(ordinary.complete, ordinary.issues)
        self.assertEqual(ordinary.directory_totals, ())
        self.assertEqual(ordinary.logical_bytes, 4)
        self.assertTrue(hierarchical.complete, hierarchical.issues)
        self.assertEqual(hierarchical.logical_bytes, ordinary.logical_bytes)
        self.assert_directory_totals(hierarchical, {self.root: 4, child: 4})


if __name__ == "__main__":
    unittest.main()
