"""Safety tests for explicit, verified artifact fetching."""

import hashlib
import io
import json
import os
from pathlib import Path
import errno
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error, parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import artifacts  # noqa: E402


PAYLOAD = b"a signed, immutable artifact\x00" * 3200


class ArtifactHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.observed.append((self.path, self.headers.get("Authorization")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/payload")
            self.end_headers()
            return
        if self.path == "/short":
            body = PAYLOAD[:500]
        elif self.path == "/extra":
            body = PAYLOAD + b"injected extra bytes"
        elif self.path == "/empty":
            body = b""
        elif self.path == "/missing":
            self.send_error(404)
            return
        else:
            body = PAYLOAD
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class ArtifactFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "artifacts"
        self.root.mkdir()
        self.url = "http://127.0.0.1:8765"
        self.observed = []
        self.actual_build_opener = artifacts.request.build_opener

        # Default tests do not bind a socket, so they run in restricted
        # sandboxes as well as in CI.  One separate test uses a real server.
        class FakeOpener:
            def open(_, req, timeout):
                self.observed.append((req.full_url, req.get_header("Authorization")))
                route = parse.urlsplit(req.full_url).path
                if route == "/redirect":
                    raise error.HTTPError(
                        req.full_url, 302, "Moved", {"Location": "/payload"}, None,
                    )
                if route == "/missing":
                    raise error.HTTPError(req.full_url, 404, "Missing", {}, None)
                if route == "/short":
                    body = PAYLOAD[:500]
                elif route == "/extra":
                    body = PAYLOAD + b"injected extra bytes"
                elif route == "/empty":
                    body = b""
                else:
                    body = PAYLOAD
                response = io.BytesIO(body)
                response.headers = {}
                return response

        opener_patch = mock.patch.object(
            artifacts.request, "build_opener", side_effect=lambda *args: FakeOpener(),
        )
        opener_patch.start()
        self.addCleanup(opener_patch.stop)

    def entry(self, path="bundle/archive.tar", url="/payload", payload=PAYLOAD):
        return artifacts.ArtifactEntry(
            self.url + url,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            path,
        )

    def assert_no_staging_files(self):
        self.assertEqual(list(self.root.rglob(".ocd-download-*.part")), [])

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture; Windows native fixture tests are separate and gated")
    def test_download_is_verified_and_published_only_once(self):
        entry = self.entry()
        path = artifacts.fetch_verified(entry, self.root)
        self.assertEqual(path, self.root / "bundle/archive.tar")
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertEqual(len(self.observed), 1)
        self.assert_no_staging_files()

        with self.assertRaisesRegex(artifacts.ArtifactError, "already exists"):
            artifacts.fetch_verified(entry, self.root)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertEqual(len(self.observed), 1)  # Refused before a new GET.

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture; Windows native fixture tests are separate and gated")
    def test_empty_file_and_loopback_redirect(self):
        zero = self.entry("empty.dat", url="/empty", payload=b"")
        self.assertEqual(artifacts.fetch_verified(zero, self.root).read_bytes(), b"")
        redirected = self.entry("redirected.bin", url="/redirect")
        self.assertEqual(artifacts.fetch_verified(redirected, self.root).read_bytes(), PAYLOAD)
        self.assert_no_staging_files()

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture; Windows native fixture tests are separate and gated")
    def test_short_oversized_corrupt_and_missing_response_never_publish(self):
        cases = [
            self.entry("short.bin", url="/short"),
            self.entry("extra.bin", url="/extra"),
            self.entry("wrong-hash.bin", payload=b"different" + PAYLOAD[9:]),
            self.entry("missing.bin", url="/missing"),
        ]
        for entry in cases:
            with self.subTest(entry=entry.destination_relpath):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.fetch_verified(entry, self.root)
                self.assertFalse((self.root / entry.destination_relpath).exists())
                self.assert_no_staging_files()

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture; Windows native fixture tests are separate and gated")
    def test_existing_file_and_dangling_symlink_are_not_overwritten(self):
        target = self.root / "bundle/archive.tar"
        target.parent.mkdir()
        target.write_bytes(b"important existing data")
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.fetch_verified(self.entry(), self.root)
        self.assertEqual(target.read_bytes(), b"important existing data")

        link = self.root / "dangling"
        try:
            link.symlink_to(self.base / "does-not-exist")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.fetch_verified(self.entry("dangling"), self.root)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.observed, [])

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture; Windows native fixture tests are separate and gated")
    def test_symlink_directory_and_root_ancestors_are_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.root / "shortcut").symlink_to(outside, target_is_directory=True)
            (self.base / "root-link").symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        for entry, root in [
            (self.entry("shortcut/escape.bin"), self.root),
            (self.entry("archive.bin"), self.base / "root-link"),
        ]:
            with self.subTest(root=root, entry=entry):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.fetch_verified(entry, root)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.observed, [])

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture uses Linux _publish_noreplace")
    def test_destination_collision_at_publication_does_not_overwrite(self):
        entry = self.entry("race.bin")
        real_publish = artifacts._publish_noreplace

        def collision(stage_fd, parent_fd, target):
            (self.root / target).write_bytes(b"written concurrently")
            return real_publish(stage_fd, parent_fd, target)

        with mock.patch.object(artifacts, "_publish_noreplace", side_effect=collision):
            with self.assertRaisesRegex(artifacts.ArtifactError, "already exists"):
                artifacts.fetch_verified(entry, self.root)
        self.assertEqual((self.root / "race.bin").read_bytes(), b"written concurrently")
        self.assert_no_staging_files()

    @unittest.skipUnless(sys.platform.startswith("linux"), "anchored Linux directory fds")
    def test_parent_symlink_swap_at_publication_cannot_write_outside_root(self):
        entry = self.entry("nested/file.bin")
        outside = self.base / "outside"
        outside.mkdir()
        displaced = self.base / "displaced"
        real_publish = artifacts._publish_noreplace

        def swap(stage_fd, parent_fd, target):
            (self.root / "nested").rename(displaced)
            (self.root / "nested").symlink_to(outside, target_is_directory=True)
            return real_publish(stage_fd, parent_fd, target)

        with mock.patch.object(artifacts, "_publish_noreplace", side_effect=swap):
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.fetch_verified(entry, self.root)
        self.assertFalse((outside / "file.bin").exists())
        self.assertEqual((displaced / "file.bin").read_bytes(), PAYLOAD)

    def test_bad_destination_paths_and_urls_fail_before_network(self):
        paths = [
            "../escape", "a/../escape", "a/./file", "a//file", "/absolute",
            "C:\\windows", "dir\\windows", "C:alternate", "CON", "dir/PRN.txt",
            "dir/trailing.", "line\nfeed", "", ".", "..",
        ]
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.fetch_verified(self.entry(path), self.root)
        for url in [
            "http://example.com/data", "file:///etc/passwd", "ftp://example.com/a",
            "https://user:password@example.com/data", "https://example.com/data#frag",
            "http://127.0.0.1:0/a", "https://example.com/a?access_token=secret",
        ]:
            with self.subTest(url=url):
                bad = artifacts.ArtifactEntry(url, hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD), "safe.bin")
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.fetch_verified(bad, self.root)
        self.assertEqual(self.observed, [])

    def test_manifest_is_strict_and_load_has_no_network_side_effect(self):
        path = self.base / "manifest.json"
        item = vars(self.entry())
        path.write_text(json.dumps({"artifacts": [item]}), encoding="utf-8")
        self.assertEqual(artifacts.load_manifest(path), (self.entry(),))
        self.assertEqual(self.observed, [])

        invalid = [
            {"artifacts": [dict(item, size=-1)]},
            {"artifacts": [dict(item, size=True)]},
            {"artifacts": [dict(item, sha256="00")]},
            {"artifacts": [dict(item, destination_relpath="../escape")]},
            {"artifacts": [dict(item, surprise=True)]},
            {"artifacts": [item, item]},
            {"unrecognized": []},
        ]
        for value in invalid:
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts.load_manifest(path)
        path.write_text('{"artifacts": [], "artifacts": []}', encoding="utf-8")
        with self.assertRaisesRegex(artifacts.ArtifactError, "duplicate JSON key"):
            artifacts.load_manifest(path)

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture; Windows native fixture tests are separate and gated")
    def test_github_token_is_environment_only_and_not_sent_to_other_hosts(self):
        token_name = "OCD_TEST_RELEASE_ASSET_TOKEN"
        with mock.patch.dict(os.environ, {token_name: "secret-value"}):
            # Local HTTP destinations are never trusted with a GitHub token.
            artifacts.fetch_verified(self.entry("local.bin"), self.root, token_env=token_name)
            self.assertEqual(self.observed[-1][1], None)

            requests = []

            class FakeOpener:
                def open(self, req, timeout):
                    requests.append(req)
                    if len(requests) == 1:
                        raise error.HTTPError(
                            req.full_url, 302, "Moved",
                            {"Location": "https://release-assets.githubusercontent.com/asset?X-Amz-Signature=short-lived"},
                            None,
                        )
                    result = io.BytesIO(PAYLOAD)
                    result.headers = {}
                    return result

            github_entry = artifacts.ArtifactEntry(
                "https://api.github.com/repos/owner/repo/releases/assets/12345",
                hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD), "private.bin",
            )
            with mock.patch.object(artifacts.request, "build_opener", return_value=FakeOpener()):
                artifacts.fetch_verified(github_entry, self.root, token_env=token_name)
            self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret-value")
            self.assertIsNone(requests[1].get_header("Authorization"))
            self.assertEqual((self.root / "private.bin").read_bytes(), PAYLOAD)
            self.assert_no_staging_files()

    @unittest.skipIf(os.name == "nt", "POSIX publication fixture; Windows native fixture tests are separate and gated")
    def test_real_loopback_http_server_when_network_is_available(self):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), ArtifactHTTPHandler)
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                self.skipTest("local HTTP bind prohibited by sandbox")
            raise
        server.observed = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            actual_url = f"http://127.0.0.1:{server.server_port}/redirect"
            entry = artifacts.ArtifactEntry(
                actual_url, hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD), "http.bin",
            )
            with mock.patch.object(artifacts.request, "build_opener", self.actual_build_opener):
                path = artifacts.fetch_verified(entry, self.root)
            self.assertEqual(path.read_bytes(), PAYLOAD)
            self.assertEqual([row[0] for row in server.observed], ["/redirect", "/payload"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
