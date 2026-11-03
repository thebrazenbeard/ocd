"""Offline treemap report contracts: no scan writes or unsafe publication."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import base64
import hashlib
from io import StringIO
import json
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ocd.cli import main
from ocd.treemap import _MAX_INTERACTIVE_DIRECTORIES, render_usage_html, write_usage_html
from ocd.usage import DirectoryUsage, UsageItem, UsageReport, scan_usage


class TreemapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocd-html-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "observed"
        self.out = self.base / "reports"
        self.root.mkdir()
        self.out.mkdir()

    def test_html_escapes_untrusted_labels_and_does_not_run_scripts(self):
        report = UsageReport(
            root=self.root / "<svg onload=alert(1)>", entries_visited=1,
            files=1, directories=0, skipped_links=0, skipped_special=0,
            logical_bytes=42, allocated_bytes=None,
            largest_files=(UsageItem(self.root / "<img src=x onerror=alert(1)>", 42, None),),
            largest_children=(UsageItem(self.root / "<script>alert(1)</script>", 42, None),),
            issues=("<script>bad</script>",), complete=False,
        )
        html = render_usage_html(report)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<iframe", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn("INCOMPLETE", html)
        self.assertIn("not", html.lower())

    def test_cli_creates_only_explicit_new_report_and_refuses_overwrite(self):
        (self.root / "a&b.bin").write_bytes(b"x" * 27)
        target = self.out / "snapshot.html"
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["usage", "--path", str(self.root), "--html", str(target)])
        self.assertEqual(status, 0, err.getvalue())
        payload = target.read_text(encoding="utf-8")
        self.assertIn("27 bytes", payload)
        self.assertIn("a&amp;b.bin", payload)
        self.assertIn("COMPLETE", payload)
        self.assertIn("Nothing is eligible for deletion", payload)
        self.assertEqual((self.root / "a&b.bin").read_bytes(), b"x" * 27)
        original = target.read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            second = main(["usage", "--path", str(self.root), "--html", str(target)])
        self.assertNotEqual(second, 0)
        self.assertEqual(target.read_bytes(), original)

    def test_html_cli_requests_recursive_directory_data_only_for_html(self):
        folder = self.root / "nested"
        folder.mkdir()
        (folder / "f.bin").write_bytes(b"abc")
        target = self.out / "drilldown.html"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(["usage", "--path", str(self.root), "--html", str(target)]), 0)
        html = target.read_text(encoding="utf-8")
        self.assertIn('id="ocd-directory-drilldown" hidden', html)
        self.assertIn('id="ocd-static-treemap"', html)
        match = re.search(r'<script id="ocd-directory-data" type="application/json">([^<]*)</script>', html)
        self.assertIsNotNone(match)
        model = json.loads(match.group(1))
        self.assertEqual([item["name"] for item in model["nodes"]][1:], ["nested"])
        self.assertEqual(model["nodes"][0]["size"], "3")
        self.assertEqual(model["nodes"][1]["size"], "3")
        self.assertEqual(model["nodes"][0]["direct"], "0")
        self.assertFalse(model["truncated"])
        self.assertTrue(model["complete"])

    def test_script_hash_exact_and_fails_closed_without_network_or_unsafe_script(self):
        html = render_usage_html(scan_usage(self.root, include_hierarchy=True))
        scripts = re.findall(r'<script(?: [^>]*)?>(.*?)</script>', html, flags=re.S)
        self.assertEqual(len(scripts), 2)
        digest = base64.b64encode(hashlib.sha256(scripts[1].encode("utf-8")).digest()).decode("ascii")
        self.assertIn("script-src 'sha256-" + digest + "'", html)
        self.assertNotIn("script-src 'unsafe-inline'", html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn("object-src 'none'", html)
        self.assertNotIn("fetch(", scripts[1])
        self.assertNotIn("XMLHttpRequest", scripts[1])
        self.assertNotIn("innerHTML", scripts[1])
        self.assertNotIn("src=\"http", html)
        self.assertIn('id="ocd-static-treemap"', html, "static fallback must remain present")
        if shutil.which("node"):
            checked = subprocess.run(["node", "--check"], input=scripts[1],
                                     encoding="utf-8", capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_inert_model_escapes_html_terminators_and_odd_unicode(self):
        fake_root = Path(str(self.root) + "</script>")
        nasty = fake_root / "<svg onload=1>\u2028\u2029&\udcff"
        report = UsageReport(
            root=fake_root, entries_visited=1, files=0, directories=1,
            skipped_links=0, skipped_special=0, logical_bytes=0,
            allocated_bytes=None, largest_files=(), largest_children=(),
            issues=(), complete=True,
            directory_totals=(DirectoryUsage(fake_root, 0, None, True),
                              DirectoryUsage(nasty, 0, None, True)),
        )
        html = render_usage_html(report)
        data = re.search(r'<script id="ocd-directory-data" type="application/json">([^<]*)</script>', html)
        self.assertIsNotNone(data, "untrusted JSON cannot terminate a script element")
        self.assertNotIn("</script><svg", data.group(1))
        self.assertIn(r"\u003c", data.group(1))
        self.assertIn(r"\u0026", data.group(1))
        self.assertNotIn("\udcff", html)
        model = json.loads(data.group(1))
        self.assertIn("</script>", model["root"])
        self.assertIn("<svg", model["nodes"][1]["name"])
        self.assertIn("\u2028", model["nodes"][1]["name"])

    def test_bounded_directory_payload_discloses_truncation_and_partial_scan(self):
        entries = (DirectoryUsage(self.root, 0, None, False),) + tuple(
            DirectoryUsage(self.root / f"folder-{index:05d}", 0, None, False)
            for index in range(_MAX_INTERACTIVE_DIRECTORIES + 25)
        )
        report = UsageReport(
            root=self.root, entries_visited=1, files=0, directories=len(entries)-1,
            skipped_links=0, skipped_special=0, logical_bytes=0,
            allocated_bytes=None, largest_files=(), largest_children=(),
            issues=("entry budget reached",), complete=False, directory_totals=entries,
        )
        html = render_usage_html(report)
        data = re.search(r'<script id="ocd-directory-data" type="application/json">([^<]*)</script>', html)
        self.assertIsNotNone(data)
        model = json.loads(data.group(1))
        self.assertEqual(len(model["nodes"]), _MAX_INTERACTIVE_DIRECTORIES)
        self.assertTrue(model["truncated"])
        self.assertFalse(model["complete"])
        self.assertIn("INCOMPLETE", html)

    def test_large_logical_bytes_preserve_exact_integer_labels(self):
        huge = 9_007_199_254_740_993  # JS Number cannot represent this exactly.
        report = UsageReport(
            root=self.root, entries_visited=1, files=1, directories=0,
            skipped_links=0, skipped_special=0, logical_bytes=huge,
            allocated_bytes=None, largest_files=(), largest_children=(),
            issues=(), complete=True,
            directory_totals=(DirectoryUsage(self.root, huge, None, True),),
        )
        html = render_usage_html(report)
        data = re.search(r'<script id="ocd-directory-data" type="application/json">([^<]*)</script>', html)
        self.assertIsNotNone(data)
        model = json.loads(data.group(1))
        self.assertEqual(model["nodes"][0]["size"], str(huge))
        self.assertEqual(model["nodes"][0]["direct"], str(huge))
        self.assertIn("BigInt", html)

    def test_incomplete_bounded_scan_stays_visibly_incomplete(self):
        (self.root / "a.bin").write_bytes(b"a")
        (self.root / "b.bin").write_bytes(b"b")
        target = self.out / "partial.html"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["usage", "--path", str(self.root), "--max-entries", "1",
                         "--html", str(target)])
        self.assertEqual(code, 1)
        html = target.read_text(encoding="utf-8")
        self.assertIn("INCOMPLETE", html)
        self.assertIn("entry budget", html)
        self.assertIn("not", html.lower())
        self.assertIn("safe to reclaim", html)

    def test_rejects_output_inside_scan_tree(self):
        (self.root / "file.bin").write_bytes(b"123")
        report = scan_usage(self.root)
        target = self.root / "report.html"
        with self.assertRaises(ValueError):
            write_usage_html(report, target)
        self.assertFalse(target.exists())

    def test_refuses_relative_missing_parent_and_existing_target(self):
        report = scan_usage(self.root)
        with self.assertRaises(ValueError):
            write_usage_html(report, Path("relative.html"))
        with self.assertRaises(ValueError):
            write_usage_html(report, self.base / "missing" / "report.html")
        target = self.out / "report.html"
        target.write_bytes(b"user existing content")
        with self.assertRaises(FileExistsError):
            write_usage_html(report, target)
        self.assertEqual(target.read_bytes(), b"user existing content")

    def test_parent_symlink_refused(self):
        linked = self.base / "linked"
        try:
            linked.symlink_to(self.out, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        report = scan_usage(self.root)
        with self.assertRaises(ValueError):
            write_usage_html(report, linked / "report.html")
        self.assertFalse((self.out / "report.html").exists())

    def test_html_reports_logical_bytes_without_reclaimability_promise(self):
        (self.root / "file.bin").write_bytes(b"x" * 64)
        html = render_usage_html(scan_usage(self.root))
        self.assertIn("logical bytes", html)
        self.assertIn("not", html.lower())
        self.assertIn("eligible", html)
        self.assertNotIn("delete-button", html)
        self.assertNotIn("fetch(", html)

    def test_failed_write_never_unlinks_output_by_path(self):
        report = scan_usage(self.root)
        target = self.out / "partial.html"
        with (
            patch("ocd.treemap.os.fsync", side_effect=OSError("injected write failure")),
            patch.object(Path, "unlink", side_effect=AssertionError("unowned unlink")) as unlink,
        ):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                write_usage_html(report, target)
            unlink.assert_not_called()
        self.assertTrue(target.is_file(), "partial report must remain for explicit review")

    @unittest.skipIf(os.name == "nt", "POSIX rename while an output handle is open")
    def test_swapped_output_survives_write_failure(self):
        report = scan_usage(self.root)
        target = self.out / "report.html"
        other = self.out / "unrelated"
        other.write_bytes(b"do not delete someone else's file")

        def swap_then_fail(_fd):
            target.rename(self.out / "original-partial.html")
            other.rename(target)
            raise OSError("injected failure after pathname replacement")

        with patch("ocd.treemap.os.fsync", side_effect=swap_then_fail):
            with self.assertRaisesRegex(OSError, "injected failure"):
                write_usage_html(report, target)
        self.assertEqual(target.read_bytes(), b"do not delete someone else's file")
        self.assertTrue((self.out / "original-partial.html").is_file())

    @unittest.skipIf(os.name == "nt", "POSIX surrogateescape filename contract")
    def test_non_utf8_filename_is_safely_rendered(self):
        name = os.fsdecode(b"bad-\xff.bin")
        if "\udcff" not in name:
            self.skipTest("filesystem does not use surrogateescape")
        (self.root / name).write_bytes(b"x" * 10)
        report = scan_usage(self.root)
        html = render_usage_html(report)
        self.assertIn(r"\udcff", html)
        self.assertNotIn("\udcff", html)
        target = self.out / "report.html"
        write_usage_html(report, target)
        self.assertIn(r"\udcff", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
