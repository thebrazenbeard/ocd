"""Native qualification probes for the currently disabled Windows apply path.

All native mutations below are confined to TemporaryDirectory-owned fixtures.
The public ``apply_moves`` gate remains closed until the *whole* journalled
transaction has appropriate Windows primitives and a broader filesystem matrix.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import apply as engine  # noqa: E402
from ocd.types import MovePlan, RootConfig  # noqa: E402
from ocd import win32  # noqa: E402


class WindowsApplyGateTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "public Windows gate is native-only")
    def test_apply_does_not_write_journal_or_move(self):
        with tempfile.TemporaryDirectory(prefix="ocd-win-apply-") as temporary:
            base = Path(temporary)
            incoming, organized = base / "incoming", base / "organized"
            incoming.mkdir()
            organized.mkdir()
            source = incoming / "fixture.txt"
            source.write_bytes(b"only disposable test data")
            current = source.stat()
            destination = organized / "Documents" / source.name
            plan = MovePlan(source, destination, "document", current.st_size, current.st_mtime_ns)
            journal = base / "apply.jsonl"
            result = engine.apply_moves(
                RootConfig(source=incoming, destination=organized, mode="apply"),
                [plan], journal,
            )
            self.assertTrue(result.holds)
            self.assertIn("Windows apply disabled", result.holds[0].reason)
            self.assertTrue(source.is_file())
            self.assertFalse(destination.exists())
            self.assertFalse(journal.exists())
            self.assertFalse(destination.parent.exists())

    def test_observe_mode_is_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="ocd-win-observe-") as temporary:
            base = Path(temporary)
            incoming, organized = base / "incoming", base / "organized"
            incoming.mkdir()
            organized.mkdir()
            source = incoming / "fixture.txt"
            source.write_bytes(b"test only")
            current = source.stat()
            destination = organized / "Documents" / source.name
            plan = MovePlan(source, destination, "document", current.st_size, current.st_mtime_ns)
            with mock.patch.object(engine, "_rename_noreplace", side_effect=AssertionError("must not move")):
                result = engine.apply_moves(
                    RootConfig(source=incoming, destination=organized, mode="observe"),
                    [plan], base / "apply.jsonl",
                )
            self.assertEqual(result.observed, (plan,))
            self.assertFalse(result.moved or result.holds)
            self.assertTrue(source.exists())
            self.assertFalse(destination.parent.exists())


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("OCD_NATIVE_WINDOWS_QUALIFICATION") == "1",
    "native Win32 rename fixtures require Windows and explicit "
    "OCD_NATIVE_WINDOWS_QUALIFICATION=1 (manual qualification workflow)",
)
class NativeWin32RenameFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocd-native-win32-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.src_root = self.base / "incoming"
        self.dst_root = self.base / "organized"
        self.src_root.mkdir()
        self.dst_root.mkdir()
        try:
            self.api = win32._Win32Api()
            self.api.require_local_drive(self.base.drive.upper())
        except (OSError, AttributeError) as exc:
            self.skipTest(f"native fixture drive cannot qualify Windows primitive: {exc}")

    def identity(self, source: Path) -> tuple[int, int]:
        handle = self.api.open(str(source))
        assert handle is not None
        try:
            return self.api.info(handle).identity
        finally:
            self.api.close(handle)

    def attempt(self, source: Path, destination: Path) -> None:
        current = source.stat()
        win32.rename_noreplace(
            source, destination, identity=self.identity(source),
            size=current.st_size, mtime_ns=current.st_mtime_ns,
        )

    def test_rename_disposable_fixture_to_unoccupied_target(self):
        source = self.src_root / "fixture-ø.txt"
        destination = self.dst_root / "fixture-ø.txt"
        source.write_bytes(b"disposable Win32 test data")
        self.attempt(source, destination)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b"disposable Win32 test data")

    def test_existing_different_file_is_never_replaced(self):
        source = self.src_root / "fixture.txt"
        destination = self.dst_root / "fixture.txt"
        source.write_bytes(b"source fixture")
        destination.write_bytes(b"unrelated destination fixture")
        with self.assertRaises(OSError):
            self.attempt(source, destination)
        self.assertEqual(source.read_bytes(), b"source fixture")
        self.assertEqual(destination.read_bytes(), b"unrelated destination fixture")

    def test_existing_same_file_hardlink_is_not_consumed(self):
        # Detect the special FileRenameInfo/NTFS hardlink behavior which
        # currently blocks integration. The target is disposable test data.
        source = self.src_root / "fixture.txt"
        destination = self.dst_root / "fixture-hardlink.txt"
        source.write_bytes(b"single file with two names")
        try:
            os.link(source, destination)
        except OSError as exc:
            self.skipTest(f"native hardlink creation unavailable: {exc}")
        with self.assertRaises(OSError):
            self.attempt(source, destination)
        self.assertTrue(source.is_file(), "a hardlink alias collision must not consume source")
        self.assertEqual(destination.read_bytes(), b"single file with two names")

    def test_reparse_destination_parent_is_refused(self):
        source = self.src_root / "fixture.txt"
        source.write_bytes(b"source fixture")
        link = self.dst_root / "junction-like"
        outside = self.base / "outside"
        outside.mkdir()
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"native symlink creation unavailable: {exc}")
        with self.assertRaises(OSError):
            self.attempt(source, link / source.name)
        self.assertTrue(source.exists())
        self.assertFalse((outside / source.name).exists())


if __name__ == "__main__":
    unittest.main()
