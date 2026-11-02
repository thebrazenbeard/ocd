"""OS-owned observer lock tests; this module does not start an observer."""

from __future__ import annotations

import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.state_lock import StateLockBusy, UnsafeStateDirectory, acquire_state_lock


class StateLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ocd-state-lock-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.state = self.parent / "private-state"
        if os.name == "nt":
            # The native kernel primitive is source-only until DACL/NTFS
            # qualification. Unit fixtures explicitly opt in for this process.
            gate = mock.patch.dict(os.environ, {"OCD_EXPERIMENTAL_WINDOWS_STATE_LOCK": "1"})
            gate.start()
            self.addCleanup(gate.stop)

    @unittest.skipUnless(os.name == "nt", "native Windows fail-closed gate")
    def test_windows_native_lock_is_off_by_default(self) -> None:
        with mock.patch.dict(os.environ, {"OCD_EXPERIMENTAL_WINDOWS_STATE_LOCK": ""}):
            with self.assertRaises(UnsafeStateDirectory):
                with acquire_state_lock(self.state):
                    self.fail("unqualified Windows lock cannot be enabled implicitly")
        self.assertFalse(self.state.exists())

    def test_lock_file_is_stable_and_second_open_is_excluded(self) -> None:
        with acquire_state_lock(self.state) as held:
            self.assertEqual(held.name, "observer.lock")
            identity = held.stat().st_dev, held.stat().st_ino
            with self.assertRaises(StateLockBusy):
                with acquire_state_lock(self.state):
                    self.fail("a second independent open must not acquire")
        self.assertTrue(held.exists(), "unlock must never unlink the lock file")
        with acquire_state_lock(self.state) as again:
            self.assertEqual((again.stat().st_dev, again.stat().st_ino), identity)

    def test_exception_releases_lock_without_unlinking(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "caller failed"):
            with acquire_state_lock(self.state) as held:
                raise RuntimeError("caller failed")
        self.assertTrue(held.exists())
        with acquire_state_lock(self.state):
            pass

    def test_linked_state_directory_is_rejected_without_side_effects(self) -> None:
        actual = self.parent / "actual"
        actual.mkdir()
        alias = self.parent / "alias"
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"host disallows fixture symlinks: {exc}")
        with self.assertRaises(UnsafeStateDirectory):
            with acquire_state_lock(alias / "nested"):
                self.fail("linked ancestor should never be admitted")
        self.assertFalse((actual / "nested").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow lock contract")
    def test_linked_lock_file_is_rejected_without_touching_target(self) -> None:
        self.state.mkdir(mode=0o700)
        outside = self.parent / "important.txt"
        outside.write_text("must remain", encoding="utf-8")
        (self.state / "observer.lock").symlink_to(outside)
        with self.assertRaises(UnsafeStateDirectory):
            with acquire_state_lock(self.state):
                self.fail("lock path may not redirect outside state")
        self.assertEqual(outside.read_text(encoding="utf-8"), "must remain")

    @unittest.skipUnless(os.name == "posix", "POSIX private lock mode")
    def test_existing_world_readable_lock_file_is_rejected_without_chmod(self) -> None:
        self.state.mkdir(mode=0o700)
        sentinel = self.state / "observer.lock"
        sentinel.write_text("", encoding="utf-8")
        sentinel.chmod(0o644)
        with self.assertRaises(UnsafeStateDirectory):
            with acquire_state_lock(self.state):
                self.fail("insecure existing lock mode must not be silently repaired")
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o644)

    def test_relative_and_dotdot_state_paths_are_rejected(self) -> None:
        for unsafe in (Path("relative"), self.parent / "child" / ".." / "state"):
            with self.subTest(unsafe=unsafe), self.assertRaises(UnsafeStateDirectory):
                with acquire_state_lock(unsafe):
                    self.fail("ambiguous state path")

    @unittest.skipUnless(os.name == "posix", "POSIX untrusted-ancestor permission boundary")
    def test_nonsticky_world_writable_ancestor_is_rejected_before_creation(self) -> None:
        unsafe_parent = self.parent / "anyone-can-rename"
        unsafe_parent.mkdir(mode=0o777)
        unsafe_parent.chmod(0o777)
        state = unsafe_parent / "new-state"
        with self.assertRaises(UnsafeStateDirectory):
            with acquire_state_lock(state):
                self.fail("other users may replace this state directory")
        self.assertFalse(state.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX private mode contract")
    def test_existing_world_readable_state_directory_is_rejected_without_chmod(self) -> None:
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(UnsafeStateDirectory):
            with acquire_state_lock(self.state):
                self.fail("unsafe state dir should not be silently repaired")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)
        self.assertFalse((self.state / "observer.lock").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess handshake")
    def test_crashed_owner_releases_kernel_lock(self) -> None:
        snippet = (
            "import sys,time; from pathlib import Path; "
            "from ocd.state_lock import acquire_state_lock; "
            "lock=acquire_state_lock(Path(sys.argv[1])); "
            "lock.__enter__(); print('READY',flush=True); time.sleep(60)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        owner = subprocess.Popen(
            [sys.executable, "-u", "-c", snippet, str(self.state)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        self.addCleanup(lambda: owner.kill() if owner.poll() is None else None)
        self.addCleanup(lambda: owner.stdout.close() if owner.stdout else None)
        self.addCleanup(lambda: owner.stderr.close() if owner.stderr else None)
        assert owner.stdout is not None
        ready, _, _ = select.select([owner.stdout], [], [], 5)
        self.assertTrue(ready, "subprocess must report lock ownership promptly")
        self.assertEqual(owner.stdout.readline().strip(), b"READY")
        with self.assertRaises(StateLockBusy):
            with acquire_state_lock(self.state):
                self.fail("live child still owns kernel lock")
        owner.kill()
        owner.wait(timeout=5)
        with acquire_state_lock(self.state):
            pass

    @unittest.skipUnless(os.name == "nt", "native Windows LockFileEx crash fixture")
    def test_windows_crashed_owner_releases_lock(self) -> None:
        ready_marker = self.parent / "held.ready"
        snippet = (
            "import sys,time\nfrom pathlib import Path\n"
            "from ocd.state_lock import acquire_state_lock\n"
            "with acquire_state_lock(Path(sys.argv[1])):\n"
            "    Path(sys.argv[2]).write_text('ready')\n"
            "    time.sleep(60)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        owner = subprocess.Popen(
            [sys.executable, "-u", "-c", snippet, str(self.state), str(ready_marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        self.addCleanup(lambda: owner.kill() if owner.poll() is None else None)
        self.addCleanup(lambda: owner.stdout.close() if owner.stdout else None)
        self.addCleanup(lambda: owner.stderr.close() if owner.stderr else None)
        deadline = time.monotonic() + 5
        while not ready_marker.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)  # Wait for the child's explicit readiness file.
        self.assertTrue(ready_marker.exists(), "Windows child failed to acquire lock")
        with self.assertRaises(StateLockBusy):
            with acquire_state_lock(self.state):
                self.fail("live native child owns the LockFileEx region")
        owner.kill()
        owner.wait(timeout=5)
        with acquire_state_lock(self.state):
            pass


if __name__ == "__main__":
    unittest.main()
