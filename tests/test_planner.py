"""Cross-platform and fail-closed tests for the extension-tagged planner."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.planner import _safe_segment, build_plan
from ocd.types import FileEntry, RootConfig


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source = base / "incoming"
        self.destination = base / "organized"
        self.source.mkdir()
        self.destination.mkdir()

    def make_file(self, name: str, *, parent: Path | None = None) -> FileEntry:
        path = (parent or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello from the scanner")
        old_ns = time.time_ns() - 30_000_000_000
        os.utime(path, ns=(old_ns, old_ns))
        status = path.stat()
        return FileEntry(path, status.st_size, status.st_mtime_ns)

    def root(self, **overrides) -> RootConfig:
        values = dict(
            source=self.source,
            destination=self.destination,
            mode="observe",
            tags={".JPG": "Photographs", "txt": "Text"},
            settle_seconds=2,
        )
        values.update(overrides)
        return RootConfig(**values)

    def test_explicit_extension_tag_and_observe_is_read_only(self):
        entry = self.make_file("Summer.JPG", parent=self.source / "camera")
        result = build_plan(self.root(), [entry])
        self.assertEqual(result.holds, ())
        self.assertEqual(len(result.moves), 1)
        move = result.moves[0]
        self.assertEqual(move.source, entry.path)
        self.assertEqual(move.destination, self.destination / "Photographs" / "Summer.JPG")
        self.assertEqual(move.tag, "Photographs")
        self.assertEqual(move.size, entry.size)
        self.assertEqual(move.source_mtime_ns, entry.mtime_ns)
        self.assertTrue(entry.path.exists())
        self.assertEqual(list(self.destination.iterdir()), [], "planner must not create folders")
        self.assertEqual(build_plan(self.root(mode="apply"), [entry]), result)

    def test_unmapped_extension_is_held_even_with_plausible_name(self):
        entry = self.make_file("photo.png")
        result = build_plan(self.root(tags={"jpg": "Photographs"}), [entry])
        self.assertEqual(result.moves, ())
        self.assertIn("explicit tag", result.holds[0].reason)

    def test_file_has_to_settle_and_snapshot_must_match(self):
        entry = self.make_file("a.txt")
        fresh = FileEntry(entry.path, entry.size, time.time_ns())
        result = build_plan(self.root(), [fresh])
        self.assertEqual(result.moves, ())
        self.assertIn("changed", result.holds[0].reason)
        # Make the actual file recent, then obtain an accurate snapshot.
        now_ns = time.time_ns()
        os.utime(entry.path, ns=(now_ns, now_ns))
        status = entry.path.stat()
        recent = FileEntry(entry.path, status.st_size, status.st_mtime_ns)
        result = build_plan(self.root(), [recent])
        self.assertEqual(result.moves, ())
        self.assertIn("settled", result.holds[0].reason)
        self.assertEqual(len(build_plan(self.root(settle_seconds=0), [recent]).moves), 1)

    def test_invalid_metadata_mode_and_tag_mapping_fail_closed(self):
        entry = self.make_file("a.txt")
        invalid = (
            self.root(mode="surprise"),
            self.root(settle_seconds=-1),
            self.root(tags={".txt": "Words", "TXT": "Other"}),
            self.root(tags={".txt": "Words", "jpg": "words"}),
            self.root(tags={"*.txt": "Words"}),
            self.root(tags={".txt": ". ."}),
        )
        for root in invalid:
            with self.subTest(root=root):
                result = build_plan(root, [entry])
                self.assertEqual(result.moves, ())
                self.assertEqual(len(result.holds), 1)
        result = build_plan(self.root(), [FileEntry(entry.path, -1, entry.mtime_ns)])
        self.assertIn("metadata", result.holds[0].reason)

    def test_root_overlap_both_directions(self):
        entry = self.make_file("a.txt")
        for destination in (self.source, self.source / "child", self.source.parent):
            with self.subTest(destination=destination):
                result = build_plan(self.root(destination=destination), [entry])
                self.assertEqual(result.moves, ())
                self.assertIn("overlap", result.holds[0].reason)

    def test_root_overlap_detects_resolved_aliases(self):
        alias = self.destination / "source-alias"
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        entry = self.make_file("a.txt")
        result = build_plan(self.root(destination=alias), [entry])
        self.assertEqual(result.moves, ())
        self.assertIn("overlap", result.holds[0].reason)

    def test_nonoverlapping_root_reparse_ancestor_is_held(self):
        alias = self.source.parent / "incoming-alias"
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        entry = self.make_file("a.txt")
        via_alias = FileEntry(alias / "a.txt", entry.size, entry.mtime_ns)
        result = build_plan(self.root(source=alias), [via_alias])
        self.assertEqual(result.moves, ())
        self.assertIn("symlink", result.holds[0].reason)

    def test_rejects_symlink_source_and_symlink_ancestor(self):
        original = self.make_file("original.txt")
        link = self.source / "link.txt"
        folder_link = self.source / "directory-alias"
        real_dir = self.source / "real-dir"
        real_dir.mkdir()
        nested = self.make_file("nested.txt", parent=real_dir)
        try:
            link.symlink_to(original.path)
            folder_link.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        aliased = folder_link / nested.path.name
        for path in (link, aliased):
            with self.subTest(path=path):
                status = path.stat()
                result = build_plan(self.root(), [FileEntry(path, status.st_size, status.st_mtime_ns)])
                self.assertEqual(result.moves, ())
                self.assertIn("symlink", result.holds[0].reason)

    def test_existing_destination_and_casefold_category_collision(self):
        entry = self.make_file("A.txt")
        category = self.destination / "Text"
        category.mkdir()
        (category / "A.txt").write_bytes(b"do not replace")
        result = build_plan(self.root(), [entry])
        self.assertEqual(result.moves, ())
        self.assertIn("exists", result.holds[0].reason)

        (category / "A.txt").unlink()
        # On POSIX 'text' and 'Text' are different but Windows would alias them.
        category.rename(self.destination / "text")
        result = build_plan(self.root(), [entry])
        self.assertEqual(result.moves, ())
        self.assertIn("case collision", result.holds[0].reason)

    def test_destination_symlink_category_is_held(self):
        entry = self.make_file("A.txt")
        outside = self.source.parent / "outside"
        outside.mkdir()
        try:
            (self.destination / "Text").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        result = build_plan(self.root(), [entry])
        self.assertEqual(result.moves, ())
        self.assertTrue(any(word in result.holds[0].reason for word in ("escapes", "safe")))

    def test_ambiguous_casefold_sources_all_hold_in_any_order(self):
        if os.name == "nt":
            self.skipTest("case-sensitive source names unavailable on ordinary Windows volumes")
        left = self.make_file("A.txt")
        right = self.make_file("a.TXT")
        first = build_plan(self.root(), [left, right])
        reverse = build_plan(self.root(), [right, left])
        self.assertEqual(first, reverse)
        self.assertEqual(first.moves, ())
        self.assertEqual(len(first.holds), 2)
        self.assertTrue(all("multiple sources" in hold.reason for hold in first.holds))

    def test_sanitized_name_collision_holds_every_participant(self):
        if os.name == "nt":
            self.skipTest("Windows forbids these names at source")
        first_entry = self.make_file("report?.txt")
        second_entry = self.make_file("report*.txt")
        result = build_plan(self.root(), [first_entry, second_entry])
        self.assertEqual(result.moves, ())
        self.assertEqual(len(result.holds), 2)
        self.assertTrue(all("multiple sources" in hold.reason for hold in result.holds))

    def test_filename_and_category_are_windows_safe(self):
        self.assertEqual(_safe_segment("CON.txt"), "_CON.txt")
        self.assertEqual(_safe_segment("LPT¹.log"), "_LPT¹.log")
        self.assertEqual(_safe_segment("data:old?.txt. "), "data_old_.txt")
        self.assertEqual(_safe_segment("../../Escape"), ".._.._Escape")
        self.assertEqual(_safe_segment(".camera.txt"), ".camera.txt")
        self.assertIsNone(_safe_segment("..."))
        self.assertIsNone(_safe_segment("a" * 256))
        if os.name != "nt":
            entry = self.make_file("report:2026?.txt")
            result = build_plan(self.root(tags={"txt": "Docs/Reports"}), [entry])
            self.assertEqual(len(result.moves), 1)
            self.assertEqual(result.moves[0].destination, self.destination / "Docs_Reports" / "report_2026_.txt")

    def test_foreign_windows_path_flavor_is_not_interpreted_as_posix(self):
        windows_root = RootConfig(
            source=PureWindowsPath(r"C:\incoming"),
            destination=PureWindowsPath(r"D:\organized"),
            tags={"txt": "Text"},
        )
        entry = FileEntry(PureWindowsPath(r"C:\incoming\a.txt"), 1, 1)
        result = build_plan(windows_root, [entry])
        self.assertEqual(result.moves, ())
        self.assertIn("flavor", result.holds[0].reason)


if __name__ == "__main__":
    unittest.main()
