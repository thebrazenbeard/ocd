"""Adversarial filesystem regression tests.

Tests use disposable temporary directories and do not mutate user files.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.scanner import scan_root
from ocd.apply import apply_moves
from ocd import apply as apply_engine
from ocd.types import MovePlan, RootConfig


class ScannerAdversarialTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "dirfd swap fixture is Linux-specific")
    def test_directory_swapped_to_outside_symlink_before_scandir_is_not_enumerated(self) -> None:
        """A check/open race must not return files from outside the root."""
        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            source = base / "source"
            source.mkdir()
            nested = source / "nested"
            nested.mkdir()
            outside = base / "outside"
            outside.mkdir()
            external_file = outside / "private.mkv"
            external_file.write_bytes(b"external content")
            os.utime(external_file, ns=(1_000_000_000, 1_000_000_000))

            # Require symlink support before starting. The link is removed so
            # the controlled swap can create it at the exact race point.
            probe = base / "probe"
            try:
                probe.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            probe.unlink()

            original_scandir = os.scandir
            original_open = os.open
            swapped = False

            def perform_swap() -> None:
                nonlocal swapped
                swapped = True
                nested.rename(base / "retired-nested")
                nested.symlink_to(outside, target_is_directory=True)

            def swap_then_scandir(directory: os.PathLike[str] | str | int):
                if not isinstance(directory, int) and Path(directory) == nested and not swapped:
                    perform_swap()
                return original_scandir(directory)

            def swap_then_open(file, flags, *args, **kwargs):
                # POSIX directory-fd recursion opens the just-lstat'ed child
                # by its basename relative to an already opened parent fd.
                if file == "nested" and "dir_fd" in kwargs and not swapped:
                    perform_swap()
                return original_open(file, flags, *args, **kwargs)

            root = RootConfig(source=source, destination=base / "destination", settle_seconds=0)
            with patch("ocd.scanner.os.scandir", side_effect=swap_then_scandir), patch(
                "ocd.scanner.os.open", side_effect=swap_then_open
            ):
                try:
                    entries = scan_root(root)
                except ValueError:
                    return  # Explicit fail-closed abort is also safe.

            self.assertTrue(swapped, "adversarial race did not execute")
            self.assertEqual(entries, (), "scanner returned a path backed by outside-root content")


class ApplyAdversarialTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is Linux-only")
    def test_destination_parent_symlink_swap_at_commit_must_not_redirect_move(self) -> None:
        """A replaced destination directory cannot redirect a move outside root."""
        with tempfile.TemporaryDirectory() as workspace:
            base = Path(workspace)
            source_root = base / "source"
            destination_root = base / "destination"
            outside = base / "outside"
            for directory in (source_root, destination_root, outside):
                directory.mkdir()
            probe = base / "probe"
            try:
                probe.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            probe.unlink()

            source = source_root / "valuable.txt"
            source.write_bytes(b"must stay within the authorized roots")
            details = source.stat()
            category = destination_root / "Documents"
            destination = category / "valuable.txt"
            plan = MovePlan(source, destination, "Documents", details.st_size, details.st_mtime_ns)
            root = RootConfig(source_root, destination_root, mode="apply")
            original_rename = apply_engine._rename_noreplace
            swapped = False

            def swap_parent_then_rename(src: Path, dst: Path, **kwargs) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    category.rename(base / "parked-category")
                    category.symlink_to(outside, target_is_directory=True)
                original_rename(src, dst, **kwargs)

            with patch.object(apply_engine, "_rename_noreplace", side_effect=swap_parent_then_rename):
                result = apply_moves(root, [plan], base / "journal.jsonl")

            self.assertTrue(swapped, "adversarial race did not execute")
            self.assertFalse(
                (outside / "valuable.txt").exists(),
                f"out-of-root destination was modified; apply result: {result}",
            )


if __name__ == "__main__":
    unittest.main()
