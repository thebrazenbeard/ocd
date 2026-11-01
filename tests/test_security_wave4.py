"""Independent, disposable-fixture tests for cross-platform trust boundaries.

These tests deliberately use only temporary trees, mocked network calls, and
pure path parsers.  In particular they never inspect or move a real model.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import apply as apply_engine, artifacts, win32  # noqa: E402
from ocd.types import MovePlan, RootConfig  # noqa: E402


class CrossPlatformPathTrustTests(unittest.TestCase):
    def _entry(self, name: str) -> artifacts.ArtifactEntry:
        return artifacts.ArtifactEntry(
            url="https://example.org/download/sample",
            sha256=hashlib.sha256(b"safe").hexdigest(),
            size=4,
            destination_relpath=name,
        )

    def test_artifact_manifest_rejects_windows_devices_and_illegal_names(self):
        # Superscript-digit device names and CONIN$/CONOUT$ are reserved on
        # Windows even though they are ordinary Linux path components.
        # The manifest path schema must be portable before any download.
        for name in (
            "COM\u00b9.txt", "dir/LPT\u00b2.bin", "dir/CONIN$.log", "CONOUT$.log",
            "dir/bad<name.txt", "dir/bad>name.txt", "dir/a?b", "dir/a*b",
            "dir/a|b", "dir/a\"b",
        ):
            with self.subTest(name=name):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts._parse_entry(vars(self._entry(name)))

    def test_artifact_manifest_rejects_bidi_controls_in_paths(self):
        # Visual spoofing is particularly harmful for binary weights or
        # executables presented to a human for cleanup/retrieval decisions.
        for name in ("evil\u202eexe.txt", "dir/hidden\u2066name", "dir/a\ufeffb"):
            with self.subTest(name=name):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts._parse_entry(vars(self._entry(name)))

    def test_win32_rename_rejects_console_aliases_and_bidi_names(self):
        for name in ("CONIN$.txt", "CONOUT$.txt", "evil\u202eexe.txt"):
            with self.subTest(name=name):
                with self.assertRaises(win32.UnsafeWindowsMove):
                    win32._path_parts("C:\\incoming\\" + name)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux renameat2 and hardlinks")
class LinuxCommitRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source_root = self.base / "incoming"
        self.destination_root = self.base / "organized"
        self.source_root.mkdir()
        self.destination_root.mkdir()
        self.source = self.source_root / "important.bin"
        self.source.write_bytes(b"original bytes that must survive")
        details = self.source.stat()
        self.destination = self.destination_root / "safe" / "important.bin"
        self.plan = MovePlan(
            self.source, self.destination, "safe", details.st_size, details.st_mtime_ns,
        )
        self.root = RootConfig(self.source_root, self.destination_root, mode="apply")

    def test_destination_hardlink_inserted_at_commit_is_not_replaced(self):
        actual_rename = apply_engine._rename_noreplace

        def create_hardlink_before_commit(source, destination, **kwargs):
            os.link(source, destination)
            return actual_rename(source, destination, **kwargs)

        with mock.patch.object(
            apply_engine, "_rename_noreplace", side_effect=create_hardlink_before_commit,
        ):
            result = apply_engine.apply_moves(
                self.root, (self.plan,), self.base / "transaction.jsonl",
            )
        self.assertFalse(result.moved)
        self.assertTrue(result.holds)
        self.assertFalse(result.recovery_required)
        self.assertEqual(self.source.read_bytes(), b"original bytes that must survive")
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.source.stat().st_ino, self.destination.stat().st_ino)

    def test_source_replaced_by_symlink_to_outside_at_commit_is_held(self):
        outside = self.base / "outside"
        outside.mkdir()
        sensitive = outside / "sensitive.bin"
        sensitive.write_bytes(b"sensitive outside the selected source")
        parked = self.base / "parked-original.bin"
        actual_rename = apply_engine._rename_noreplace

        def swap_source_then_commit(source, destination, **kwargs):
            source.rename(parked)
            source.symlink_to(sensitive)
            return actual_rename(source, destination, **kwargs)

        with mock.patch.object(
            apply_engine, "_rename_noreplace", side_effect=swap_source_then_commit,
        ):
            result = apply_engine.apply_moves(
                self.root, (self.plan,), self.base / "transaction.jsonl",
            )
        self.assertFalse(result.moved)
        self.assertTrue(result.holds)
        self.assertFalse(result.recovery_required)
        self.assertEqual(sensitive.read_bytes(), b"sensitive outside the selected source")
        self.assertEqual(parked.read_bytes(), b"original bytes that must survive")
        self.assertTrue(self.source.is_symlink())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
