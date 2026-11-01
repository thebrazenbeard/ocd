"""Read-only duplicate candidate scanning on Linux and Windows."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import duplicates as duplicate_module
from ocd.duplicates import scan_duplicates
from ocd.types import RootConfig


class DuplicateScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "incoming"
        self.source.mkdir()

    def root(self, source: Path | None = None, destination: Path | None = None) -> RootConfig:
        return RootConfig(
            source=source or self.source,
            destination=destination or (self.base / "organized"),
            tags={"jpg": "photos"},
            settle_seconds=60,
        )

    def write(self, name: str, content: bytes, *, source: Path | None = None) -> Path:
        base = source or self.source
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_equal_content_in_untagged_files_uses_sha256_and_size(self) -> None:
        first = self.write("z/one.unknown", b"ab" * 8)
        second = self.write("a/two.bin", b"ab" * 8)
        self.write("different.bin", b"XY" * 8)  # same size, different bytes
        self.write("different-size.bin", b"ab" * 9)

        result = scan_duplicates([self.root()], chunk_size=3)

        self.assertEqual(result.files_scanned, 4)
        self.assertEqual(result.files_hashed, 3)
        self.assertEqual(result.skipped_hardlinks, 0)
        self.assertEqual(result.skipped_unstable, ())
        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual(group.sha256, hashlib.sha256(b"ab" * 8).hexdigest())
        self.assertEqual(group.size, 16)
        self.assertEqual([item.path for item in group.files], [second, first])
        self.assertEqual(group.logical_duplicate_bytes, 16)
        self.assertEqual(group.potential_reclaim_bytes, 16)
        self.assertEqual(result.logical_duplicate_bytes, 16)
        self.assertEqual(result.potential_reclaim_bytes, 16)
        # Does not require a tag or the organizer's settle period.
        self.assertEqual(scan_duplicates([self.root()], chunk_size=3), result)
        self.assertTrue(first.exists() and second.exists())

    def test_identical_empty_files_are_candidates(self) -> None:
        self.write("empty-a", b"")
        self.write("empty-b", b"")
        group, = scan_duplicates([self.root()]).groups
        self.assertEqual(group.sha256, hashlib.sha256(b"").hexdigest())
        self.assertEqual(group.logical_duplicate_bytes, 0)
        self.assertEqual(group.potential_reclaim_bytes, 0)

    def test_hardlinks_do_not_inflate_duplicates_or_reclaim(self) -> None:
        first = self.write("alpha", b"shared")
        alias = self.source / "beta"
        other = self.write("gamma", b"shared")
        try:
            os.link(first, alias)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")

        report = scan_duplicates([self.root()])

        self.assertEqual(report.files_scanned, 2)
        self.assertEqual(report.skipped_hardlinks, 1)
        self.assertEqual([entry.path for entry in report.groups[0].files], [first, other])
        self.assertEqual(report.logical_duplicate_bytes, 6)
        # The first inode has another link, so retain it; only gamma can be
        # considered individually reclaimable without tracking every link.
        self.assertEqual(report.potential_reclaim_bytes, 6)
        self.assertTrue(alias.samefile(first))

    def test_two_names_for_same_inode_alone_make_no_group(self) -> None:
        first = self.write("alpha", b"shared")
        try:
            os.link(first, self.source / "beta")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        report = scan_duplicates([self.root()])
        self.assertEqual(report.groups, ())
        self.assertEqual(report.skipped_hardlinks, 1)
        self.assertEqual(report.potential_reclaim_bytes, 0)

    def test_symlink_files_and_directory_are_not_followed(self) -> None:
        outside = self.base / "private"
        outside.mkdir()
        secret = self.write("secret", b"same", source=outside)
        local = self.write("local", b"same")
        try:
            (self.source / "file-alias").symlink_to(secret)
            (self.source / "dir-alias").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        report = scan_duplicates([self.root()])
        self.assertEqual(report.files_scanned, 1)
        self.assertEqual(report.groups, ())
        self.assertTrue(local.exists() and secret.exists())

    def test_linked_root_and_linked_parent_are_rejected(self) -> None:
        root_alias = self.base / "linked-root"
        try:
            root_alias.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(ValueError):
            scan_duplicates([self.root(source=root_alias)])
        with self.assertRaises(ValueError):
            scan_duplicates([self.root(source=root_alias / "nested")])

    def test_nested_destination_excluded_and_roots_can_cross_match(self) -> None:
        first = self.write("a.bin", b"equal")
        self.write("organized/ignored.bin", b"equal")
        second_root = self.base / "second"
        second_root.mkdir()
        second = self.write("b.bin", b"equal", source=second_root)
        configured = self.root(destination=self.source / "organized")
        other = self.root(source=second_root)

        report = scan_duplicates([other, configured])
        self.assertEqual(report.files_scanned, 2)
        self.assertEqual([entry.path for entry in report.groups[0].files], [first, second])
        self.assertEqual(scan_duplicates([configured, other]), report)

    def test_overlapping_roots_do_not_double_count_paths(self) -> None:
        nested = self.source / "nested"
        nested.mkdir()
        self.write("one", b"same")
        self.write("two", b"same", source=nested)
        report = scan_duplicates([self.root(), self.root(source=nested)])
        self.assertEqual(report.files_scanned, 2)
        self.assertEqual(len(report.groups), 1)

    def test_budgets_fail_closed(self) -> None:
        self.write("a", b"same")
        self.write("b", b"same")
        with self.assertRaisesRegex(ValueError, "max_files"):
            scan_duplicates([self.root()], max_files=1)
        with self.assertRaisesRegex(ValueError, "max_entries"):
            scan_duplicates([self.root()], max_entries=1)
        with self.assertRaisesRegex(ValueError, "max_roots"):
            scan_duplicates([self.root(), self.root()], max_roots=1)
        for invalid in (0, -1, 1.2, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                scan_duplicates([self.root()], chunk_size=invalid)
        with self.assertRaises(ValueError):
            scan_duplicates([self.root()], chunk_size=9 * 1024 * 1024)

    def test_file_changed_during_streaming_is_skipped(self) -> None:
        first = self.write("a", b"01234567")
        second = self.write("b", b"01234567")
        original_read = os.read
        changed = False

        def concurrent_write(fd: int, count: int) -> bytes:
            nonlocal changed
            data = original_read(fd, count)
            if not changed and data and os.fstat(fd).st_ino == second.stat().st_ino:
                second.write_bytes(b"changed!")  # same byte length, different mtime/ctime
                changed = True
            return data

        with patch("ocd.duplicates.os.read", side_effect=concurrent_write):
            report = scan_duplicates([self.root()], chunk_size=2)
        self.assertTrue(changed)
        self.assertEqual(report.groups, ())
        self.assertEqual(report.files_hashed, 1)
        self.assertEqual(report.skipped_unstable, (second,))
        self.assertEqual(first.read_bytes(), b"01234567")

    def test_path_based_fallback_can_hash_and_verify_regular_files(self) -> None:
        # Exercise the non-directory-FD branch on Linux too; on Windows this
        # is the normal implementation used for native path traversal.
        self.write("nested/a", b"same content")
        self.write("nested/b", b"same content")
        with patch("ocd.duplicates._CAN_SCAN_FD", False):
            report = scan_duplicates([self.root()], chunk_size=2)
        self.assertEqual(len(report.groups), 1)
        self.assertEqual(report.files_hashed, 2)

    def test_path_based_fallback_rechecks_root_identity(self) -> None:
        first = self.write("a", b"equal")
        self.write("b", b"equal")
        # Simulate the source directory being replaced between discovery and
        # content reads. The full scan must never claim those files matched.
        original_hash = duplicate_module._hash_candidate
        swapped = False

        def swap_before_hash(candidate, chunk_size):
            nonlocal swapped
            if not swapped:
                self.source.rename(self.base / "retired")
                self.source.mkdir()
                self.write("a", b"equal")
                self.write("b", b"equal")
                swapped = True
            return original_hash(candidate, chunk_size)

        with patch("ocd.duplicates._CAN_SCAN_FD", False), patch(
            "ocd.duplicates._hash_candidate", side_effect=swap_before_hash
        ):
            report = scan_duplicates([self.root()])
        self.assertTrue(swapped)
        self.assertEqual(report.groups, ())
        self.assertEqual(report.files_hashed, 0)
        self.assertEqual(len(report.skipped_unstable), 2)
        self.assertEqual(first.read_bytes(), b"equal")

    @unittest.skipUnless(os.name != "nt", "requires POSIX O_NOFOLLOW/openat")
    def test_file_replaced_by_symlink_before_open_is_not_hashed(self) -> None:
        first = self.write("a", b"shared")
        second = self.write("b", b"shared")
        outside = self.write("secret", b"shared", source=self.base)
        original_open = os.open
        swapped = False

        def replace_on_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if path == "b" and dir_fd is not None and not swapped:
                second.unlink()
                second.symlink_to(outside)
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with patch("ocd.duplicates.os.open", side_effect=replace_on_open):
            report = scan_duplicates([self.root()])
        self.assertTrue(swapped)
        self.assertEqual(report.groups, ())
        self.assertEqual(report.skipped_unstable, (second,))
        self.assertTrue(first.exists() and outside.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_junction_is_excluded(self) -> None:
        import subprocess

        outside = self.base / "private"
        outside.mkdir()
        self.write("secret", b"data", source=outside)
        junction = self.source / "junction"
        creation = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            text=True,
            capture_output=True,
            check=False,
        )
        if creation.returncode:
            self.skipTest(f"junction creation unavailable: {creation.stderr}")
        self.assertEqual(scan_duplicates([self.root()]).files_scanned, 0)


if __name__ == "__main__":
    unittest.main()
