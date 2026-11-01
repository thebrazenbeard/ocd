"""Static guardrails for optional observe-only startup templates."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OptionalStartupGuardrailsTests(unittest.TestCase):
    def test_linux_template_only_starts_observe_watch(self):
        unit = (ROOT / "packaging/linux/ocd-watch.service.template").read_text()
        exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(exec_lines), 1)
        command = exec_lines[0]
        self.assertIn("-m ocd --config", command)
        self.assertIn(" watch --interval 2", command)
        self.assertNotIn(" apply ", command)
        self.assertNotIn(" --yes", command)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_windows_task_uses_explicit_action_and_observe_only(self):
        script = (ROOT / "packaging/windows/Manage-OCDWatchTask.ps1").read_text()
        self.assertIn("[Parameter(Mandatory = $true)]", script)
        self.assertIn("[ValidateSet('Install', 'Remove')]", script)
        self.assertIn("-m ocd --config", script)
        self.assertIn(" watch --interval ", script)
        self.assertNotIn(" --yes", script)
        self.assertIn("-RunLevel Limited", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("taskMarker", script)

    def test_installation_is_explicit_in_docs(self):
        guide = (ROOT / "packaging/README.md").read_text()
        self.assertIn("enable --now ocd-watch.service", guide)
        self.assertIn("disable --now ocd-watch.service", guide)
        self.assertIn("-Action Install", guide)
        self.assertIn("-Action Remove", guide)
        self.assertIn("does **not** call `apply`", guide)


if __name__ == "__main__":
    unittest.main()
