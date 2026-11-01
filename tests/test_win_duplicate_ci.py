"""Exercise the Windows scandir identity contract on any test platform."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ocd import duplicates as duplicate_module
from ocd.types import RootConfig


class _ZeroIdentityStat:
    """Windows DirEntry.stat() omits device, inode, and link count."""

    def __init__(self, actual):
        self.actual = actual

    def __getattr__(self, key):
        if key in {"st_dev", "st_ino", "st_nlink"}:
            return 0
        return getattr(self.actual, key)


class _WindowsDirEntry:
    def __init__(self, actual):
        self.actual = actual
        self.name = actual.name

    def stat(self, *, follow_symlinks=False):
        return _ZeroIdentityStat(self.actual.stat(follow_symlinks=follow_symlinks))


class _EarlierCtime:
    """A just-enumerated Windows stat snapshot before NTFS ctime settles."""

    def __init__(self, actual):
        self.actual = actual

    def __getattr__(self, key):
        if key == "st_ctime_ns":
            return self.actual.st_ctime_ns - 2_000_000
        return getattr(self.actual, key)


class WindowsIdentityTests(unittest.TestCase):
    def test_windows_ctime_settles_before_hash_but_hash_window_stays_strict(self):
        with tempfile.TemporaryDirectory() as base:
            source = Path(base) / "source"
            source.mkdir()
            first = source / "first.bin"
            second = source / "second.bin"
            first.write_bytes(b"same bytes")
            second.write_bytes(b"same bytes")
            actual = first.lstat()
            stale = _EarlierCtime(actual)
            self.assertFalse(duplicate_module._matching(actual, stale))
            self.assertTrue(duplicate_module._matching(actual, stale, check_ctime=False))

            real_from_stat = duplicate_module._from_stat

            def enumerated_before_settle(path, root, root_stat, file_stat):
                candidate = real_from_stat(path, root, root_stat, file_stat)
                if path == first:
                    return replace(candidate, details=_EarlierCtime(candidate.details))
                return candidate

            with (
                patch.object(duplicate_module, "_CAN_SCAN_FD", False),
                patch.object(duplicate_module, "_WINDOWS_NEEDS_PATH_LSTAT", True),
                patch.object(duplicate_module, "_WINDOWS_CTIME_CAN_SETTLE", True),
                patch.object(duplicate_module, "_from_stat", side_effect=enumerated_before_settle),
            ):
                report = duplicate_module.scan_duplicates([
                    RootConfig(source, Path(base) / "organized")
                ])

            self.assertEqual(report.files_hashed, 2)
            self.assertEqual(report.skipped_unstable, ())
            self.assertEqual(len(report.groups), 1)
            self.assertEqual({f.path for f in report.groups[0].files}, {first, second})

    def test_mixed_alias_destination_projects_back_under_source_spelling(self):
        # A POSIX symlink models only the Windows 8.3 spelling expansion for
        # this pure pruning helper. Actual admission still rejects symlinks.
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            long = root / "canonical"
            incoming = long / "incoming"
            destination = incoming / "organized"
            destination.mkdir(parents=True)
            short = root / "alias"
            try:
                short.symlink_to(long, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"alias fixture unavailable: {exc}")
            original_spelling = short / "incoming"
            with patch.object(duplicate_module.os, "name", "nt"):
                pruned = duplicate_module._prune_destination_for_source(
                    original_spelling, destination
                )
            self.assertEqual(pruned, original_spelling / "organized")

    def test_path_fallback_uses_full_path_identity_instead_of_zero_direntry_fields(self):
        with tempfile.TemporaryDirectory() as base:
            source = Path(base) / "source"
            source.mkdir()
            (source / "nested").mkdir()
            first = source / "nested" / "first.bin"
            second = source / "second.bin"
            first.write_bytes(b"equal")
            second.write_bytes(b"equal")
            alias = source / "third.bin"
            os.link(second, alias)

            real_scandir = os.scandir

            @contextmanager
            def windows_scandir(path):
                with real_scandir(path) as entries:
                    yield (_WindowsDirEntry(entry) for entry in entries)

            root = RootConfig(source, Path(base) / "organized")
            with (
                patch.object(duplicate_module, "_CAN_SCAN_FD", False),
                patch.object(duplicate_module, "_WINDOWS_NEEDS_PATH_LSTAT", True, create=True),
                patch.object(duplicate_module.os, "scandir", side_effect=windows_scandir),
            ):
                report = duplicate_module.scan_duplicates([root])

            self.assertEqual(report.files_scanned, 2)
            self.assertEqual(report.skipped_hardlinks, 1)
            self.assertEqual(report.files_hashed, 2)
            self.assertEqual(len(report.groups), 1)
            self.assertEqual({x.path for x in report.groups[0].files}, {first, second})
            self.assertEqual(report.skipped_unstable, ())


if __name__ == "__main__":
    unittest.main()
