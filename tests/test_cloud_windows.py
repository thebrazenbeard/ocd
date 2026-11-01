"""Native Windows content-addressed cache must fail closed until qualified.

The POSIX cache tests exercise anchored fd publication, which Windows cannot
provide. Native Windows tests assert that requests do not weaken that boundary
or accidentally create or replace a model file.
"""

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.cloud import BlobRef, CloudStoreError, LocalContentStore  # noqa: E402


@unittest.skipUnless(os.name == "nt", "requires native Windows")
class WindowsContentStoreFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cache_root = self.base / "cache"
        self.cache_root.mkdir()
        self.job_root = self.base / "job"
        self.job_root.mkdir()
        self.source = self.base / "weight.gguf"
        self.source.write_bytes(b"fixture model bytes")
        self.blob = BlobRef(hashlib.sha256(self.source.read_bytes()).hexdigest(),
                            self.source.stat().st_size)
        self.store = LocalContentStore(self.cache_root)

    def test_import_refuses_windows_without_modifying_source_or_cache(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(CloudStoreError, "unavailable on this platform"):
            self.store.import_file(self.source, self.blob)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_materialization_refuses_windows_without_modifying_destination(self):
        existing = self.job_root / "weight.gguf"
        existing.write_bytes(b"preexisting user bytes")
        with self.assertRaisesRegex(CloudStoreError, "unavailable on this platform"):
            self.store.materialize(self.blob, self.job_root, "weight.gguf")
        self.assertEqual(existing.read_bytes(), b"preexisting user bytes")
        self.assertEqual(sorted(self.job_root.iterdir()), [existing])


if __name__ == "__main__":
    unittest.main()
