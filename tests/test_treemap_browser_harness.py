"""Source-level checks for the opt-in, real-browser treemap qualification tool.

The routine CI suite must not silently install a browser or claim that a
static test is equivalent to a real browser probe.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


_NODE = shutil.which("node")
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_treemap_browser.cjs"


@unittest.skipUnless(_NODE, "Node.js unavailable; browser harness is opt-in")
class BrowserHarnessSourceTests(unittest.TestCase):
    def test_harness_has_valid_javascript_syntax(self) -> None:
        result = subprocess.run(
            [_NODE, "--check", str(_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_static_fixture_never_claims_browser_qualification(self) -> None:
        result = subprocess.run(
            [_NODE, str(_SCRIPT), "--static-check"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["status"], "PASS_STATIC_FIXTURE_ONLY")
        self.assertFalse(status["browser_qualified"])
        self.assertEqual(len(status["html_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
