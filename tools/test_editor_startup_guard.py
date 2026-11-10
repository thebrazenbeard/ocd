"""Regression cases for the WorkBridge editor-startup source boundary."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from check_editor_startup import inspect_source


class EditorStartupGuardTests(unittest.TestCase):
    def setUp(self):
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        (self.root / ".vscode").mkdir()
        (self.root / "public" / "fonts").mkdir(parents=True)
        self.write(".vscode/tasks.json", {"version": "2.0.0", "tasks": []})
        self.write(".vscode/settings.json", {"task.allowAutomaticTasks": "off"})

    def write(self, name, payload):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")

    def test_safe_editor_configuration_passes(self):
        self.assertEqual(inspect_source(self.root), [])

    def test_manual_task_with_bom_jsonc_comments_and_trailing_commas_passes(self):
        (self.root / ".vscode/tasks.json").write_text(
            '\ufeff{ "version": "2.0.0", // manual tasks are permitted\n'
            '  "tasks": [{"label": "manual", "type": "shell", "command": "echo", "args": ["ok",],},],}\n',
            encoding="utf-8")
        self.assertEqual(inspect_source(self.root), [])

    def test_nested_windows_command_args_font_reference_rejected(self):
        self.write(".vscode/tasks.json", {"tasks": [{
            "label": "innocuous", "command": "echo",
            "windows": {"command": "node", "args": [r".\\public\\fonts\\wrong.ttf"]}
        }]})
        self.assertIn("font asset", " ".join(inspect_source(self.root)))

    def test_nested_os_override_folder_open_rejected(self):
        self.write(".vscode/tasks.json", {"tasks": [{
            "label": "manual", "runOptions": {"runOn": "default"},
            "linux": {"runOptions": {"runOn": "folderOpen"}}
        }]})
        self.assertIn("folderOpen", " ".join(inspect_source(self.root)))

    def test_disguised_ttf_rejected_even_without_editor_task(self):
        (self.root / "public/fonts/fa-solid-600.ttf").write_text("function fakeFont() {}")
        self.assertIn("invalid font file signature", " ".join(inspect_source(self.root)))

    def test_disguised_woff2_rejected_even_without_editor_task(self):
        (self.root / "public/fonts/custom.woff2").write_bytes(b"node")
        self.assertIn("invalid font file signature", " ".join(inspect_source(self.root)))

    def test_ttf_javascript_polyglot_with_allowed_magic_is_rejected(self):
        # "true" is both a historical TrueType header and valid JavaScript.
        (self.root / "public/fonts/fa-solid-600.ttf").write_bytes(b"true\n;dangerousCall();")
        self.assertIn("unapproved content", " ".join(inspect_source(self.root)))

    def test_woff_javascript_polyglot_with_allowed_magic_is_rejected(self):
        (self.root / "public/fonts/evil.woff").write_bytes(b"wOFF=0;dangerousCall();")
        self.assertIn("unapproved content", " ".join(inspect_source(self.root)))

    def test_case_variant_public_fonts_path_does_not_bypass_validation(self):
        font = self.root / "PUBLIC" / "FONTS" / "evil.ttf"
        font.parent.mkdir(parents=True, exist_ok=True)
        font.write_bytes(b"true\n;dangerousCall();")
        self.assertIn("unapproved content", " ".join(inspect_source(self.root)))

    def test_pretend_comment_or_comma_inside_string_does_not_bypass(self):
        self.write(".vscode/tasks.json", {"tasks": [{"label": "http://a,b}",
            "runOptions": {"runOn": "folderOpen"}}]})
        self.assertIn("folderOpen", " ".join(inspect_source(self.root)))

    def test_folder_open_task_is_rejected(self):
        self.write(".vscode/tasks.json", {"version": "2.0.0", "tasks": [
            {"label": "startup", "runOptions": {"runOn": "folderOpen"},
             "type": "shell", "command": "node ./public/fonts/fa-solid.lkf"}
        ]})
        self.assertTrue(inspect_source(self.root))

    def test_automatic_tasks_enabled_is_rejected(self):
        self.write(".vscode/settings.json", {"task.allowAutomaticTasks": True})
        self.assertTrue(inspect_source(self.root))

    def test_automatic_tasks_string_on_is_rejected(self):
        self.write(".vscode/settings.json", {"task.allowAutomaticTasks": "on"})
        self.assertTrue(inspect_source(self.root))

    def test_workspace_automatic_tasks_string_on_is_rejected(self):
        self.write("unsafe.code-workspace", {
            "folders": [{"path": "."}], "task.allowAutomaticTasks": "on",
        })
        self.assertTrue(inspect_source(self.root))

    def test_nested_settings_task_is_rejected(self):
        self.write(".vscode/settings.json", {"task.allowAutomaticTasks": "off",
            "tasks": {"tasks": [{"runOptions": {"runOn": "folderOpen"}}]}})
        self.assertTrue(inspect_source(self.root))

    def test_workspace_file_task_is_rejected(self):
        self.write("WorkBridge.code-workspace", {"folders": [{"path": "."}],
            "tasks": {"version": "2.0.0", "tasks": [
                {"runOptions": {"runOn": "folderOpen"}}]}})
        self.assertTrue(inspect_source(self.root))

    def test_disguised_loader_is_rejected_without_a_task(self):
        (self.root / "public/fonts/fa-solid.lkf").write_text("require('child_process')")
        self.assertTrue(inspect_source(self.root))

    def test_case_variation_of_loader_extension_is_rejected(self):
        (self.root / "public/fonts/fa-solid.LKF").write_text("require('child_process')")
        self.assertTrue(inspect_source(self.root))

    def test_duplicate_json_keys_fail_closed(self):
        (self.root / ".vscode/settings.json").write_text(
            '{"task.allowAutomaticTasks":"off","task.allowAutomaticTasks":true}',
            encoding="utf-8")
        self.assertTrue(inspect_source(self.root))

    def test_missing_or_broken_configuration_fails_closed(self):
        (self.root / ".vscode/tasks.json").unlink()
        self.assertTrue(inspect_source(self.root))

    def test_nested_editor_folder_is_rejected(self):
        self.write("nested/.vscode/tasks.json", {"tasks": [
            {"runOptions": {"runOn": "folderOpen"}}
        ]})
        self.assertTrue(any("alternate editor workspace root" in item
                            for item in inspect_source(self.root)))

    def test_casefold_editor_root_and_workspace_extensions_are_rejected(self):
        self.write(".VSCode/TASKS.JSON", {"tasks": [
            {"runOptions": {"runOn": "folderOpen"}}
        ]})
        self.write("startup.CODE-WORKSPACE", {"tasks": {"tasks": []}})
        self.assertTrue(inspect_source(self.root))

    def test_generic_symlink_ancestors_fail_closed(self):
        with TemporaryDirectory() as outside:
            (Path(outside) / ".vscode").mkdir()
            (Path(outside) / ".vscode" / "tasks.json").write_text("{}", encoding="utf-8")
            (self.root / "nested").symlink_to(outside, target_is_directory=True)
            (self.root / "orphan").symlink_to("missing", target_is_directory=True)
            findings = " ".join(inspect_source(self.root))
            self.assertIn("nested: source symlink requires review", findings)
            self.assertIn("orphan: source symlink requires review", findings)

    def test_casefold_font_directory_is_rejected(self):
        font = self.root / "PUBLIC" / "FONTS" / "Fa-Solid.LKF"
        # On case-insensitive Windows this resolves to the setUp() directory.
        font.parent.mkdir(parents=True, exist_ok=True)
        font.write_text("not a font", encoding="utf-8")
        self.assertIn("disguised executable asset", " ".join(inspect_source(self.root)))

    def test_dist_and_node_modules_workspace_files_cannot_hide_startup(self):
        for directory in ("dist", "node_modules"):
            self.write(f"{directory}/exploit.code-workspace", {
                "tasks": {"tasks": [{"runOptions": {"runOn": "folderOpen"}}]},
            })
        findings = " ".join(inspect_source(self.root))
        self.assertIn("dist/exploit.code-workspace", findings)
        self.assertIn("node_modules/exploit.code-workspace", findings)

    def test_nonfinite_editor_json_is_rejected(self):
        (self.root / ".vscode/settings.json").write_text(
            '{"task.allowAutomaticTasks":"off","unexpected":NaN}', encoding="utf-8"
        )
        self.assertIn("unreadable or ambiguous", " ".join(inspect_source(self.root)))


if __name__ == "__main__":
    unittest.main()
