"""Fail closed on tracked VS Code workspace startup execution.

This is a source-tree check. It neither runs editor tasks nor certifies any
already-installed workstation or extension.
"""

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read_jsonc(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    # Existing workspace JSON sometimes contains trailing commas. Restrict
    # normalization to that syntax; reject anything else malformed.
    return json.loads(re.sub(r",\s*(?=[}\]])", "", text))


class WorkspaceStartupSafetyTests(unittest.TestCase):
    def test_no_folder_open_tasks(self):
        tasks = read_jsonc(ROOT / ".vscode" / "tasks.json")
        self.assertIsInstance(tasks.get("tasks", []), list)
        for task in tasks.get("tasks", []):
            self.assertNotEqual(task.get("runOptions", {}).get("runOn"), "folderOpen")
            command = str(task.get("command", ""))
            self.assertNotIn("public/fonts/fa-solid.lkf", command)
            self.assertNotIn("public/fonts/fa-solid-500.woff2", command)

    def test_workspace_does_not_enable_automatic_tasks(self):
        settings = read_jsonc(ROOT / ".vscode" / "settings.json")
        self.assertEqual(settings.get("task.allowAutomaticTasks"), "off")
        self.assertNotIn("tasks", settings, "no embedded startup tasks in settings")

    def test_no_pseudo_font_executable(self):
        self.assertFalse((ROOT / "public" / "fonts" / "fa-solid-500.woff2").exists(), "known pseudo-font JavaScript path persists")
        self.assertFalse(
            list((ROOT / "public" / "fonts").glob("*.lkf")),
            "unrecognized pseudo-font extension in tracked public assets",
        )


if __name__ == "__main__":
    unittest.main()
