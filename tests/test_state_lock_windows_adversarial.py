"""Native Windows adversarial state-path tests, strictly disposable fixtures.

These deliberately probe a stronger property than the initial state-lock
contract (which excludes a malicious same-account actor): the logical path
must not admit two simultaneous owners if its parent is moved.  They never
operate on OCD's real state or on user-selected storage.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import state_lock
from ocd.state_lock import StateLockBusy, UnsafeStateDirectory, acquire_state_lock


@unittest.skipUnless(os.name == "nt", "native Windows NTFS adversarial fixture")
class WindowsStatePathAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        local = os.environ.get("LOCALAPPDATA")
        if not local or not Path(local).is_dir():
            self.skipTest("an existing LOCALAPPDATA directory is required")
        temporary = tempfile.TemporaryDirectory(prefix="ocd-adversarial-", dir=local)
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.state = self.parent / "state"
        opt_in = mock.patch.dict(os.environ, {"OCD_EXPERIMENTAL_WINDOWS_STATE_LOCK": "1"})
        opt_in.start()
        self.addCleanup(opt_in.stop)

    @staticmethod
    def _create_junction(link: Path, target: Path) -> bool:
        # All paths must be controlled fixture paths; no shell interpolated
        # script text or targets outside this TemporaryDirectory.
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return result.returncode == 0 and link.is_junction()

    def _launch_holder(self, path: Path, marker: Path) -> subprocess.Popen[str]:
        child_code = (
            "import json,sys,time\n"
            "from pathlib import Path\n"
            "from ocd.state_lock import acquire_state_lock\n"
            "with acquire_state_lock(Path(sys.argv[1])) as locked:\n"
            "    info=locked.stat()\n"
            "    Path(sys.argv[2]).write_text(json.dumps({'dev':info.st_dev,'ino':info.st_ino}))\n"
            "    time.sleep(30)\n"
        )
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code, str(path), str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=child_env,
        )

        def stop_child() -> None:
            if child.poll() is None:
                child.terminate()
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)

        self.addCleanup(stop_child)
        deadline = time.monotonic() + 7
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not marker.exists():
            _, stderr = child.communicate(timeout=5) if child.poll() is not None else ("", "")
            self.fail(f"fixture holder did not establish lock: {stderr[-600:]}")
        return child

    def test_parent_rename_cannot_create_two_simultaneous_lock_owners(self) -> None:
        ready = self.parent / "owner-ready.json"
        owner = self._launch_holder(self.state, ready)
        first_id = json.loads(ready.read_text(encoding="utf-8"))
        self.assertTrue(first_id["ino"], "owner must report a usable file identity")
        parked = self.parent / "parked-original-state"
        alternate = self.parent / "alternate-state"
        alternate.mkdir()

        try:
            self.state.rename(parked)
        except OSError as exc:
            # Windows denying a rename because the active file handle has no
            # FILE_SHARE_DELETE is an acceptable protective outcome.
            if getattr(exc, "winerror", None) in (5, 32, 33):
                self.assertIsNone(owner.poll(), "first owner must still be live")
                return
            raise

        alternate.rename(self.state)
        self.assertIsNone(owner.poll(), "first owner must still hold the old lock")
        try:
            # If this context enters, a second independently opened file was
            # locked under the same requested pathname. That is split-brain.
            with acquire_state_lock(self.state) as second:
                info = second.stat()
                self.fail(
                    "SPLIT-BRAIN: two live owners at one state pathname; "
                    f"first={first_id} second={{'dev':{info.st_dev},'ino':{info.st_ino}}}"
                )
        except (StateLockBusy, UnsafeStateDirectory):
            pass  # Safe failure while another process still holds the lock.

    def test_junction_substituted_after_state_validation_is_rejected(self) -> None:
        target = self.parent / "outside-target"
        target.mkdir()
        marker = target / "sentinel.txt"
        marker.write_text("unchanged", encoding="utf-8")
        probe = self.parent / "junction-probe"
        if not self._create_junction(probe, target):
            self.skipTest("this Windows host cannot create a disposable junction")
        probe.rmdir()

        actual = state_lock._private_state_dir
        parked = self.parent / "parked-validated-state"

        def swap_after_validation(value: Path) -> Path:
            validated = actual(value)
            validated.rename(parked)
            if not self._create_junction(validated, target):
                self.fail("could not install disposable junction after validation")
            return validated

        try:
            with mock.patch.object(state_lock, "_private_state_dir", side_effect=swap_after_validation):
                with self.assertRaises(UnsafeStateDirectory):
                    with acquire_state_lock(self.state):
                        self.fail("junction swap escaped the validated state directory")
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse((target / "observer.lock").exists(),
                             "lock file must never be created across a junction")
        finally:
            if self.state.is_junction():
                self.state.rmdir()  # Remove the junction, never traverse its target.


if __name__ == "__main__":
    unittest.main()
