from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "package-source" / "spk-packager.toml"
RESOURCE = ROOT / "package-source" / "resource.json"
POSTINST = ROOT / "package-source" / "scripts" / "postinst"
PROVENANCE = ROOT / "package-source" / "payload" / "SOURCE_PROVENANCE.json"
BUILD_CMD = ROOT / "scripts" / "build-spk.cmd"
BUILD_SH = ROOT / "scripts" / "build-spk.sh"


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

    def test_postinst_stages_bootstrap_instead_of_resolving_share(self) -> None:
        text = POSTINST.read_text(encoding="utf-8")
        self.assertIn("bootstrap stage", text)
        self.assertIn("--tmdb-bearer-stdin", text)
        self.assertNotIn("/var/packages/OCD/shares/", text)

    def test_build_scripts_write_provenance_as_bytes(self) -> None:
        for script in (BUILD_CMD, BUILD_SH):
            text = script.read_text(encoding="utf-8")
            self.assertIn("write_bytes", text)
            self.assertNotIn("write_text", text)

    def test_generated_provenance_uses_lf_only(self) -> None:
        if not PROVENANCE.exists():
            self.skipTest("provenance is generated during SPK build")
        data = PROVENANCE.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        self.assertNotIn(b"\r\n", data)


if __name__ == "__main__":
    unittest.main()
