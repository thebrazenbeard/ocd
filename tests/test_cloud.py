"""On-demand local/cloud-boundary tests with no outbound network requests."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import artifacts, cloud  # noqa: E402


PAYLOAD = (b"immutable, recoverable model artifact bytes\x00" * 90000) + b"end"


class ContentAddressedStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "blob-store"
        self.root.mkdir()
        self.destination = self.base / "job-cache"
        self.destination.mkdir()
        self.source = self.base / "model.gguf"
        self.source.write_bytes(PAYLOAD)
        self.ref = cloud.BlobRef(hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD))
        self.store = cloud.LocalContentStore(self.root)

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_verified_content_addressed_import_and_explicit_materialization(self):
        cached = self.store.import_file(self.source, self.ref)
        self.assertEqual(cached, self.root / self.ref.key)
        self.assertEqual(cached.read_bytes(), PAYLOAD)
        self.assertEqual(self.source.read_bytes(), PAYLOAD)  # No source cleanup.

        requested = self.store.materialize(self.ref, self.destination, "models/one.gguf")
        self.assertEqual(requested, self.destination / "models/one.gguf")
        self.assertEqual(requested.read_bytes(), PAYLOAD)
        self.assertEqual(self.store.import_file(self.source, self.ref), cached)
        self.assertFalse(list(self.root.rglob(".ocd-*")))
        self.assertFalse(list(self.destination.rglob(".ocd-*")))

    def test_invalid_identity_and_source_corruption_never_publish(self):
        for digest, size in [("not-a-sha", len(PAYLOAD)), ("0" * 64, len(PAYLOAD)),
                             (self.ref.sha256, len(PAYLOAD) + 1)]:
            with self.subTest(digest=digest, size=size):
                if digest == "not-a-sha":
                    with self.assertRaises(cloud.CloudStoreError):
                        cloud.BlobRef(digest, size)
                    continue
                ref = cloud.BlobRef(digest, size)
                with self.assertRaises(cloud.CloudStoreError):
                    self.store.import_file(self.source, ref)
                self.assertFalse((self.root / ref.key).exists())
        for size in (-1, True, 1.5):
            with self.assertRaises(cloud.CloudStoreError):
                cloud.BlobRef("0" * 64, size)

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_existing_destination_is_not_overwritten(self):
        self.store.import_file(self.source, self.ref)
        existing = self.destination / "models/one.gguf"
        existing.parent.mkdir()
        existing.write_bytes(b"precious existing data")
        with self.assertRaisesRegex(cloud.CloudStoreError, "already exists"):
            self.store.materialize(self.ref, self.destination, "models/one.gguf")
        self.assertEqual(existing.read_bytes(), b"precious existing data")

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_existing_content_addressed_blob_must_still_match_digest(self):
        cache_path = self.store.import_file(self.source, self.ref)
        cache_path.write_bytes(b"modified by another application")
        with self.assertRaisesRegex(cloud.CloudStoreError, "shorter"):
            self.store.import_file(self.source, self.ref)
        with self.assertRaises(cloud.CloudStoreError):
            self.store.materialize(self.ref, self.destination, "not-published.gguf")
        self.assertFalse((self.destination / "not-published.gguf").exists())

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_reimport_refuses_changed_source_even_when_correct_blob_is_cached(self):
        cache_path = self.store.import_file(self.source, self.ref)
        self.source.write_bytes(b"altered source")
        with self.assertRaises(cloud.CloudStoreError):
            self.store.import_file(self.source, self.ref)
        self.assertEqual(cache_path.read_bytes(), PAYLOAD)

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_refuses_unsafe_destination_and_symlink_escape(self):
        self.store.import_file(self.source, self.ref)
        for relpath in ("../secret", "/root/secret", "nested/../secret", "CON.txt", "C:unsafe", "x\\y"):
            with self.subTest(relpath=relpath):
                with self.assertRaises(cloud.CloudStoreError):
                    self.store.materialize(self.ref, self.destination, relpath)
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.destination / "shortcut").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink support unavailable")
        with self.assertRaises(cloud.CloudStoreError):
            self.store.materialize(self.ref, self.destination, "shortcut/escaped.gguf")
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_refuses_symlink_source_and_symlink_existing_blob(self):
        symlink = self.base / "model-link.gguf"
        try:
            symlink.symlink_to(self.source)
        except (OSError, NotImplementedError):
            self.skipTest("symlink support unavailable")
        with self.assertRaises(cloud.CloudStoreError):
            self.store.import_file(symlink, self.ref)
        self.assertFalse((self.root / self.ref.key).exists())

        self.store.import_file(self.source, self.ref)
        cache_path = self.root / self.ref.key
        cache_path.unlink()
        cache_path.symlink_to(self.source)
        with self.assertRaises(cloud.CloudStoreError):
            self.store.import_file(self.source, self.ref)

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_publication_collision_is_no_replace(self):
        self.store.import_file(self.source, self.ref)
        real_publish = artifacts._publish_noreplace

        def racing_publish(stage_fd, parent_fd, filename):
            (self.destination / "late.gguf").write_bytes(b"racing writer")
            return real_publish(stage_fd, parent_fd, filename)

        with mock.patch.object(artifacts, "_publish_noreplace", side_effect=racing_publish):
            with self.assertRaisesRegex(cloud.CloudStoreError, "already exists"):
                self.store.materialize(self.ref, self.destination, "late.gguf")
        self.assertEqual((self.destination / "late.gguf").read_bytes(), b"racing writer")

    @unittest.skipUnless(os.name == "posix", "anchored cache publication requires POSIX primitives")
    def test_source_and_destination_roots_must_be_explicit_and_absolute(self):
        with self.assertRaises(cloud.CloudStoreError):
            cloud.LocalContentStore("somewhere/relative")
        with self.assertRaises(cloud.CloudStoreError):
            self.store.import_file("relative.gguf", self.ref)
        self.store.import_file(self.source, self.ref)
        with self.assertRaises(cloud.CloudStoreError):
            self.store.materialize(self.ref, "relative/destination", "model.gguf")


class HTTPSManifestProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.manifest = self.base / "download-manifest.json"
        self.entry = artifacts.ArtifactEntry(
            "https://github.com/example/models/releases/download/verified/role.gguf",
            "a" * 64, 12, "bt2/role.gguf",
        )
        self.manifest.write_text(json.dumps({"artifacts": [vars(self.entry)]}), encoding="utf-8")

    def test_provider_is_lazy_and_downloads_only_a_requested_manifest_entry(self):
        with mock.patch.object(artifacts, "fetch_verified") as fetch:
            provider = cloud.HTTPSManifestProvider(self.manifest)
            fetch.assert_not_called()
            target = self.base / "job-cache"
            fetch.return_value = target / "bt2/role.gguf"
            self.assertEqual(provider.fetch("bt2/role.gguf", target), target / "bt2/role.gguf")
            fetch.assert_called_once_with(self.entry, target, token_env="GITHUB_TOKEN", timeout=30.0)

            with self.assertRaisesRegex(cloud.CloudStoreError, "not present"):
                provider.fetch("bt2/unrelated.gguf", target)
            fetch.assert_called_once()

    def test_manifest_rejects_duplicate_names_and_unsafe_urls(self):
        self.manifest.write_text(json.dumps([vars(self.entry), vars(self.entry)]), encoding="utf-8")
        with self.assertRaises(artifacts.ArtifactError):
            cloud.HTTPSManifestProvider(self.manifest)
        self.manifest.write_text(json.dumps([
            dict(vars(self.entry), url="http://files.example.com/role.gguf")
        ]), encoding="utf-8")
        with self.assertRaises(artifacts.ArtifactError):
            cloud.HTTPSManifestProvider(self.manifest)


if __name__ == "__main__":
    unittest.main()
