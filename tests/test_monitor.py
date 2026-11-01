"""Bounded, read-only monitor behavior and failure recovery."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.monitor import MonitorEvent, watch_roots
from ocd.scanner import scan_root as real_scan_root
from ocd.types import RootConfig


class RecordingEvent(Event):
    """Avoid sleeping while checking the actual wait/stop protocol."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self.is_set()


class MonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "incoming"
        self.destination = self.base / "organized"
        self.source.mkdir()
        self.destination.mkdir()

    def root(self, *, source: Path | None = None, destination: Path | None = None,
             settle_seconds: float = 0, mode: str = "observe") -> RootConfig:
        return RootConfig(
            source=source or self.source,
            destination=destination or self.destination,
            mode=mode,
            tags={".jpg": "Photos"},
            settle_seconds=settle_seconds,
        )

    def file(self, name: str, *, source: Path | None = None,
             mtime_ns: int = 1_000_000_000) -> Path:
        path = (source or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sample")
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def test_initial_plan_has_moves_and_holds_but_never_applies_even_in_apply_mode(self) -> None:
        image = self.file("one.jpg")
        untagged = self.file("notes.txt")
        events: list[MonitorEvent] = []
        stop = RecordingEvent()

        watch_roots((self.root(mode="apply"),), events.append, stop_event=stop,
                    interval_seconds=0.01, max_polls=3)

        self.assertEqual(len(events), 1, "unchanged scans should be silent")
        self.assertEqual(events[0].kind, "change")
        self.assertIsNone(events[0].error)
        self.assertEqual([move.source for move in events[0].plan.moves], [image])
        self.assertEqual([hold.path for hold in events[0].plan.holds], [untagged])
        self.assertTrue(image.exists())
        self.assertTrue(untagged.exists())
        self.assertFalse((self.destination / "Photos").exists())
        self.assertEqual(stop.waits, [0.01, 0.01])

    def test_added_and_removed_files_emit_changed_full_snapshots(self) -> None:
        original = self.file("one.jpg")
        events: list[MonitorEvent] = []

        def record(event: MonitorEvent) -> None:
            events.append(event)
            if len(events) == 1:
                self.file("new.txt")
            elif len(events) == 2:
                original.unlink()

        watch_roots((self.root(),), record, stop_event=RecordingEvent(),
                    interval_seconds=0.01, max_polls=4)

        self.assertEqual([event.kind for event in events], ["change"] * 3)
        self.assertEqual([len(event.plan.moves) for event in events], [1, 1, 0])
        self.assertEqual([len(event.plan.holds) for event in events], [0, 1, 1])
        self.assertEqual([entry.path.name for entry in events[-1].files], ["new.txt"])

    def test_last_file_disappearance_emits_empty_plan(self) -> None:
        original = self.file("one.jpg")
        events: list[MonitorEvent] = []

        def record(event: MonitorEvent) -> None:
            events.append(event)
            if len(events) == 1:
                original.unlink()

        watch_roots((self.root(),), record, stop_event=RecordingEvent(),
                    interval_seconds=0.01, max_polls=2)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].files, ())
        self.assertEqual(events[1].plan.moves, ())
        self.assertEqual(events[1].plan.holds, ())

    def test_destination_collision_updates_holds_without_source_changes(self) -> None:
        image = self.file("one.jpg")
        events: list[MonitorEvent] = []

        def record(event: MonitorEvent) -> None:
            events.append(event)
            if len(events) == 1:
                category = self.destination / "Photos"
                category.mkdir()
                (category / image.name).write_bytes(b"already exists")

        watch_roots((self.root(),), record, stop_event=RecordingEvent(),
                    interval_seconds=0.01, max_polls=3)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].files, events[1].files)
        self.assertEqual(len(events[0].plan.moves), 1)
        self.assertEqual(events[1].plan.moves, ())
        self.assertEqual(events[1].plan.holds[0].reason, "destination already exists")

    def test_settle_window_is_respected_across_poll_rounds(self) -> None:
        now = [30_000_000_000]
        item = self.file("arriving.jpg", mtime_ns=now[0] - 1_000_000_000)
        events: list[MonitorEvent] = []

        def record(event: MonitorEvent) -> None:
            events.append(event)
            now[0] += 15_000_000_000

        # scanner and planner use the same time module; this controls their
        # shared wall-clock cutoff without adding sleeps to a test.
        with patch("ocd.scanner.time.time_ns", side_effect=lambda: now[0]):
            watch_roots((self.root(settle_seconds=10),), record,
                        stop_event=RecordingEvent(), interval_seconds=0.01, max_polls=2)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].files, ())
        self.assertEqual([move.source for move in events[1].plan.moves], [item])

    def test_permission_error_is_deduplicated_per_root_and_recovery_emits_change(self) -> None:
        first = self.root()
        second_source = self.base / "second-incoming"
        second_destination = self.base / "second-organized"
        second_source.mkdir()
        second_destination.mkdir()
        second = self.root(source=second_source, destination=second_destination)
        self.file("one.jpg", source=second_source)
        attempts = 0

        def flaky_scan(root: RootConfig):
            nonlocal attempts
            if root == first:
                attempts += 1
                if attempts <= 2:
                    raise PermissionError("temporary denial")
            return real_scan_root(root)

        events: list[MonitorEvent] = []
        with patch("ocd.monitor.scan_root", side_effect=flaky_scan):
            watch_roots((first, second), events.append, stop_event=RecordingEvent(),
                        interval_seconds=0.01, max_polls=3)

        self.assertEqual(attempts, 3)
        self.assertEqual([(event.root, event.kind) for event in events], [
            (first, "error"), (second, "change"), (first, "change"),
        ])
        self.assertIn("PermissionError: temporary denial", events[0].error)
        self.assertIsNone(events[0].plan)
        self.assertEqual(events[-1].plan.moves, ())

    def test_planner_failure_is_reported_without_blocking_another_root(self) -> None:
        other_source = self.base / "other-incoming"
        other_destination = self.base / "other-organized"
        other_source.mkdir()
        other_destination.mkdir()
        first = self.root()
        second = self.root(source=other_source, destination=other_destination)
        from ocd.planner import build_plan as real_plan

        def flaky_plan(root, entries):
            if root == first:
                raise OSError("read denied")
            return real_plan(root, entries)

        events: list[MonitorEvent] = []
        with patch("ocd.monitor.build_plan", side_effect=flaky_plan):
            watch_roots((first, second), events.append, stop_event=Event(),
                        interval_seconds=0.001, max_polls=1)

        self.assertEqual([event.kind for event in events], ["error", "change"])
        self.assertIn("read denied", events[0].error)

    def test_stop_event_prevents_work_and_can_stop_between_roots(self) -> None:
        stop = Event()
        stop.set()
        events: list[MonitorEvent] = []
        with patch("ocd.monitor.scan_root") as scan:
            watch_roots((self.root(),), events.append, stop_event=stop, max_polls=10)
        scan.assert_not_called()
        self.assertEqual(events, [])

        other_source = self.base / "second"
        other_destination = self.base / "second-dest"
        other_source.mkdir()
        other_destination.mkdir()
        stop.clear()

        def stop_on_first(event: MonitorEvent) -> None:
            events.append(event)
            stop.set()

        with patch("ocd.monitor.scan_root", wraps=real_scan_root) as scan:
            watch_roots((self.root(), self.root(source=other_source, destination=other_destination)),
                        stop_on_first, stop_event=stop, max_polls=10)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(len(events), 1)

    def test_callback_errors_propagate_without_retry(self) -> None:
        def broken_callback(_: MonitorEvent) -> None:
            raise RuntimeError("consumer failed")

        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            watch_roots((self.root(),), broken_callback, stop_event=Event(), max_polls=2)

    def test_symlinked_directories_are_not_enumerated(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.file("secret.jpg", source=outside)
        try:
            (self.source / "external").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink not available: {exc}")
        events: list[MonitorEvent] = []
        watch_roots((self.root(),), events.append, stop_event=Event(), max_polls=1)
        self.assertEqual(events[0].files, ())
        self.assertEqual(events[0].plan.moves, ())

    def test_replaced_registered_root_is_rejected_before_next_scan(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.file("private.jpg", source=outside)
        self.file("safe.jpg")
        events: list[MonitorEvent] = []

        def replace_root(event: MonitorEvent) -> None:
            events.append(event)
            if len(events) == 1:
                self.source.rename(self.base / "former-incoming")
                try:
                    self.source.symlink_to(outside, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"symlink not available: {exc}")

        with patch("ocd.monitor.scan_root", wraps=real_scan_root) as scan:
            watch_roots((self.root(),), replace_root, stop_event=RecordingEvent(),
                        interval_seconds=0.01, max_polls=3)

        self.assertEqual(scan.call_count, 1)
        self.assertEqual([event.kind for event in events], ["change", "error"])
        self.assertIn("ConfigError", events[-1].error)

    def test_invalid_poll_parameters_are_rejected_without_a_loop(self) -> None:
        root = self.root()
        for interval in (0, -1, float("nan"), float("inf"), True, "invalid"):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                watch_roots((root,), lambda _: None, stop_event=Event(),
                            interval_seconds=interval, max_polls=1)
        for limit in (0, -1, True, 1.5, "3"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                watch_roots((root,), lambda _: None, stop_event=Event(), max_polls=limit)
        with self.assertRaises(ValueError):
            watch_roots((), lambda _: None, stop_event=Event(), max_polls=1)
        with self.assertRaises(TypeError):
            watch_roots(("not a root",), lambda _: None, stop_event=Event(), max_polls=1)
        with self.assertRaises(TypeError):
            watch_roots((root,), None, stop_event=Event(), max_polls=1)
        with self.assertRaises(TypeError):
            watch_roots((root,), lambda _: None, stop_event=None, max_polls=1)


if __name__ == "__main__":
    unittest.main()
