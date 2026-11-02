"""Source-only Linux mount admission tests for the OCD state-lock prototype."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.state_lock import UnsafeStateDirectory, _linux_local_filesystem, acquire_state_lock


def _entry(mount: str, filesystem: str, identifier: int = 30) -> str:
    return (
        f"{identifier} 20 8:1 / {mount} rw,nosuid,nodev - "
        f"{filesystem} /dev/test rw"
    )


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux mount namespace only")
class LocalLinuxFilesystemTests(unittest.TestCase):
    @staticmethod
    def _mountinfo(text: str):
        original = Path.read_text

        def reader(path: Path, *args: object, **kwargs: object) -> str:
            if str(path) == "/proc/self/mountinfo":
                return text
            return original(path, *args, **kwargs)

        return mock.patch.object(Path, "read_text", reader)

    def test_known_local_filesystems_admitted(self) -> None:
        for filesystem in ("ext4", "xfs", "btrfs", "tmpfs", "zfs", "f2fs"):
            with self.subTest(filesystem=filesystem):
                with self._mountinfo(_entry("/srv", filesystem)):
                    _linux_local_filesystem(Path("/srv/state"))

    def test_network_or_unqualified_filesystems_fail_closed(self) -> None:
        for filesystem in (
            "nfs", "nfs4", "cifs", "smb3", "fuse", "fuse.sshfs", "9p",
            "overlay", "ntfs3", "unknownfs",
        ):
            with self.subTest(filesystem=filesystem):
                with self._mountinfo(_entry("/srv", filesystem)):
                    with self.assertRaises(UnsafeStateDirectory):
                        _linux_local_filesystem(Path("/srv/state"))

    def test_longest_mount_wins_for_bind_mount_or_nested_mount(self) -> None:
        view = "\n".join((_entry("/", "nfs"), _entry("/srv", "ext4", 31)))
        with self._mountinfo(view):
            _linux_local_filesystem(Path("/srv/state"))
            with self.assertRaises(UnsafeStateDirectory):
                _linux_local_filesystem(Path("/other/state"))

    def test_escaped_mount_names_are_decoded(self) -> None:
        with self._mountinfo(_entry("/srv/my\\040models", "ext4")):
            _linux_local_filesystem(Path("/srv/my models/state"))

    def test_ambiguous_stacked_mount_and_malformed_mountinfo_refused(self) -> None:
        views = (
            "\n".join((_entry("/srv", "ext4"), _entry("/srv", "nfs", 31))),
            "mountinfo content is not valid",
            "30 20 8:1 / /srv rw - ext4",
            "",
        )
        for view in views:
            with self.subTest(view=view), self._mountinfo(view):
                with self.assertRaises(UnsafeStateDirectory):
                    _linux_local_filesystem(Path("/srv/state"))

    def test_missing_mountinfo_refused(self) -> None:
        with mock.patch.object(Path, "read_text", side_effect=OSError("proc unavailable")):
            with self.assertRaises(UnsafeStateDirectory):
                _linux_local_filesystem(Path("/srv/state"))

    def test_network_mount_refused_before_state_directory_created(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as parent:
            state = Path(parent) / "new-state"
            with self._mountinfo(_entry("/", "nfs")):
                with self.assertRaises(UnsafeStateDirectory):
                    with acquire_state_lock(state):
                        self.fail("remote states must not be locked")
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
