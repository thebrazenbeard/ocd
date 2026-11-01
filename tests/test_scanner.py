"""Filesystem-only scanner behavior on Windows and Linux."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.scanner import scan_root
from ocd.types import FileEntry, RootConfig


NOW_NS = 30_000_000_000


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "incoming"
        self.source.mkdir()
        self.destination = self.base / "organized"

    def root(self, *, destination: Path | None = None, settle_seconds: float = 2) -> RootConfig:
        return RootConfig(
            source=self.source,
            destination=self.destination if destination is None else destination,
            settle_seconds=settle_seconds,
        )

    def write_at(self, path: Path, mtime_ns: int = 1_000_000_000) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def scan(self, root: RootConfig | None = None) -> tuple[FileEntry, ...]:
        with patch("ocd.scanner.time.time_ns", return_value=NOW_NS):
            return scan_root(root or self.root())

    def test_recursive_regular_files_with_stability_cutoff_and_deterministic_order(self) -> None:
        second = self.write_at(self.source / "B" / "second.mkv")
        first = self.write_at(self.source / "A" / "first.mkv")
        self.write_at(self.source / "A" / "still-writing.mkv", NOW_NS - 500_000_000)
        self.write_at(self.source / "A" / "future.mkv", NOW_NS + 1_000_000_000)

        result = self.scan()

        self.assertIsInstance(result, tuple)
        self.assertEqual([entry.path for entry in result], [first, second])
        self.assertEqual([entry.size for entry in result], [5, 5])
        self.assertEqual(
            [entry.mtime_ns for entry in result],
            [first.stat().st_mtime_ns, second.stat().st_mtime_ns],
        )
        self.assertEqual(self.scan(), result)

    def test_settle_boundary_includes_file_at_cutoff(self) -> None:
        boundary = self.write_at(self.source / "boundary.txt", NOW_NS - 2_000_000_000)
        self.assertEqual([entry.path for entry in self.scan()], [boundary])

    def test_destination_subtree_is_excluded_even_when_nested(self) -> None:
        included = self.write_at(self.source / "new.mkv")
        nested = self.source / "organized" / "by-date"
        self.write_at(nested / "already-moved.mkv")

        result = self.scan(self.root(destination=self.source / "organized"))

        self.assertEqual([entry.path for entry in result], [included])

    def test_destination_equal_to_source_returns_no_entries(self) -> None:
        self.write_at(self.source / "new.mkv")
        self.assertEqual(self.scan(self.root(destination=self.source)), ())

    def test_symlink_files_and_directories_are_not_followed(self) -> None:
        outside = self.base / "elsewhere"
        outside.mkdir()
        self.write_at(outside / "not-registered.mkv")
        self.write_at(self.source / "safe.mkv")
        try:
            (self.source / "shortcut-dir").symlink_to(outside, target_is_directory=True)
            (self.source / "shortcut-file.mkv").symlink_to(outside / "not-registered.mkv")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        self.assertEqual([entry.path.name for entry in self.scan()], ["safe.mkv"])

    def test_symlink_source_is_rejected(self) -> None:
        alias = self.base / "source-alias"
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        with self.assertRaises(ValueError):
            self.scan(RootConfig(source=alias, destination=self.destination))

    @unittest.skipUnless(os.name != "nt", "requires Linux-style directory descriptors")
    def test_directory_swapped_to_symlink_during_descent_is_not_followed(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.write_at(outside / "private.mkv")
        nested = self.source / "nested"
        nested.mkdir()
        original_open = os.open
        swapped = False

        def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == "nested" and dir_fd is not None and not swapped:
                nested.rename(self.base / "retired-nested")
                nested.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with patch("ocd.scanner.os.open", side_effect=swap_before_open):
            self.assertEqual(self.scan(), ())
        self.assertTrue(swapped, "race hook did not run")

    @unittest.skipUnless(os.name != "nt", "requires Linux-style directory descriptors")
    def test_symlink_swap_after_directory_open_stays_on_opened_directory(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.write_at(outside / "private.mkv")
        nested = self.source / "nested"
        nested.mkdir()
        nested_ino = nested.stat().st_ino
        original_scandir = os.scandir
        swapped = False

        def swap_before_scandir(directory):
            nonlocal swapped
            if isinstance(directory, int) and os.fstat(directory).st_ino == nested_ino and not swapped:
                nested.rename(self.base / "retired-nested")
                nested.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_scandir(directory)

        with patch("ocd.scanner.os.scandir", side_effect=swap_before_scandir):
            self.assertEqual(self.scan(), ())
        self.assertTrue(swapped, "race hook did not run")

    @unittest.skipUnless(os.name != "nt", "requires Linux-style directory descriptors")
    def test_queued_descendant_cannot_follow_swapped_ancestor(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.write_at(outside / "queued" / "private.mkv")
        nested = self.source / "nested"
        (nested / "queued").mkdir(parents=True)
        self.write_at(nested / "queued" / "original.mkv")
        nested_ino = nested.stat().st_ino
        original_scandir = os.scandir
        swapped = False

        def swap_before_scandir(directory):
            nonlocal swapped
            if isinstance(directory, int) and os.fstat(directory).st_ino == nested_ino and not swapped:
                nested.rename(self.base / "retired-nested")
                nested.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_scandir(directory)

        with patch("ocd.scanner.os.scandir", side_effect=swap_before_scandir):
            self.assertEqual(self.scan(), ())
        self.assertTrue(swapped, "race hook did not run")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux RLIMIT_NOFILE regression")
    def test_wide_directory_tree_works_with_low_fd_limit(self) -> None:
        # More immediate children than the common 1024-descriptor soft limit;
        # the subprocess also enforces a lower cap to make this deterministic.
        count = 1152
        for index in range(count):
            self.write_at(self.source / f"batch-{index:04d}" / "media.mkv")
        deep = self.source
        for _ in range(96):
            deep = deep / "d"
            deep.mkdir()
        self.write_at(deep / "deep.mkv")

        script = """
import resource
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from ocd.scanner import scan_root
from ocd.types import RootConfig

soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(64, soft), hard))
entries = scan_root(RootConfig(source=Path(sys.argv[2]), destination=Path(sys.argv[3]), settle_seconds=0))
if len(entries) != int(sys.argv[4]):
    raise AssertionError(f"Expected {sys.argv[4]} entries, got {len(entries)}")
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(Path(__file__).resolve().parents[1] / "src"),
                str(self.source),
                str(self.destination),
                str(count + 1),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_directory_with_symlink_parent_is_rejected(self) -> None:
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.base, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        with self.assertRaises(ValueError):
            self.scan(RootConfig(source=alias / "incoming", destination=self.destination))

    def test_non_directory_source_is_rejected(self) -> None:
        ordinary_file = self.write_at(self.base / "file.txt")
        with self.assertRaises(ValueError):
            self.scan(RootConfig(source=ordinary_file, destination=self.destination))

    def test_negative_or_non_finite_settle_seconds_is_rejected(self) -> None:
        for seconds in (-0.1, float("nan"), float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.scan(self.root(settle_seconds=seconds))

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_windows_junction_is_not_followed(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.write_at(outside / "not-registered.mkv")
        junction = self.source / "linked-dir"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            self.skipTest(f"junction creation unavailable: {result.stderr}")

        self.assertEqual(self.scan(), ())


if __name__ == "__main__":
    unittest.main()
