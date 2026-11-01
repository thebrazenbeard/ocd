"""Characterization and authorization tests for folder-attached category tags."""

from __future__ import annotations

import json
import os
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.config import ConfigError, add_root, init_config, load_config, save_config
from ocd.cli import main
from ocd.planner import build_plan
from ocd.types import FileEntry, RootConfig


class FolderTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "incoming"
        self.destination = self.base / "organized"
        self.source.mkdir()
        self.destination.mkdir()
        self.config_path = self.base / "config.json"

    def file(self, relative: str) -> FileEntry:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unchanged fixture")
        when = time.time_ns() - 30_000_000_000
        os.utime(path, ns=(when, when))
        status = path.stat()
        return FileEntry(path, status.st_size, status.st_mtime_ns)

    def root(self, tags: dict[str, str]) -> RootConfig:
        return RootConfig(self.source, self.destination, tags=tags, settle_seconds=0)

    def test_legacy_extension_tags_keep_their_behavior(self):
        entry = self.file("invoices/receipt.pdf")
        plan = build_plan(self.root({"pdf": "Documents"}), [entry])
        self.assertEqual(plan.holds, ())
        self.assertEqual(plan.moves[0].destination, self.destination / "Documents" / "receipt.pdf")

    def test_explicit_folder_tag_matches_descendants_regardless_of_extension(self):
        entry = self.file("Invoices/2026/receipt.no-such-extension")
        plan = build_plan(self.root({"folder:Invoices": "Accounting"}), [entry])
        self.assertEqual(plan.holds, ())
        self.assertEqual(plan.moves[0].destination, self.destination / "Accounting" / "receipt.no-such-extension")
        self.assertTrue(entry.path.exists())
        self.assertFalse(any(self.destination.iterdir()))

    def test_longest_folder_tag_wins_then_extension_fallback(self):
        tags = {"folder:Projects": "Work", "folder:Projects/Photos": "Pictures", ".txt": "Text"}
        nested = self.file("Projects/Photos/IMG.unknown")
        sibling = self.file("Projects/notes.txt")
        other = self.file("Unregistered/notes.txt")
        plan = build_plan(self.root(tags), [nested, sibling, other])
        self.assertEqual(plan.holds, ())
        self.assertEqual({move.source: move.tag for move in plan.moves}, {
            nested.path: "Pictures", sibling.path: "Work", other.path: "Text",
        })

    def test_folder_tag_requires_full_directory_component(self):
        similarly_named = self.file("Invoices-old/file.unregistered")
        at_root = self.file("Invoices.unregistered")
        plan = build_plan(self.root({"folder:Invoices": "Accounting"}), [similarly_named, at_root])
        self.assertEqual(plan.moves, ())
        self.assertEqual(len(plan.holds), 2)
        self.assertTrue(all("explicit tag" in item.reason for item in plan.holds))

    def test_ambiguous_portable_spellings_and_unsafe_folder_keys_fail_closed(self):
        entry = self.file("Invoices/file.txt")
        invalid = (
            {"folder:Invoices": "Docs", "folder:invoices": "Other"},
            {"folder:../outside": "Docs"},
            {"folder:/absolute": "Docs"},
            {"folder:Invoices//2026": "Docs"},
            {"folder:Invoices/./2026": "Docs"},
            {"folder:Invoices\\2026": "Docs"},
            {"folder:CON": "Docs"},
            {"folder:2026": "Docs", "txt": "docs"},
        )
        for tags in invalid:
            with self.subTest(tags=tags):
                result = build_plan(self.root(tags), [entry])
                self.assertEqual(result.moves, ())
                self.assertEqual(len(result.holds), 1)

    def test_version_one_config_round_trip_preserves_folder_tags_and_defaults(self):
        init_config(self.config_path)
        legacy = load_config(self.config_path)
        new = add_root(legacy, self.source, self.destination,
                       tags={"folder:Projects/Receipts": "Accounting", "pdf": "Documents"},
                       settle_seconds=0)
        save_config(new, self.config_path)
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 1)
        self.assertEqual(load_config(self.config_path).roots[0].tags, {
            "folder:Projects/Receipts": "Accounting", ".pdf": "Documents",
        })
        self.assertEqual(load_config(self.config_path).roots[0].mode, "observe")

    def test_folder_tag_cli_plan_requires_registered_root_and_never_moves(self):
        entry = self.file("Invoices/2026/item.future")
        init_config(self.config_path)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            register_status = main([
                "--config", str(self.config_path), "roots", "add",
                str(self.source), str(self.destination),
                "--tag", "folder:Invoices=Accounting", "--settle-seconds", "0",
            ])
            plan_status = main([
                "--config", str(self.config_path), "plan", "--root", str(self.source),
            ])
        self.assertEqual((register_status, plan_status), (0, 0), error.getvalue())
        self.assertIn(str(self.destination / "Accounting" / "item.future"), output.getvalue())
        self.assertTrue(entry.path.exists())
        self.assertFalse(any(self.destination.iterdir()))

    def test_config_rejects_unsafe_or_ambiguous_folder_keys_before_save(self):
        for tags in (
            {"folder:..": "No"},
            {"folder:Documents/../Outside": "No"},
            {"folder:A": "One", "folder:a": "Two"},
        ):
            with self.subTest(tags=tags), self.assertRaises(ConfigError):
                add_root(load_config(self.config_path) if self.config_path.exists() else
                         self._empty_config(), self.source, self.destination, tags=tags)

    def _empty_config(self):
        from ocd.config import Config

        return Config()


if __name__ == "__main__":
    unittest.main()
