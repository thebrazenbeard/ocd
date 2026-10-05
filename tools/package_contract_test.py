from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "package-source" / "spk-packager.toml"
RESOURCE = ROOT / "package-source" / "resource.json"


class PackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with MANIFEST.open("rb") as handle:
            cls.manifest = tomllib.load(handle)
        cls.resource = json.loads(RESOURCE.read_text(encoding="utf-8"))

    def test_dsm7_package_user_is_sc_prefixed(self) -> None:
        package_id = self.manifest["package"]["id"]
        privilege = self.manifest["privilege"]
        self.assertEqual(privilege["run_as"], "package")
        self.assertEqual(privilege.get("username"), f"sc-{package_id}")

    def test_data_share_rw_matches_package_user(self) -> None:
        expected = self.manifest["privilege"]["username"]
        shares = self.resource["data-share"]["shares"]
        self.assertGreater(len(shares), 0)
        for share in shares:
            self.assertIn(expected, share["permission"]["rw"])


if __name__ == "__main__":
    unittest.main()
