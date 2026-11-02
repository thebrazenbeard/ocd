"""Evidence-only cleanup review never authorizes or performs deletion."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from ocd.cleanup import CleanupObservation, review_cleanup
from ocd.cli import main


class CleanupReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()

    def cli(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_complete_review_blocks_every_observation_and_preserves_bytes(self):
        large = self.root / "big.gguf"
        small = self.root / "small.tmp"
        large.write_bytes(b"x" * 8192)
        small.write_bytes(b"abc")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in (large, small)}

        review = review_cleanup(self.root, top=2)
        self.assertTrue(review.usage.complete, review.usage.issues)
        self.assertEqual([o.path for o in review.observations], [large, small])
        self.assertEqual([o.logical_bytes for o in review.observations], [8192, 3])
        self.assertTrue(all(not o.deletion_eligible and o.reclaimable_bytes is None
                            for o in review.observations))
        with self.assertRaises(TypeError):
            CleanupObservation(large, 8192, deletion_eligible=True)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in (large, small)})
        self.assertFalse((self.root / "journal.jsonl").exists())

    def test_standalone_cli_json_never_creates_config(self):
        target = self.root / "weights.bin"
        target.write_bytes(b"model")

        absent = Path(self.temp.name) / "no-config.json"
        status, output, error = self.cli(
            "--config", str(absent), "cleanup", "review",
            "--path", str(self.root), "--json",
        )
        self.assertEqual(status, 0, error)
        data = json.loads(output)
        self.assertTrue(data["complete"])
        self.assertEqual(data["logical_bytes"], 5)
        self.assertEqual(data["observations"][0]["path"], str(target))
        self.assertFalse(data["observations"][0]["deletion_eligible"])
        self.assertIsNone(data["observations"][0]["reclaimable_bytes"])
        self.assertFalse(data["deletion_eligible"])
        self.assertIsNone(data["reclaimable_bytes"])
        self.assertFalse(absent.exists())
        self.assertEqual(target.read_bytes(), b"model")

    def test_incomplete_scan_returns_no_candidates_and_nonzero(self):
        (self.root / "a.bin").write_bytes(b"a")
        (self.root / "b.bin").write_bytes(b"b")
        partial = review_cleanup(self.root, max_entries=1)
        self.assertFalse(partial.usage.complete)
        self.assertEqual(partial.observations, ())

        status, output, error = self.cli(
            "cleanup", "review", "--path", str(self.root),
            "--max-entries", "1", "--json",
        )
        self.assertEqual(status, 1, error)
        data = json.loads(output)
        self.assertFalse(data["complete"])
        self.assertEqual(data["observations"], [])
        self.assertTrue(data["issues"])

    def test_invalid_limits_and_relative_path_fail_closed(self):
        for args in (
            ("--path", "relative"),
            ("--path", str(self.root), "--top", "0"),
            ("--path", str(self.root), "--max-depth", "65"),
            ("--path", str(self.root), "--max-entries", "0"),
        ):
            with self.subTest(args=args):
                code, out, err = self.cli("cleanup", "review", *args)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("ocd:", err)


if __name__ == "__main__":
    unittest.main()
