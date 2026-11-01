"""Public CLI and config safety contract; no mutation by default."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocd.cli import main
from ocd.config import ConfigError, default_config_path, load_config
from ocd.types import Hold, MovePlan, PlanResult


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "incoming"
        self.destination = self.base / "organized"
        self.source.mkdir()
        self.destination.mkdir()
        self.config = self.base / "user-config" / "config.json"

    def invoke(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--config", str(self.config), *args])
        return status, out.getvalue(), err.getvalue()

    def initialize(self):
        status, _, error = self.invoke("init")
        self.assertEqual(status, 0, error)

    def register(self, *, mode: str = "observe"):
        status, _, error = self.invoke(
            "roots", "add", str(self.source), str(self.destination),
            "--mode", mode, "--tag", ".txt=documents", "--settle-seconds", "0",
        )
        self.assertEqual(status, 0, error)

    def test_init_is_versioned_empty_and_exclusive(self):
        self.initialize()
        self.assertEqual(json.loads(self.config.read_text()), {"version": 1, "roots": []})
        self.assertEqual(self.invoke("init")[0], 2)
        self.assertEqual(json.loads(self.config.read_text()), {"version": 1, "roots": []})

    def test_no_implicit_configuration_or_discovery(self):
        self.assertEqual(self.invoke("scan")[0], 2)
        self.initialize()
        status, output, _ = self.invoke("roots", "list")
        self.assertEqual(status, 0)
        self.assertIn("No registered roots", output)
        self.assertEqual(self.invoke("scan")[0], 2)

    def test_add_root_persists_canonical_allowlist_and_observe_default(self):
        self.initialize()
        status, _, error = self.invoke(
            "roots", "add", str(self.source), str(self.destination), "--tag", "JPG=photos",
        )
        self.assertEqual(status, 0, error)
        config = load_config(self.config)
        self.assertEqual(len(config.roots), 1)
        root = config.roots[0]
        self.assertEqual(root.mode, "observe")
        self.assertEqual(root.source, self.source)
        self.assertEqual(root.destination, self.destination)
        self.assertEqual(root.tags, {".jpg": "photos"})

    def test_unregistered_and_relative_paths_are_rejected(self):
        self.initialize()
        self.register(mode="apply")
        for path in (str(self.destination), "incoming"):
            for command in ("scan", "plan", "apply"):
                args = [command, "--root", path]
                if command == "apply":
                    args.append("--yes")
                status, _, error = self.invoke(*args)
                self.assertEqual(status, 2, (args, error))
                self.assertFalse((self.config.parent / "journal.jsonl").exists())

    def test_apply_requires_both_mode_and_explicit_yes(self):
        self.initialize()
        self.register()
        candidate = self.source / "note.txt"
        candidate.write_text("important data")
        status, _, error = self.invoke("apply", "--root", str(self.source))
        self.assertEqual(status, 2)
        self.assertIn("--yes", error)
        status, _, error = self.invoke("apply", "--root", str(self.source), "--yes")
        self.assertEqual(status, 2)
        self.assertIn("observe", error)
        self.assertTrue(candidate.is_file())
        self.assertFalse((self.config.parent / "journal.jsonl").exists())

    def test_scan_and_plan_never_apply(self):
        self.initialize()
        self.register(mode="apply")
        candidate = self.source / "note.txt"
        candidate.write_text("important data")
        for command in ("scan", "plan"):
            with patch("ocd.apply.apply_moves", side_effect=AssertionError("mutation invoked")):
                status, output, error = self.invoke(command, "--root", str(self.source))
            self.assertEqual(status, 0, (command, error))
            self.assertIn("no changes made", output)
        self.assertTrue(candidate.exists())
        self.assertFalse((self.config.parent / "journal.jsonl").exists())

    def test_apply_passes_replanned_moves_and_journal_after_consent(self):
        self.initialize()
        self.register(mode="apply")
        move = MovePlan(self.source / "a.txt", self.destination / "documents" / "a.txt", "documents", 1, 0)
        plan = PlanResult((move,), ())

        class Result:
            moved = (move,)
            holds = ()
            rolled_back = ()
            recovery_required = False

        with patch("ocd.cli._plan", return_value=plan) as planner, patch(
            "ocd.apply.apply_moves", return_value=Result()
        ) as apply:
            status, output, error = self.invoke("apply", "--root", str(self.source), "--yes")
        self.assertEqual(status, 0, error)
        planner.assert_called_once()
        apply.assert_called_once()
        args = apply.call_args.args
        self.assertEqual(args[0].source, self.source)
        self.assertEqual(args[1], (move,))
        self.assertEqual(args[2], self.config.parent / "journal.jsonl")
        self.assertIn("Applied 1 move(s)", output)

    def test_apply_operational_holds_are_reported_as_failure(self):
        self.initialize()
        self.register(mode="apply")
        move = MovePlan(self.source / "a.txt", self.destination / "documents" / "a.txt", "documents", 1, 0)
        plan = PlanResult((move,), ())

        class HeldResult:
            moved = ()
            holds = (Hold(move.source, "preflight hold: file changed"),)
            rolled_back = ()
            recovery_required = False

        with patch("ocd.cli._plan", return_value=plan), patch(
            "ocd.apply.apply_moves", return_value=HeldResult()
        ):
            status, output, error = self.invoke("apply", "--root", str(self.source), "--yes")
        self.assertEqual(status, 1, error)
        self.assertIn("HOLD", output)
        self.assertIn("Applied 0 move(s); 1 hold(s)", output)

    def test_apply_recovery_required_is_nonzero_even_without_holds(self):
        self.initialize()
        self.register(mode="apply")
        move = MovePlan(self.source / "a.txt", self.destination / "documents" / "a.txt", "documents", 1, 0)

        class RecoveryResult:
            moved = ()
            holds = ()
            rolled_back = ()
            recovery_required = True

        with patch("ocd.cli._plan", return_value=PlanResult((move,), ())), patch(
            "ocd.apply.apply_moves", return_value=RecoveryResult()
        ):
            status, _, error = self.invoke("apply", "--root", str(self.source), "--yes")
        self.assertEqual(status, 1)
        self.assertIn("Manual recovery required", error)

    def test_watch_observes_allowlisted_roots_and_stops_cleanly_on_ctrl_c(self):
        self.initialize()
        self.register(mode="apply")
        move = MovePlan(self.source / "a.txt", self.destination / "documents" / "a.txt", "documents", 1, 0)
        from ocd.monitor import MonitorEvent

        def watch(selected, callback, *, stop_event, interval_seconds):
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].source, self.source)
            self.assertEqual(interval_seconds, 0.2)
            self.assertFalse(stop_event.is_set())
            callback(MonitorEvent(selected[0], "change", plan=PlanResult((move,), ())))
            callback(MonitorEvent(selected[0], "error", error="PermissionError: read denied"))
            raise KeyboardInterrupt

        with patch("ocd.monitor.watch_roots", side_effect=watch) as monitor, patch(
            "ocd.apply.apply_moves", side_effect=AssertionError("watch attempted mutation")
        ):
            status, output, error = self.invoke("watch", "--root", str(self.source), "--interval", "0.2")
        self.assertEqual(status, 130)
        monitor.assert_called_once()
        self.assertIn("WATCH", output)
        self.assertIn("MOVE", output)
        self.assertIn("no changes made", output)
        self.assertIn("PermissionError: read denied", error)
        self.assertIn("Watch stopped", output)
        self.assertFalse((self.config.parent / "journal.jsonl").exists())

    def test_watch_rejects_unregistered_root_and_invalid_interval(self):
        self.initialize()
        self.register()
        for selected in (str(self.destination), "incoming"):
            status, _, error = self.invoke("watch", "--root", selected)
            self.assertEqual(status, 2, error)
        for interval in ("0", "-1", "nan", "inf"):
            status, _, error = self.invoke("watch", "--interval", interval)
            self.assertEqual(status, 2)
            self.assertIn("interval_seconds", error)
        status, _, error = self.invoke("watch", "--interval", "1e100")
        self.assertEqual(status, 2)
        self.assertIn("--interval", error)
        self.assertFalse((self.config.parent / "journal.jsonl").exists())

    def test_watch_reports_root_failure_without_disabling_other_roots(self):
        self.initialize()
        self.register()
        other_source = self.base / "second-incoming"
        other_destination = self.base / "second-organized"
        other_source.mkdir()
        other_destination.mkdir()
        status, _, error = self.invoke("roots", "add", str(other_source), str(other_destination))
        self.assertEqual(status, 0, error)
        self.source.rename(self.base / "disconnected-incoming")
        from ocd.monitor import watch_roots as actual_watch_roots

        def one_poll(roots, callback, *, stop_event, interval_seconds):
            return actual_watch_roots(roots, callback, stop_event=stop_event,
                                      interval_seconds=interval_seconds, max_polls=1)

        with patch("ocd.monitor.watch_roots", side_effect=one_poll):
            status, output, error = self.invoke("watch")
        self.assertEqual(status, 0, error)
        self.assertIn("WATCH ERROR", error)
        self.assertIn(str(self.source), error)
        self.assertIn(str(other_source), output)
        self.assertIn("no changes made", output)

    def test_duplicates_reports_content_candidates_without_mutation(self):
        self.initialize()
        self.register(mode="apply")
        payload = b"duplicate but not tagged"
        first = self.source / "first.bin"
        second = self.source / "second.xyz"
        first.write_bytes(payload)
        second.write_bytes(payload)
        (self.source / "different.bin").write_bytes(b"other contents")
        with patch("ocd.apply.apply_moves", side_effect=AssertionError("duplicates attempted mutation")):
            status, output, error = self.invoke(
                "duplicates", "--root", str(self.source), "--max-files", "10", "--max-entries", "100",
            )
        self.assertEqual(status, 0, error)
        self.assertIn("1 duplicate candidate group(s)", output)
        self.assertIn(hashlib.sha256(payload).hexdigest(), output)
        self.assertIn(str(first), output)
        self.assertIn(str(second), output)
        self.assertIn(f"{len(payload)} logical duplicate byte(s)", output)
        self.assertIn("no changes made", output)
        self.assertEqual(first.read_bytes(), payload)
        self.assertEqual(second.read_bytes(), payload)
        self.assertFalse((self.config.parent / "journal.jsonl").exists())

    def test_duplicates_rejects_unregistered_roots_and_invalid_budgets(self):
        self.initialize()
        self.register()
        for selected in (str(self.destination), "incoming"):
            status, _, error = self.invoke("duplicates", "--root", selected)
            self.assertEqual(status, 2, error)
        for option in ("--max-files", "--max-entries"):
            status, _, error = self.invoke("duplicates", option, "0")
            self.assertEqual(status, 2)
            self.assertIn(option.lstrip("-").replace("-", "_"), error)
        self.assertFalse((self.config.parent / "journal.jsonl").exists())

    def test_artifact_fetch_selects_one_manifest_entry_without_organizer_config(self):
        import hashlib
        from ocd.artifacts import ArtifactEntry

        payload = b"verified artifact"
        manifest = self.base / "manifest.json"
        destination_root = self.base / "downloaded"
        destination_root.mkdir()
        entry = ArtifactEntry(
            "https://example.org/releases/archive.bin", hashlib.sha256(payload).hexdigest(),
            len(payload), "build/archive.bin",
        )
        other = ArtifactEntry(
            "https://example.org/releases/other.bin", "0" * 64, 8, "build/other.bin",
        )
        manifest.write_text(json.dumps({"artifacts": [vars(entry), vars(other)]}))
        target = destination_root / "build" / "archive.bin"

        with patch("ocd.artifacts.fetch_verified", return_value=target) as fetch, patch(
            "ocd.apply.apply_moves", side_effect=AssertionError("artifact fetch attempted organizer apply")
        ):
            status, output, error = self.invoke(
                "artifact", "fetch", "--manifest", str(manifest),
                "--destination-root", str(destination_root), "--artifact", "build/archive.bin",
            )
        self.assertEqual(status, 0, error)
        fetch.assert_called_once_with(entry, destination_root)
        self.assertIn(str(target), output)
        self.assertIn("verified", output)
        self.assertFalse(self.config.exists(), "explicit artifact fetch must not initialize organizer config")

    def test_artifact_fetch_rejects_unknown_selection_and_reports_fetch_failure(self):
        from ocd.artifacts import ArtifactError

        manifest = self.base / "manifest.json"
        destination_root = self.base / "downloaded"
        destination_root.mkdir()
        manifest.write_text(json.dumps([{
            "url": "https://example.org/archive.bin", "sha256": "0" * 64,
            "size": 8, "destination_relpath": "build/archive.bin",
        }]))
        arguments = (
            "artifact", "fetch", "--manifest", str(manifest),
            "--destination-root", str(destination_root), "--artifact",
        )
        with patch("ocd.artifacts.fetch_verified") as fetch:
            with self.assertRaises(SystemExit) as missing_selection:
                self.invoke(*arguments[:-1])
        self.assertEqual(missing_selection.exception.code, 2)
        fetch.assert_not_called()
        with patch("ocd.artifacts.fetch_verified") as fetch:
            status, _, error = self.invoke(*arguments, "not-in-manifest.bin")
        self.assertEqual(status, 2)
        self.assertIn("not present in manifest", error)
        fetch.assert_not_called()
        with patch("ocd.artifacts.fetch_verified", side_effect=ArtifactError("digest mismatch")) as fetch:
            status, _, error = self.invoke(*arguments, "build/archive.bin")
        self.assertEqual(status, 1)
        self.assertIn("digest mismatch", error)
        fetch.assert_called_once()
        self.assertFalse(self.config.exists())

    @unittest.skipUnless(os.name == "nt", "Windows artifact publication default-deny CLI contract")
    def test_windows_artifact_fetch_fails_closed_before_publication_without_optin(self):
        from ocd.artifacts import ArtifactEntry

        destination = self.base / "staging"
        destination.mkdir()
        entry = ArtifactEntry("https://example.org/model.gguf", "0" * 64, 100, "model.gguf")
        manifest = self.base / "artifacts.json"
        manifest.write_text(json.dumps({"artifacts": [vars(entry)]}), encoding="utf-8")
        with patch.dict(os.environ, {"OCD_EXPERIMENTAL_WINDOWS_ARTIFACT_PUBLISH": "0"}), patch(
            "ocd.artifacts._fetch_verified_windows", side_effect=AssertionError("Windows publication attempted")
        ) as publisher:
            status, _, error = self.invoke(
                "artifact", "fetch", "--manifest", str(manifest),
                "--destination-root", str(destination), "--artifact", "model.gguf",
            )
        self.assertEqual(status, 1)
        self.assertIn("disabled", error)
        publisher.assert_not_called()
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(self.config.exists())

    @unittest.skipIf(os.name == "nt", "Windows organizer apply intentionally fails closed pending native qualification")
    def test_authorized_apply_moves_without_deleting_content(self):
        self.initialize()
        self.register(mode="apply")
        original = self.source / "notes.txt"
        original.write_bytes(b"preserve these bytes\x00")
        status, output, error = self.invoke("plan", "--root", str(self.source))
        self.assertEqual(status, 0, error)
        self.assertIn("MOVE", output)
        self.assertTrue(original.exists(), "plan cannot mutate")

        status, output, error = self.invoke("apply", "--root", str(self.source), "--yes")
        self.assertEqual(status, 0, error)
        target = self.destination / "documents" / "notes.txt"
        self.assertEqual(target.read_bytes(), b"preserve these bytes\x00")
        self.assertFalse(original.exists())
        self.assertIn("Applied 1 move(s)", output)
        self.assertTrue((self.config.parent / "journal.jsonl").exists())

        status, output, error = self.invoke("apply", "--root", str(self.source), "--yes")
        self.assertEqual(status, 0, error)
        self.assertIn("Applied 0 move(s)", output)
        self.assertEqual(target.read_bytes(), b"preserve these bytes\x00")

    @unittest.skipIf(os.name == "nt", "Windows organizer apply intentionally fails closed pending native qualification")
    def test_relative_config_path_supports_apply_journal(self):
        self.initialize()
        self.register(mode="apply")
        original = self.source / "portable.txt"
        original.write_text("portable")
        out = io.StringIO()
        err = io.StringIO()
        previous_directory = Path.cwd()
        try:
            os.chdir(self.base)
            with redirect_stdout(out), redirect_stderr(err):
                status = main([
                    "--config", str(Path("user-config") / "config.json"),
                    "apply", "--root", str(self.source), "--yes",
                ])
        finally:
            os.chdir(previous_directory)
        self.assertEqual(status, 0, err.getvalue())
        self.assertEqual((self.destination / "documents" / "portable.txt").read_text(), "portable")
        self.assertTrue((self.config.parent / "journal.jsonl").exists())

    def test_native_windows_and_linux_config_locations(self):
        appdata = self.base / "roaming"
        xdg = self.base / "xdg"
        with patch("ocd.config.sys.platform", "win32"), patch.dict(
            os.environ, {"APPDATA": str(appdata)}, clear=True
        ):
            self.assertEqual(default_config_path(), appdata / "ocd" / "config.json")
        with patch("ocd.config.sys.platform", "linux"), patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=True
        ):
            self.assertEqual(default_config_path(), xdg / "ocd" / "config.json")

    def test_import_does_not_create_config_or_journal(self):
        side_effect_path = self.base / "uncreated" / "config.json"
        environment = dict(os.environ)
        environment["OCD_CONFIG"] = str(side_effect_path)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [sys.executable, "-c", "import ocd.config, ocd.cli, ocd.__main__"],
            cwd=self.base, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(side_effect_path.exists())
        self.assertFalse((side_effect_path.parent / "journal.jsonl").exists())

    def test_unknown_fields_credentials_and_versions_fail_closed(self):
        self.initialize()
        cases = (
            {"version": 2, "roots": []},
            {"version": 1, "roots": [], "api_token": "secret"},
            {"version": 1, "roots": [{"source": str(self.source), "destination": str(self.destination),
                                      "mode": "apply", "tags": {}, "settle_seconds": 0,
                                      "password": "secret"}]},
        )
        for document in cases:
            with self.subTest(document=document):
                self.config.write_text(json.dumps(document))
                with self.assertRaises(ConfigError):
                    load_config(self.config)
                self.assertEqual(self.invoke("scan")[0], 2)

    def test_duplicate_json_keys_fail_closed(self):
        self.initialize()
        self.config.write_text('{"version": 1, "version": 1, "roots": []}')
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            load_config(self.config)

    def test_unbounded_settle_seconds_fails_closed(self):
        self.initialize()
        self.config.write_text(json.dumps({"version": 1, "roots": [{
            "source": str(self.source), "destination": str(self.destination),
            "mode": "observe", "tags": {}, "settle_seconds": 10 ** 1000,
        }]}))
        with self.assertRaises(ConfigError):
            load_config(self.config)

    def test_overlapping_roots_and_extension_duplicates_rejected(self):
        self.initialize()
        self.register()
        child = self.source / "child"
        child.mkdir()
        status, _, _ = self.invoke("roots", "add", str(child), str(self.base / "organized"))
        self.assertEqual(status, 2)
        self.assertEqual(len(load_config(self.config).roots), 1)

        another = self.base / "second-incoming"
        another.mkdir()
        status, _, error = self.invoke(
            "roots", "add", str(another), str(self.destination), "--tag", "jpg=images", "--tag", ".JPG=other",
        )
        self.assertEqual(status, 2)
        self.assertIn("duplicate", error)

    def test_source_and_destination_must_not_overlap(self):
        self.initialize()
        nested = self.source / "output"
        nested.mkdir()
        status, _, error = self.invoke("roots", "add", str(self.source), str(nested))
        self.assertEqual(status, 2)
        self.assertIn("non-overlapping", error)
        self.assertFalse(load_config(self.config).roots)

    def test_redirected_root_is_not_followed(self):
        self.initialize()
        self.register(mode="apply")
        alternate = self.base / "alternate"
        alternate.mkdir()
        moved = self.base / "old-incoming"
        self.source.rename(moved)
        try:
            self.source.symlink_to(alternate, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        status, _, error = self.invoke("apply", "--root", str(self.source), "--yes")
        self.assertEqual(status, 2)
        self.assertIn("changed", error)
        self.assertFalse((self.config.parent / "journal.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
