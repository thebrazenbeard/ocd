"""Safety properties of the explicit mutation boundary."""

import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd import apply as engine  # noqa: E402
from ocd.types import MovePlan, RootConfig  # noqa: E402


def _paused_apply_process(root, plan, journal, stage, ready, resume, results):
    """Hold a real apply process inside preflight or journal commit."""
    if stage == "preflight":
        original = engine._verify_move

        def pause(*args, **kwargs):
            ready.set()
            if not resume.wait(10):
                raise OSError("test apply preflight timed out")
            return original(*args, **kwargs)

        target = "_verify_move"
    else:
        original = engine._journal_event

        def pause(*args, **kwargs):
            if kwargs.get("event") == "end":
                ready.set()
                if not resume.wait(10):
                    raise OSError("test journal commit timed out")
            return original(*args, **kwargs)

        target = "_journal_event"
    try:
        with mock.patch.object(engine, target, side_effect=pause):
            outcome = engine.apply_moves(root, [plan], journal)
        results.put((outcome.moved == (plan,), outcome.recovery_required, len(outcome.holds)))
    except BaseException as exc:
        results.put((type(exc).__name__, str(exc)))


class ApplyMoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "incoming"
        self.dest = self.base / "organized"
        self.source.mkdir()
        self.dest.mkdir()
        self.journal = self.base / "journal.jsonl"

    def root(self, mode="apply"):
        return RootConfig(source=self.source, destination=self.dest, mode=mode)

    def plan(self, name="example.mkv", destination=None, content=b"important media bytes"):
        path = self.source / name
        path.write_bytes(content)
        stat = path.stat()
        return MovePlan(
            source=path,
            destination=destination or (self.dest / "Movies" / name),
            tag="movie",
            size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
        )

    def test_observe_does_not_touch_files_directories_or_journal(self):
        plan = self.plan()
        result = engine.apply_moves(self.root("observe"), [plan], self.journal)
        self.assertEqual(result.observed, (plan,))
        self.assertFalse(result.moved or result.holds or result.recovery_required)
        self.assertTrue(plan.source.is_file())
        self.assertFalse(plan.destination.parent.exists())
        self.assertFalse(self.journal.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is enabled only on Linux")
    def test_success_is_no_overwrite_and_journalled(self):
        plan = self.plan()
        result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertEqual(result.moved, (plan,))
        self.assertFalse(result.holds or result.recovery_required)
        self.assertFalse(plan.source.exists())
        self.assertEqual(plan.destination.read_bytes(), b"important media bytes")
        events = [json.loads(line) for line in self.journal.read_text().splitlines()]
        self.assertEqual([event["event"] for event in events], ["begin", "intent", "complete", "end"])
        self.assertEqual(events[-1]["status"], "complete")

    def test_existing_destination_is_preserved_and_batch_does_not_start(self):
        target = self.dest / "Movies" / "example.mkv"
        target.parent.mkdir()
        target.write_bytes(b"someone else's important file")
        plan = self.plan(destination=target)
        result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertTrue(result.holds)
        self.assertFalse(result.moved)
        self.assertEqual(target.read_bytes(), b"someone else's important file")
        self.assertTrue(plan.source.exists())
        self.assertFalse(self.journal.exists())

    def test_source_metadata_or_identity_change_is_held(self):
        plan = self.plan()
        plan.source.write_bytes(b"a changed file")
        result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertTrue(result.holds)
        self.assertTrue(plan.source.exists())
        self.assertFalse(plan.destination.exists())

    def test_parent_symlink_traversal_is_held(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.dest / "shortcut"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks unavailable on this filesystem")
        plan = self.plan(destination=link / "example.mkv")
        result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertTrue(result.holds)
        self.assertTrue(plan.source.exists())
        self.assertFalse((outside / "example.mkv").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is enabled only on Linux")
    def test_collision_created_at_commit_does_not_overwrite(self):
        plan = self.plan()
        actual_rename = engine._rename_noreplace

        def race(source, destination, **kwargs):
            destination.write_bytes(b"concurrent writer")
            actual_rename(source, destination, **kwargs)

        with mock.patch.object(engine, "_rename_noreplace", side_effect=race):
            result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertTrue(result.holds)
        self.assertFalse(result.moved or result.recovery_required)
        self.assertTrue(plan.source.exists())
        self.assertEqual(plan.destination.read_bytes(), b"concurrent writer")
        events = [json.loads(line)["event"] for line in self.journal.read_text().splitlines()]
        self.assertEqual(events, ["begin", "intent", "abort_intent", "end"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux dirfd rename safety")
    def test_parent_swapped_to_symlink_at_commit_cannot_escape_root(self):
        plan = self.plan()
        outside = self.base / "outside"
        outside.mkdir()
        displaced = self.dest / "displaced-directory"
        original = engine._rename_noreplace

        def swap_then_commit(source, destination, **kwargs):
            destination.parent.rename(displaced)
            destination.parent.symlink_to(outside, target_is_directory=True)
            original(source, destination, **kwargs)

        with mock.patch.object(engine, "_rename_noreplace", side_effect=swap_then_commit):
            result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertTrue(result.holds)
        self.assertFalse(result.moved or result.recovery_required)
        self.assertTrue(plan.source.exists())
        self.assertFalse((outside / plan.source.name).exists())
        self.assertFalse((displaced / plan.source.name).exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is enabled only on Linux")
    def test_second_failure_rolls_first_back_without_deletion(self):
        one = self.plan("one.mkv")
        two = self.plan("two.mkv")
        actual_rename = engine._rename_noreplace
        calls = 0

        def fail_second(source, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated failure")
            actual_rename(source, destination, **kwargs)

        with mock.patch.object(engine, "_rename_noreplace", side_effect=fail_second):
            result = engine.apply_moves(self.root(), [one, two], self.journal)
        self.assertFalse(result.moved or result.recovery_required)
        self.assertEqual(result.rolled_back, (one,))
        self.assertTrue(result.holds)
        self.assertTrue(one.source.exists() and two.source.exists())
        self.assertFalse(one.destination.exists() or two.destination.exists())
        events = [json.loads(line)["event"] for line in self.journal.read_text().splitlines()]
        self.assertEqual(
            events,
            ["begin", "intent", "complete", "intent", "abort_intent", "rollback_intent", "rollback_complete", "end"],
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is enabled only on Linux")
    def test_rollback_collision_holds_both_user_files(self):
        first = self.plan("first.mkv", content=b"original first")
        second = self.plan("second.mkv")
        original = engine._rename_noreplace
        calls = 0

        def conflict(source, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                first.source.write_bytes(b"new occupant of original path")
                raise OSError("simulated second move failure")
            original(source, destination, **kwargs)

        with mock.patch.object(engine, "_rename_noreplace", side_effect=conflict):
            result = engine.apply_moves(self.root(), [first, second], self.journal)
        self.assertTrue(result.recovery_required)
        self.assertEqual(result.moved, (first,))
        self.assertFalse(result.rolled_back)
        self.assertEqual(first.destination.read_bytes(), b"original first")
        self.assertEqual(first.source.read_bytes(), b"new occupant of original path")
        self.assertTrue(second.source.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is enabled only on Linux")
    def test_completed_journal_can_be_reused_for_another_batch(self):
        first = self.plan("first.mkv")
        second = self.plan("second.mkv")
        self.assertEqual(engine.apply_moves(self.root(), [first], self.journal).moved, (first,))
        next_result = engine.apply_moves(self.root(), [second], self.journal)
        self.assertEqual(next_result.moved, (second,))
        self.assertFalse(next_result.recovery_required or next_result.holds)
        self.assertTrue(first.destination.exists() and second.destination.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is enabled only on Linux")
    def test_incomplete_journal_blocks_retries_and_preserves_source(self):
        plan = self.plan()
        self.journal.write_text(json.dumps({"event": "begin", "transaction": "interrupted"}) + "\n")
        before = self.journal.read_bytes()
        result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertTrue(result.recovery_required)
        self.assertTrue(result.holds)
        self.assertFalse(result.moved)
        self.assertTrue(plan.source.exists())
        self.assertFalse(plan.destination.exists())
        self.assertEqual(self.journal.read_bytes(), before)

    @unittest.skipUnless(sys.platform.startswith("linux"), "organizer apply is enabled only on Linux")
    def test_corrupt_journal_is_never_ignored(self):
        plan = self.plan()
        self.journal.write_bytes(b'{"event": "begin"')
        result = engine.apply_moves(self.root(), [plan], self.journal)
        self.assertTrue(result.recovery_required)
        self.assertTrue(plan.source.exists())
        self.assertFalse(plan.destination.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux flock and fork")
    def test_separate_process_cannot_apply_during_preflight_or_journal_commit(self):
        # Different journals are intentional: a journal-only lock would let
        # two batches mutate the same root at the same time.
        context = multiprocessing.get_context("fork")
        for stage in ("preflight", "journal_end"):
            with self.subTest(stage=stage):
                first = self.plan(f"first-{stage}.mkv")
                second = self.plan(f"second-{stage}.mkv")
                first_journal = self.base / f"first-{stage}.jsonl"
                second_journal = self.base / f"second-{stage}.jsonl"
                ready = context.Event()
                resume = context.Event()
                results = context.Queue()
                child = context.Process(
                    target=_paused_apply_process,
                    args=(self.root(), first, first_journal, stage, ready, resume, results),
                )
                child.start()
                try:
                    self.assertTrue(ready.wait(5), "first process did not enter the locked stage")
                    blocked = engine.apply_moves(self.root(), [second], second_journal)
                    self.assertFalse(blocked.moved or blocked.recovery_required)
                    self.assertEqual(len(blocked.holds), 1)
                    self.assertIn("lock unavailable", blocked.holds[0].reason)
                    self.assertTrue(second.source.exists())
                    self.assertFalse(second.destination.exists())
                    self.assertFalse(second_journal.exists())
                finally:
                    resume.set()
                    child.join(5)
                    if child.is_alive():
                        child.terminate()
                        child.join(5)
                self.assertEqual(child.exitcode, 0)
                self.assertEqual(results.get(timeout=5), (True, False, 0))
                # Kernel lock release is automatic; no stale-PID cleanup or
                # manual deletion is needed before the next batch succeeds.
                retried = engine.apply_moves(self.root(), [second], second_journal)
                self.assertEqual(retried.moved, (second,))
                self.assertFalse(retried.holds or retried.recovery_required)
                self.assertTrue(first.destination.exists())


if __name__ == "__main__":
    unittest.main()
