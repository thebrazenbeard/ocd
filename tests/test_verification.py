"""Read-only artifact verification contract across Windows and Linux."""

from contextlib import redirect_stderr, redirect_stdout
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.artifacts import ArtifactEntry, ArtifactError  # noqa: E402
from ocd.cli import main  # noqa: E402
from ocd import verification  # noqa: E402


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ocd-verification-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "store"
        self.root.mkdir()
        (self.root / "models").mkdir()
        self.target = self.root / "models" / "one.gguf"
        self.payload = b"verified independent bytes\x00" * 223
        self.target.write_bytes(self.payload)
        self.entry = ArtifactEntry(
            "https://api.github.com/repos/example/models/releases/assets/1234",
            hashlib.sha256(self.payload).hexdigest(),
            len(self.payload),
            "models/one.gguf",
        )
        self.manifest = self.base / "artifacts.json"
        self.manifest.write_text(
            json.dumps({"artifacts": [vars(self.entry)]}), encoding="utf-8"
        )

    def test_matching_artifact_is_verified_without_changes(self):
        old = self.target.stat()
        result = verification.verify_local_artifact(self.entry, self.root)
        self.assertEqual((result.path, result.size, result.sha256),
                         (self.target, len(self.payload), self.entry.sha256))
        new = self.target.stat()
        self.assertEqual((old.st_size, old.st_mtime_ns, old.st_ino),
                         (new.st_size, new.st_mtime_ns, new.st_ino))
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["models"])

    def test_zero_byte_artifact(self):
        self.target.write_bytes(b"")
        entry = ArtifactEntry(self.entry.url, hashlib.sha256(b"").hexdigest(), 0,
                              self.entry.destination_relpath)
        self.assertEqual(verification.verify_local_artifact(entry, self.root).size, 0)

    def test_missing_corrupt_short_and_oversized_fail_closed(self):
        self.target.write_bytes(b"tamper" + self.payload[6:])
        with self.assertRaisesRegex(ArtifactError, "SHA-256"):
            verification.verify_local_artifact(self.entry, self.root)
        self.target.write_bytes(self.payload[:-1])
        with self.assertRaisesRegex(ArtifactError, "size"):
            verification.verify_local_artifact(self.entry, self.root)
        self.target.write_bytes(self.payload + b"!")
        with self.assertRaisesRegex(ArtifactError, "size"):
            verification.verify_local_artifact(self.entry, self.root)
        self.target.unlink()
        with self.assertRaises(ArtifactError):
            verification.verify_local_artifact(self.entry, self.root)

    def test_manifest_path_and_root_safety(self):
        for bad in ("../escape", "models/../../escape", "CON.txt", "models/name:stream"):
            with self.subTest(path=bad), self.assertRaises(ArtifactError):
                verification.verify_local_artifact(
                    ArtifactEntry(self.entry.url, self.entry.sha256,
                                  self.entry.size, bad), self.root,
                )
        with self.assertRaises(ArtifactError):
            verification.verify_local_artifact(self.entry, Path("relative"))
        with self.assertRaises(ArtifactError):
            verification.verify_local_artifact(self.entry, self.base / "nonexistent")

    def test_file_and_directory_symlinks_refused_if_supported(self):
        outside = self.base / "outside"
        outside.mkdir()
        outside_file = outside / "bytes.gguf"
        outside_file.write_bytes(self.payload)
        try:
            link = self.root / "models" / "link.gguf"
            link.symlink_to(outside_file)
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("host does not permit creating symlinks")
        for candidate in ("models/link.gguf", "linked/bytes.gguf"):
            with self.subTest(candidate=candidate), self.assertRaises(ArtifactError):
                verification.verify_local_artifact(
                    ArtifactEntry(self.entry.url, self.entry.sha256,
                                  self.entry.size, candidate), self.root,
                )
        self.assertEqual(outside_file.read_bytes(), self.payload)

    @unittest.skipUnless(os.name == "nt", "native Windows file-sharing fixture")
    def test_windows_refuses_already_open_writer(self):
        # Deliberately hold a writable handle with FILE_SHARE_READ|WRITE.
        # Verification's new FILE_SHARE_READ-only handle must be denied by
        # NTFS sharing rules instead of hashing bytes a writer could change.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p]
        create.restype = ctypes.c_void_p
        close = kernel.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        writer = create(str(self.target), 0x40000000, 0x1 | 0x2, None, 3, 0, None)
        invalid = ctypes.c_void_p(-1).value
        if writer in (None, invalid):
            self.fail(f"could not open writable test fixture: WinError {ctypes.get_last_error()}")
        try:
            with self.assertRaises(ArtifactError):
                verification.verify_local_artifact(self.entry, self.root)
            self.assertEqual(self.target.read_bytes(), self.payload)
        finally:
            self.assertTrue(close(writer))
        # Closing the writer restores verification eligibility.
        self.assertEqual(verification.verify_local_artifact(self.entry, self.root).sha256,
                         self.entry.sha256)

    @unittest.skipUnless(os.name == "posix", "POSIX rename-race fixture")
    def test_file_path_replacement_during_hash_is_refused(self):
        original_hash = verification._hash_descriptor

        def replaced(fd, entry):
            digest = original_hash(fd, entry)
            self.target.rename(self.base / "original.gguf")
            self.target.write_bytes(self.payload)
            return digest

        with mock.patch.object(verification, "_hash_descriptor", side_effect=replaced):
            with self.assertRaisesRegex(ArtifactError, "changed identity"):
                verification.verify_local_artifact(self.entry, self.root)

    def _run(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["artifact", "verify", "--manifest", str(self.manifest),
                           "--destination-root", str(self.root),
                           "--artifact", "models/one.gguf", *arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_json_success_has_no_eviction_authority(self):
        status, out, err = self._run("--json")
        self.assertEqual(status, 0, err)
        report = json.loads(out)
        self.assertEqual(report["path"], str(self.target))
        self.assertEqual(report["sha256"], self.entry.sha256)
        self.assertTrue(report["verified"])
        self.assertFalse(report["deletion_eligible"])
        self.assertIsNone(report["reclaimable_bytes"])

    def test_cli_json_failure_has_no_eviction_authority_or_partial_success(self):
        self.target.write_bytes(b"corrupt")
        status, out, err = self._run("--json")
        self.assertEqual(status, 1)
        report = json.loads(out)
        self.assertFalse(report["verified"])
        self.assertFalse(report["deletion_eligible"])
        self.assertIsNone(report["reclaimable_bytes"])
        self.assertIn("error", report)

    def test_cli_invalid_manifest_reports_failure_as_json(self):
        self.manifest.write_text("not json", encoding="utf-8")
        status, out, err = self._run("--json")
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(out)["verified"])


if __name__ == "__main__":
    unittest.main()
