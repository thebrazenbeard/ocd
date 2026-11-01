"""End-to-end tests for explicit, non-destructive cache CLI operations."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.cli import main


class CloudCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.config = self.base / "uncreated.json"
        self.source = self.base / "model.gguf"
        self.payload = b"on-demand training artifact " * 1024
        self.source.write_bytes(self.payload)
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.store = self.base / "content-store"
        self.job = self.base / "job"
        self.store.mkdir()
        self.job.mkdir()

    def invoke(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--config", str(self.config), *args])
        return result, stdout.getvalue(), stderr.getvalue()

    def _import_args(self, sha=None, size=None):
        return (
            "cache", "import", "--store-root", str(self.store),
            "--source", str(self.source), "--sha256", sha or self.digest,
            "--size", str(len(self.payload) if size is None else size),
        )

    def _stage_args(self, relpath="bt2/role.gguf"):
        return (
            "cache", "materialize", "--store-root", str(self.store),
            "--sha256", self.digest, "--size", str(len(self.payload)),
            "--destination-root", str(self.job), "--destination", relpath,
        )

    @unittest.skipUnless(os.name == "posix", "Linux content store primitives are required")
    def test_import_and_materialize_explicit_verified_bytes_no_delete(self):
        status, out, err = self.invoke(*self._import_args())
        self.assertEqual(status, 0, err)
        blob = self.store / "sha256" / self.digest[:2] / self.digest
        self.assertEqual(blob.read_bytes(), self.payload)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertIn("source preserved", out)
        self.assertFalse(self.config.exists())

        status, out, err = self.invoke(*self._stage_args())
        self.assertEqual(status, 0, err)
        self.assertIn("no overwrite", out)
        self.assertEqual((self.job / "bt2" / "role.gguf").read_bytes(), self.payload)
        self.assertEqual(blob.read_bytes(), self.payload)
        self.assertFalse(self.config.exists())

    @unittest.skipUnless(os.name == "posix", "Linux content store primitives are required")
    def test_wrong_expected_digest_or_size_leaves_no_blob(self):
        for sha, size in (("0" * 64, None), (self.digest, len(self.payload) + 1)):
            with self.subTest(sha=sha, size=size):
                status, out, err = self.invoke(*self._import_args(sha=sha, size=size))
                self.assertEqual(status, 1, (out, err))
                self.assertIn("failed", err)
                self.assertFalse((self.store / "sha256" / sha[:2] / sha).exists())
                self.assertEqual(self.source.read_bytes(), self.payload)

    @unittest.skipUnless(os.name == "posix", "Linux content store primitives are required")
    def test_existing_materialization_is_never_overwritten(self):
        self.assertEqual(self.invoke(*self._import_args())[0], 0)
        target = self.job / "bt2" / "role.gguf"
        target.parent.mkdir()
        target.write_bytes(b"precious user data")
        status, _, error = self.invoke(*self._stage_args())
        self.assertEqual(status, 1)
        self.assertIn("already exists", error)
        self.assertEqual(target.read_bytes(), b"precious user data")

    @unittest.skipUnless(os.name == "posix", "Linux content store primitives are required")
    def test_nonexistent_cache_blob_does_not_publish_file(self):
        status, _, error = self.invoke(*self._stage_args())
        self.assertEqual(status, 1)
        self.assertIn("failed", error)
        self.assertFalse((self.job / "bt2" / "role.gguf").exists())

    def test_invalid_user_identity_is_configuration_error_without_io(self):
        with patch("ocd.cloud.LocalContentStore", side_effect=AssertionError("IO invoked")):
            status, _, error = self.invoke(*self._import_args(sha="bad"))
        self.assertEqual(status, 2)
        self.assertIn("64 hexadecimal", error)
        with patch("ocd.cloud.LocalContentStore", side_effect=AssertionError("IO invoked")):
            status, _, error = self.invoke(*self._import_args(size=-1))
        self.assertEqual(status, 2)
        self.assertIn("nonnegative", error)
        self.assertFalse(self.config.exists())

    def test_cache_errors_cannot_trigger_organizer_apply(self):
        from ocd.cloud import CloudStoreError

        with patch("ocd.cloud.LocalContentStore", side_effect=CloudStoreError("unsupported host")):
            with patch("ocd.apply.apply_moves", side_effect=AssertionError("organizer apply invoked")):
                status, _, err = self.invoke(*self._import_args())
        self.assertEqual(status, 1)
        self.assertIn("unsupported host", err)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
