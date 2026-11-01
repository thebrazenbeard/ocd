"""Windows artifact publication contract and native disposable-fixture probes.

Native tests intentionally skip on non-Windows hosts; no real user files are
read, moved, or deleted.  These tests do not establish a complete adversarial
qualification of every supported Windows/filesystem combination.
"""

from __future__ import annotations

import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest import mock

import ocd.artifacts as artifacts
from ocd.artifacts import (
    ArtifactEntry,
    ArtifactError,
    _validate_relpath,
    _windows_mark_delete,
    fetch_verified,
)


class PortableNameTests(unittest.TestCase):
    def test_windows_publication_is_disabled_before_any_network_or_filesystem_io(self):
        entry = ArtifactEntry(
            "https://example.com/model.gguf", "a" * 64, 1024, "models/model.gguf"
        )
        with (
            mock.patch.object(artifacts.os, "name", "nt"),
            mock.patch.dict(os.environ, {"OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH": "0"}),
            mock.patch.object(artifacts, "_open_download") as download,
            mock.patch.object(artifacts, "_fetch_verified_windows") as publish,
            mock.patch.object(artifacts, "Path") as construct_path,
        ):
            with self.assertRaisesRegex(ArtifactError, "unqualified and disabled"):
                fetch_verified(entry, r"C:\Temp\OCD-Fixture")
            download.assert_not_called()
            publish.assert_not_called()
            construct_path.assert_not_called()

    def test_windows_qualification_requires_exact_opt_in_value(self):
        entry = ArtifactEntry(
            "https://example.com/model.gguf", "b" * 64, 1024, "models/model.gguf"
        )
        with (
            mock.patch.object(artifacts.os, "name", "nt"),
            mock.patch.dict(os.environ, {"OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH": "1"}),
            mock.patch.object(artifacts, "Path", side_effect=lambda path: ("wrapped", path)),
            mock.patch.object(artifacts, "_fetch_verified_windows", return_value="qualified-fixture") as publish,
        ):
            self.assertEqual(fetch_verified(entry, r"C:\Temp\OCD-Fixture"), "qualified-fixture")
            publish.assert_called_once_with(
                entry, ("wrapped", r"C:\Temp\OCD-Fixture"),
                token_env="GITHUB_TOKEN", timeout=30.0,
            )

    def test_rejects_win32_device_and_ambiguous_names(self):
        bad = (
            "CONIN$.txt", "CONOUT$.json", "CLOCK$.txt", "COM¹.bin", "LPT².bin",
            "file<.txt", "file>.txt", "file?.txt", "file*.txt", "file|.txt",
            "x/unsafe\u202Ename", "bad\ud800name", "x/..", "x/one ",
        )
        for name in bad:
            with self.subTest(name=repr(name)), self.assertRaises(ArtifactError):
                _validate_relpath(name)

    def test_valid_unicode_and_nested_names(self):
        for name in ("models/Qwen2.5.gguf", "文档/ä-ß.txt", "a/.cache/model.bin"):
            with self.subTest(name=name):
                self.assertEqual(_validate_relpath(name), name)

    def test_failure_disposition_targets_open_file_handle(self):
        calls = []

        class Dummy:
            def _set(self, handle, kind, ptr, size):
                calls.append((handle, kind, ctypes.string_at(ptr, size)))
                return True

        _windows_mark_delete(1234, Dummy())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (1234, 4, b"\x01"))


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("OCD_NATIVE_WINDOWS_QUALIFICATION") == "1",
    "native Windows artifact publication fixtures require Windows and explicit "
    "OCD_NATIVE_WINDOWS_QUALIFICATION=1 (manual qualification workflow)",
)
class NativeWindowsArtifactTests(unittest.TestCase):
    def setUp(self):
        qualification = mock.patch.dict(
            os.environ, {"OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH": "1"}
        )
        qualification.start()
        self.addCleanup(qualification.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="ocd-artifact-win32-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        payload = b"win32-verified-artifact\n" * 4096
        self.payload = payload
        self.digest = hashlib.sha256(payload).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            self.skipTest(f"loopback socket unavailable: {exc}")
        self.addCleanup(self.server.server_close)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/fixture"

    def _entry(self, relpath="models/fixture.gguf", sha=None):
        return ArtifactEntry(self.url, sha or self.digest, len(self.payload), relpath)

    def test_publishes_verified_new_file_atomically(self):
        published = fetch_verified(self._entry(), self.root)
        self.assertEqual(published.read_bytes(), self.payload)
        self.assertEqual(list(self.root.rglob("*.stage")), [])
        with self.assertRaisesRegex(ArtifactError, "already exists"):
            fetch_verified(self._entry(), self.root)
        self.assertEqual(published.read_bytes(), self.payload)

    def test_bad_digest_leaves_no_published_or_staging_file(self):
        with self.assertRaisesRegex(ArtifactError, "SHA-256"):
            fetch_verified(self._entry("bad/data.bin", "0" * 64), self.root)
        self.assertFalse((self.root / "bad" / "data.bin").exists())
        self.assertEqual(list(self.root.rglob("*.stage")), [])


if __name__ == "__main__":
    unittest.main()
