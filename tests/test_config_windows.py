"""Windows root-admission regressions, runnable without a Windows host."""

from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest import mock

from ocd.config import ConfigError, verified_root
from ocd.types import RootConfig


class WindowsRootAdmissionTests(unittest.TestCase):
    def test_short_dos_alias_spelling_may_resolve_differently(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "RUNNER~1"
            source = parent / "source"
            destination = parent / "destination"
            source.mkdir(parents=True)
            destination.mkdir()
            root = RootConfig(source, destination)

            # Native Windows resolves RUNNER~1 to its real long name. It is
            # still the same directory and not a link/reparse traversal.
            with mock.patch("ocd.config._on_windows", return_value=True), \
                    mock.patch.object(Path, "resolve", return_value=Path(temp) / "Runner Name"), \
                    mock.patch("ocd.config.os.path.samefile", return_value=True) as same:
                self.assertIs(verified_root(root), root)
                self.assertEqual(same.call_count, 2)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_reparse_ancestor_is_rejected_even_when_target_is_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            physical = base / "physical"
            (physical / "source").mkdir(parents=True)
            (physical / "destination").mkdir()
            alias = base / "alias"
            try:
                alias.symlink_to(physical, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation restricted")
            root = RootConfig(alias / "source", alias / "destination")
            with mock.patch("ocd.config._on_windows", return_value=True):
                with self.assertRaisesRegex(ConfigError, "has changed"):
                    verified_root(root)

    def test_directory_with_windows_reparse_bit_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, dest = base / "source", base / "destination"
            source.mkdir()
            dest.mkdir()
            root = RootConfig(source, dest)
            actual_lstat = Path.lstat

            def lstat_with_reparse(self, *args, **kwargs):
                details = actual_lstat(self, *args, **kwargs)
                if self == source:
                    return type("Attributes", (), {
                        "st_mode": details.st_mode,
                        "st_file_attributes": getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                    })()
                return details

            with mock.patch("ocd.config._on_windows", return_value=True), \
                    mock.patch.object(Path, "lstat", lstat_with_reparse):
                with self.assertRaisesRegex(ConfigError, "has changed"):
                    verified_root(root)


if __name__ == "__main__":
    unittest.main()
